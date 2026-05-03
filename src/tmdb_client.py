"""
TMDBClient — fetches show metadata from TMDB API to classify anime.

Checks if a TV show is anime by verifying:
  - "Animation" is in the TMDB genre list
  - original_language is "ja" (Japanese)

All responses are cached to disk so re-runs don't repeat API calls.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .models import ShowMetadata
from .rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

TMDB_BASE_URL = "https://api.themoviedb.org/3"


class TMDBClient:
    """Fetches TV show metadata from TMDB and determines if a show is anime."""

    def __init__(
        self,
        api_key: str,
        cache_dir: Path,
        rate_limiter: RateLimiter,
        timeout: int = 15,
        max_retries: int = 3,
    ):
        self.api_key = api_key
        self.cache_dir = cache_dir
        self.rate_limiter = rate_limiter
        self.timeout = timeout
        self.max_retries = max_retries
        self._cache: dict[int, dict] = {}
        
        # Persistent client for connection pooling
        limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
        self._http_client = httpx.Client(timeout=self.timeout, limits=limits)

        # Load disk cache
        self._cache_file = self.cache_dir / "tmdb_cache.json"
        self._load_cache()

    # ── Public API ────────────────────────────────────────────────────────

    def get_show_metadata(self, tmdb_id: int) -> Optional[ShowMetadata]:
        """
        Fetch show details from TMDB. Returns ShowMetadata with is_anime flag.
        Results are cached to disk.
        """
        # Check cache first
        cached = self._cache.get(str(tmdb_id))
        if cached:
            return self._parse_show_data(tmdb_id, cached)

        # Fetch from TMDB API
        data = self._fetch_show(tmdb_id)
        if data is None:
            return None

        # Cache to disk
        self._cache[str(tmdb_id)] = data
        self._save_cache()

        return self._parse_show_data(tmdb_id, data)

    def get_show_metadata_batch(self, tmdb_ids: list[int]) -> dict[int, Optional[ShowMetadata]]:
        """Fetch metadata for multiple shows, with progress logging."""
        results: dict[int, Optional[ShowMetadata]] = {}
        total = len(tmdb_ids)

        for i, tmdb_id in enumerate(tmdb_ids, 1):
            if i % 25 == 0 or i == total:
                logger.info("[TMDB] Progress: %d/%d shows fetched", i, total)

            results[tmdb_id] = self.get_show_metadata(tmdb_id)

        return results

    def is_anime_movie(self, tmdb_id: int) -> bool:
        """
        Check if a movie is anime via TMDB API.
        Returns True if the movie has Animation genre + Japanese language.
        Uses cache to avoid redundant API calls.
        """
        cache_key = f"movie_{tmdb_id}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            genres = [g.get("name", "") for g in cached.get("genres", [])]
            orig_lang = cached.get("original_language", "")
            return "Animation" in genres and orig_lang == "ja"

        data = self._fetch_endpoint(f"/movie/{tmdb_id}")
        if data is None:
            return False

        self._cache[cache_key] = data
        # Don't save cache here -- batch save at end of classify_movies_batch

        genres = [g.get("name", "") for g in data.get("genres", [])]
        orig_lang = data.get("original_language", "")
        return "Animation" in genres and orig_lang == "ja"

    def classify_movies_batch(self, tmdb_ids: list[int]) -> dict[int, bool]:
        """
        Check if multiple movies are anime. Returns {tmdb_id: is_anime}.
        Skips movies already in cache (no API call needed).
        """
        results: dict[int, bool] = {}

        # Split into cached vs uncached
        cached_ids = []
        uncached_ids = []
        for tmdb_id in tmdb_ids:
            cache_key = f"movie_{tmdb_id}"
            if cache_key in self._cache:
                cached_ids.append(tmdb_id)
            else:
                uncached_ids.append(tmdb_id)

        logger.info(
            "[TMDB] Movies: %d cached (instant), %d need API calls",
            len(cached_ids), len(uncached_ids),
        )

        # Process cached movies instantly (no API calls)
        for tmdb_id in cached_ids:
            results[tmdb_id] = self.is_anime_movie(tmdb_id)

        # Process uncached movies (API calls with progress)
        total_api = len(uncached_ids)
        for i, tmdb_id in enumerate(uncached_ids, 1):
            if i % 25 == 0 or i == total_api:
                logger.info("[TMDB] Movie API progress: %d/%d fetched", i, total_api)
            results[tmdb_id] = self.is_anime_movie(tmdb_id)

        # Batch save cache after all fetches
        if uncached_ids:
            self._save_cache()

        return results

    # ── Private Helpers ───────────────────────────────────────────────────

    def _fetch_endpoint(self, path: str) -> Optional[dict]:
        """Make a rate-limited API call to any TMDB endpoint."""
        url = f"{TMDB_BASE_URL}{path}"
        params = {"api_key": self.api_key, "language": "en-US"}

        for attempt in range(1, self.max_retries + 1):
            try:
                self.rate_limiter.sync_acquire()

                response = self._http_client.get(url, params=params)

                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 2))
                    wait_time = retry_after * attempt
                    logger.warning(
                        "[TMDB] Rate limited on %s, waiting %ds (attempt %d/%d)",
                        path, wait_time, attempt, self.max_retries,
                    )
                    time.sleep(wait_time)
                    continue
                elif response.status_code == 404:
                    logger.warning("[TMDB] Not found: %s", path)
                    return None
                else:
                    logger.warning(
                        "[TMDB] HTTP %d for %s (attempt %d/%d)",
                        response.status_code, path, attempt, self.max_retries,
                    )

            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                wait_time = 2 ** attempt
                logger.warning(
                    "[TMDB] %s for %s, retrying in %ds (attempt %d/%d)",
                    type(exc).__name__, path, wait_time, attempt, self.max_retries,
                )
                time.sleep(wait_time)

        logger.error("[TMDB] Failed to fetch %s after %d attempts", path, self.max_retries)
        return None

    def _fetch_show(self, tmdb_id: int) -> Optional[dict]:
        """Fetch TV show data from TMDB."""
        return self._fetch_endpoint(f"/tv/{tmdb_id}")

    @staticmethod
    def _parse_show_data(tmdb_id: int, data: dict) -> ShowMetadata:
        """Parse TMDB response into ShowMetadata."""
        genres = [g.get("name", "") for g in data.get("genres", [])]
        orig_lang = data.get("original_language", "")
        title = data.get("name", data.get("original_name", "(unknown)"))

        # Determine anime: Animation genre + Japanese language
        is_anime = "Animation" in genres and orig_lang == "ja"

        # Extract season episode counts
        season_counts: dict[int, int] = {}
        for season in data.get("seasons", []):
            s_num = season.get("season_number")
            s_eps = season.get("episode_count", 0)
            if s_num is not None:
                season_counts[s_num] = s_eps

        return ShowMetadata(
            tmdb_id=tmdb_id,
            title=title,
            original_language=orig_lang,
            genres=genres,
            is_anime=is_anime,
            total_seasons=data.get("number_of_seasons", 0),
            season_episode_counts=season_counts,
        )

    def _load_cache(self) -> None:
        """Load cached TMDB responses from disk."""
        if self._cache_file.exists():
            try:
                with open(self._cache_file, "r", encoding="utf-8") as f:
                    self._cache = json.load(f)
                logger.info("[TMDB] Loaded %d cached entries", len(self._cache))
            except (json.JSONDecodeError, OSError):
                self._cache = {}

    def _save_cache(self) -> None:
        """Persist cache to disk."""
        self._cache_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self._cache_file, "w", encoding="utf-8") as f:
            json.dump(self._cache, f, ensure_ascii=False)
