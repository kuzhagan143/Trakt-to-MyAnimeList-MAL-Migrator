"""
Token-bucket rate limiter with automatic retry and exponential backoff.
Shared across all API client classes.
"""

from __future__ import annotations

import asyncio
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Token-bucket rate limiter for API calls.

    Usage:
        limiter = RateLimiter(max_requests=40, window_seconds=10, name="TMDB")
        await limiter.acquire()   # blocks until a token is available
    """

    def __init__(
        self,
        max_requests: int,
        window_seconds: float,
        name: str = "API",
    ):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.name = name

        # Minimum gap between requests to spread them evenly
        self.min_interval = window_seconds / max_requests

        self._timestamps: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until a request slot is available, then consume it."""
        async with self._lock:
            now = time.monotonic()

            # Purge timestamps outside the current window
            cutoff = now - self.window_seconds
            self._timestamps = [t for t in self._timestamps if t > cutoff]

            if len(self._timestamps) >= self.max_requests:
                # Must wait until the oldest timestamp exits the window
                wait_until = self._timestamps[0] + self.window_seconds
                sleep_time = wait_until - now
                if sleep_time > 0:
                    logger.debug(
                        "[%s] Rate limit reached, sleeping %.2fs", self.name, sleep_time
                    )
                    await asyncio.sleep(sleep_time)

            # Enforce min interval since last request
            if self._timestamps:
                elapsed = time.monotonic() - self._timestamps[-1]
                if elapsed < self.min_interval:
                    await asyncio.sleep(self.min_interval - elapsed)

            self._timestamps.append(time.monotonic())

    def sync_acquire(self) -> None:
        """Synchronous version — blocks the calling thread."""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        self._timestamps = [t for t in self._timestamps if t > cutoff]

        if len(self._timestamps) >= self.max_requests:
            wait_until = self._timestamps[0] + self.window_seconds
            sleep_time = wait_until - now
            if sleep_time > 0:
                logger.debug(
                    "[%s] Rate limit reached, sleeping %.2fs", self.name, sleep_time
                )
                time.sleep(sleep_time)

        if self._timestamps:
            elapsed = time.monotonic() - self._timestamps[-1]
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)

        self._timestamps.append(time.monotonic())
