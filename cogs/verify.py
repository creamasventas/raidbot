"""
cogs/verify.py
Twitter / X account verification commands.

Workflow
--------
1. Member runs /set-twitter @alice  — links their Discord account to @alice.
2. Admin creates a task whose URL is a tweet (via /create-task).
3. Member runs /verify <task_id>  — bot scrapes that tweet's replies, looks for
   @alice, and if found calls TasksService.complete_task to award points.

Commands
--------
/set-twitter <handle>     — link your X handle to your Discord account
/verify      <task_id>    — verify you replied to the task's tweet, claim points
/my-x                     — show your currently linked X handle
/unlink-x                 — remove your X link
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from utils.models import DiscordXLink
from utils.scrape_cache import ScrapeCache
from utils.tasks_db import TaskError, TasksService
from utils.twitter_checker import TwitterChecker
from utils.verify_db import VerifyService

log = logging.getLogger(__name__)

# Seconds the bot has to finish scraping before Discord times out the followup.
# Discord gives 15 min for deferred interactions; scraping is usually ≤ 60 s.
_SCRAPE_TIMEOUT_S = 120

# Rate limit: how many /verify calls a single user may make per window.
_VERIFY_RATE = 1          # calls
_VERIFY_PER_SECONDS = 60  # per this many seconds


class Verify(commands.Cog):
    """Twitter / X verification commands."""

    def __init__(
        self,
        bot: commands.Bot,
        verify_svc: VerifyService,
        checker: TwitterChecker,
        tasks_svc: TasksService,
    ) -> None:
        self.bot       = bot
        self.verify    = verify_svc
        self.checker   = checker
        self.tasks     = tasks_svc
        # One scrape per tweet per 5 min, shared across all users.
        self.cache     = ScrapeCache(checker, ttl_seconds=300)

    # ── /set-twitter ──────────────────────────────────────────────────────────

    @app_commands.command(
        name="set-twitter",
        description="Link your X / Twitter handle to your Discord account.",
    )
    @app_commands.describe(handle='Your X username, e.g. "alice" or "@alice".')
    async def set_twitter(
        self, interaction: discord.Interaction, handle: str
    ) -> None:
        normalized = DiscordXLink.normalize(handle)
        if not normalized:
            await interaction.response.send_message(
                "❌ That doesn't look like a valid X handle.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        link = await self.verify.link_account(
            discord_user_id=interaction.user.id,
            discord_username=interaction.user.display_name,
            twitter_handle=normalized,
        )

        embed = discord.Embed(
            title="🔗 X Account Linked",
            description=(
                f"Your Discord account is now linked to **{link.display_handle}**.\n\n"
                f"Use `/verify <task_id>` on any tweet task to claim your reward."
            ),
            colour=discord.Colour.blurple(),
        )
        embed.set_footer(text="Run /unlink-x to remove this link at any time.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /my-x ─────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="my-x",
        description="Show your currently linked X / Twitter handle.",
    )
    async def my_x(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        link = await self.verify.get_link(interaction.user.id)

        if link is None:
            await interaction.followup.send(
                "You haven't linked an X account yet.\n"
                "Use `/set-twitter <handle>` to get started.",
                ephemeral=True,
            )
            return

        ts = int(link.linked_at.timestamp())
        embed = discord.Embed(
            title="🐦 Your Linked X Account",
            description=f"**{link.display_handle}**",
            colour=discord.Colour.blurple(),
        )
        embed.add_field(name="Linked", value=f"<t:{ts}:R>", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /unlink-x ─────────────────────────────────────────────────────────────

    @app_commands.command(
        name="unlink-x",
        description="Remove your linked X / Twitter account.",
    )
    async def unlink_x(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        removed = await self.verify.unlink_account(interaction.user.id)

        if removed:
            await interaction.followup.send(
                "✅ Your X account link has been removed.", ephemeral=True
            )
        else:
            await interaction.followup.send(
                "You don't have a linked X account.", ephemeral=True
            )

    # ── /verify ───────────────────────────────────────────────────────────────

    @app_commands.command(
        name="verify",
        description="Verify you replied to a task's tweet and claim your reward.",
    )
    @app_commands.describe(task_id="The task ID shown in /tasks (e.g. 3).")
    @app_commands.checks.cooldown(
        _VERIFY_RATE, _VERIFY_PER_SECONDS, key=lambda i: i.user.id
    )
    async def verify(
        self, interaction: discord.Interaction, task_id: int
    ) -> None:
        # The cooldown is charged on invocation. For early validation failures
        # (no link, bad task, already done) we refund it so the user isn't
        # locked out for 60 s over a no-op. It's only "spent" on a real scrape.
        def _refund() -> None:
            cmd = interaction.command
            if cmd is not None:
                cmd.reset_cooldown(interaction)

        # ── 1. Must have a linked X account ──────────────────────────────────
        link = await self.verify.get_link(interaction.user.id)
        if link is None:
            _refund()
            await interaction.response.send_message(
                "❌ You haven't linked an X account yet.\n"
                "Run `/set-twitter <handle>` first.",
                ephemeral=True,
            )
            return

        # ── 2. Defer immediately — scraping takes time ────────────────────────
        await interaction.response.defer(ephemeral=True)

        # ── 3. Look up the task ───────────────────────────────────────────────
        task = await self.tasks.get_task(task_id)

        if task is None:
            _refund()
            await interaction.followup.send(
                f"❌ Task #{task_id} doesn't exist.", ephemeral=True
            )
            return

        if not task.is_claimable:
            _refund()
            status = "expired" if task.is_expired else "deactivated"
            await interaction.followup.send(
                f"❌ Task #{task_id} is {status} and can no longer be claimed.",
                ephemeral=True,
            )
            return

        if not task.url:
            _refund()
            await interaction.followup.send(
                f"❌ Task #{task_id} doesn't have a tweet URL — "
                "contact an admin to update it.",
                ephemeral=True,
            )
            return

        # ── 4. Check already completed ────────────────────────────────────────
        if await self.tasks.has_completed(task_id, interaction.user.id):
            _refund()
            await interaction.followup.send(
                f"✅ You've already verified and claimed task #{task_id}.",
                ephemeral=True,
            )
            return

        # ── 5. Announce that the check is starting ────────────────────────────
        await interaction.followup.send(
            f"🔍 Checking **{link.display_handle}** in the replies of task #{task_id}…\n"
            f"If this tweet was checked recently it'll be instant, "
            f"otherwise it takes 20–60 seconds.",
            ephemeral=True,
        )

        # ── 6. Get replies (cached — one scrape serves many users) ────────────
        try:
            replies = await asyncio.wait_for(
                self.cache.get_replies(tweet_url=task.url),
                timeout=_SCRAPE_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            await interaction.followup.send(
                "⏱️ The verification check timed out. Please try again in a moment.",
                ephemeral=True,
            )
            return
        except Exception as exc:
            log.exception("Scrape error for user %d: %s", interaction.user.id, exc)
            await interaction.followup.send(
                "⚠️ Something went wrong during verification. Please try again later.",
                ephemeral=True,
            )
            return

        # Look up this user's handle in the (possibly cached) reply set.
        match = self.cache.find_handle(replies, link.twitter_handle)

        # ── 7. Handle result ──────────────────────────────────────────────────
        if match is None:
            embed = discord.Embed(
                title="❌ Reply Not Found",
                colour=discord.Colour.red(),
            )
            embed.description = (
                f"**{link.display_handle}** wasn't found in the replies of task #{task_id}.\n\n"
                "Make sure you've replied to the tweet linked in the task, "
                "then try again. It may take a few minutes for replies to appear."
            )
            embed.add_field(name="Tweet", value=task.url, inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # ── 8. Award points via TasksService ──────────────────────────────────
        try:
            result = await self.tasks.complete_task(
                task_id=task_id,
                user_id=interaction.user.id,
                username=interaction.user.display_name,
            )
        except TaskError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return

        # ── 9. Success embed ──────────────────────────────────────────────────
        embed = discord.Embed(
            title="🎉 Verification Successful!",
            colour=discord.Colour.green(),
        )
        embed.add_field(
            name="Verified Handle",
            value=link.display_handle,
            inline=True,
        )
        embed.add_field(
            name="Task",
            value=f"#{result.task_id} — {result.task_title}",
            inline=True,
        )
        embed.add_field(
            name="Points Earned",
            value=f"**+{result.reward_points:,}**",
            inline=True,
        )
        embed.add_field(
            name="Your Total",
            value=f"**{result.new_total:,} pts**",
            inline=True,
        )

        # Show a snippet of the matched reply
        if match.reply_text:
            snippet = match.reply_text[:120] + ("…" if len(match.reply_text) > 120 else "")
            embed.add_field(
                name="Matched Reply",
                value=f'"{snippet}"\n[View tweet]({match.reply_url})',
                inline=False,
            )

        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.set_footer(text="Keep completing tasks to climb the leaderboard! 🏆")
        await interaction.followup.send(embed=embed, ephemeral=True)

        log.info(
            "Verified: Discord user %d (@%s on X) completed task #%d — %d pts awarded",
            interaction.user.id, link.twitter_handle, task_id, result.reward_points,
        )

    # ── Error handler ─────────────────────────────────────────────────────────

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        # ── Rate-limit hit: tell the user how long to wait ────────────────────
        if isinstance(error, app_commands.CommandOnCooldown):
            secs = int(error.retry_after) + 1
            msg = (
                f"⏳ You're going a bit fast! Please wait **{secs}s** "
                "before verifying again."
            )
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return

        log.exception("Unhandled error in Verify cog: %s", error)
        msg = "⚠️ Something went wrong. Please try again later."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(
    bot: commands.Bot,
    verify_svc: VerifyService,
    checker: TwitterChecker,
    tasks_svc: TasksService,
) -> None:
    await bot.add_cog(Verify(bot, verify_svc, checker, tasks_svc))
