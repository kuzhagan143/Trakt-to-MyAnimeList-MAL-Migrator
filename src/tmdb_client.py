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

    # ── Private Helpers ───────────────────────────────────────────────────

    def _fetch_show(self, tmdb_id: int) -> Optional[dict]:
        """Make a rate-limited API call to TMDB."""
        url = f"{TMDB_BASE_URL}/tv/{tmdb_id}"
        params = {"api_key": self.api_key, "language": "en-US"}

        for attempt in range(1, self.max_retries + 1):
            try:
                self.rate_limiter.sync_acquire()

                response = self._http_client.get(url, params=params)

                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 429:
                    # Rate limited — back off
                    retry_after = int(response.headers.get("Retry-After", 2))
                    wait_time = retry_after * attempt
                    logger.warning(
                        "[TMDB] Rate limited on ID %d, waiting %ds (attempt %d/%d)",
                        tmdb_id, wait_time, attempt, self.max_retries,
                    )
                    time.sleep(wait_time)
                    continue
                elif response.status_code == 404:
                    logger.warning("[TMDB] Show not found: ID %d", tmdb_id)
                    return None
                else:
                    logger.warning(
                        "[TMDB] HTTP %d for ID %d (attempt %d/%d)",
                        response.status_code, tmdb_id, attempt, self.max_retries,
                    )

            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                wait_time = 2 ** attempt
                logger.warning(
                    "[TMDB] %s for ID %d, retrying in %ds (attempt %d/%d)",
                    type(exc).__name__, tmdb_id, wait_time, attempt, self.max_retries,
                )
                time.sleep(wait_time)

        logger.error("[TMDB] Failed to fetch ID %d after %d attempts", tmdb_id, self.max_retries)
        return None

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
