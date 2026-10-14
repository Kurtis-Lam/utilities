import asyncio
import os
import re
import secrets
import time
import typing
import discord
from discord import app_commands
from discord.ext import commands, tasks

from views.embeds import (
    BRAND_COLOR, ok_embed, err_embed, info_embed, handle_common_error, send_usage
)
from views.loading import loading

# Compile regex once at module level to avoid recompiling on every command call
TIME_PATTERN = re.compile(r"^(\d+)([smhd])$")
# Timers also accept combined durations such as 1h30m or 2d12h.
DURATION_PART = re.compile(r"(\d+)([smhd])")
DURATION_FULL = re.compile(r"^(?:\d+[smhd])+$")
UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
MAX_TIMER_SECONDS = 2592000  # 30 days


def parse_duration(text: str):
    """'90s' / '10m' / '1h30m' -> seconds, or None when the text isn't a duration."""
    text = text.strip().lower()
    if not DURATION_FULL.match(text):
        return None
    return sum(int(amount) * UNIT_SECONDS[unit] for amount, unit in DURATION_PART.findall(text))


class Utilities(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._timer_tasks: dict[str, asyncio.Task] = {}
        self.check_reminders.start()

    # Retrieves MongoDB instance dynamically from main.py's bot.mongo_client
    @property
    def mongo_client(self):
        return self.bot.mongo_client

    @property
    def db(self):
        return self.mongo_client["utilities"]

    @property
    def collection(self):
        return self.db["reminders"]

    @property
    def timers_collection(self):
        return self.db["timers"]

    async def cog_load(self):
        """Warms up the database connection when the bot starts."""
        try:
            await self.mongo_client.admin.command('ping')
        except Exception as e:
            print(f"Utilities Cog: MongoDB warmup failed: {e}")

        # Timers survive restarts: re-schedule every unfinished one (overdue ones fire right away).
        try:
            for doc in await self.timers_collection.find({}).to_list(length=None):
                self._schedule_timer(doc)
        except Exception as e:
            print(f"Utilities Cog: could not restore timers: {e}")

    def cog_unload(self):
        self.check_reminders.cancel()
        for task in self._timer_tasks.values():
            task.cancel()
        self._timer_tasks.clear()

    async def cog_command_error(self, ctx, error):
        if not await handle_common_error(ctx, error):
            raise error

    @tasks.loop(seconds=10)
    async def check_reminders(self):
        current_time = time.time()
        
        due_reminders = await self.collection.find({"ends_at": {"$lte": current_time}}).to_list(length=None)

        if not due_reminders:
            return

        for rem in due_reminders:
            try:
                channel = self.bot.get_channel(rem["channel_id"]) or await self.bot.fetch_channel(rem["channel_id"])
                if channel:
                    embed = discord.Embed(
                        title="⏰ Reminder",
                        description=rem["message"],
                        color=BRAND_COLOR
                    )
                    embed.set_footer(text=f"ID: {rem['id']}")
                    # The ping stays in `content` so the user is actually notified.
                    await channel.send(content=f"<@{rem['user_id']}>", embed=embed)
            except Exception as e:
                print(f"Failed to send reminder {rem['id']}: {e}")

            await self.collection.delete_one({"_id": rem["_id"]})

    @check_reminders.before_loop
    async def before_check_reminders(self):
        await self.bot.wait_until_ready()

    @commands.hybrid_command(name="remind", aliases=["rm", "remindme"], description="Sets a reminder.")
    @app_commands.describe(
        time_str="When to remind you: a number plus s, m, h or d (e.g. 10m, 2h, 1d). Max 30 days.",
        message="What to remind you about.",
    )
    @commands.is_owner()
    async def remind(
        self,
        ctx,
        time_str: str = commands.parameter(description="When to remind you: a number plus s, m, h or d (e.g. 10m, 2h, 1d). Max 30 days."),
        *,
        message: str = commands.parameter(description="What to remind you about."),
    ):
        match = TIME_PATTERN.match(time_str)
        if not match:
            return await send_usage(ctx, note="Invalid time format. Use `<number><unit>` (e.g., `10s`, `5m`, `2h`, `1d`).")

        amount, unit = int(match.group(1)), match.group(2)
        multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        seconds = amount * multipliers[unit]

        if seconds > 2592000:
            return await ctx.reply(embed=err_embed("Too Long", "Reminder cannot be longer than 30 days."), mention_author=False)

        reminder_id = secrets.token_hex(3)
        ends_at = time.time() + seconds
        reminder_data = {
            "id": reminder_id,
            "user_id": ctx.author.id,
            "channel_id": ctx.channel.id,
            "ends_at": ends_at,
            "message": message,
        }

        await self.collection.insert_one(reminder_data)

        embed = ok_embed("Reminder Set", message, emoji="⏰")
        embed.add_field(name="⏳ Fires", value=f"<t:{int(ends_at)}:R> (in **{time_str}**)", inline=True)
        embed.add_field(name="🆔 ID", value=f"`{reminder_id}`", inline=True)
        await ctx.reply(embed=embed, mention_author=False)

    # ------------------------------------------------------------------
    # Timers (.timer)
    # ------------------------------------------------------------------

    def _schedule_timer(self, doc: dict):
        old = self._timer_tasks.pop(doc["id"], None)
        if old:
            old.cancel()
        task = asyncio.create_task(self._run_timer(doc))
        self._timer_tasks[doc["id"]] = task
        task.add_done_callback(lambda _t, timer_id=doc["id"]: self._timer_tasks.pop(timer_id, None))

    async def _run_timer(self, doc: dict):
        delay = doc["ends_at"] - time.time()
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            channel = self.bot.get_channel(doc["channel_id"]) or await self.bot.fetch_channel(doc["channel_id"])
            reference = discord.MessageReference(
                message_id=doc["message_id"],
                channel_id=doc["channel_id"],
                guild_id=doc.get("guild_id"),
                fail_if_not_exists=False,  # the timer message may have been deleted
            )
            await channel.send(
                f"Time's up, <@{doc['user_id']}>!",
                reference=reference,
                allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[discord.Object(id=doc["user_id"])], replied_user=False),
            )
        except Exception as e:
            print(f"Failed to finish timer {doc['id']}: {e}")
        finally:
            try:
                await self.timers_collection.delete_one({"id": doc["id"]})
            except Exception as e:
                print(f"Failed to delete timer {doc['id']}: {e}")

    @commands.hybrid_command(name="timer", description="Starts a timer and pings you in this channel when it ends.")
    @app_commands.describe(
        time_str="How long to wait, e.g. 30s, 10m, 1h30m (s, m, h, d can be combined). Max 30 days.",
        name="Optional name shown in the timer (e.g. Pizza).",
    )
    @commands.is_owner()
    @commands.guild_only()
    async def timer(
        self,
        ctx,
        time_str: str = commands.parameter(description="How long to wait: a number plus s, m, h or d, and they can be combined (e.g. 30s, 10m, 1h30m). Max 30 days."),
        *,
        name: typing.Optional[str] = commands.parameter(default=None, description="Optional name shown in the timer (e.g. Pizza)."),
    ):
        seconds = parse_duration(time_str)
        if not seconds:
            return await send_usage(ctx, note="Invalid time format. Use `<number><unit>` (e.g., `30s`, `10m`, `1h30m`, `1d`).")
        if seconds > MAX_TIMER_SECONDS:
            return await ctx.reply(embed=err_embed("Too Long", "A timer cannot be longer than 30 days."), mention_author=False)

        ends_at = int(time.time()) + seconds
        description = f"{name.strip()}\n" if name and name.strip() else ""
        embed = discord.Embed(description=f"{description}# <t:{ends_at}:R>", color=BRAND_COLOR)
        embed.set_author(name=ctx.guild.name, icon_url=ctx.guild.icon.url if ctx.guild.icon else None)
        message = await ctx.reply(embed=embed, mention_author=False)

        doc = {
            "id": secrets.token_hex(3),
            "user_id": ctx.author.id,
            "guild_id": ctx.guild.id,
            "channel_id": ctx.channel.id,
            "message_id": message.id,
            "ends_at": ends_at,
            "name": name,
        }
        try:
            await self.timers_collection.insert_one(dict(doc))
        except Exception as e:  # still works until the next restart
            print(f"Failed to save timer {doc['id']}: {e}")
        self._schedule_timer(doc)

    @commands.hybrid_group(
        name="reminders",
        fallback="list",
        invoke_without_command=True,
        description="View your active reminders.",
    )
    @commands.is_owner()
    @loading
    async def reminders(self, ctx):
        user_reminders = await self.collection.find({"user_id": ctx.author.id}).to_list(length=None)
        
        if not user_reminders:
            return await ctx.reply(embed=info_embed("No Reminders", "You have no active reminders.", emoji="📭"), mention_author=False)

        embed = discord.Embed(title="⏰ Your Active Reminders", color=BRAND_COLOR)
        current_time = time.time()

        for r in user_reminders:
            remaining = max(0, int(r["ends_at"] - current_time))
            days, rem = divmod(remaining, 86400)
            hours, rem = divmod(rem, 3600)
            mins, secs = divmod(rem, 60)

            parts = []
            if days: parts.append(f"{days}d")
            if hours: parts.append(f"{hours}h")
            if mins: parts.append(f"{mins}m")
            if secs or not parts: parts.append(f"{secs}s")

            msg_preview = r["message"] if len(r["message"]) <= 50 else r["message"][:47] + "..."
            embed.add_field(
                name=f"🆔 {r['id']} (in {' '.join(parts)})",
                value=msg_preview,
                inline=False,
            )

        await ctx.reply(embed=embed, mention_author=False)

    @reminders.command(name="remove", aliases=["r"], description="Remove a reminder by its ID.")
    @app_commands.describe(reminder_id="The ID of the reminder to remove (shown by /reminders and when you set it).")
    @commands.is_owner()
    async def reminders_remove(
        self,
        ctx,
        reminder_id: str = commands.parameter(description="The ID of the reminder to remove (shown by .reminders and when you set it)."),
    ):
        result = await self.collection.delete_one({"id": reminder_id, "user_id": ctx.author.id})

        if result.deleted_count > 0:
            await ctx.reply(embed=ok_embed("Reminder Removed", f"Successfully removed reminder `{reminder_id}`.", emoji="🗑️"), mention_author=False)
        else:
            await ctx.reply(embed=err_embed("Reminder Not Found", f"Could not find a reminder with ID `{reminder_id}` belonging to you."), mention_author=False)


async def setup(bot):
    await bot.add_cog(Utilities(bot))