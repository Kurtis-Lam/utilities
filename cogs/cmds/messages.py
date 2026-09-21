import asyncio
import typing
import discord
from discord import app_commands
from discord.ext import commands

# Import ConfirmView from views/common.py
from views.common import ConfirmView


class Messages(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # Retrieves MongoDB instance dynamically from main.py's bot.mongo_client
    @property
    def mongo_client(self):
        return self.bot.mongo_client

    @property
    def db(self):
        return self.mongo_client["utilities"]

    @property
    def collection(self):
        return self.db["sticks"]

    async def cog_load(self):
        """Warms up the database connection when the bot starts so commands are fast instantly."""
        try:
            await self.mongo_client.admin.command('ping')
        except Exception as e:
            print(f"Messages Cog: MongoDB warmup failed: {e}")

    @commands.Cog.listener()
    async def on_message(self, message):
        # Ignore bot messages and DMs
        if message.author.bot or not message.guild:
            return

        channel_id_str = str(message.channel.id)
        
        # AWAITED: Non-blocking DB fetch
        stick_data = await self.collection.find_one({"_id": channel_id_str})

        if stick_data:
            last_msg_id = stick_data.get("last_msg_id")
            content = stick_data.get("content")

            # Delete the old sticky message if it exists
            if last_msg_id:
                try:
                    old_msg = await message.channel.fetch_message(last_msg_id)
                    await old_msg.delete()
                except (discord.NotFound, discord.HTTPException):
                    pass

            # Send a new sticky message at the bottom
            try:
                new_msg = await message.channel.send(content)
                # AWAITED: Non-blocking DB update
                await self.collection.update_one(
                    {"_id": channel_id_str},
                    {"$set": {"last_msg_id": new_msg.id}}
                )
            except discord.HTTPException:
                pass

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            perms = ", ".join([f"`{p}`" for p in error.missing_permissions])
            await ctx.send(f"❌ You lack the required permissions: {perms}")
        elif isinstance(error, commands.BotMissingPermissions):
            perms = ", ".join([f"`{p}`" for p in error.missing_permissions])
            await ctx.send(f"❌ I am missing permissions: {perms}")
        else:
            raise error

    @commands.hybrid_command(name="purge", description="Bulk deletes messages, optionally restricted to a specific user. Use '*' for all messages.")
    @app_commands.describe(
        amt="Number of messages to clear, or '*' for all messages.",
        user="Optional user whose messages to target."
    )
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def purge(
        self, 
        ctx, 
        amt: str = commands.parameter(description="Number of messages to clear, or '*' for all."), 
        user: typing.Optional[discord.Member] = commands.parameter(default=None, description="Optional user whose messages to target.")
    ):
        if amt != "*":
            try:
                amount = int(amt)
            except ValueError:
                return await ctx.send("❌ Amount must be a valid number or `*`.")
            if amount <= 0:
                return await ctx.send("❌ Amount must be greater than 0.")
            limit = amount + 1 if ctx.interaction is None else amount
        else:
            limit = None

        def check_msg(m):
            return m.author == user if user else True

        deleted = await ctx.channel.purge(limit=limit, check=check_msg)
        
        if amt != "*":
            count = len(deleted) - 1 if ctx.interaction is None else len(deleted)
            if count < 0:
                count = 0
        else:
            count = len(deleted)

        target_str = f" from {user.display_name}" if user else ""
        msg = await ctx.send(f"🧹 Purged {count} messages{target_str}.")
        await asyncio.sleep(1)
        await msg.delete()

    @commands.hybrid_command(name="pin", description="Pins a message (reply to a message or provide a message ID/link).")
    @app_commands.describe(
        message_ref="The message ID or link to pin (leave blank if replying to a message)."
    )
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def pin(
        self, 
        ctx, 
        message_ref: typing.Optional[str] = commands.parameter(default=None, description="Message ID or link to pin.")
    ):
        target_msg = None
        if message_ref:
            try:
                msg_id = int(message_ref.split("/")[-1]) if "/" in message_ref else int(message_ref)
                target_msg = await ctx.channel.fetch_message(msg_id)
            except (ValueError, discord.NotFound, discord.HTTPException):
                return await ctx.send("❌ Could not find a valid message with that ID or link.")
        elif ctx.message and ctx.message.reference and ctx.message.reference.message_id:
            try:
                target_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
            except discord.NotFound:
                return await ctx.send("❌ Could not find the referenced message.")
        
        if not target_msg:
            return await ctx.send("❌ Please reply to a message or provide a message ID/link to pin.")

        try:
            await target_msg.pin(reason=f"Pinned by {ctx.author}")
            if ctx.message and ctx.message.reference:
                try:
                    await ctx.message.delete()
                except discord.HTTPException:
                    pass
            else:
                await ctx.send(f"✅ Successfully pinned the message.", delete_after=3)
        except discord.HTTPException:
            await ctx.send("❌ Failed to pin the message. It might already be pinned or the pin limit (50) was reached.")

    @commands.command(name="echo", description="Repeats whatever text message is supplied.")
    @commands.has_permissions(administrator=True)
    async def echo(self, ctx, *, msg: str = commands.parameter(description="The message to mirror back.")):
        try:
            await ctx.message.delete()
        except (discord.HTTPException, AttributeError):
            pass
        await ctx.send(msg)

    @commands.hybrid_command(name="stick", description="Sticks a message to the current channel.")
    @app_commands.describe(
        msg="The message content to stick."
    )
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def stick(self, ctx, *, msg: str):
        channel_id_str = str(ctx.channel.id)

        # AWAITED: Non-blocking DB fetch
        existing_stick = await self.collection.find_one({"_id": channel_id_str})
        
        if existing_stick and existing_stick.get("last_msg_id"):
            try:
                old_msg = await ctx.channel.fetch_message(existing_stick["last_msg_id"])
                await old_msg.delete()
            except (discord.NotFound, discord.HTTPException):
                pass

        sent_msg = await ctx.channel.send(f"{msg}")

        stick_data = {
            "_id": channel_id_str,
            "channel_id": ctx.channel.id,
            "guild_id": ctx.guild.id,
            "content": msg,
            "last_msg_id": sent_msg.id
        }
        
        # AWAITED: Non-blocking DB update
        await self.collection.update_one({"_id": channel_id_str}, {"$set": stick_data}, upsert=True)

        try:
            if ctx.message:
                await ctx.message.delete()
        except (discord.HTTPException, AttributeError):
            pass

    @commands.hybrid_group(name="sticks", invoke_without_command=True, description="View current server sticks.")
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def sticks(self, ctx):
        # AWAITED: Changed to async iteration format using to_list()
        guild_sticks = await self.collection.find({"guild_id": ctx.guild.id}).to_list(length=None)

        if not guild_sticks:
            return await ctx.send("❌ There are no active sticky messages in this server.")

        desc = []
        for idx, data in enumerate(guild_sticks, 1):
            channel = ctx.guild.get_channel(data["channel_id"])
            channel_mention = channel.mention if channel else f"<#{data['channel_id']}>"
            preview = data["content"][:50] + ("..." if len(data["content"]) > 50 else "")
            desc.append(f"**{idx}.** Channel: {channel_mention}\n> {preview}")

        embed = discord.Embed(
            title=f"📌 Active Sticky Messages ({len(guild_sticks)})",
            description="\n\n".join(desc),
            color=discord.Color.blurple()
        )
        await ctx.send(embed=embed)

    @sticks.command(name="remove", aliases=["r"], description="Removes a stick from a channel.")
    @app_commands.describe(
        channel="The text channel to remove the sticky message from (defaults to current channel)."
    )
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def sticks_remove(self, ctx, channel: typing.Optional[discord.TextChannel] = None):
        target_channel = channel or ctx.channel
        channel_id_str = str(target_channel.id)

        # AWAITED
        existing_stick = await self.collection.find_one({"_id": channel_id_str})
        if not existing_stick:
            return await ctx.send(f"❌ There is no sticky message active in {target_channel.mention}.")

        # Step 1: Prompt for confirmation
        view = ConfirmView(ctx.author)
        confirm_embed = discord.Embed(
            title="⚠️ Confirmation Required",
            description=f"Are you sure you want to remove the sticky message in {target_channel.mention}?",
            color=discord.Color.gold()
        )
        msg = await ctx.send(embed=confirm_embed, view=view)
        await view.wait()

        # Step 2: Handle confirmation result
        if view.value is True:
            last_msg_id = existing_stick.get("last_msg_id")
            if last_msg_id:
                try:
                    old_msg = await target_channel.fetch_message(last_msg_id)
                    await old_msg.delete()
                except (discord.NotFound, discord.HTTPException):
                    pass

            # AWAITED: Non-blocking DB deletion
            await self.collection.delete_one({"_id": channel_id_str})

            success_embed = discord.Embed(
                title="✅ Sticky Removed",
                description=f"Successfully removed the sticky message from {target_channel.mention}.",
                color=discord.Color.green()
            )
            try:
                await msg.edit(embed=success_embed, view=None)
            except discord.HTTPException:
                pass

        elif view.value is False:
            cancel_embed = discord.Embed(
                title="❌ Action Cancelled",
                description="Sticky message removal was cancelled.",
                color=discord.Color.red()
            )
            try:
                await msg.edit(embed=cancel_embed, view=None)
            except discord.HTTPException:
                pass
        else:
            timeout_embed = discord.Embed(
                title="⏱️ Timeout",
                description="You took too long to confirm. Removal cancelled.",
                color=discord.Color.red()
            )
            try:
                await msg.edit(embed=timeout_embed, view=None)
            except discord.HTTPException:
                pass


async def setup(bot):
    await bot.add_cog(Messages(bot))