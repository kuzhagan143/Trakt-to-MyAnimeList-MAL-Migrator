"""
Data models — immutable data classes used throughout the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AnimeEntry:
    """A single anime entry detected from Trakt history, ready for MAL export."""

    # ── Source identifiers ────────────────────────────────────────────────
    trakt_id: int
    trakt_type: str                          # "movie" or "show"
    title: str
    year: Optional[int] = None

    # ── External IDs ──────────────────────────────────────────────────────
    tmdb_id: Optional[int] = None
    imdb_id: Optional[str] = None
    tvdb_id: Optional[int] = None

    # ── MAL mapping ───────────────────────────────────────────────────────
    mal_id: Optional[int] = None
    mal_title: Optional[str] = None          # Title as it appears on MAL
    mal_type: Optional[str] = None           # "TV", "Movie", "OVA", "ONA", "Special"
    mal_total_episodes: Optional[int] = None # Total episodes on MAL

    # ── Watch data (from Trakt) ───────────────────────────────────────────
    season_number: Optional[int] = None      # For shows: which Trakt season
    episodes_watched: int = 0
    total_episodes: Optional[int] = None     # Total episodes from Trakt/TMDB
    last_watched_at: Optional[str] = None
    start_date: Optional[str] = None         # Derived from first_watched_at
    finish_date: Optional[str] = None        # Derived from last_watched_at

    # ── MAL fields ────────────────────────────────────────────────────────
    status: str = "Completed"                # "Completed", "Watching", etc.
    score: int = 0                           # 0 = unscored (user decision)

    # ── Detection metadata ────────────────────────────────────────────────
    detection_reason: str = ""               # Human-readable reason
    resolution_tier: str = "unresolved"      # "offline_db", "anilist", "jikan", "unresolved"

    def is_resolved(self) -> bool:
        """Whether this entry has a resolved MAL ID."""
        return self.mal_id is not None


@dataclass
class SkipRecord:
    """An entry that was skipped during processing, with the reason why."""

    title: str
    trakt_id: Optional[int] = None
    trakt_type: str = ""                     # "movie" or "show"
    tmdb_id: Optional[int] = None
    season_number: Optional[int] = None
    reason: str = ""                         # Why it was skipped
    phase: str = ""                          # "detection", "tmdb_lookup", "mal_resolution"

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "trakt_id": self.trakt_id,
            "trakt_type": self.trakt_type,
            "tmdb_id": self.tmdb_id,
            "season_number": self.season_number,
            "reason": self.reason,
            "phase": self.phase,
        }


@dataclass
class ShowMetadata:
    """Metadata fetched from TMDB for a TV show."""

    tmdb_id: int
    title: str
    original_language: str
    genres: list[str] = field(default_factory=list)
    is_anime: bool = False
    total_seasons: int = 0
    season_episode_counts: dict[int, int] = field(default_factory=dict)  # {season_num: episode_count}


@dataclass
class SeasonWatchData:
    """Watch progress for a single season of a show from Trakt."""

    show_title: str
    show_trakt_id: int
    show_tmdb_id: Optional[int]
    show_imdb_id: Optional[str]
    show_tvdb_id: Optional[int]
    show_year: Optional[int]
    season_number: int
    episodes_watched: int
    total_aired_episodes: int               # From the show-level aired_episodes field
    first_watched_at: Optional[str] = None
    last_watched_at: Optional[str] = None
    is_complete: bool = False               # All episodes of this season watched?
