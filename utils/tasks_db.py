"""
utils/tasks_db.py
Async SQLAlchemy service for the campaign / task system.

All public methods are coroutines — call them with `await`.

Design notes
------------
* Tasks are soft-deletable (is_active flag) so completion history is never lost.
* Expiry is enforced at query time AND at claim time — no background job needed.
* Double-claiming is blocked at the DB level via a UNIQUE constraint on
  (task_id, user_id); the service converts IntegrityError → TaskError cleanly.
* Points are awarded through the existing PointsService so the audit log stays
  unified — one source of truth for all point movements.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from utils.models import Base, Task, TaskCompletion, User
from utils.points_db import PointsService

log = logging.getLogger(__name__)


# ── Domain errors ─────────────────────────────────────────────────────────────

class TaskError(Exception):
    """Raised for expected, user-facing task failures."""


# ── Public result types ───────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class TaskResult:
    """Returned after a successful task completion."""
    task_id: int
    task_title: str
    reward_points: int
    new_total: int          # user's total points after the reward


@dataclass(frozen=True, slots=True)
class TaskRow:
    """Read-only snapshot of a task for display purposes."""
    id: int
    title: str
    description: str | None
    url: str | None
    reward_points: int
    expires_at: datetime | None
    is_active: bool
    created_by: int
    created_at: datetime
    completion_count: int   # how many users have completed this task

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) >= self.expires_at

    @property
    def is_claimable(self) -> bool:
        return self.is_active and not self.is_expired

    @property
    def status_emoji(self) -> str:
        if not self.is_active:
            return "🔴"
        if self.is_expired:
            return "⏰"
        return "🟢"


# ── Expiry string parser ──────────────────────────────────────────────────────

_DURATION_RE = re.compile(
    r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?$", re.IGNORECASE
)

def parse_duration(raw: str) -> timedelta | None:
    """Parse a human-readable duration like '7d', '24h', '1d12h', '30m'.

    Returns None if *raw* is 'never' / '' / '0'.
    Raises ValueError for unrecognised formats.
    """
    raw = raw.strip().lower()
    if raw in ("never", "none", "0", ""):
        return None

    m = _DURATION_RE.match(raw)
    if not m or not any(m.groups()):
        raise ValueError(
            f"Can't parse duration {raw!r}. "
            "Use formats like: 7d, 24h, 1d12h, 30m, or 'never'."
        )

    days = int(m.group(1) or 0)
    hours = int(m.group(2) or 0)
    minutes = int(m.group(3) or 0)
    total = timedelta(days=days, hours=hours, minutes=minutes)

    if total.total_seconds() <= 0:
        raise ValueError("Duration must be greater than zero.")

    return total


# ── Service class ─────────────────────────────────────────────────────────────

class TasksService:
    """All database operations for the task / campaign system.

    This service shares the same SQLite file as PointsService; both use the
    same Base metadata so `create_all` is idempotent across restarts.

    Usage
    -----
    svc = await TasksService.create(db_path, points_service)
    task = await svc.create_task(title="RT our tweet", url="https://…",
                                  reward_points=50, expires_in=timedelta(days=7),
                                  created_by=admin_user_id)
    result = await svc.complete_task(task_id=task.id,
                                      user_id=member_id, username="Alice")
    await svc.close()
    """

    def __init__(
        self,
        engine: AsyncEngine,
        session_factory: async_sessionmaker,
        points_svc: PointsService,
    ) -> None:
        self._engine = engine
        self._Session = session_factory
        self._points = points_svc

    # ── Constructor ───────────────────────────────────────────────────────────

    @classmethod
    async def create(
        cls, db_path: Path, points_svc: PointsService
    ) -> "TasksService":
        """Create the engine, run migrations, return a ready service."""
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
        log.info("TasksService ready — database: %s", db_path)
        return cls(engine, session_factory, points_svc)

    async def close(self) -> None:
        await self._engine.dispose()
        log.info("TasksService database connection closed.")

    # ── Task CRUD ─────────────────────────────────────────────────────────────

    async def create_task(
        self,
        *,
        title: str,
        reward_points: int,
        created_by: int,
        description: str | None = None,
        url: str | None = None,
        expires_in: timedelta | None = None,
    ) -> TaskRow:
        """Insert a new task and return its snapshot."""
        if reward_points <= 0:
            raise TaskError("Reward points must be greater than zero.")

        expires_at: datetime | None = None
        if expires_in is not None:
            expires_at = datetime.now(timezone.utc) + expires_in

        async with self._Session() as session:
            async with session.begin():
                task = Task(
                    title=title,
                    description=description,
                    url=url,
                    reward_points=reward_points,
                    expires_at=expires_at,
                    is_active=True,
                    created_by=created_by,
                    created_at=datetime.now(timezone.utc),
                )
                session.add(task)
                await session.flush()   # populate task.id
                task_id = task.id

        log.info(
            "Task #%d created by %d: %r (%d pts, expires %s)",
            task_id, created_by, title, reward_points, expires_at,
        )
        return await self.get_task(task_id)  # type: ignore[return-value]

    async def deactivate_task(self, task_id: int, admin_id: int) -> None:
        """Soft-delete a task so it no longer appears in /tasks."""
        async with self._Session() as session:
            async with session.begin():
                await session.execute(
                    update(Task)
                    .where(Task.id == task_id)
                    .values(is_active=False)
                )
        log.info("Task #%d deactivated by admin %d", task_id, admin_id)

    # ── Queries ───────────────────────────────────────────────────────────────

    async def get_task(self, task_id: int) -> TaskRow | None:
        """Return a single task by ID, or None if not found."""
        async with self._Session() as session:
            row = await session.get(Task, task_id)
            if row is None:
                return None
            count = await self._completion_count(session, task_id)
            return self._to_row(row, count)

    async def get_active_tasks(self) -> list[TaskRow]:
        """Return all tasks that are active and not yet expired, newest first."""
        now = datetime.now(timezone.utc)
        async with self._Session() as session:
            result = await session.execute(
                select(Task)
                .where(Task.is_active.is_(True))
                .where(
                    (Task.expires_at.is_(None)) | (Task.expires_at > now)
                )
                .order_by(Task.created_at.desc())
            )
            tasks = result.scalars().all()

            rows: list[TaskRow] = []
            for t in tasks:
                count = await self._completion_count(session, t.id)
                rows.append(self._to_row(t, count))
            return rows

    async def get_all_tasks(self) -> list[TaskRow]:
        """Return every task (active + inactive + expired) for admin views."""
        async with self._Session() as session:
            result = await session.execute(
                select(Task).order_by(Task.created_at.desc())
            )
            tasks = result.scalars().all()
            rows: list[TaskRow] = []
            for t in tasks:
                count = await self._completion_count(session, t.id)
                rows.append(self._to_row(t, count))
            return rows

    async def has_completed(self, task_id: int, user_id: int) -> bool:
        """Return True if the user already claimed this task."""
        async with self._Session() as session:
            result = await session.execute(
                select(TaskCompletion).where(
                    TaskCompletion.task_id == task_id,
                    TaskCompletion.user_id == user_id,
                )
            )
            return result.scalar_one_or_none() is not None

    # ── Task completion ───────────────────────────────────────────────────────

    async def complete_task(
        self, task_id: int, user_id: int, username: str
    ) -> TaskResult:
        """Mark a task as completed by a user and award its points.

        Raises TaskError for every expected failure (not found, expired,
        already claimed). Let unexpected exceptions bubble up naturally.
        """
        # ── 1. Load and validate the task ────────────────────────────────────
        async with self._Session() as session:
            task = await session.get(Task, task_id)

            if task is None:
                raise TaskError(f"Task #{task_id} doesn't exist.")
            if not task.is_active:
                raise TaskError(f"Task #{task_id} has been deactivated by an admin.")
            if task.is_expired:
                raise TaskError(
                    f"Task #{task_id} expired "
                    f"<t:{int(task.expires_at.timestamp())}:R>."  # type: ignore[union-attr]
                )

            # ── 2. Ensure the user row exists (FK requirement) ────────────────
            existing = await session.get(User, user_id)
            if existing is None:
                async with session.begin():
                    session.add(User(user_id=user_id, username=username))

            # ── 3. Record the completion (UNIQUE constraint is the race guard) ─
            try:
                async with session.begin():
                    session.add(
                        TaskCompletion(
                            task_id=task_id,
                            user_id=user_id,
                            completed_at=datetime.now(timezone.utc),
                        )
                    )
            except IntegrityError:
                raise TaskError(
                    f"You've already completed task #{task_id}."
                )

            reward = task.reward_points
            title = task.title

        # ── 4. Award points (outside the task session to avoid holding locks) ─
        result = await self._points.add_points(
            user_id=user_id,
            username=username,
            delta=reward,
            reason=f'Completed task #{task_id}: "{title}"',
            granted_by=None,
        )

        log.info(
            "User %d (%s) completed task #%d (%r) — awarded %d pts (total %d)",
            user_id, username, task_id, title, reward, result.total,
        )
        return TaskResult(
            task_id=task_id,
            task_title=title,
            reward_points=reward,
            new_total=result.total,
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    async def _completion_count(session: AsyncSession, task_id: int) -> int:
        result = await session.execute(
            select(TaskCompletion).where(TaskCompletion.task_id == task_id)
        )
        return len(result.scalars().all())

    @staticmethod
    def _to_row(task: Task, completion_count: int) -> TaskRow:
        return TaskRow(
            id=task.id,
            title=task.title,
            description=task.description,
            url=task.url,
            reward_points=task.reward_points,
            expires_at=task.expires_at,
            is_active=task.is_active,
            created_by=task.created_by,
            created_at=task.created_at,
            completion_count=completion_count,
        )
