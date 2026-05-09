"""
TraktParser — reads Trakt JSON exports and extracts anime entries.

Handles 5 data sources from the Trakt data folder:
  - watched-movies.json   -> Anime movie detection (local, genre-based)
  - watched-shows.json    -> Show season extraction (for TMDB classification)
  - lists-watchlist.json  -> Plan to Watch entries
  - ratings-movies.json   -> Movie scores (1-10)
  - ratings-shows.json    -> Show scores (1-10, applied to all seasons)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from .models import AnimeEntry, SeasonWatchData, SkipRecord

logger = logging.getLogger(__name__)


class TraktParser:
    """Parses all Trakt export JSON files from a data folder."""

    # Trakt uses "anime" as a genre for Japanese anime movies.
    # Western animated films use "animation" — they are mutually exclusive.
    ANIME_GENRE = "anime"
    ANIMATION_GENRE = "animation"
    JAPANESE_LANG = "ja"

    def __init__(
        self,
        movies_path: Path,
        shows_path: Path,
        watchlist_path: Optional[Path] = None,
        ratings_movies_path: Optional[Path] = None,
        ratings_shows_path: Optional[Path] = None,
    ):
        self.movies_path = movies_path
        self.shows_path = shows_path
        self.watchlist_path = watchlist_path
        self.ratings_movies_path = ratings_movies_path
        self.ratings_shows_path = ratings_shows_path

        # Rating lookup tables: trakt_id -> rating (1-10)
        self._movie_ratings: dict[int, int] = {}
        self._show_ratings: dict[int, int] = {}

    # ── Public API ────────────────────────────────────────────────────────

    def load_ratings(self) -> None:
        """
        Pre-load all ratings into lookup tables.
        Call this before extract_anime_movies / extract_show_seasons
        so that ratings can be injected into entries.
        """
        # Movie ratings
        if self.ratings_movies_path and self.ratings_movies_path.exists():
            raw = self._load_json(self.ratings_movies_path)
            if raw:
                for item in raw:
                    movie = item.get("movie", {})
                    trakt_id = movie.get("ids", {}).get("trakt")
                    rating = item.get("rating")
                    if trakt_id and rating:
                        self._movie_ratings[trakt_id] = int(rating)
                logger.info("Loaded %d movie ratings", len(self._movie_ratings))

        # Show ratings (applied to ALL seasons of that show)
        if self.ratings_shows_path and self.ratings_shows_path.exists():
            raw = self._load_json(self.ratings_shows_path)
            if raw:
                for item in raw:
                    show = item.get("show", {})
                    trakt_id = show.get("ids", {}).get("trakt")
                    rating = item.get("rating")
                    if trakt_id and rating:
                        self._show_ratings[trakt_id] = int(rating)
                logger.info("Loaded %d show ratings", len(self._show_ratings))

    def extract_anime_movies(self) -> tuple[list[AnimeEntry], list[AnimeEntry], list[SkipRecord]]:
        """
        Parse watched-movies.json and return anime movies.

        Returns:
            (anime_entries_detected_locally, unclassified_movie_candidates, skip_records)

        If the Trakt export includes genre data, anime is detected locally.
        If not (raw API export), all movies are returned as unclassified candidates
        for TMDB-based classification.
        """
        raw_movies = self._load_json(self.movies_path)
        if raw_movies is None:
            return [], [], [SkipRecord(
                title="(all movies)",
                reason=f"Failed to load {self.movies_path}",
                phase="detection",
            )]

        anime_entries: list[AnimeEntry] = []
        unclassified: list[AnimeEntry] = []
        skip_records: list[SkipRecord] = []
        seen_trakt_ids: set[int] = set()

        logger.info("Parsing %d movies from %s", len(raw_movies), self.movies_path.name)

        # Check if first movie has genre data to decide classification mode
        first_movie = raw_movies[0].get("movie", {}) if raw_movies else {}
        has_genres = bool(first_movie.get("genres"))
        if has_genres:
            logger.info("Genre data found in export -- using local anime detection")
        else:
            logger.info("No genre data in export -- all movies will be classified via TMDB")

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

            ids = movie.get("ids", {})
            last_watched = raw.get("last_watched_at")
            finish_date = self._extract_date(last_watched)
            score = self._movie_ratings.get(trakt_id, 0) if trakt_id else 0

            entry = AnimeEntry(
                trakt_id=trakt_id or 0,
                trakt_type="movie",
                title=title,
                year=movie.get("year"),
                tmdb_id=ids.get("tmdb"),
                imdb_id=ids.get("imdb"),
                tvdb_id=None,
                episodes_watched=1,
                total_episodes=1,
                last_watched_at=last_watched,
                start_date=finish_date,
                finish_date=finish_date,
                status="Completed",
                score=score,
                detection_reason="",
            )

            if has_genres:
                # Local classification path
                genres = movie.get("genres", [])
                language = movie.get("language", "")

                if self.ANIME_GENRE in genres:
                    entry.detection_reason = f'Genre "{self.ANIME_GENRE}" found in Trakt genres: {genres}'
                    anime_entries.append(entry)
                elif self.ANIMATION_GENRE in genres and language == self.JAPANESE_LANG:
                    entry.detection_reason = (
                        f'Genre "{self.ANIMATION_GENRE}" + language "{language}" '
                        f"detected as anime (fallback rule)"
                    )
                    anime_entries.append(entry)
                # else: not anime, silently skip
            else:
                # No genre data -- send to TMDB for classification
                if ids.get("tmdb"):
                    unclassified.append(entry)

        if has_genres:
            logger.info(
                "Found %d anime movies out of %d total movies (local detection)",
                len(anime_entries), len(raw_movies),
            )
        else:
            logger.info(
                "Extracted %d movie candidates for TMDB classification",
                len(unclassified),
            )

        return anime_entries, unclassified, skip_records

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
                    reason="No TMDB ID available -- cannot classify via TMDB API",
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

                # Find the earliest and latest watch timestamp in this season
                first_watched = None
                last_watched = None
                for ep in episodes:
                    ep_watched = ep.get("last_watched_at")
                    if ep_watched:
                        if first_watched is None or ep_watched < first_watched:
                            first_watched = ep_watched
                        if last_watched is None or ep_watched > last_watched:
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
                    first_watched_at=first_watched,
                    last_watched_at=last_watched,
                ))

        logger.info(
            "Extracted %d season entries from %d shows",
            len(seasons), len(raw_shows),
        )
        return seasons, skip_records

    def extract_watchlist(self) -> tuple[list[dict], list[SkipRecord]]:
        """
        Parse lists-watchlist.json and return raw watchlist items.
        Each item has 'type' ('show' or 'movie') and the corresponding data.

        Returns:
            (watchlist_items, skip_records)
        """
        if not self.watchlist_path or not self.watchlist_path.exists():
            logger.info("No watchlist file found, skipping")
            return [], []

        raw = self._load_json(self.watchlist_path)
        if raw is None:
            return [], [SkipRecord(
                title="(watchlist)",
                reason=f"Failed to load {self.watchlist_path}",
                phase="detection",
            )]

        logger.info("Loaded %d watchlist items from %s", len(raw), self.watchlist_path.name)
        return raw, []

    def get_show_rating(self, trakt_id: int) -> int:
        """Get the Trakt rating (1-10) for a show. Returns 0 if unrated."""
        return self._show_ratings.get(trakt_id, 0)

    def get_movie_rating(self, trakt_id: int) -> int:
        """Get the Trakt rating (1-10) for a movie. Returns 0 if unrated."""
        return self._movie_ratings.get(trakt_id, 0)

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
        # "2026-04-17T14:44:00.000Z" -> "2026-04-17"
        return iso_timestamp[:10] if len(iso_timestamp) >= 10 else None
