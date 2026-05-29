"""
cogs/points.py
Points management commands (moderator + user-facing).

Commands
--------
/points add    <user> <amount> [reason]  — mod: grant points
/points remove <user> <amount> [reason]  — mod: deduct points
/points check  [user]                   — anyone: view current total
/points history [user]                  — anyone: last 10 transactions
/points leaderboard                     — anyone: top-10 by points
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from utils.points_db import LeaderboardEntry, PointsService

log = logging.getLogger(__name__)

# ── Permissions shorthand ─────────────────────────────────────────────────────
# Only members who can manage the server can add/remove points.
_manage_guild = app_commands.checks.has_permissions(manage_guild=True)

MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


class Points(commands.Cog):
    """Points & leaderboard commands."""

    def __init__(self, bot: commands.Bot, points_svc: PointsService) -> None:
        self.bot = bot
        self.svc = points_svc

    # ── Command group ─────────────────────────────────────────────────────────

    points_group = app_commands.Group(
        name="points",
        description="Points system commands.",
    )

    # ── /points add ───────────────────────────────────────────────────────────

    @points_group.command(name="add", description="[MOD] Add points to a member.")
    @app_commands.describe(
        member="Who receives the points.",
        amount="How many points to add (must be > 0).",
        reason="Optional reason shown in their history.",
    )
    @_manage_guild
    async def points_add(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: app_commands.Range[int, 1, 100_000],
        reason: str | None = None,
    ) -> None:
        if member.bot:
            await interaction.response.send_message(
                "❌ Bots can't receive points.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        result = await self.svc.add_points(
            user_id=member.id,
            username=member.display_name,
            delta=amount,
            reason=reason,
            granted_by=interaction.user.id,
        )

        embed = discord.Embed(
            title="✅ Points Added",
            colour=discord.Colour.green(),
        )
        embed.add_field(name="Member", value=member.mention, inline=True)
        embed.add_field(name="Added", value=f"**+{amount:,}**", inline=True)
        embed.add_field(name="New Total", value=f"**{result.total:,}**", inline=True)
        if reason:
            embed.add_field(name="Reason", value=reason, inline=False)
        embed.set_thumbnail(url=member.display_avatar.url)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /points remove ────────────────────────────────────────────────────────

    @points_group.command(name="remove", description="[MOD] Remove points from a member.")
    @app_commands.describe(
        member="Who loses the points.",
        amount="How many points to remove (must be > 0).",
        reason="Optional reason shown in their history.",
    )
    @_manage_guild
    async def points_remove(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: app_commands.Range[int, 1, 100_000],
        reason: str | None = None,
    ) -> None:
        if member.bot:
            await interaction.response.send_message(
                "❌ Bots don't have points.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        result = await self.svc.remove_points(
            user_id=member.id,
            username=member.display_name,
            amount=amount,
            reason=reason,
            granted_by=interaction.user.id,
        )

        embed = discord.Embed(
            title="🔻 Points Removed",
            colour=discord.Colour.red(),
        )
        embed.add_field(name="Member", value=member.mention, inline=True)
        embed.add_field(name="Removed", value=f"**-{amount:,}**", inline=True)
        embed.add_field(name="New Total", value=f"**{result.total:,}**", inline=True)
        if reason:
            embed.add_field(name="Reason", value=reason, inline=False)
        embed.set_thumbnail(url=member.display_avatar.url)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /points check ─────────────────────────────────────────────────────────

    @points_group.command(
        name="check", description="Check your points (or another member's)."
    )
    @app_commands.describe(member="Leave blank to check your own points.")
    async def points_check(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        target = member or interaction.user
        await interaction.response.defer(ephemeral=True)

        total = await self.svc.get_total_points(target.id)

        embed = discord.Embed(
            title=f"💰 {target.display_name}'s Points",
            colour=discord.Colour.blurple(),
        )
        embed.description = f"**{total:,}** point{'s' if total != 1 else ''}"
        embed.set_thumbnail(url=target.display_avatar.url)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /points history ───────────────────────────────────────────────────────

    @points_group.command(
        name="history", description="Show the last 10 point transactions."
    )
    @app_commands.describe(member="Leave blank to view your own history.")
    async def points_history(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        target = member or interaction.user
        await interaction.response.defer(ephemeral=True)

        txns = await self.svc.get_history(target.id, limit=10)

        embed = discord.Embed(
            title=f"📜 {target.display_name}'s Recent Transactions",
            colour=discord.Colour.blurple(),
        )

        if not txns:
            embed.description = "No transactions yet."
        else:
            lines: list[str] = []
            for txn in txns:
                sign = "+" if txn.delta > 0 else ""
                ts = int(txn.created_at.timestamp())
                reason_str = f" — *{txn.reason}*" if txn.reason else ""
                lines.append(f"`{sign}{txn.delta:,}` pts · <t:{ts}:R>{reason_str}")
            embed.description = "\n".join(lines)

        embed.set_thumbnail(url=target.display_avatar.url)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /points leaderboard ───────────────────────────────────────────────────

    @points_group.command(
        name="leaderboard", description="Show the top 10 members by points."
    )
    async def points_leaderboard(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

        board: list[LeaderboardEntry] = await self.svc.get_leaderboard(limit=10)

        embed = discord.Embed(
            title="🏆 Points Leaderboard",
            colour=discord.Colour.gold(),
        )

        if not board:
            embed.description = "No points have been awarded yet!"
            await interaction.followup.send(embed=embed)
            return

        lines: list[str] = []
        for entry in board:
            medal = MEDALS.get(entry.rank, f"`#{entry.rank}`")
            # Attempt live member lookup for a fresh display name
            guild = interaction.guild
            member = guild.get_member(entry.user_id) if guild else None
            name = member.display_name if member else entry.username
            lines.append(f"{medal} **{name}** — {entry.total_points:,} pts")

        embed.description = "\n".join(lines)
        embed.set_footer(text="Points are awarded by moderators.")
        await interaction.followup.send(embed=embed)

    # ── Error handler ─────────────────────────────────────────────────────────

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "❌ You need the **Manage Server** permission to use this command.",
                ephemeral=True,
            )
        else:
            log.exception("Unhandled error in Points cog: %s", error)
            msg = "⚠️ Something went wrong. Please try again later."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot, points_svc: PointsService) -> None:  # type: ignore[override]
    cog = Points(bot, points_svc)
    bot.tree.add_command(cog.points_group)
    await bot.add_cog(cog)
