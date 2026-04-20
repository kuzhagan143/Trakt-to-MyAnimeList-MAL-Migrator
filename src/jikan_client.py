"""
JikanClient — REST client for the Jikan API (unofficial MAL API wrapper).

Used as Tier 3 (last resort) fallback in MAL ID resolution when both
the offline database and AniList fail to find a match.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx
from .rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

JIKAN_BASE_URL = "https://api.jikan.moe/v4"


class JikanClient:
    """Searches MAL via the Jikan API as a last-resort fallback."""

    def __init__(
        self,
        rate_limiter: RateLimiter,
        timeout: int = 15,
        max_retries: int = 3,
    ):
        self.rate_limiter = rate_limiter
        self.timeout = timeout
        self.max_retries = max_retries
        
        limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
        self._http_client = httpx.Client(timeout=self.timeout, limits=limits)

    def search_anime(
        self,
        title: str,
        anime_type: Optional[str] = None,  # "tv", "movie", "ova", "ona", "special"
    ) -> Optional[list[dict]]:
        """
        Search Jikan for anime matching the title.

        Returns a list of results, each containing:
          - mal_id
          - title, title_english, title_japanese
          - type (TV, Movie, OVA, etc.)
          - episodes
          - status
          - year
        """
        params: dict = {"q": title, "limit": 10}
        if anime_type:
            params["type"] = anime_type

        for attempt in range(1, self.max_retries + 1):
            try:
                self.rate_limiter.sync_acquire()

                response = self._http_client.get(
                    f"{JIKAN_BASE_URL}/anime",
                    params=params,
                )

                if response.status_code == 200:
                    data = response.json()
                    return data.get("data", [])

                elif response.status_code == 429:
                    wait_time = 2 ** attempt
                    logger.warning(
                        "[Jikan] Rate limited, waiting %ds (attempt %d/%d)",
                        wait_time, attempt, self.max_retries,
                    )
                    time.sleep(wait_time)
                    continue

                else:
                    logger.warning(
                        "[Jikan] HTTP %d for '%s' (attempt %d/%d)",
                        response.status_code, title, attempt, self.max_retries,
                    )

            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                wait_time = 2 ** attempt
                logger.warning(
                    "[Jikan] %s for '%s', retrying in %ds (attempt %d/%d)",
                    type(exc).__name__, title, wait_time, attempt, self.max_retries,
                )
                time.sleep(wait_time)

        logger.error("[Jikan] Failed to search '%s' after %d attempts", title, self.max_retries)
        return None
