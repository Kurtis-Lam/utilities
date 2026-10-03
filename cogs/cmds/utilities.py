import os
import re
import secrets
import time
import discord
from discord.ext import commands, tasks

from views.embeds import (
    BRAND_COLOR, ok_embed, err_embed, info_embed, handle_common_error, send_usage
)

# Compile regex once at module level to avoid recompiling on every command call
TIME_PATTERN = re.compile(r"^(\d+)([smhd])$")


class Utilities(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
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

    async def cog_load(self):
        """Warms up the database connection when the bot starts."""
        try:
            await self.mongo_client.admin.command('ping')
        except Exception as e:
            print(f"Utilities Cog: MongoDB warmup failed: {e}")

    def cog_unload(self):
        self.check_reminders.cancel()

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

    @commands.command(name="remind", aliases=["rm"], description="Sets a reminder.")
    @commands.is_owner()
    async def remind(self, ctx, time_str: str, *, message: str):
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

    @commands.group(name="reminders", invoke_without_command=True, description="View your active reminders.")
    @commands.is_owner()
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
    @commands.is_owner()
    async def reminders_remove(self, ctx, reminder_id: str):
        result = await self.collection.delete_one({"id": reminder_id, "user_id": ctx.author.id})

        if result.deleted_count > 0:
            await ctx.reply(embed=ok_embed("Reminder Removed", f"Successfully removed reminder `{reminder_id}`.", emoji="🗑️"), mention_author=False)
        else:
            await ctx.reply(embed=err_embed("Reminder Not Found", f"Could not find a reminder with ID `{reminder_id}` belonging to you."), mention_author=False)


async def setup(bot):
    await bot.add_cog(Utilities(bot))