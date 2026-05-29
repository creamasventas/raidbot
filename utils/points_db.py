"""
utils/points_db.py
Async SQLAlchemy service for the points & leaderboard system.

All public methods are coroutines — call them with `await`.

Design notes
------------
* Uses SQLAlchemy 2.x async API (AsyncEngine + AsyncSession).
* aiosqlite is the underlying async SQLite driver (no extra process needed).
* All writes go through explicit transactions so partial failures roll back.
* The audit-log design (PointTransaction rows) means you can always
  reconstruct history and never lose data by accident.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from utils.models import Base, PointTransaction, User

log = logging.getLogger(__name__)


# ── Public result types ───────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class PointResult:
    """Returned by add_points / remove_points."""
    user_id: int
    username: str
    delta: int          # what changed this call (+/-)
    total: int          # new running total
    reason: str | None


@dataclass(frozen=True, slots=True)
class LeaderboardEntry:
    rank: int
    user_id: int
    username: str
    total_points: int


# ── Service class ─────────────────────────────────────────────────────────────

class PointsService:
    """All database operations for the points system.

    Usage
    -----
    service = await PointsService.create("data/bot.db")
    await service.add_points(user_id=123, username="Alice", delta=50)
    board = await service.get_leaderboard(limit=10)
    await service.close()
    """

    def __init__(self, engine: AsyncEngine, session_factory: async_sessionmaker) -> None:
        self._engine = engine
        self._Session = session_factory

    # ── Constructor ───────────────────────────────────────────────────────────

    @classmethod
    async def create(cls, db_path: Path) -> "PointsService":
        """Create the engine, run migrations, return a ready service."""
        # aiosqlite:///path  →  async SQLite via aiosqlite driver
        url = f"sqlite+aiosqlite:///{db_path}"
        engine = create_async_engine(
            url,
            echo=False,          # set True to log every SQL statement
            connect_args={
                "check_same_thread": False,    # required for SQLite
            },
        )

        # Enable WAL mode for better concurrent read performance
        from sqlalchemy import event, text

        @event.listens_for(engine.sync_engine, "connect")
        def set_wal(dbapi_conn, _):
            dbapi_conn.execute("PRAGMA journal_mode=WAL")
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

        # Create tables that don't exist yet (idempotent)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        log.info("PointsService ready — database: %s", db_path)

        session_factory = async_sessionmaker(
            engine,
            expire_on_commit=False,   # keep attributes accessible after commit
            class_=AsyncSession,
        )
        return cls(engine, session_factory)

    async def close(self) -> None:
        await self._engine.dispose()
        log.info("PointsService database connection closed.")

    # ── User helpers ──────────────────────────────────────────────────────────

    async def _get_or_create_user(
        self, session: AsyncSession, user_id: int, username: str
    ) -> User:
        """Return existing User row or insert a new one (within a session)."""
        result = await session.execute(select(User).where(User.user_id == user_id))
        user = result.scalar_one_or_none()

        if user is None:
            user = User(user_id=user_id, username=username)
            session.add(user)
            await session.flush()   # get the PK without committing
            log.debug("Created new user row: %s (%d)", username, user_id)
        elif user.username != username:
            # Keep the stored display name fresh
            user.username = username

        return user

    # ── Points operations ─────────────────────────────────────────────────────

    async def add_points(
        self,
        user_id: int,
        username: str,
        delta: int,
        reason: str | None = None,
        granted_by: int | None = None,
    ) -> PointResult:
        """Add *delta* points to a user (use a negative delta to subtract).

        Always appends a PointTransaction row so the history is complete.
        Returns a PointResult with the new running total.
        """
        if delta == 0:
            raise ValueError("delta must not be zero.")

        async with self._Session() as session:
            async with session.begin():
                user = await self._get_or_create_user(session, user_id, username)

                txn = PointTransaction(
                    user_id=user_id,
                    delta=delta,
                    reason=reason,
                    granted_by=granted_by,
                    created_at=datetime.now(timezone.utc),
                )
                session.add(txn)

            # Compute the new total AFTER the commit so it includes this txn
            total = await self._sum_points(session, user_id)

        action = "Added" if delta > 0 else "Removed"
        log.info("%s %+d points to user %d (%s). Total: %d", action, delta, user_id, username, total)
        return PointResult(user_id=user_id, username=username, delta=delta, total=total, reason=reason)

    async def remove_points(
        self,
        user_id: int,
        username: str,
        amount: int,
        reason: str | None = None,
        granted_by: int | None = None,
    ) -> PointResult:
        """Remove *amount* points (convenience wrapper around add_points)."""
        if amount <= 0:
            raise ValueError("amount must be positive.")
        return await self.add_points(
            user_id=user_id,
            username=username,
            delta=-amount,
            reason=reason,
            granted_by=granted_by,
        )

    async def get_total_points(self, user_id: int) -> int:
        """Return the current running total for a single user."""
        async with self._Session() as session:
            return await self._sum_points(session, user_id)

    async def get_history(
        self, user_id: int, limit: int = 10
    ) -> Sequence[PointTransaction]:
        """Return the most recent point transactions for a user."""
        async with self._Session() as session:
            result = await session.execute(
                select(PointTransaction)
                .where(PointTransaction.user_id == user_id)
                .order_by(PointTransaction.created_at.desc())
                .limit(limit)
            )
            return result.scalars().all()

    # ── Leaderboard ───────────────────────────────────────────────────────────

    async def get_leaderboard(self, limit: int = 10) -> list[LeaderboardEntry]:
        """Return the top *limit* users ranked by total points (highest first).

        Uses a GROUP BY + SUM query so it's a single round-trip to the DB.
        Users with zero or negative total points are excluded from the board.
        """
        async with self._Session() as session:
            # SUM all deltas per user, join to users table for display name
            stmt = (
                select(
                    User.user_id,
                    User.username,
                    func.sum(PointTransaction.delta).label("total_points"),
                )
                .join(PointTransaction, User.user_id == PointTransaction.user_id)
                .group_by(User.user_id)
                .having(func.sum(PointTransaction.delta) > 0)
                .order_by(func.sum(PointTransaction.delta).desc())
                .limit(limit)
            )
            rows = (await session.execute(stmt)).all()

        return [
            LeaderboardEntry(
                rank=i + 1,
                user_id=row.user_id,
                username=row.username,
                total_points=row.total_points,
            )
            for i, row in enumerate(rows)
        ]

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    async def _sum_points(session: AsyncSession, user_id: int) -> int:
        """SUM all point deltas for user_id; returns 0 if no transactions."""
        result = await session.execute(
            select(func.coalesce(func.sum(PointTransaction.delta), 0)).where(
                PointTransaction.user_id == user_id
            )
        )
        return result.scalar_one()
