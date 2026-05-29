"""
utils/models.py
SQLAlchemy ORM models — single source of truth for the database schema.

Tables
------
users            — one row per Discord user (shared with the existing XP system)
points           — immutable audit log of every point transaction
tasks            — admin-created campaigns / tasks with optional expiry
task_completions — one row per (task, user) pair; prevents double-claiming
discord_x_links  — maps Discord user_id → normalized Twitter/X handle
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# ── Base class ────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    """All ORM models inherit from this."""


# ── Users ─────────────────────────────────────────────────────────────────────

class User(Base):
    """One row per Discord user.

    The user_id is the Discord snowflake (globally unique integer).
    We store it as BigInteger because Discord IDs exceed SQLite's default
    INTEGER affinity limit for some ORMs — explicit is safer.
    """

    __tablename__ = "users"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(String(100), nullable=False)

    # XP system columns (owned by the existing aiosqlite layer — SQLAlchemy
    # mirrors them here so we can JOIN without raw SQL, but writes go through
    # the original Database class to avoid conflicts).
    xp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    messages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    joined_at: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        server_default=func.datetime("now"),
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    point_transactions: Mapped[list[PointTransaction]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="raise",
    )
    task_completions: Mapped[list[TaskCompletion]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="raise",
    )
    x_link: Mapped[DiscordXLink | None] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="raise",
        uselist=False,
    )

    # ── Helpers ───────────────────────────────────────────────────────────────
    @property
    def total_points(self) -> int:
        """Computed via DB query in PointsService — don't use this property."""
        raise NotImplementedError("Use PointsService.get_total_points() instead.")

    def __repr__(self) -> str:
        return f"<User user_id={self.user_id} username={self.username!r}>"


# ── Points audit log ──────────────────────────────────────────────────────────

class PointTransaction(Base):
    """Every point change is recorded here as an immutable event.

    This gives you a full history:  who gave / removed how many points,
    when, and why (reason field).  The running total is always computable
    as SUM(delta) over all rows for a given user_id.
    """

    __tablename__ = "points"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Positive = points added, negative = points removed.
    delta: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    granted_by: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="Discord user_id of the mod who made the change"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    user: Mapped[User] = relationship(back_populates="point_transactions", lazy="raise")

    def __repr__(self) -> str:
        sign = "+" if self.delta >= 0 else ""
        return (
            f"<PointTransaction id={self.id} user_id={self.user_id} "
            f"delta={sign}{self.delta}>"
        )


# ── Tasks (campaigns) ─────────────────────────────────────────────────────────

class Task(Base):
    """An admin-created task / campaign that members can complete for points.

    Fields
    ------
    title           Short display name shown in /tasks list.
    description     Optional longer explanation of what to do.
    url             Optional link (e.g. tweet to retweet, form to fill).
    reward_points   How many points completing this task awards.
    expires_at      UTC datetime after which the task can no longer be claimed.
                    NULL means the task never expires on its own.
    is_active       Admins can soft-delete a task without losing completion history.
    created_by      Discord snowflake of the admin who created the task.
    created_at      UTC timestamp of creation.
    """

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    reward_points: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    completions: Mapped[list[TaskCompletion]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        lazy="raise",
    )

    # ── Computed helpers ──────────────────────────────────────────────────────
    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) >= self.expires_at

    @property
    def is_claimable(self) -> bool:
        return self.is_active and not self.is_expired

    def __repr__(self) -> str:
        return f"<Task id={self.id} title={self.title!r} reward={self.reward_points}>"


# ── Task completions ──────────────────────────────────────────────────────────

class TaskCompletion(Base):
    """One row per (task, user) pair — the DB-level double-claim guard.

    The UNIQUE constraint on (task_id, user_id) means that even if two
    concurrent requests race to claim the same task, only one will commit;
    the other gets an IntegrityError that the service layer catches and
    converts into a clean UserFacingError.
    """

    __tablename__ = "task_completions"
    __table_args__ = (
        UniqueConstraint("task_id", "user_id", name="uq_task_completion"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    task: Mapped[Task] = relationship(back_populates="completions", lazy="raise")
    user: Mapped[User] = relationship(back_populates="task_completions", lazy="raise")

    def __repr__(self) -> str:
        return f"<TaskCompletion task_id={self.task_id} user_id={self.user_id}>"


# ── Discord ↔ X / Twitter account links ──────────────────────────────────────

class DiscordXLink(Base):
    """Stores the mapping between a Discord user and their X (Twitter) handle.

    One row per Discord user — updated in-place when they change their handle.
    The twitter_handle is stored normalized (lowercase, no leading @) so
    comparisons against scraped usernames are always case-insensitive.
    """

    __tablename__ = "discord_x_links"

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    # Stored normalized: lowercase, no @ prefix.
    twitter_handle: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    user: Mapped[User] = relationship(back_populates="x_link", lazy="raise")

    @staticmethod
    def normalize(handle: str) -> str:
        """Strip leading @ and lowercase — canonical form for all comparisons."""
        return handle.lstrip("@").lower().strip()

    def __repr__(self) -> str:
        return f"<DiscordXLink user_id={self.user_id} handle=@{self.twitter_handle}>"
