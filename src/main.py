"""
main.py — CLI entry point and orchestrator for the Trakt-to-MAL pipeline.

Pipeline:
  1. Parse Trakt exports → detect anime movies (local) + extract show seasons
  2. Classify shows via TMDB API → filter anime shows
  3. Resolve MAL IDs (offline DB → AniList → Jikan)
  4. Generate MAL XML import file + reports
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from .config import Config
from .models import AnimeEntry, SkipRecord, SeasonWatchData
from .trakt_parser import TraktParser
from .tmdb_client import TMDBClient
from .offline_db import OfflineDBLookup
from .anilist_client import AniListClient
from .jikan_client import JikanClient
from .mal_id_resolver import MALIdResolver
from .mal_xml_generator import MALXmlGenerator
from .report_generator import ReportGenerator
from .rate_limiter import RateLimiter


def setup_logging(verbose: bool = False) -> None:
    """Configure logging with a clean console format."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-5s  %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    # Suppress noisy HTTP logs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def run_pipeline(config: Config, verbose: bool = False) -> None:
    """Execute the full Trakt-to-MAL pipeline."""

    setup_logging(verbose)
    logger = logging.getLogger(__name__)

    all_anime: list[AnimeEntry] = []
    all_skips: list[SkipRecord] = []
    anime_movies: list[AnimeEntry] = []
    anime_shows: list[AnimeEntry] = []

    # ══════════════════════════════════════════════════════════════════
    # PHASE 1: Parse Trakt exports & detect anime movies
    # ══════════════════════════════════════════════════════════════════
    logger.info("=" * 50)
    logger.info("PHASE 1: Parsing Trakt exports")
    logger.info("=" * 50)

    parser = TraktParser(config.trakt_movies_file, config.trakt_shows_file)

    # Movies — fully local detection
    anime_movies, movie_skips = parser.extract_anime_movies()
    all_skips.extend(movie_skips)

    # Shows — extract season data (TMDB classification comes in Phase 2)
    show_seasons, show_parse_skips = parser.extract_show_seasons()
    all_skips.extend(show_parse_skips)

    logger.info(
        "Phase 1 complete: %d anime movies, %d show seasons to classify",
        len(anime_movies), len(show_seasons),
    )

    # ══════════════════════════════════════════════════════════════════
    # PHASE 2: Classify shows via TMDB API
    # ══════════════════════════════════════════════════════════════════
    logger.info("=" * 50)
    logger.info("PHASE 2: Classifying shows via TMDB API")
    logger.info("=" * 50)

    tmdb_limiter = RateLimiter(
        max_requests=config.tmdb_rate_limit,
        window_seconds=config.tmdb_rate_window,
        name="TMDB",
    )
    tmdb_client = TMDBClient(
        api_key=config.tmdb_api_key,
        cache_dir=config.cache_dir,
        rate_limiter=tmdb_limiter,
        timeout=config.request_timeout,
        max_retries=config.max_retries,
    )

    # Get unique TMDB IDs to minimize API calls
    unique_tmdb_ids = list({s.show_tmdb_id for s in show_seasons if s.show_tmdb_id})
    logger.info("Fetching metadata for %d unique shows from TMDB", len(unique_tmdb_ids))

    tmdb_metadata = tmdb_client.get_show_metadata_batch(unique_tmdb_ids)

    # Classify each season and build AnimeEntry objects for anime shows
    for season_data in show_seasons:
        if not season_data.show_tmdb_id:
            continue

        metadata = tmdb_metadata.get(season_data.show_tmdb_id)

        if metadata is None:
            all_skips.append(SkipRecord(
                title=season_data.show_title,
                trakt_id=season_data.show_trakt_id,
                trakt_type="show",
                tmdb_id=season_data.show_tmdb_id,
                season_number=season_data.season_number,
                reason="TMDB API returned no data",
                phase="tmdb_lookup",
            ))
            continue

        if not metadata.is_anime:
            continue  # Not anime — silently skip

        # Determine if this season is fully completed
        season_ep_count = metadata.season_episode_counts.get(
            season_data.season_number, 0
        )
        is_complete = (
            season_ep_count > 0
            and season_data.episodes_watched >= season_ep_count
        )

        # User decision: only import fully completed entries
        if not is_complete:
            all_skips.append(SkipRecord(
                title=f"{season_data.show_title} S{season_data.season_number}",
                trakt_id=season_data.show_trakt_id,
                trakt_type="show",
                tmdb_id=season_data.show_tmdb_id,
                season_number=season_data.season_number,
                reason=(
                    f"Not fully completed: {season_data.episodes_watched}/{season_ep_count} episodes watched"
                ),
                phase="tmdb_lookup",
            ))
            continue

        finish_date = None
        if season_data.last_watched_at and len(season_data.last_watched_at) >= 10:
            finish_date = season_data.last_watched_at[:10]

        entry = AnimeEntry(
            trakt_id=season_data.show_trakt_id,
            trakt_type="show",
            title=season_data.show_title,
            year=season_data.show_year,
            tmdb_id=season_data.show_tmdb_id,
            imdb_id=season_data.show_imdb_id,
            tvdb_id=season_data.show_tvdb_id,
            season_number=season_data.season_number,
            episodes_watched=season_data.episodes_watched,
            total_episodes=season_ep_count or None,
            last_watched_at=season_data.last_watched_at,
            finish_date=finish_date,
            status="Completed",
            score=0,
            detection_reason=(
                f"TMDB genres {metadata.genres} + language '{metadata.original_language}' "
                f"→ Animation + Japanese"
            ),
        )
        anime_shows.append(entry)

    logger.info(
        "Phase 2 complete: %d anime show seasons detected",
        len(anime_shows),
    )

    # ══════════════════════════════════════════════════════════════════
    # PHASE 3: Resolve MAL IDs
    # ══════════════════════════════════════════════════════════════════
    logger.info("=" * 50)
    logger.info("PHASE 3: Resolving MAL IDs")
    logger.info("=" * 50)

    all_anime = anime_movies + anime_shows

    # Set up resolution pipeline
    offline_db = OfflineDBLookup(cache_dir=config.cache_dir)

    anilist_limiter = RateLimiter(
        max_requests=config.anilist_rate_limit,
        window_seconds=config.anilist_rate_window,
        name="AniList",
    )
    anilist_client = AniListClient(
        rate_limiter=anilist_limiter,
        timeout=config.request_timeout,
        max_retries=config.max_retries,
    )

    jikan_limiter = RateLimiter(
        max_requests=config.jikan_rate_limit,
        window_seconds=config.jikan_rate_window,
        name="Jikan",
    )
    jikan_client = JikanClient(
        rate_limiter=jikan_limiter,
        timeout=config.request_timeout,
        max_retries=config.max_retries,
    )

    resolver = MALIdResolver(
        offline_db=offline_db,
        anilist_client=anilist_client,
        jikan_client=jikan_client,
    )

    all_anime, resolution_skips = resolver.resolve_all(all_anime)
    all_skips.extend(resolution_skips)

    resolved_count = sum(1 for e in all_anime if e.is_resolved())
    unresolved_count = len(all_anime) - resolved_count

    logger.info(
        "Phase 3 complete: %d resolved, %d unresolved",
        resolved_count, unresolved_count,
    )

    # ══════════════════════════════════════════════════════════════════
    # PHASE 4: Generate outputs
    # ══════════════════════════════════════════════════════════════════
    logger.info("=" * 50)
    logger.info("PHASE 4: Generating output files")
    logger.info("=" * 50)

    report = ReportGenerator(config.output_dir)

    # MAL XML
    xml_gen = MALXmlGenerator(config.output_dir)
    xml_path = xml_gen.generate(all_anime)

    # JSON outputs
    report.write_anime_entries(all_anime)
    report.write_detection_report(all_anime)
    report.write_skip_log(all_skips)

    # Console summary
    report.print_summary(
        anime_movies=anime_movies,
        anime_shows=anime_shows,
        skips=all_skips,
        resolved_count=resolved_count,
        unresolved_count=unresolved_count,
    )

    logger.info("All outputs written to: %s", config.output_dir)
    logger.info("Done! 🎉")


