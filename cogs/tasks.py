"""
cogs/tasks.py
Campaign / task system commands.

Commands
--------
/create-task                   — [ADMIN] opens a Modal to create a new task
/tasks                         — list all active tasks (paginated, 5 per page)
/complete-task <task_id>       — claim a task's reward (once per user per task)
/task-admin deactivate <id>    — [ADMIN] soft-delete a task
/task-admin list-all           — [ADMIN] view all tasks including expired/inactive
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks as ext_tasks

from utils.tasks_db import TaskError, TaskRow, TasksService, parse_duration

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

TASKS_PER_PAGE = 5
_admin_perm = app_commands.checks.has_permissions(manage_guild=True)


# ── Create-task Modal ─────────────────────────────────────────────────────────

class CreateTaskModal(discord.ui.Modal, title="Create New Task"):
    """A Discord Modal (popup form) for admins to fill in task details.

    Using a Modal lets us collect 5 fields in one interaction without
    cramming them all into a single slash-command signature.
    """

    task_title = discord.ui.TextInput(
        label="Task Title",
        placeholder="e.g. Retweet our announcement",
        max_length=100,
    )
    description = discord.ui.TextInput(
        label="Description (optional)",
        placeholder="Explain what members need to do…",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )
    task_url = discord.ui.TextInput(
        label="URL (optional)",
        placeholder="https://twitter.com/…",
        required=False,
        max_length=500,
    )
    reward_points = discord.ui.TextInput(
        label="Reward Points",
        placeholder="e.g. 50",
        max_length=7,
    )
    expires_in = discord.ui.TextInput(
        label="Expires In",
        placeholder="e.g.  7d  |  24h  |  1d12h  |  never",
        max_length=20,
        default="never",
    )

    def __init__(self, svc: TasksService) -> None:
        super().__init__()
        self.svc = svc

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # ── Validate reward_points ────────────────────────────────────────────
        try:
            points = int(self.reward_points.value.strip())
            if points <= 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "❌ Reward points must be a positive whole number.", ephemeral=True
            )
            return

        # ── Parse expiry duration ─────────────────────────────────────────────
        try:
            duration = parse_duration(self.expires_in.value)
        except ValueError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        # ── Create the task ───────────────────────────────────────────────────
        try:
            task = await self.svc.create_task(
                title=self.task_title.value.strip(),
                description=self.description.value.strip() or None,
                url=self.task_url.value.strip() or None,
                reward_points=points,
                expires_in=duration,
                created_by=interaction.user.id,
            )
        except TaskError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return

        embed = _task_embed(task, show_id=True)
        embed.title = f"✅ Task #{task.id} Created"
        embed.colour = discord.Colour.green()
        await interaction.followup.send(embed=embed, ephemeral=True)
        log.info("Admin %s created task #%d: %r", interaction.user, task.id, task.title)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        log.exception("Error in CreateTaskModal: %s", error)
        await interaction.response.send_message(
            "⚠️ Something went wrong creating the task.", ephemeral=True
        )


# ── Pagination view ───────────────────────────────────────────────────────────

class TaskListView(discord.ui.View):
    """Previous / Next buttons for the /tasks paginated list."""

    def __init__(self, task_rows: list[TaskRow], invoker_id: int) -> None:
        super().__init__(timeout=120)
        self.rows = task_rows
        self.invoker_id = invoker_id
        self.page = 0
        self.total_pages = max(1, -(-len(task_rows) // TASKS_PER_PAGE))  # ceil div
        self._update_buttons()

    # ── Guard: only the person who ran the command can page through ───────────
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Only the person who ran `/tasks` can flip pages.", ephemeral=True
            )
            return False
        return True

    def _update_buttons(self) -> None:
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.total_pages - 1
        self.page_label.label = f"{self.page + 1} / {self.total_pages}"

    def current_embed(self) -> discord.Embed:
        start = self.page * TASKS_PER_PAGE
        slice_ = self.rows[start : start + TASKS_PER_PAGE]

        embed = discord.Embed(
            title="📋 Active Tasks",
            colour=discord.Colour.blurple(),
            description=(
                "Complete tasks to earn points!\n"
                "Use `/complete-task <id>` to claim a reward.\n\u200b"
            ),
        )

        for task in slice_:
            expiry_str = (
                f"Expires <t:{int(task.expires_at.timestamp())}:R>"
                if task.expires_at
                else "Never expires"
            )
            value_parts = [
                f"🏅 **{task.reward_points:,} pts** reward",
                f"👥 {task.completion_count} completion(s)",
                expiry_str,
            ]
            if task.description:
                value_parts.insert(0, f"*{task.description}*")
            if task.url:
                value_parts.append(f"🔗 [Open link]({task.url})")

            embed.add_field(
                name=f"`#{task.id}` {task.title}",
                value="\n".join(value_parts),
                inline=False,
            )

        embed.set_footer(
            text=f"Page {self.page + 1}/{self.total_pages} · "
                 f"{len(self.rows)} active task(s) total"
        )
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(label="1 / 1", style=discord.ButtonStyle.primary, disabled=True)
    async def page_label(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        pass  # Non-interactive label button

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    async def on_timeout(self) -> None:
        # Disable all buttons when the view expires so stale buttons don't error
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True


# ── Helper: build a single-task embed ────────────────────────────────────────

def _task_embed(task: TaskRow, *, show_id: bool = False) -> discord.Embed:
    title_prefix = f"Task #{task.id} — " if show_id else ""
    embed = discord.Embed(
        title=f"{title_prefix}{task.title}",
        colour=discord.Colour.blurple(),
    )
    if task.description:
        embed.description = task.description
    if task.url:
        embed.add_field(name="🔗 Link", value=task.url, inline=False)
    embed.add_field(name="🏅 Reward", value=f"**{task.reward_points:,} points**", inline=True)
    embed.add_field(name="👥 Completions", value=str(task.completion_count), inline=True)

    if task.expires_at:
        ts = int(task.expires_at.timestamp())
        embed.add_field(
            name="⏰ Expires",
            value=f"<t:{ts}:F> (<t:{ts}:R>)",
            inline=False,
        )
    else:
        embed.add_field(name="⏰ Expires", value="Never", inline=True)

    embed.set_footer(
        text=f"Created <t:{int(task.created_at.timestamp())}:R>  ·  "
             f"Status: {task.status_emoji}"
    )
    return embed


# ── Cog ───────────────────────────────────────────────────────────────────────

class Tasks(commands.Cog):
    """Campaign / task commands."""

    def __init__(self, bot: commands.Bot, svc: TasksService) -> None:
        self.bot = bot
        self.svc = svc
        self._expire_loop.start()

    def cog_unload(self) -> None:
        self._expire_loop.cancel()

    # ── Background: auto-expire ───────────────────────────────────────────────

    @ext_tasks.loop(minutes=5)
    async def _expire_loop(self) -> None:
        """Every 5 min, log tasks that have just expired (informational only).
        Queries already exclude expired tasks at read time; this loop is here
        so you could add push notifications without extra scaffolding."""
        pass  # extend here if you want expiry-notification DMs, webhooks, etc.

    @_expire_loop.before_loop
    async def _before_expire(self) -> None:
        await self.bot.wait_until_ready()

    # ── /create-task ──────────────────────────────────────────────────────────

    @app_commands.command(
        name="create-task",
        description="[ADMIN] Open a form to create a new task / campaign.",
    )
    @_admin_perm
    async def create_task(self, interaction: discord.Interaction) -> None:
        """Sends a Modal popup — no parameters needed in the slash command."""
        await interaction.response.send_modal(CreateTaskModal(self.svc))

    # ── /tasks ────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="tasks",
        description="Browse all active tasks and their point rewards.",
    )
    async def list_tasks(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

        active = await self.svc.get_active_tasks()

        if not active:
            await interaction.followup.send(
                "📋 No active tasks right now — check back later!", ephemeral=True
            )
            return

        view = TaskListView(active, invoker_id=interaction.user.id)
        await interaction.followup.send(embed=view.current_embed(), view=view)

    # ── /complete-task ────────────────────────────────────────────────────────

    @app_commands.command(
        name="complete-task",
        description="Claim the reward for a completed task.",
    )
    @app_commands.describe(task_id="The task ID shown in /tasks (e.g. 3).")
    async def complete_task(
        self, interaction: discord.Interaction, task_id: int
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        try:
            result = await self.svc.complete_task(
                task_id=task_id,
                user_id=interaction.user.id,
                username=interaction.user.display_name,
            )
        except TaskError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return

        embed = discord.Embed(
            title="🎉 Task Completed!",
            colour=discord.Colour.green(),
        )
        embed.add_field(name="Task", value=f"#{result.task_id} — {result.task_title}", inline=False)
        embed.add_field(name="Points Earned", value=f"**+{result.reward_points:,}**", inline=True)
        embed.add_field(name="Your Total", value=f"**{result.new_total:,}**", inline=True)
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.set_footer(text="Keep completing tasks to climb the leaderboard!")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /task-admin group ─────────────────────────────────────────────────────

    task_admin = app_commands.Group(
        name="task-admin",
        description="[ADMIN] Task management commands.",
    )

    @task_admin.command(
        name="deactivate",
        description="[ADMIN] Deactivate a task so members can no longer claim it.",
    )
    @app_commands.describe(task_id="ID of the task to deactivate.")
    @_admin_perm
    async def deactivate_task(
        self, interaction: discord.Interaction, task_id: int
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        task = await self.svc.get_task(task_id)
        if task is None:
            await interaction.followup.send(
                f"❌ Task #{task_id} not found.", ephemeral=True
            )
            return

        await self.svc.deactivate_task(task_id, admin_id=interaction.user.id)
        await interaction.followup.send(
            f"✅ Task **#{task_id} — {task.title}** has been deactivated.",
            ephemeral=True,
        )

    @task_admin.command(
        name="list-all",
        description="[ADMIN] Show all tasks including expired and inactive ones.",
    )
    @_admin_perm
    async def list_all(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        all_tasks = await self.svc.get_all_tasks()

        if not all_tasks:
            await interaction.followup.send("No tasks in the database yet.", ephemeral=True)
            return

        lines: list[str] = []
        for t in all_tasks:
            expiry = (
                f"exp <t:{int(t.expires_at.timestamp())}:R>"
                if t.expires_at
                else "no expiry"
            )
            lines.append(
                f"{t.status_emoji} `#{t.id}` **{t.title}** "
                f"— {t.reward_points:,} pts · {t.completion_count} completions · {expiry}"
            )

        embed = discord.Embed(
            title="📋 All Tasks (Admin View)",
            description="\n".join(lines),
            colour=discord.Colour.orange(),
        )
        embed.set_footer(text="🟢 active  🔴 deactivated  ⏰ expired")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── Error handler ─────────────────────────────────────────────────────────

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            msg = "❌ You need the **Manage Server** permission to use this command."
        else:
            log.exception("Unhandled error in Tasks cog: %s", error)
            msg = "⚠️ Something went wrong. Please try again later."

        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot, tasks_svc: TasksService) -> None:  # type: ignore[override]
    cog = Tasks(bot, tasks_svc)
    bot.tree.add_command(cog.task_admin)
    await bot.add_cog(cog)
