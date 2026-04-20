"""
OfflineDBLookup — downloads and queries the Kometa Anime-IDs mapping database.

Tier 1 in MAL ID resolution: provides instant TMDB/TVDB → MAL ID lookups
without any API calls. The database is downloaded once and cached locally.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Kometa Anime-IDs — auto-generated mapping file with TMDB → MAL mappings
ANIME_IDS_URL = (
    "https://raw.githubusercontent.com/Kometa-Team/Anime-IDs/master/anime_ids.json"
)


class OfflineDBLookup:
    """
    Offline lookup using the Kometa Anime-IDs database.

    The database maps AniDB IDs to various external IDs including:
    - mal_id
    - anilist_id
    - tmdb_movie_id / tmdb_show_id
    - tvdb_id, tvdb_season, tvdb_epoffset
    - imdb_id

    We build reverse indexes (TMDB→entry, TVDB→entry) for fast lookups.
    """

    def __init__(self, cache_dir: Path, timeout: int = 30):
        self.cache_dir = cache_dir
        self.timeout = timeout
        self._db_file = self.cache_dir / "anime_ids.json"
        self._raw_db: dict = {}

        # Reverse indexes for fast lookup
        self._tmdb_show_index: dict[int, list[dict]] = {}
        self._tmdb_movie_index: dict[int, list[dict]] = {}
        self._tvdb_index: dict[int, list[dict]] = {}
        self._imdb_index: dict[str, list[dict]] = {}

        self._loaded = False

    # ── Public API ────────────────────────────────────────────────────────

    def ensure_loaded(self) -> bool:
        """Download the database if needed and build indexes. Returns True on success."""
        if self._loaded:
            return True

        if not self._db_file.exists():
            logger.info("[OfflineDB] Downloading Kometa Anime-IDs database...")
            if not self._download_db():
                logger.error("[OfflineDB] Failed to download database")
                return False

        if not self._load_db():
            return False

        self._build_indexes()
        self._loaded = True
        logger.info(
            "[OfflineDB] Loaded %d entries | TMDB-show index: %d | TMDB-movie index: %d | TVDB index: %d",
            len(self._raw_db),
            len(self._tmdb_show_index),
            len(self._tmdb_movie_index),
            len(self._tvdb_index),
        )
        return True

    def lookup_by_tmdb_show(self, tmdb_id: int, season: Optional[int] = None) -> Optional[int]:
        """
        Look up MAL ID by TMDB show ID and optional season number.

        The Kometa database often has separate entries for each season of a show,
        each mapped to a different MAL ID. If season is provided, we try to find
        the entry matching that specific season.
        """
        if not self._loaded:
            return None

        entries = self._tmdb_show_index.get(tmdb_id, [])
        if not entries:
            return None

        if season is not None:
            # Try to find an entry for this specific season
            for entry in entries:
                tvdb_season = entry.get("tvdb_season")
                if tvdb_season == season:
                    mal_id = entry.get("mal_id")
                    if mal_id:
                        return int(mal_id)

            # Some entries use tvdb_epoffset to differentiate seasons
            # If only one entry exists, use it (likely single-season or movie)
            if len(entries) == 1:
                mal_id = entries[0].get("mal_id")
                if mal_id:
                    return int(mal_id)

            # For season 1, try the entry with tvdb_season=1 or the first entry
            if season == 1:
                # Return the first entry with a MAL ID
                for entry in entries:
                    mal_id = entry.get("mal_id")
                    if mal_id:
                        return int(mal_id)

        else:
            # No season specified — return the first MAL ID found
            for entry in entries:
                mal_id = entry.get("mal_id")
                if mal_id:
                    return int(mal_id)

        return None

    def lookup_by_tmdb_movie(self, tmdb_id: int) -> Optional[int]:
        """Look up MAL ID by TMDB movie ID."""
        if not self._loaded:
            return None

        entries = self._tmdb_movie_index.get(tmdb_id, [])
        for entry in entries:
            mal_id = entry.get("mal_id")
            if mal_id:
                return int(mal_id)
        return None

    def lookup_by_tvdb(self, tvdb_id: int, season: Optional[int] = None) -> Optional[int]:
        """Look up MAL ID by TVDB ID and optional season."""
        if not self._loaded:
            return None

        entries = self._tvdb_index.get(tvdb_id, [])
        if not entries:
            return None

        if season is not None:
            for entry in entries:
                if entry.get("tvdb_season") == season:
                    mal_id = entry.get("mal_id")
                    if mal_id:
                        return int(mal_id)

        # Fallback: first entry with MAL ID
        for entry in entries:
            mal_id = entry.get("mal_id")
            if mal_id:
                return int(mal_id)
        return None

    def lookup_by_imdb(self, imdb_id: str) -> Optional[int]:
        """Look up MAL ID by IMDb ID."""
        if not self._loaded:
            return None

        entries = self._imdb_index.get(imdb_id, [])
        for entry in entries:
            mal_id = entry.get("mal_id")
            if mal_id:
                return int(mal_id)
        return None

    def get_all_mal_ids_for_tmdb_show(self, tmdb_id: int) -> dict[int, int]:
        """
        Get all season→MAL ID mappings for a TMDB show.

        Returns: {season_number: mal_id}
        """
        if not self._loaded:
            return {}

        entries = self._tmdb_show_index.get(tmdb_id, [])
        result: dict[int, int] = {}

        for entry in entries:
            tvdb_season = entry.get("tvdb_season")
            mal_id = entry.get("mal_id")
            if tvdb_season is not None and mal_id:
                result[int(tvdb_season)] = int(mal_id)

        return result

    # ── Private Helpers ───────────────────────────────────────────────────

    def _download_db(self) -> bool:
        """Download the anime_ids.json file from GitHub."""
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                response = client.get(ANIME_IDS_URL)

            if response.status_code == 200:
                with open(self._db_file, "wb") as f:
                    f.write(response.content)
                logger.info("[OfflineDB] Downloaded anime_ids.json (%.1f KB)", len(response.content) / 1024)
                return True
            else:
                logger.error("[OfflineDB] Download failed: HTTP %d", response.status_code)
                return False
        except (httpx.TimeoutException, httpx.ConnectError, OSError) as exc:
            logger.error("[OfflineDB] Download error: %s", exc)
            return False

    def _load_db(self) -> bool:
        """Load the cached JSON database from disk."""
        try:
            with open(self._db_file, "r", encoding="utf-8") as f:
                self._raw_db = json.load(f)
            return True
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("[OfflineDB] Failed to load database: %s", exc)
            return False

    def _build_indexes(self) -> None:
        """Build reverse lookup indexes from the raw database."""
        for anidb_id, entry in self._raw_db.items():
            if not isinstance(entry, dict):
                continue

            # TMDB show index
            tmdb_show = entry.get("tmdb_show_id")
            if tmdb_show:
                try:
                    key = int(tmdb_show)
                    self._tmdb_show_index.setdefault(key, []).append(entry)
                except (ValueError, TypeError):
                    pass

            # TMDB movie index
            tmdb_movie = entry.get("tmdb_movie_id")
            if tmdb_movie:
                try:
                    key = int(tmdb_movie)
                    self._tmdb_movie_index.setdefault(key, []).append(entry)
                except (ValueError, TypeError):
                    pass

            # TVDB index
            tvdb = entry.get("tvdb_id")
            if tvdb:
                try:
                    key = int(tvdb)
                    self._tvdb_index.setdefault(key, []).append(entry)
                except (ValueError, TypeError):
                    pass

            # IMDb index
            imdb = entry.get("imdb_id")
            if imdb and isinstance(imdb, str):
                self._imdb_index.setdefault(imdb, []).append(entry)