def main() -> None:
    """CLI entry point. Launches GUI by default unless --cli is specified."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Trakt-to-MAL: Extract anime from Trakt history and generate MAL import file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m src.main                    # Launch UI
  python -m src.main --cli              # Run with default settings from .env
  python -m src.main --cli --verbose    # Run with debug logging
        """,
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Run without GUI",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging (CLI only)",
    )
    parser.add_argument(
        "--movies",
        type=str,
        default=None,
        help="Path to watched-movies.json (overrides .env)",
    )
    parser.add_argument(
        "--shows",
        type=str,
        default=None,
        help="Path to watched-shows.json (overrides .env)",
    )

    args = parser.parse_args()

    # If not CLI mode, launch GUI
    if not args.cli:
        try:
            from .gui import launch_gui
            launch_gui()
            return
        except ImportError as e:
            print(f"Failed to load GUI: {e}")
            print("Falling back to CLI mode...")

    # Load config for CLI mode
    config = Config()

    # Override file paths if provided via CLI
    if args.movies:
        config._movies_override = Path(args.movies)
    if args.shows:
        config._shows_override = Path(args.shows)

    try:
        run_pipeline(config, verbose=args.verbose)
    except ValueError as exc:
        print(f"\n[ERROR] Configuration error: {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n\n[INFO]  Interrupted by user")
        sys.exit(130)


if __name__ == "__main__":
    main()
