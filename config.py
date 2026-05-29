"""
config.py
Centralised configuration loaded from environment variables / .env file.
Import `settings` anywhere in the project instead of calling os.getenv directly.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env file if present (ignored in production where real env vars are set)
load_dotenv()


@dataclass(frozen=True)
class Settings:
    # ── Discord ──────────────────────────────────────────────────────────────
    token: str = field(default_factory=lambda: _require("DISCORD_TOKEN"))
    # If set, commands are synced to this guild instantly (great for dev).
    # Leave empty / unset to sync globally.
    guild_id: int | None = field(
        default_factory=lambda: _optional_int("GUILD_ID")
    )

    # ── Database ─────────────────────────────────────────────────────────────
    database_path: Path = field(
        default_factory=lambda: Path(os.getenv("DATABASE_PATH", "data/bot.db"))
    )

    # ── Logging ──────────────────────────────────────────────────────────────
    log_level: str = field(
        default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper()
    )

    # ── Twitter / X (required only when the verify cog is enabled) ───────────
    twitter_email: str = field(
        default_factory=lambda: os.getenv("TWITTER_EMAIL", "")
    )
    twitter_username: str = field(
        default_factory=lambda: os.getenv("TWITTER_USERNAME", "")
    )
    twitter_password: str = field(
        default_factory=lambda: os.getenv("TWITTER_PASSWORD", "")
    )
    twitter_session_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv("TWITTER_SESSION_DIR", "data/twitter_session")
        )
    )
    twitter_headless: bool = field(
        default_factory=lambda: os.getenv("TWITTER_HEADLESS", "true").lower() == "true"
    )
    # How many scroll steps the checker performs when searching for a reply.
    verify_scroll_steps: int = field(
        default_factory=lambda: int(os.getenv("VERIFY_SCROLL_STEPS", "15"))
    )
    verify_scroll_delay_ms: int = field(
        default_factory=lambda: int(os.getenv("VERIFY_SCROLL_DELAY_MS", "1200"))
    )
    # Set to "false" to disable the verify cog entirely (skips Playwright start).
    verify_enabled: bool = field(
        default_factory=lambda: os.getenv("VERIFY_ENABLED", "true").lower() == "true"
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {key}\n"
            f"  → Copy .env.example to .env and fill in the value."
        )
    return value


def _optional_int(key: str) -> int | None:
    value = os.getenv(key)
    if value and value.strip():
        return int(value.strip())
    return None


# Singleton — import this everywhere
settings = Settings()
