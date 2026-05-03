"""
ReportGenerator — produces detection reports, skip logs, and anime JSON output.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from dataclasses import asdict
from typing import Optional

from .models import AnimeEntry, SkipRecord

logger = logging.getLogger(__name__)


class ReportGenerator:
    """Generates all output reports: detection report, skip log, anime JSON."""

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ── Detection Report ──────────────────────────────────────────────────

    def write_detection_report(
        self,
        entries: list[AnimeEntry],
        filepath: Optional[Path] = None,
    ) -> Path:
        """
        Write a detection report showing each anime entry
        and the exact reason it was classified as anime.
        """
        filepath = filepath or self.output_dir / "detection_report.json"

        report = []
        for entry in entries:
            report.append({
                "title": entry.title,
                "year": entry.year,
                "type": entry.trakt_type,
                "trakt_id": entry.trakt_id,
                "tmdb_id": entry.tmdb_id,
                "mal_id": entry.mal_id,
                "mal_title": entry.mal_title,
                "season": entry.season_number,
                "episodes_watched": entry.episodes_watched,
                "total_episodes": entry.total_episodes,
                "status": entry.status,
                "score": entry.score,
                "detection_reason": entry.detection_reason,
                "resolution_tier": entry.resolution_tier,
                "finish_date": entry.finish_date,
            })

        self._write_json(filepath, report)
        logger.info("Detection report: %d entries → %s", len(report), filepath.name)
        return filepath

    # ── Skip Log ──────────────────────────────────────────────────────────

    def write_skip_log(
        self,
        skips: list[SkipRecord],
        filepath: Optional[Path] = None,
    ) -> Path:
        """Write a log of all skipped/unresolved entries with reasons."""
        filepath = filepath or self.output_dir / "skip_log.json"

        log = [skip.to_dict() for skip in skips]
        self._write_json(filepath, log)
        logger.info("Skip log: %d entries → %s", len(log), filepath.name)
        return filepath

    # ── Anime Entries JSON ────────────────────────────────────────────────

    def write_anime_entries(
        self,
        entries: list[AnimeEntry],
        filepath: Optional[Path] = None,
    ) -> Path:
        """Write the filtered anime entries as a JSON file."""
        filepath = filepath or self.output_dir / "anime_entries.json"

        data = [asdict(entry) for entry in entries]
        self._write_json(filepath, data)
        logger.info("Anime entries: %d entries → %s", len(data), filepath.name)
        return filepath

    # ── Console Summary ───────────────────────────────────────────────────

    def print_summary(
        self,
        anime_movies: list[AnimeEntry],
        anime_shows: list[AnimeEntry],
        skips: list[SkipRecord],
        anime_watchlist: list[AnimeEntry] | None = None,
        resolved_count: int = 0,
        unresolved_count: int = 0,
    ) -> None:
        """Print a formatted summary to the console."""
        anime_watchlist = anime_watchlist or []
        total = len(anime_movies) + len(anime_shows) + len(anime_watchlist)

        completed_shows = [s for s in anime_shows if s.status == "Completed"]
        watching_shows = [s for s in anime_shows if s.status == "Watching"]
        scored_count = sum(1 for e in (anime_movies + anime_shows) if e.score > 0)

        print("\n" + "=" * 60)
        print("  TRAKT -> MAL  |  Processing Summary")
        print("=" * 60)
        print(f"  Anime Movies (Completed) : {len(anime_movies)}")
        print(f"  Anime Shows  (Completed) : {len(completed_shows)}")
        print(f"  Anime Shows  (Watching)  : {len(watching_shows)}")
        print(f"  Watchlist (Plan to Watch) : {len(anime_watchlist)}")
        print(f"  ---------------------------------")
        print(f"  Total Anime Entries      : {total}")
        print(f"  Entries with Scores      : {scored_count}")

        if resolved_count or unresolved_count:
            print(f"  MAL IDs Resolved         : {resolved_count}")
            print(f"  MAL IDs Unresolved       : {unresolved_count}")

        print(f"  Skipped Entries          : {len(skips)}")
        print("=" * 60)

        if anime_movies:
            print("\n  [MOVIE]  Anime Movies (Completed):")
            for m in anime_movies:
                mal_str = f"  [MAL:{m.mal_id}]" if m.mal_id else ""
                score_str = f"  Score:{m.score}" if m.score > 0 else ""
                print(f"    - {m.title} ({m.year}){score_str}{mal_str}")

        if completed_shows:
            print(f"\n  [TV]  Anime Shows - Completed ({len(completed_shows)}):")
            for s in completed_shows:
                season_str = f" S{s.season_number}" if s.season_number is not None else ""
                ep_str = f" [{s.episodes_watched}ep]"
                mal_str = f"  [MAL:{s.mal_id}]" if s.mal_id else ""
                score_str = f"  Score:{s.score}" if s.score > 0 else ""
                print(f"    - {s.title}{season_str}{ep_str}{score_str}{mal_str}")

        if watching_shows:
            print(f"\n  [TV]  Anime Shows - Watching ({len(watching_shows)}):")
            for s in watching_shows:
                season_str = f" S{s.season_number}" if s.season_number is not None else ""
                total_str = f"/{s.total_episodes}" if s.total_episodes else ""
                ep_str = f" [{s.episodes_watched}{total_str}ep]"
                mal_str = f"  [MAL:{s.mal_id}]" if s.mal_id else ""
                score_str = f"  Score:{s.score}" if s.score > 0 else ""
                print(f"    - {s.title}{season_str}{ep_str}{score_str}{mal_str}")

        if anime_watchlist:
            print(f"\n  [PTW]  Plan to Watch ({len(anime_watchlist)}):")
            for w in anime_watchlist:
                mal_str = f"  [MAL:{w.mal_id}]" if w.mal_id else ""
                print(f"    - {w.title} ({w.year}){mal_str}")

        if skips:
            print(f"\n  [WARN]  Skipped ({len(skips)}):")
            for sk in skips[:10]:  # Show first 10
                print(f"    - {sk.title}: {sk.reason}")
            if len(skips) > 10:
                print(f"    ... and {len(skips) - 10} more (see skip_log.json)")

        print()

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _write_json(filepath: Path, data: list | dict) -> None:
        """Write data as formatted JSON."""
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
