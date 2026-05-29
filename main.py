"""
main.py
Entry point for the Discord bot.

Run locally:
    python main.py

Run with Docker:
    docker compose up
"""

from __future__ import annotations

import asyncio
import logging
import sys

import discord
from discord.ext import commands

from config import settings
from utils.database import Database
from utils.points_db import PointsService
from utils.tasks_db import TasksService
from utils.verify_db import VerifyService
from utils.twitter_checker import TwitterChecker

# ── Logging setup ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# Silence noisy discord.py gateway logs unless we're in DEBUG
if settings.log_level != "DEBUG":
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    logging.getLogger("discord.client").setLevel(logging.WARNING)


# ── Bot class ─────────────────────────────────────────────────────────────────

class MyBot(commands.Bot):
    """Custom Bot subclass — holds shared resources (db, etc.)."""

    def __init__(
        self,
        db: Database,
        points_svc: PointsService,
        tasks_svc: TasksService,
        verify_svc: VerifyService,
        checker: TwitterChecker | None,
    ) -> None:
        self.db         = db
        self.points_svc = points_svc
        self.tasks_svc  = tasks_svc
        self.verify_svc = verify_svc
        self.checker    = checker

        intents = discord.Intents.default()
        intents.message_content = True   # needed for XP on_message listener
        intents.members = True           # needed to resolve member names

        super().__init__(
            command_prefix="!",          # prefix commands (optional, slash is primary)
            intents=intents,
            help_command=None,           # we use slash commands instead
        )

    async def setup_hook(self) -> None:
        """Called once before the bot connects — load cogs here."""
        await self._load_cogs()
        await self._sync_commands()

    async def _load_cogs(self) -> None:
        """Dynamically load every cog.
        Cogs that need the database receive it via their setup() function."""

        # Cogs that only need `bot`
        simple_cogs = ["cogs.general"]

        # Cogs that need `bot` + `db`
        db_cogs = ["cogs.profile", "cogs.leaderboard"]

        # Cogs that need `bot` + a specific service
        points_cogs = ["cogs.points"]
        tasks_cogs  = ["cogs.tasks"]
        verify_cogs = ["cogs.verify"] if settings.verify_enabled and self.checker else []

        for module in simple_cogs:
            await self.load_extension(module)
            log.info("Loaded cog: %s", module)

        for module in db_cogs:
            # load_extension calls setup(bot); we pass db via a workaround:
            # store db on the bot so setup() can reach it.
            await self.load_extension(module)
            log.info("Loaded cog: %s", module)

        for module in points_cogs:
            await self.load_extension(module)
            log.info("Loaded cog: %s", module)

        for module in tasks_cogs:
            await self.load_extension(module)
            log.info("Loaded cog: %s", module)

        for module in verify_cogs:
            await self.load_extension(module)
            log.info("Loaded cog: %s", module)

    async def _sync_commands(self) -> None:
        """Sync slash commands to Discord."""
        if settings.guild_id:
            guild = discord.Object(id=settings.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Slash commands synced to guild %s (instant).", settings.guild_id)
        else:
            await self.tree.sync()
            log.info("Slash commands synced globally (may take up to 1 hour).")

    async def on_ready(self) -> None:
        log.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        log.info("Connected to %d guild(s).", len(self.guilds))
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=f"{len(self.guilds)} server(s) | /help",
            )
        )

    async def on_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        log.error("Command error: %s", error)


# ── Cog setup helpers (called by load_extension) ──────────────────────────────
# discord.py calls `setup(bot)` inside each cog module.
# For cogs that need `db`, we patch their setup to pull it from bot.db.
# See each cog file for the `async def setup(bot, db)` signature.

import importlib  # noqa: E402  (below the class on purpose)

_original_load_extension = commands.Bot.load_extension

async def _patched_load_extension(self: MyBot, name: str, **kwargs):  # type: ignore[override]
    module = importlib.import_module(name.replace(".", "/").replace("/", "."))
    import inspect
    sig = inspect.signature(module.setup)
    params = list(sig.parameters.keys())
    if "verify_svc" in params:
        # Verify cog needs verify_svc + checker + tasks_svc
        await module.setup(self, self.verify_svc, self.checker, self.tasks_svc)
    elif "tasks_svc" in params:
        await module.setup(self, self.tasks_svc)
    elif "points_svc" in params:
        await module.setup(self, self.points_svc)
    elif "db" in params:
        await module.setup(self, self.db)
    else:
        await module.setup(self)

MyBot.load_extension = _patched_load_extension  # type: ignore[method-assign]


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    db = Database(settings.database_path)
    await db.connect()

    points_svc = await PointsService.create(settings.database_path)
    tasks_svc  = await TasksService.create(settings.database_path, points_svc)
    verify_svc = await VerifyService.create(settings.database_path)

    # TwitterChecker is optional — skip if credentials aren't set or verify is disabled.
    checker: TwitterChecker | None = None
    if settings.verify_enabled:
        if not settings.twitter_email or not settings.twitter_password:
            log.warning(
                "VERIFY_ENABLED=true but TWITTER_EMAIL / TWITTER_PASSWORD are missing. "
                "Verify cog will be skipped. Set credentials in .env to enable it."
            )
        else:
            try:
                checker = await TwitterChecker.create(
                    email=settings.twitter_email,
                    username=settings.twitter_username,
                    password=settings.twitter_password,
                    session_dir=settings.twitter_session_dir,
                    headless=settings.twitter_headless,
                )
            except Exception as exc:
                log.error("Failed to start TwitterChecker: %s — verify cog disabled.", exc)

    async with MyBot(db, points_svc, tasks_svc, verify_svc, checker) as bot:
        try:
            await bot.start(settings.token)
        finally:
            await db.close()
            await points_svc.close()
            await tasks_svc.close()
            await verify_svc.close()
            if checker:
                await checker.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped by user.")
