"""
utils/scrape_cache.py
TTL cache + single-flight wrapper around TwitterChecker.scrape_all_replies.

Problem this solves
-------------------
When a campaign goes viral, dozens of users run /verify on the same tweet
within minutes. Without caching, that's one full Playwright scrape per user —
slow, and they all queue behind the checker's single lock.

This cache scrapes each tweet at most once per TTL window and serves every
subsequent /verify from memory. It also de-duplicates *concurrent* requests:
if 10 users hit the same uncached tweet simultaneously, only ONE scrape runs
and all 10 await its result (the "single-flight" pattern).

Usage
-----
cache = ScrapeCache(checker, ttl_seconds=300)
replies = await cache.get_replies(tweet_url, scroll_steps=15)
handle_found = cache.contains_handle(replies, "alice")
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from utils.twitter_checker import MatchedReply, TwitterChecker

log = logging.getLogger(__name__)


@dataclass
class _CacheEntry:
    replies: list[MatchedReply]
    expires_at: float                          # monotonic seconds
    handles: set[str] = field(default_factory=set)


class ScrapeCache:
    """In-memory TTL cache with single-flight de-duplication.

    Thread-safety: all access goes through asyncio primitives. There's one
    asyncio.Lock guarding the cache dict, plus one asyncio.Event per in-flight
    scrape so concurrent callers wait instead of launching duplicate scrapes.
    """

    def __init__(self, checker: TwitterChecker, ttl_seconds: int = 300) -> None:
        self._checker = checker
        self._ttl = ttl_seconds
        self._cache: dict[str, _CacheEntry] = {}
        self._inflight: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    async def get_replies(
        self,
        tweet_url: str,
        scroll_steps: int = 15,
        scroll_delay_ms: int = 1200,
    ) -> list[MatchedReply]:
        """Return all replies for a tweet, scraping only if not cached/fresh."""
        now = time.monotonic()

        # ── Fast path: fresh cache hit ────────────────────────────────────────
        async with self._lock:
            entry = self._cache.get(tweet_url)
            if entry and entry.expires_at > now:
                log.info("Cache HIT for %s (%d replies)", tweet_url, len(entry.replies))
                return entry.replies

            # Is someone already scraping this URL? Wait on their result.
            event = self._inflight.get(tweet_url)
            if event is None:
                # We're the leader — mark in-flight and scrape outside the lock.
                event = asyncio.Event()
                self._inflight[tweet_url] = event
                is_leader = True
            else:
                is_leader = False

        if not is_leader:
            # ── Follower: wait for the leader's scrape to finish ──────────────
            log.info("Cache MISS for %s — waiting on in-flight scrape", tweet_url)
            await event.wait()
            async with self._lock:
                entry = self._cache.get(tweet_url)
            if entry is not None:
                return entry.replies
            # Leader failed; fall through to scrape ourselves as a fallback.
            log.warning("In-flight scrape produced no entry — scraping directly.")

        # ── Leader: perform the actual scrape ─────────────────────────────────
        try:
            log.info("Cache MISS for %s — scraping now", tweet_url)
            replies = await self._checker.scrape_all_replies(
                tweet_url, scroll_steps=scroll_steps, scroll_delay_ms=scroll_delay_ms
            )
            entry = _CacheEntry(
                replies=replies,
                expires_at=time.monotonic() + self._ttl,
                handles={r.twitter_handle for r in replies},
            )
            async with self._lock:
                self._cache[tweet_url] = entry
            return replies
        finally:
            # Wake any followers and clear the in-flight marker
            async with self._lock:
                ev = self._inflight.pop(tweet_url, None)
            if ev is not None:
                ev.set()

    def find_handle(
        self, replies: list[MatchedReply], handle: str
    ) -> MatchedReply | None:
        """Return the MatchedReply for *handle* (normalized) or None."""
        target = handle.lstrip("@").lower().strip()
        for reply in replies:
            if reply.twitter_handle == target:
                return reply
        return None

    def invalidate(self, tweet_url: str) -> None:
        """Drop a tweet from the cache (e.g. admin forces a re-check)."""
        self._cache.pop(tweet_url, None)

    def stats(self) -> dict:
        """Lightweight introspection for a future /stats command."""
        now = time.monotonic()
        fresh = sum(1 for e in self._cache.values() if e.expires_at > now)
        return {
            "cached_tweets": len(self._cache),
            "fresh_tweets": fresh,
            "in_flight": len(self._inflight),
            "ttl_seconds": self._ttl,
        }
