"""
TraktParser — reads Trakt JSON exports and extracts anime entries.

Movie detection is fully local (genre + language check).
Show detection requires external TMDB calls, so this class yields
show candidates for the TMDBClient to classify.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from .models import AnimeEntry, SeasonWatchData, SkipRecord

logger = logging.getLogger(__name__)


class TraktParser:
    """Parses Trakt watched-movies.json and watched-shows.json files."""

    # Trakt uses "anime" as a genre for Japanese anime movies.
    # Western animated films use "animation" — they are mutually exclusive.
    ANIME_GENRE = "anime"
    ANIMATION_GENRE = "animation"
    JAPANESE_LANG = "ja"

    def __init__(self, movies_path: Path, shows_path: Path):
        self.movies_path = movies_path
        self.shows_path = shows_path

    # ── Public API ────────────────────────────────────────────────────────

    def extract_anime_movies(self) -> tuple[list[AnimeEntry], list[SkipRecord]]:
        """
        Parse watched-movies.json and return detected anime movies.
        No API calls needed — detection is purely genre-based.

        Returns:
            (anime_entries, skip_records)
        """
        raw_movies = self._load_json(self.movies_path)
        if raw_movies is None:
            return [], [SkipRecord(
                title="(all movies)",
                reason=f"Failed to load {self.movies_path}",
                phase="detection",
            )]

        anime_entries: list[AnimeEntry] = []
        skip_records: list[SkipRecord] = []
        seen_trakt_ids: set[int] = set()

        logger.info("Parsing %d movies from %s", len(raw_movies), self.movies_path.name)

        for raw in raw_movies:
            movie = raw.get("movie")
            if not movie:
                skip_records.append(SkipRecord(
                    title="(unknown)",
                    reason="Movie entry has no 'movie' field",
                    phase="detection",
                ))
                continue

            trakt_id = movie.get("ids", {}).get("trakt")
            title = movie.get("title", "(untitled)")

            # Deduplicate
            if trakt_id and trakt_id in seen_trakt_ids:
                continue
            if trakt_id:
                seen_trakt_ids.add(trakt_id)

            # ── Anime detection ───────────────────────────────────────
            genres = movie.get("genres", [])
            language = movie.get("language", "")

            is_anime = False
            reason = ""

            if self.ANIME_GENRE in genres:
                # Primary rule: Trakt explicitly tags it as "anime"
                is_anime = True
                reason = f'Genre "{self.ANIME_GENRE}" found in Trakt genres: {genres}'
            elif self.ANIMATION_GENRE in genres and language == self.JAPANESE_LANG:
                # Fallback rule: "animation" + Japanese language
                is_anime = True
                reason = (
                    f'Genre "{self.ANIMATION_GENRE}" + language "{language}" '
                    f"detected as anime (fallback rule)"
                )

            if not is_anime:
                continue  # Not anime — silently skip (don't log non-anime movies)

            # ── Build AnimeEntry ──────────────────────────────────────
            ids = movie.get("ids", {})
            last_watched = raw.get("last_watched_at")
            finish_date = self._extract_date(last_watched)

            entry = AnimeEntry(
                trakt_id=trakt_id or 0,
                trakt_type="movie",
                title=title,
                year=movie.get("year"),
                tmdb_id=ids.get("tmdb"),
                imdb_id=ids.get("imdb"),
                tvdb_id=None,
                episodes_watched=1,        # Movies are always 1 episode
                total_episodes=1,
                last_watched_at=last_watched,
                finish_date=finish_date,
                status="Completed",
                score=0,
                detection_reason=reason,
            )
            anime_entries.append(entry)

        logger.info(
            "Found %d anime movies out of %d total movies",
            len(anime_entries), len(raw_movies),
        )
        return anime_entries, skip_records

    def extract_show_seasons(self) -> tuple[list[SeasonWatchData], list[SkipRecord]]:
        """
        Parse watched-shows.json and return per-season watch data.
        Does NOT classify anime — that requires TMDB API calls.

        Returns:
            (season_watch_list, skip_records)
        """
        raw_shows = self._load_json(self.shows_path)
        if raw_shows is None:
            return [], [SkipRecord(
                title="(all shows)",
                reason=f"Failed to load {self.shows_path}",
                phase="detection",
            )]

        seasons: list[SeasonWatchData] = []
        skip_records: list[SkipRecord] = []
        seen_keys: set[str] = set()

        logger.info("Parsing %d shows from %s", len(raw_shows), self.shows_path.name)

        for raw in raw_shows:
            show = raw.get("show")
            if not show:
                skip_records.append(SkipRecord(
                    title="(unknown)",
                    reason="Show entry has no 'show' field",
                    phase="detection",
                ))
                continue

            title = show.get("title", "(untitled)")
            ids = show.get("ids", {})
            trakt_id = ids.get("trakt")
            tmdb_id = ids.get("tmdb")
            imdb_id = ids.get("imdb")
            tvdb_id = ids.get("tvdb")
            year = show.get("year")
            aired_episodes = show.get("aired_episodes", 0)

            if not tmdb_id:
                skip_records.append(SkipRecord(
                    title=title,
                    trakt_id=trakt_id,
                    trakt_type="show",
                    reason="No TMDB ID available — cannot classify via TMDB API",
                    phase="detection",
                ))
                continue

            # Extract per-season watch data
            for season_data in raw.get("seasons", []):
                season_num = season_data.get("number")
                if season_num is None:
                    continue

                episodes = season_data.get("episodes", [])
                ep_count = len(episodes)

                # Find the latest watch timestamp in this season
                last_watched = None
                for ep in episodes:
                    ep_watched = ep.get("last_watched_at")
                    if ep_watched and (last_watched is None or ep_watched > last_watched):
                        last_watched = ep_watched

                # Deduplicate by (trakt_id, season_number)
                key = f"{trakt_id}:S{season_num}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)

                seasons.append(SeasonWatchData(
                    show_title=title,
                    show_trakt_id=trakt_id or 0,
                    show_tmdb_id=tmdb_id,
                    show_imdb_id=imdb_id,
                    show_tvdb_id=tvdb_id,
                    show_year=year,
                    season_number=season_num,
                    episodes_watched=ep_count,
                    total_aired_episodes=aired_episodes,
                    last_watched_at=last_watched,
                ))

        logger.info(
            "Extracted %d season entries from %d shows",
            len(seasons), len(raw_shows),
        )
        return seasons, skip_records

    # ── Private Helpers ───────────────────────────────────────────────────

    def _load_json(self, path: Path) -> Optional[list]:
        """Load and parse a JSON file, returning None on failure."""
        if not path.exists():
            logger.error("File not found: %s", path)
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                logger.error("Expected a JSON array in %s, got %s", path, type(data))
                return None
            return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Failed to load %s: %s", path, exc)
            return None

    @staticmethod
    def _extract_date(iso_timestamp: Optional[str]) -> Optional[str]:
        """Extract YYYY-MM-DD from an ISO 8601 timestamp string."""
        if not iso_timestamp:
            return None
        # "2026-04-17T14:44:00.000Z" → "2026-04-17"
        return iso_timestamp[:10] if len(iso_timestamp) >= 10 else None
