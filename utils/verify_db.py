"""
utils/verify_db.py
Async SQLAlchemy service for Discord ↔ X / Twitter account linking.

All public methods are coroutines — call them with `await`.

Responsibilities
----------------
* Store and update the mapping between a Discord user_id and their X handle.
* Provide the linked handle for the /verify command to use.
* Record which tasks a user has verified via Twitter (delegates actual point
  award to TasksService.complete_task so the audit log stays unified).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from utils.models import Base, DiscordXLink, User

log = logging.getLogger(__name__)


# ── Public result type ────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class LinkedAccount:
    discord_user_id: int
    twitter_handle: str        # normalized (no @, lowercase)
    linked_at: datetime
    updated_at: datetime

    @property
    def display_handle(self) -> str:
        """Handle with the @ prefix for display."""
        return f"@{self.twitter_handle}"


# ── Service ───────────────────────────────────────────────────────────────────

class VerifyService:
    """All DB operations for account linking and verification gating.

    Usage
    -----
    svc = await VerifyService.create(db_path)
    await svc.link_account(user_id=123, discord_username="Alice", twitter_handle="alice_x")
    link = await svc.get_link(user_id=123)  # → LinkedAccount | None
    """

    def __init__(self, engine: AsyncEngine, session_factory: async_sessionmaker) -> None:
        self._engine = engine
        self._Session = session_factory

    @classmethod
    async def create(cls, db_path: Path) -> "VerifyService":
        url = f"sqlite+aiosqlite:///{db_path}"
        engine = create_async_engine(url, echo=False)

        from sqlalchemy import event

        @event.listens_for(engine.sync_engine, "connect")
        def _pragmas(dbapi_conn, _):
            dbapi_conn.execute("PRAGMA journal_mode=WAL")
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )
        log.info("VerifyService ready — database: %s", db_path)
        return cls(engine, session_factory)

    async def close(self) -> None:
        await self._engine.dispose()
        log.info("VerifyService database connection closed.")

    # ── Account linking ───────────────────────────────────────────────────────

    async def link_account(
        self,
        discord_user_id: int,
        discord_username: str,
        twitter_handle: str,
    ) -> LinkedAccount:
        """Create or update the Discord ↔ X link for a user.

        If the user has never been seen before, a `users` row is created first
        to satisfy the FK constraint. Subsequent calls update the handle in-place.
        """
        normalized = DiscordXLink.normalize(twitter_handle)

        async with self._Session() as session:
            async with session.begin():
                # Ensure the users row exists
                user = await session.get(User, discord_user_id)
                if user is None:
                    session.add(User(user_id=discord_user_id, username=discord_username))
                    await session.flush()

                # Upsert the link row
                existing = await session.get(DiscordXLink, discord_user_id)
                now = datetime.now(timezone.utc)

                if existing is None:
                    link = DiscordXLink(
                        user_id=discord_user_id,
                        twitter_handle=normalized,
                        linked_at=now,
                        updated_at=now,
                    )
                    session.add(link)
                else:
                    existing.twitter_handle = normalized
                    existing.updated_at = now
                    link = existing

                await session.flush()

        log.info(
            "Linked Discord user %d (%s) → @%s",
            discord_user_id, discord_username, normalized,
        )
        # Re-fetch to get a clean snapshot outside the session
        return await self._fetch_link(discord_user_id)  # type: ignore[return-value]

    async def unlink_account(self, discord_user_id: int) -> bool:
        """Remove the link. Returns True if a row was deleted, False if none existed."""
        async with self._Session() as session:
            async with session.begin():
                result = await session.execute(
                    delete(DiscordXLink).where(
                        DiscordXLink.user_id == discord_user_id
                    )
                )
        deleted = result.rowcount > 0
        if deleted:
            log.info("Unlinked Discord user %d", discord_user_id)
        return deleted

    async def get_link(self, discord_user_id: int) -> LinkedAccount | None:
        """Return the linked X account for a Discord user, or None."""
        return await self._fetch_link(discord_user_id)

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _fetch_link(self, discord_user_id: int) -> LinkedAccount | None:
        async with self._Session() as session:
            row = await session.get(DiscordXLink, discord_user_id)
            if row is None:
                return None
            return LinkedAccount(
                discord_user_id=row.user_id,
                twitter_handle=row.twitter_handle,
                linked_at=row.linked_at,
                updated_at=row.updated_at,
            )
