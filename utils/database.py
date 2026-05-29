"""
utils/database.py
Thin async wrapper around aiosqlite.
Handles connection lifecycle and schema migrations.
"""

from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite

log = logging.getLogger(__name__)


class Database:
    """Single shared database connection used by all cogs."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Open the connection and run first-time migrations."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row   # rows behave like dicts
        await self._db.execute("PRAGMA journal_mode=WAL")  # better concurrency
        await self._migrate()
        log.info("Database connected: %s", self.path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            log.info("Database connection closed.")

    # ── Migrations ────────────────────────────────────────────────────────────

    async def _migrate(self) -> None:
        """Create tables if they don't exist yet.
        Add new ALTER TABLE statements here as your schema evolves."""
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT    NOT NULL,
                xp          INTEGER NOT NULL DEFAULT 0,
                level       INTEGER NOT NULL DEFAULT 1,
                messages    INTEGER NOT NULL DEFAULT 0,
                joined_at   TEXT    NOT NULL DEFAULT (datetime('now'))
            );
        """)
        await self._db.commit()
        log.debug("Database migrations applied.")

    # ── Convenience helpers ───────────────────────────────────────────────────

    async def get_or_create_user(
        self, user_id: int, username: str
    ) -> aiosqlite.Row:
        """Fetch a user row, creating it with defaults if it doesn't exist."""
        async with self._db.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            await self._db.execute(
                "INSERT INTO users (user_id, username) VALUES (?, ?)",
                (user_id, username),
            )
            await self._db.commit()
            async with self._db.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ) as cursor:
                row = await cursor.fetchone()

        return row

    async def add_xp(self, user_id: int, amount: int) -> dict:
        """Add XP to a user and handle level-ups. Returns updated stats."""
        async with self._db.execute(
            "SELECT xp, level FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            return {}

        new_xp = row["xp"] + amount
        new_level = self._xp_to_level(new_xp)
        leveled_up = new_level > row["level"]

        await self._db.execute(
            """
            UPDATE users
               SET xp = ?, level = ?, messages = messages + 1
             WHERE user_id = ?
            """,
            (new_xp, new_level, user_id),
        )
        await self._db.commit()
        return {"xp": new_xp, "level": new_level, "leveled_up": leveled_up}

    async def get_leaderboard(self, limit: int = 10) -> list[aiosqlite.Row]:
        async with self._db.execute(
            "SELECT * FROM users ORDER BY xp DESC LIMIT ?", (limit,)
        ) as cursor:
            return await cursor.fetchall()

    # ── Static helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _xp_to_level(xp: int) -> int:
        """Simple levelling curve: each level needs 100 * level XP."""
        level = 1
        while xp >= 100 * level:
            xp -= 100 * level
            level += 1
        return level

    @staticmethod
    def xp_for_next_level(level: int) -> int:
        return 100 * level
