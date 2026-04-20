"""
Configuration loader — reads .env file and provides typed settings.
"""

import os
from pathlib import Path
from dotenv import load_dotenv


class Config:
    """Centralized configuration loaded from environment variables / .env file."""

    def __init__(self, project_root: Path | None = None):
        self.project_root = project_root or Path(__file__).resolve().parent.parent
        self._load_env()

    def _load_env(self) -> None:
        """Load .env file from project root if it exists."""
        env_path = self.project_root / ".env"
        if env_path.exists():
            load_dotenv(env_path)
        else:
            # Try .env.example as a fallback for defaults
            example_path = self.project_root / ".env.example"
            if example_path.exists():
                load_dotenv(example_path)

    # ── API Keys ──────────────────────────────────────────────────────────

    @property
    def tmdb_api_key(self) -> str:
        key = os.getenv("TMDB_API_KEY", "")
        if not key or key == "your_tmdb_api_key_here":
            raise ValueError(
                "TMDB_API_KEY is not set. "
                "Copy .env.example to .env and add your TMDB API key."
            )
        return key

    # ── Input Files ───────────────────────────────────────────────────────

    @property
    def trakt_movies_file(self) -> Path:
        if hasattr(self, '_movies_override') and self._movies_override:
            return self._movies_override
        return self.project_root / os.getenv("TRAKT_MOVIES_FILE", "watched-movies.json")

    @property
    def trakt_shows_file(self) -> Path:
        if hasattr(self, '_shows_override') and self._shows_override:
            return self._shows_override
        return self.project_root / os.getenv("TRAKT_SHOWS_FILE", "watched-shows.json")

    # ── Output Directory ──────────────────────────────────────────────────

    @property
    def output_dir(self) -> Path:
        path = self.project_root / os.getenv("OUTPUT_DIR", "output")
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ── Cache Directory ───────────────────────────────────────────────────

    @property
    def cache_dir(self) -> Path:
        path = self.project_root / os.getenv("CACHE_DIR", ".cache")
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ── Rate Limits ───────────────────────────────────────────────────────

    @property
    def tmdb_rate_limit(self) -> int:
        return int(os.getenv("TMDB_RATE_LIMIT", "40"))

    @property
    def tmdb_rate_window(self) -> int:
        return int(os.getenv("TMDB_RATE_WINDOW", "10"))

    @property
    def anilist_rate_limit(self) -> int:
        return int(os.getenv("ANILIST_RATE_LIMIT", "80"))

    @property
    def anilist_rate_window(self) -> int:
        return int(os.getenv("ANILIST_RATE_WINDOW", "60"))

    @property
    def jikan_rate_limit(self) -> int:
        return int(os.getenv("JIKAN_RATE_LIMIT", "2"))

    @property
    def jikan_rate_window(self) -> int:
        return int(os.getenv("JIKAN_RATE_WINDOW", "1"))

    # ── Request Settings ──────────────────────────────────────────────────

    @property
    def request_timeout(self) -> int:
        return int(os.getenv("REQUEST_TIMEOUT", "15"))

    @property
    def max_retries(self) -> int:
        return int(os.getenv("MAX_RETRIES", "3"))
