"""
MALIdResolver — orchestrates the 3-tier MAL ID resolution pipeline.

Tier 1: Offline database (Kometa Anime-IDs) — instant, no API calls
Tier 2: AniList GraphQL API — search by title, returns idMal
Tier 3: Jikan API (MAL wrapper) — last-resort fallback
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from .models import AnimeEntry, SkipRecord
from .offline_db import OfflineDBLookup
from .anilist_client import AniListClient
from .jikan_client import JikanClient

logger = logging.getLogger(__name__)

# Common season title patterns used for searching
SEASON_PATTERNS = [
    "{title} Season {n}",
    "{title} {n}nd Season",
    "{title} {n}rd Season",
    "{title} {n}th Season",
    "{title} Part {n}",
    "{title} {n}",
]


class MALIdResolver:
    """
    Resolves anime entries to MAL IDs using a 3-tier cascade strategy.

    Priority:
        1. Offline DB (instant, covers ~90% of known anime)
        2. AniList API (search by title, returns idMal)
        3. Jikan API (MAL search, last resort)
    """

    def __init__(
        self,
        offline_db: OfflineDBLookup,
        anilist_client: AniListClient,
        jikan_client: JikanClient,
    ):
        self.offline_db = offline_db
        self.anilist = anilist_client
        self.jikan = jikan_client

    # ── Public API ────────────────────────────────────────────────────────

    def resolve_all(
        self,
        entries: list[AnimeEntry],
    ) -> tuple[list[AnimeEntry], list[SkipRecord]]:
        """
        Resolve MAL IDs for all anime entries.

        Returns (resolved_entries, skip_records_for_unresolved).
        Entries that are resolved have mal_id and resolution_tier set.
        Entries that fail all tiers are returned with mal_id=None and
        also added to skip_records.
        """
        # Make sure offline DB is loaded
        self.offline_db.ensure_loaded()

        resolved: list[AnimeEntry] = []
        skips: list[SkipRecord] = []
        total = len(entries)

        for i, entry in enumerate(entries, 1):
            if i % 20 == 0 or i == total:
                logger.info("[Resolver] Progress: %d/%d entries", i, total)

            mal_id, tier = self._resolve_single(entry)

            if mal_id:
                entry.mal_id = mal_id
                entry.resolution_tier = tier
                resolved.append(entry)
                logger.debug(
                    "[Resolver] ✓ %s → MAL:%d (via %s)",
                    entry.title, mal_id, tier,
                )
            else:
                entry.resolution_tier = "unresolved"
                resolved.append(entry)  # Still include in output
                skips.append(SkipRecord(
                    title=entry.title,
                    trakt_id=entry.trakt_id,
                    trakt_type=entry.trakt_type,
                    tmdb_id=entry.tmdb_id,
                    season_number=entry.season_number,
                    reason="Could not resolve MAL ID via any tier (offline DB, AniList, Jikan)",
                    phase="mal_resolution",
                ))
                logger.warning("[Resolver] ✗ %s — unresolved", entry.title)

        resolved_count = sum(1 for e in resolved if e.is_resolved())
        logger.info(
            "[Resolver] Done: %d/%d resolved, %d unresolved",
            resolved_count, total, total - resolved_count,
        )
        return resolved, skips

    # ── Private Resolution Logic ──────────────────────────────────────────

    def _resolve_single(self, entry: AnimeEntry) -> tuple[Optional[int], str]:
        """
        Try to resolve a single entry through all 3 tiers.
        Returns (mal_id, tier_name) or (None, "").
        """
        # ── Tier 1: Offline Database ──────────────────────────────────
        mal_id = self._try_offline_db(entry)
        if mal_id:
            return mal_id, "offline_db"

        # ── Tier 2: AniList API ───────────────────────────────────────
        mal_id = self._try_anilist(entry)
        if mal_id:
            return mal_id, "anilist"

        # ── Tier 3: Jikan API ─────────────────────────────────────────
        mal_id = self._try_jikan(entry)
        if mal_id:
            return mal_id, "jikan"

        return None, ""

    def _try_offline_db(self, entry: AnimeEntry) -> Optional[int]:
        """Tier 1: Look up MAL ID from the offline Kometa database."""
        if entry.trakt_type == "movie":
            # Try TMDB movie ID
            if entry.tmdb_id:
                mal_id = self.offline_db.lookup_by_tmdb_movie(entry.tmdb_id)
                if mal_id:
                    return mal_id

            # Try IMDb ID
            if entry.imdb_id:
                mal_id = self.offline_db.lookup_by_imdb(entry.imdb_id)
                if mal_id:
                    return mal_id

        else:  # show
            # Try TMDB show ID + season
            if entry.tmdb_id:
                mal_id = self.offline_db.lookup_by_tmdb_show(
                    entry.tmdb_id, season=entry.season_number
                )
                if mal_id:
                    return mal_id

            # Try TVDB ID + season
            if entry.tvdb_id:
                mal_id = self.offline_db.lookup_by_tvdb(
                    entry.tvdb_id, season=entry.season_number
                )
                if mal_id:
                    return mal_id

        return None

    def _try_anilist(self, entry: AnimeEntry) -> Optional[int]:
        """Tier 2: Search AniList and find a matching MAL ID."""
        search_queries = self._build_search_queries(entry)

        for query in search_queries:
            results = self.anilist.search_anime(query)
            if not results:
                continue

            # Try to find the best match
            mal_id = self._pick_best_match(entry, results, source="anilist")
            if mal_id:
                return mal_id

        return None

    def _try_jikan(self, entry: AnimeEntry) -> Optional[int]:
        """Tier 3: Search Jikan (MAL) as last resort."""
        # Determine type filter
        anime_type = None
        if entry.trakt_type == "movie":
            anime_type = "movie"
        elif entry.season_number == 0:
            anime_type = "special"

        search_queries = self._build_search_queries(entry)

        for query in search_queries:
            results = self.jikan.search_anime(query, anime_type=anime_type)
            if not results:
                continue

            mal_id = self._pick_best_match(entry, results, source="jikan")
            if mal_id:
                return mal_id

        return None

    # ── Search Query Building ─────────────────────────────────────────────

    def _build_search_queries(self, entry: AnimeEntry) -> list[str]:
        """
        Build a list of search queries to try, ordered by specificity.

        For multi-season shows, generates variations like:
        - "Dr. Stone Season 2"
        - "Dr. Stone 2nd Season"
        - "Dr. Stone"  (plain title as fallback)
        """
        queries: list[str] = []
        title = entry.title

        if entry.trakt_type == "show" and entry.season_number and entry.season_number > 1:
            n = entry.season_number
            for pattern in SEASON_PATTERNS:
                q = pattern.format(title=title, n=n)
                queries.append(q)

        # Always include the plain title as fallback
        queries.append(title)

        # Deduplicate while preserving order
        seen = set()
        unique = []
        for q in queries:
            if q.lower() not in seen:
                seen.add(q.lower())
                unique.append(q)

        return unique

    # ── Match Selection ───────────────────────────────────────────────────

    def _pick_best_match(
        self,
        entry: AnimeEntry,
        results: list[dict],
        source: str,
    ) -> Optional[int]:
        """
        Pick the best matching result from API search results.

        Scoring heuristics:
        - Exact title match (any variant) → high score
        - Year match → bonus
        - Episode count match → bonus
        - Format match (TV vs Movie) → bonus
        """
        entry_title_lower = entry.title.lower().strip()
        best_mal_id: Optional[int] = None
        best_score = -1

        for result in results:
            mal_id = self._extract_mal_id(result, source)
            if not mal_id:
                continue

            score = 0

            # Title matching
            titles = self._extract_titles(result, source)
            title_matched = False
            for t in titles:
                if t and t.lower().strip() == entry_title_lower:
                    score += 100
                    title_matched = True
                    break

            if not title_matched:
                # Partial match
                for t in titles:
                    if t and entry_title_lower in t.lower():
                        score += 50
                        break
                    if t and t.lower() in entry_title_lower:
                        score += 40
                        break

            # Year matching
            result_year = self._extract_year(result, source)
            if entry.year and result_year and entry.year == result_year:
                score += 20

            # Format matching
            result_format = self._extract_format(result, source)
            if entry.trakt_type == "movie" and result_format in ("Movie", "movie"):
                score += 15
            elif entry.trakt_type == "show" and result_format in ("TV", "tv", "ONA", "OVA"):
                score += 15

            # Episode count matching (for shows)
            result_episodes = self._extract_episodes(result, source)
            if entry.episodes_watched and result_episodes:
                if entry.episodes_watched == result_episodes:
                    score += 25
                elif abs(entry.episodes_watched - result_episodes) <= 2:
                    score += 10

            if score > best_score:
                best_score = score
                best_mal_id = mal_id

        # Require minimum confidence
        if best_score >= 40:
            return best_mal_id

        return None

    # ── Result Field Extractors ───────────────────────────────────────────

    @staticmethod
    def _extract_mal_id(result: dict, source: str) -> Optional[int]:
        if source == "anilist":
            return result.get("idMal")
        elif source == "jikan":
            return result.get("mal_id")
        return None

    @staticmethod
    def _extract_titles(result: dict, source: str) -> list[str]:
        if source == "anilist":
            title_obj = result.get("title", {})
            return [
                title_obj.get("english", ""),
                title_obj.get("romaji", ""),
                title_obj.get("native", ""),
            ]
        elif source == "jikan":
            return [
                result.get("title", ""),
                result.get("title_english", ""),
                result.get("title_japanese", ""),
            ]
        return []

    @staticmethod
    def _extract_year(result: dict, source: str) -> Optional[int]:
        if source == "anilist":
            return result.get("seasonYear") or (result.get("startDate") or {}).get("year")
        elif source == "jikan":
            return result.get("year")
        return None

    @staticmethod
    def _extract_format(result: dict, source: str) -> Optional[str]:
        if source == "anilist":
            fmt = result.get("format", "")
            return {"TV": "TV", "MOVIE": "Movie", "OVA": "OVA", "ONA": "ONA", "SPECIAL": "Special"}.get(fmt, fmt)
        elif source == "jikan":
            return result.get("type")
        return None

    @staticmethod
    def _extract_episodes(result: dict, source: str) -> Optional[int]:
        if source == "anilist":
            return result.get("episodes")
        elif source == "jikan":
            return result.get("episodes")
        return None
