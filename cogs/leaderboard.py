"""
cogs/leaderboard.py
Server leaderboard commands.

Commands
--------
/leaderboard  — show the top 10 members by XP
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from utils.database import Database

# Medal emojis for the top 3
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


class Leaderboard(commands.Cog):
    """Leaderboard commands."""

    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

    # ── /leaderboard ──────────────────────────────────────────────────────────

    @app_commands.command(
        name="leaderboard",
        description="Show the top 10 members by XP.",
    )
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

        rows = await self.db.get_leaderboard(limit=10)

        if not rows:
            await interaction.followup.send(
                "No data yet — start chatting to earn XP! 🚀", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🏆 Server Leaderboard",
            colour=discord.Colour.gold(),
        )

        lines: list[str] = []
        for rank, row in enumerate(rows, start=1):
            medal = MEDALS.get(rank, f"`#{rank}`")
            # Try to resolve the member so we get a fresh display name
            member = interaction.guild.get_member(row["user_id"]) if interaction.guild else None
            name = member.display_name if member else row["username"]
            lines.append(
                f"{medal} **{name}** — Level {row['level']} · {row['xp']} XP"
            )

        embed.description = "\n".join(lines)
        embed.set_footer(text="XP is earned by chatting in the server.")
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot, db: Database) -> None:  # type: ignore[override]
    await bot.add_cog(Leaderboard(bot, db))
