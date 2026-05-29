"""
cogs/profile.py
User profile commands.

Commands
--------
/profile [user]  — display XP, level, and message count for a user
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from utils.database import Database


class Profile(commands.Cog):
    """User profile commands."""

    def __init__(self, bot: commands.Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

    # ── /profile ──────────────────────────────────────────────────────────────

    @app_commands.command(
        name="profile",
        description="View your profile (or someone else's).",
    )
    @app_commands.describe(user="The user whose profile to view (defaults to you).")
    async def profile(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ) -> None:
        target = user or interaction.user

        # Defer so we have time to hit the database
        await interaction.response.defer(ephemeral=True)

        row = await self.db.get_or_create_user(target.id, target.display_name)

        xp_needed = Database.xp_for_next_level(row["level"])
        progress = min(row["xp"] / xp_needed, 1.0)  # 0.0 – 1.0
        bar = self._progress_bar(progress)

        embed = discord.Embed(
            title=f"📋 {target.display_name}'s Profile",
            colour=target.colour if target.colour.value else discord.Colour.blurple(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Level", value=str(row["level"]), inline=True)
        embed.add_field(name="XP", value=f"{row['xp']} / {xp_needed}", inline=True)
        embed.add_field(name="Messages", value=str(row["messages"]), inline=True)
        embed.add_field(name="Progress to next level", value=bar, inline=False)
        embed.set_footer(text=f"Member since {row['joined_at'][:10]}")

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _progress_bar(progress: float, length: int = 20) -> str:
        filled = round(progress * length)
        bar = "█" * filled + "░" * (length - filled)
        return f"`{bar}` {progress:.0%}"

    # ── Passive XP listener ───────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Award 5 XP for every non-bot message. Announces level-ups."""
        if message.author.bot:
            return

        await self.db.get_or_create_user(message.author.id, message.author.display_name)
        result = await self.db.add_xp(message.author.id, 5)

        if result.get("leveled_up"):
            try:
                await message.channel.send(
                    f"🎉 Congratulations {message.author.mention}! "
                    f"You reached **level {result['level']}**!",
                    delete_after=10,
                )
            except discord.Forbidden:
                pass  # Bot lacks send permissions in this channel


async def setup(bot: commands.Bot, db: Database) -> None:  # type: ignore[override]
    await bot.add_cog(Profile(bot, db))
