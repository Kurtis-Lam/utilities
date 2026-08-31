import os
import re
import secrets
import time
import certifi
import discord
from discord.ext import commands, tasks
import motor.motor_asyncio # Replaced pymongo with asynchronous motor

MONGO_URI = "mongodb+srv://KurtisLam:CsHLOnDqihiU5uYG@cluster0.7rwx3oc.mongodb.net/?appName=Cluster0"

# Compile regex once at module level to avoid recompiling on every command call
TIME_PATTERN = re.compile(r"^(\d+)([smhd])$")

class Utilities(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Use Motor for asynchronous, non-blocking MongoDB access
        self.mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI, tlsCAFile=certifi.where())
        self.db = self.mongo_client["utilities"]
        self.collection = self.db["reminders"]

        self.check_reminders.start()

    async def cog_load(self):
        """Warms up the database connection when the bot starts."""
        try:
            await self.mongo_client.admin.command('ping')
        except Exception as e:
            print(f"Utilities Cog: MongoDB warmup failed: {e}")

    def cog_unload(self):
        self.check_reminders.cancel()

    @tasks.loop(seconds=10)
    async def check_reminders(self):
        current_time = time.time()
        
        # AWAITED: Changed to async iteration format using to_list()
        # This prevents the loop from blocking other bot functions every 10 seconds
        due_reminders = await self.collection.find({"ends_at": {"$lte": current_time}}).to_list(length=None)

        if not due_reminders:
            return

        for rem in due_reminders:
            try:
                channel = self.bot.get_channel(rem["channel_id"]) or await self.bot.fetch_channel(rem["channel_id"])
                if channel:
                    # Avoid fetching user over API; direct raw mention uses 0 RAM/API calls
                    await channel.send(f"<@{rem['user_id']}> ⏰ **Reminder:** {rem['message']}")
            except Exception as e:
                print(f"Failed to send reminder {rem['id']}: {e}")

            # AWAITED: Delete the processed reminder asynchronously
            await self.collection.delete_one({"_id": rem["_id"]})

    @check_reminders.before_loop
    async def before_check_reminders(self):
        await self.bot.wait_until_ready()

    @commands.command(name="addprefix", description="Adds the # in front of all integers")
    @commands.is_owner()
    async def addprefix(self, ctx, amt: int):
        if amt <= 0:
            return await ctx.send("Amount must be greater than 0.")

        # Stream text with generator expressions to avoid creating large list objects in RAM
        result = " ".join(f"#{i}" for i in range(1, amt + 1))
        formatted_result = f"```\n{result}\n```"

        # Prevent Discord HTTP 400 errors if string exceeds 2000 character limit
        if len(formatted_result) > 2000:
            return await ctx.send("Result is too long to fit in a single Discord message (2000 char limit).")

        await ctx.send(formatted_result)

    @commands.command(name="note", description="Notes a referral message on a specific channel")
    @commands.is_owner()
    async def note(self, ctx):
        if not ctx.message.reference:
            return await ctx.send("Please reply to the message you want to note with `.note`")

        try:
            replied_message = await ctx.channel.fetch_message(ctx.message.reference.message_id)
            target_channel_id = 1458732659378229248
            target_channel = self.bot.get_channel(target_channel_id) or await self.bot.fetch_channel(target_channel_id)

            if not target_channel:
                return await ctx.send(f"Could not find target channel ID {target_channel_id}.")

            final_content = f"{replied_message.content}\n\n{replied_message.jump_url}"

            # Append image/file URLs instead of downloading raw bytes into RAM via to_file()
            if replied_message.attachments:
                attachment_urls = "\n".join(a.url for a in replied_message.attachments)
                final_content += f"\n\n**Attachments:**\n{attachment_urls}"

            await target_channel.send(content=final_content, embeds=replied_message.embeds)
            await ctx.message.add_reaction("✅")

        except discord.Forbidden:
            await ctx.send("I do not have permission to send messages to the target channel.")
        except discord.HTTPException as e:
            await ctx.send(f"Failed to send message: {e}")
        except Exception as e:
            await ctx.send(f"An error occurred: {e}")

    @commands.command(name="remind", aliases=["rm"], description="Sets a reminder.")
    @commands.is_owner()
    async def remind(self, ctx, time_str: str, *, message: str):
        match = TIME_PATTERN.match(time_str)
        if not match:
            return await ctx.send("Invalid time format. Use `<number><unit>` (e.g., `10s`, `5m`, `2h`, `1d`).")

        amount, unit = int(match.group(1)), match.group(2)
        multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        seconds = amount * multipliers[unit]

        if seconds > 2592000:
            return await ctx.send("Reminder cannot be longer than 30 days.")

        reminder_id = secrets.token_hex(3)
        reminder_data = {
            "id": reminder_id,
            "user_id": ctx.author.id,
            "channel_id": ctx.channel.id,
            "ends_at": time.time() + seconds,
            "message": message,
        }

        # AWAITED: Insert asynchronously so setting a reminder doesn't block other commands
        await self.collection.insert_one(reminder_data)

        await ctx.send(f"I will remind you in **{time_str}** (ID: `{reminder_id}`): {message}")

    @commands.group(name="reminders", invoke_without_command=True, description="View your active reminders.")
    @commands.is_owner()
    async def reminders(self, ctx):
        # AWAITED: Used async .to_list() format
        user_reminders = await self.collection.find({"user_id": ctx.author.id}).to_list(length=None)
        
        if not user_reminders:
            return await ctx.send("You have no active reminders.")

        embed = discord.Embed(title="Your Active Reminders", color=discord.Color.blue())
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
                name=f"ID: {r['id']} (in {' '.join(parts)})",
                value=msg_preview,
                inline=False,
            )

        await ctx.send(embed=embed)

    @reminders.command(name="remove", aliases=["r"], description="Remove a reminder by its ID.")
    @commands.is_owner()
    async def reminders_remove(self, ctx, reminder_id: str):
        # AWAITED: Non-blocking deletion
        result = await self.collection.delete_one({"id": reminder_id, "user_id": ctx.author.id})

        if result.deleted_count > 0:
            await ctx.send(f"✅ Successfully removed reminder `{reminder_id}`.")
        else:
            await ctx.send(f"❌ Could not find a reminder with ID `{reminder_id}` belonging to you.")


async def setup(bot):
    await bot.add_cog(Utilities(bot))