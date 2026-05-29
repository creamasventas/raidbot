"""
cogs/general.py
General-purpose commands available to everyone.

Commands
--------
/ping  — show bot latency
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands


class General(commands.Cog):
    """General commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── /ping ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="ping", description="Check the bot's latency.")
    async def ping(self, interaction: discord.Interaction) -> None:
        latency_ms = round(self.bot.latency * 1000)

        embed = discord.Embed(
            title="🏓 Pong!",
            description=f"Gateway latency: **{latency_ms} ms**",
            colour=discord.Colour.green() if latency_ms < 150 else discord.Colour.orange(),
        )
        embed.set_footer(text=f"Requested by {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(General(bot))
