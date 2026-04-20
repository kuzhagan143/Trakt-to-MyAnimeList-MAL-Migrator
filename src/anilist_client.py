"""
AniListClient — GraphQL client for the AniList API.

Used as Tier 2 in MAL ID resolution. AniList returns `idMal` directly,
making it ideal for cross-referencing. No authentication required.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx
from .rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

ANILIST_URL = "https://graphql.anilist.co"

# GraphQL query to search anime by title and get MAL ID
SEARCH_QUERY = """
query ($search: String, $type: MediaType) {
  Page(perPage: 10) {
    media(search: $search, type: $type) {
      id
      idMal
      title {
        romaji
        english
        native
      }
      format
      episodes
      status
      seasonYear
      startDate {
        year
      }
    }
  }
}
"""


class AniListClient:
    """Searches AniList for anime and retrieves their MAL IDs."""

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
        year: Optional[int] = None,
        media_type: str = "ANIME",
    ) -> Optional[list[dict]]:
        """
        Search AniList for anime matching the title.

        Returns a list of media results, each containing:
          - id (AniList ID)
          - idMal (MyAnimeList ID, may be None)
          - title (romaji, english, native)
          - format (TV, MOVIE, OVA, ONA, SPECIAL)
          - episodes
          - seasonYear
        """
        variables = {"search": title, "type": media_type}

        for attempt in range(1, self.max_retries + 1):
            try:
                self.rate_limiter.sync_acquire()

                response = self._http_client.post(
                    ANILIST_URL,
                    json={"query": SEARCH_QUERY, "variables": variables},
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                )

                if response.status_code == 200:
                    data = response.json()
                    media_list = data.get("data", {}).get("Page", {}).get("media", [])
                    return media_list

                elif response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 60))
                    logger.warning(
                        "[AniList] Rate limited, waiting %ds (attempt %d/%d)",
                        retry_after, attempt, self.max_retries,
                    )
                    time.sleep(retry_after)
                    continue

                else:
                    logger.warning(
                        "[AniList] HTTP %d for '%s' (attempt %d/%d)",
                        response.status_code, title, attempt, self.max_retries,
                    )

            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                wait_time = 2 ** attempt
                logger.warning(
                    "[AniList] %s for '%s', retrying in %ds (attempt %d/%d)",
                    type(exc).__name__, title, wait_time, attempt, self.max_retries,
                )
                time.sleep(wait_time)

        logger.error("[AniList] Failed to search '%s' after %d attempts", title, self.max_retries)
        return None
