"""
Quick Phase 1 test — validates anime movie detection from Trakt exports.
No API keys or network access needed.
"""

import sys
import logging
from pathlib import Path

# Add project root to path
project_root = Path(__file__).resolve().parent
sys.path.insert(0, str(project_root))

from src.trakt_parser import TraktParser
from src.report_generator import ReportGenerator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(message)s",
    datefmt="%H:%M:%S",
)

def main():
    movies_file = project_root / "watched-movies.json"
    shows_file = project_root / "watched-shows.json"
    output_dir = project_root / "output"

    parser = TraktParser(movies_file, shows_file)

    # ── Phase 1: Anime Movie Detection ────────────────────────────────
    print("\n" + "=" * 60)
    print("  PHASE 1 TEST: Anime Movie Detection")
    print("=" * 60)

    anime_movies, movie_skips = parser.extract_anime_movies()

    print(f"\n  [OK] Found {len(anime_movies)} anime movies\n")
    for i, m in enumerate(anime_movies, 1):
        print(f"  {i:2d}. {m.title} ({m.year})")
        print(f"      Reason: {m.detection_reason}")
        print(f"      TMDB: {m.tmdb_id} | IMDb: {m.imdb_id}")
        print(f"      Finished: {m.finish_date}")
        print()

    # ── Show season extraction (no classification yet) ────────────────
    show_seasons, show_skips = parser.extract_show_seasons()
    print(f"  [TV] Extracted {len(show_seasons)} show seasons (pending TMDB classification)")
    print(f"  [SKIP] Skipped: {len(movie_skips) + len(show_skips)} entries")

    # ── Write Phase 1 outputs ─────────────────────────────────────────
    reporter = ReportGenerator(output_dir)
    reporter.write_detection_report(anime_movies)
    reporter.write_skip_log(movie_skips + show_skips)
    reporter.write_anime_entries(anime_movies)

    reporter.print_summary(
        anime_movies=anime_movies,
        anime_shows=[],
        skips=movie_skips + show_skips,
    )

    print(f"  [DIR] Output files written to: {output_dir}\n")


if __name__ == "__main__":
    main()
