"""
main.py -- CLI entry point and orchestrator for the Trakt-to-MAL pipeline.

Pipeline:
  1. Parse Trakt exports -> detect anime movies (local) + extract show seasons
     Also loads ratings and watchlist data.
  2. Classify shows via TMDB API -> filter anime shows
     Now creates Completed AND Watching entries. Injects show ratings.
  3. Process watchlist -> create Plan to Watch entries for anime items
  4. Resolve MAL IDs (offline DB -> AniList -> Jikan)
  5. Generate MAL XML import file + reports
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
    anime_watchlist: list[AnimeEntry] = []

    # ==================================================================
    # PHASE 1: Parse Trakt exports & detect anime movies
    # ==================================================================
    logger.info("=" * 50)
    logger.info("PHASE 1: Parsing Trakt exports")
    logger.info("=" * 50)

    parser = TraktParser(
        movies_path=config.trakt_movies_file,
        shows_path=config.trakt_shows_file,
        watchlist_path=config.trakt_watchlist_file,
        ratings_movies_path=config.trakt_ratings_movies_file,
        ratings_shows_path=config.trakt_ratings_shows_file,
    )

    # Pre-load ratings so they get injected during extraction
    parser.load_ratings()

    # Movies -- local detection OR candidates for TMDB classification
    anime_movies_local, movie_candidates, movie_skips = parser.extract_anime_movies()
    anime_movies.extend(anime_movies_local)
    all_skips.extend(movie_skips)

    # Shows -- extract season data (TMDB classification comes in Phase 2)
    show_seasons, show_parse_skips = parser.extract_show_seasons()
    all_skips.extend(show_parse_skips)

    # Watchlist -- raw items for Phase 2B processing
    watchlist_items, watchlist_skips = parser.extract_watchlist()
    all_skips.extend(watchlist_skips)

    logger.info(
        "Phase 1 complete: %d anime movies (local), %d movie candidates (TMDB), "
        "%d show seasons, %d watchlist items",
        len(anime_movies_local), len(movie_candidates),
        len(show_seasons), len(watchlist_items),
    )

    # ==================================================================
    # PHASE 2: Classify shows via TMDB API
    # ==================================================================
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

    # ── 2-PRE: Classify movie candidates via TMDB (if export lacked genres) ──
    if movie_candidates:
        logger.info("Classifying %d movie candidates via TMDB...", len(movie_candidates))
        movie_tmdb_ids = [m.tmdb_id for m in movie_candidates if m.tmdb_id]
        movie_results = tmdb_client.classify_movies_batch(movie_tmdb_ids)

        for candidate in movie_candidates:
            if candidate.tmdb_id and movie_results.get(candidate.tmdb_id, False):
                candidate.detection_reason = "TMDB Animation genre + Japanese language"
                anime_movies.append(candidate)

        logger.info(
            "TMDB movie classification: %d anime movies found out of %d candidates",
            len(anime_movies) - len(anime_movies_local), len(movie_candidates),
        )

    # Collect ALL unique TMDB IDs (from watched shows + watchlist shows)
    unique_tmdb_ids = set()
    for s in show_seasons:
        if s.show_tmdb_id:
            unique_tmdb_ids.add(s.show_tmdb_id)
    for item in watchlist_items:
        if item.get("type") == "show":
            tmdb_id = item.get("show", {}).get("ids", {}).get("tmdb")
            if tmdb_id:
                unique_tmdb_ids.add(tmdb_id)

    unique_tmdb_ids_list = list(unique_tmdb_ids)
    logger.info("Fetching metadata for %d unique shows from TMDB", len(unique_tmdb_ids_list))
    tmdb_metadata = tmdb_client.get_show_metadata_batch(unique_tmdb_ids_list)

    # ── 2A: Process watched show seasons ──────────────────────────────
    # Track which trakt show IDs are already in watched data
    watched_show_trakt_ids: set[int] = set()

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
            continue  # Not anime -- silently skip

        watched_show_trakt_ids.add(season_data.show_trakt_id)

        # Determine if this season is fully completed
        season_ep_count = metadata.season_episode_counts.get(
            season_data.season_number, 0
        )
        is_complete = (
            season_ep_count > 0
            and season_data.episodes_watched >= season_ep_count
        )

        # Determine status: Completed or Watching
        if is_complete:
            status = "Completed"
        else:
            status = "Watching"

        finish_date = None
        if is_complete and season_data.last_watched_at and len(season_data.last_watched_at) >= 10:
            finish_date = season_data.last_watched_at[:10]

        # Inject show-level rating from Trakt (applies to all seasons)
        score = parser.get_show_rating(season_data.show_trakt_id)

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
            status=status,
            score=score,
            detection_reason=(
                f"TMDB genres {metadata.genres} + language '{metadata.original_language}' "
                f"-> Animation + Japanese"
            ),
        )
        anime_shows.append(entry)

    completed_shows = sum(1 for e in anime_shows if e.status == "Completed")
    watching_shows = sum(1 for e in anime_shows if e.status == "Watching")
    logger.info(
        "Phase 2A complete: %d anime show seasons (%d Completed, %d Watching)",
        len(anime_shows), completed_shows, watching_shows,
    )

    # ── 2B: Process watchlist items -> Plan to Watch ──────────────────
    logger.info("Processing watchlist for anime items...")

    for item in watchlist_items:
        item_type = item.get("type")

        if item_type == "show":
            show = item.get("show", {})
            trakt_id = show.get("ids", {}).get("trakt")
            tmdb_id = show.get("ids", {}).get("tmdb")

            # Skip if already in watched data
            if trakt_id and trakt_id in watched_show_trakt_ids:
                continue

            # Check if it's anime via TMDB
            if tmdb_id:
                metadata = tmdb_metadata.get(tmdb_id)
                if metadata is None:
                    # Wasn't fetched yet (no watched data for this show)
                    metadata = tmdb_client.get_show_metadata(tmdb_id)

                if metadata and metadata.is_anime:
                    entry = AnimeEntry(
                        trakt_id=trakt_id or 0,
                        trakt_type="show",
                        title=show.get("title", "(untitled)"),
                        year=show.get("year"),
                        tmdb_id=tmdb_id,
                        imdb_id=show.get("ids", {}).get("imdb"),
                        tvdb_id=show.get("ids", {}).get("tvdb"),
                        season_number=1,
                        episodes_watched=0,
                        total_episodes=show.get("aired_episodes"),
                        status="Plan to Watch",
                        score=0,
                        detection_reason="Watchlist item classified as anime via TMDB",
                    )
                    anime_watchlist.append(entry)

        elif item_type == "movie":
            movie = item.get("movie", {})
            trakt_id = movie.get("ids", {}).get("trakt")

            # For movies in watchlist, we can't check genres (no genre field).
            # We'll still add them and let the MAL resolver try to match them.
            # The TMDB movie endpoint would be needed for classification.
            # For now, skip movies in watchlist (they need a separate TMDB movie call).
            # TODO: Add TMDB movie classification for watchlist movies
            continue

    logger.info(
        "Phase 2B complete: %d anime watchlist items (Plan to Watch)",
        len(anime_watchlist),
    )

    # ==================================================================
    # PHASE 3: Resolve MAL IDs
    # ==================================================================
    logger.info("=" * 50)
    logger.info("PHASE 3: Resolving MAL IDs")
    logger.info("=" * 50)

    all_anime = anime_movies + anime_shows + anime_watchlist

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

    # ==================================================================
    # PHASE 4: Generate outputs
    # ==================================================================
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
        anime_watchlist=anime_watchlist,
        skips=all_skips,
        resolved_count=resolved_count,
        unresolved_count=unresolved_count,
    )

    logger.info("All outputs written to: %s", config.output_dir)
    logger.info("Done!")


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
  python -m src.main --cli --data-dir "C:/path/to/Trakt data"
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
        "--data-dir",
        type=str,
        default=None,
        help="Path to Trakt data folder (overrides .env)",
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

    # Override data dir if provided via CLI
    if args.data_dir:
        config._data_dir_override = Path(args.data_dir)

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
