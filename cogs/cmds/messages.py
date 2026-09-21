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

    @commands.hybrid_command(name="unpin", description="Unpins a message (reply to a message or provide a message ID/link).")
    @app_commands.describe(
        message_ref="The message ID or link to unpin (leave blank if replying to a message)."
    )
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def unpin(
        self, 
        ctx, 
        message_ref: typing.Optional[str] = commands.parameter(default=None, description="Message ID or link to unpin.")
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
            return await ctx.send("❌ Please reply to a message or provide a message ID/link to unpin.")

        try:
            await target_msg.unpin(reason=f"Unpinned by {ctx.author}")
            if ctx.message and ctx.message.reference:
                try:
                    await ctx.message.delete()
                except discord.HTTPException:
                    pass
            else:
                await ctx.send(f"✅ Successfully unpinned the message.", delete_after=3)
        except discord.HTTPException:
            await ctx.send("❌ Failed to unpin the message. It might not be pinned.")

    @commands.hybrid_command(name="pins", description="View pinned messages in the current channel or across the server.")
    @app_commands.describe(
        scope="View pins for 'channel' ('ch') or 'server' ('guild'). Defaults to 'server'."
    )
    @app_commands.choices(scope=[
        app_commands.Choice(name="channel", value="channel"),
        app_commands.Choice(name="server", value="server")
    ])
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def pins(
        self,
        ctx,
        scope: typing.Optional[str] = commands.parameter(default="server", description="Scope: 'channel'/'ch' or 'server'/'guild'.")
    ):
        scope_clean = (scope or "server").lower()

        if scope_clean in ["channel", "ch"]:
            channels = [ctx.channel]
            title_scope = f"in #{ctx.channel.name}"
        elif scope_clean in ["server", "guild"]:
            channels = ctx.guild.text_channels
            title_scope = f"in {ctx.guild.name}"
        else:
            return await ctx.send("❌ Invalid argument. Use `channel` (`ch`) or `server` (`guild`).")

        all_pins = []
        for ch in channels:
            if not ch.permissions_for(ctx.guild.me).read_messages:
                continue
            try:
                pins_list = await ch.pins()
                for msg in pins_list:
                    all_pins.append((ch, msg))
            except discord.HTTPException:
                continue

        if not all_pins:
            return await ctx.send(f"📌 No pinned messages found {title_scope}.")

        desc = []
        for idx, (ch, msg) in enumerate(all_pins[:15], 1):
            preview = msg.content[:80] + ("..." if len(msg.content) > 80 else "")
            if not preview and msg.attachments:
                preview = f"*[{len(msg.attachments)} Attachment(s)]*"
            desc.append(f"**{idx}.** {ch.mention} | **{msg.author.display_name}**: {preview}\n🔗 [Jump to Message]({msg.jump_url})")

        embed = discord.Embed(
            title=f"📌 Pinned Messages {title_scope} ({len(all_pins)})",
            description="\n\n".join(desc),
            color=discord.Color.blurple()
        )
        if len(all_pins) > 15:
            embed.set_footer(text=f"Showing 15 of {len(all_pins)} total pins.")

        await ctx.send(embed=embed)

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

    @commands.hybrid_command(name="unstick", description="Unsticks the sticky message in the current channel.")
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def unstick(self, ctx, channel: typing.Optional[discord.TextChannel] = None):
        target_channel = channel or ctx.channel
        channel_id_str = str(target_channel.id)

        existing_stick = await self.collection.find_one({"_id": channel_id_str})
        if not existing_stick:
            return await ctx.send(f"❌ There is no sticky message active in {target_channel.mention}.")

        last_msg_id = existing_stick.get("last_msg_id")
        if last_msg_id:
            try:
                old_msg = await target_channel.fetch_message(last_msg_id)
                await old_msg.delete()
            except (discord.NotFound, discord.HTTPException):
                pass

        await self.collection.delete_one({"_id": channel_id_str})
        await ctx.send(f"✅ Successfully removed the sticky message from {target_channel.mention}.")

    @commands.hybrid_group(name="sticks", invoke_without_command=True, description="View current server sticky messages.")
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
            desc.append(f"**{idx}.** Channel: {channel_mention} (ID: `{data['channel_id']}`)\n> {preview}")

        embed = discord.Embed(
            title=f"📌 Active Sticky Messages ({len(guild_sticks)})",
            description="\n\n".join(desc),
            color=discord.Color.blurple()
        )
        embed.set_footer(text="💡 Use .unstick to remove in this channel, or .sticks remove <channel_id> for a specific channel.")
        await ctx.send(embed=embed)

    @sticks.command(name="remove", aliases=["r"], description="Removes a sticky message from a specific channel.")
    @app_commands.describe(
        target="Channel mention or ID to remove the sticky message from (defaults to current channel)."
    )
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def sticks_remove(self, ctx, target: typing.Optional[str] = None):
        target_channel = None
        target_id_str = None

        if target:
            cleaned_target = target.strip("<#> ")
            if cleaned_target.isdigit():
                target_id_str = cleaned_target
                target_channel = ctx.guild.get_channel(int(cleaned_target))
            else:
                target_channel = discord.utils.get(ctx.guild.text_channels, name=target)
                if target_channel:
                    target_id_str = str(target_channel.id)
        else:
            target_channel = ctx.channel
            target_id_str = str(ctx.channel.id)

        if not target_id_str:
            return await ctx.send("❌ Could not resolve the specified channel.")

        # AWAITED
        existing_stick = await self.collection.find_one({"_id": target_id_str})
        if not existing_stick:
            ch_str = target_channel.mention if target_channel else f"ID `{target_id_str}`"
            return await ctx.send(f"❌ There is no sticky message active in {ch_str}.")

        # Step 1: Prompt for confirmation
        view = ConfirmView(ctx.author)
        ch_display = target_channel.mention if target_channel else f"channel `{target_id_str}`"
        confirm_embed = discord.Embed(
            title="⚠️ Confirmation Required",
            description=f"Are you sure you want to remove the sticky message in {ch_display}?",
            color=discord.Color.gold()
        )
        msg = await ctx.send(embed=confirm_embed, view=view)
        await view.wait()

        # Step 2: Handle confirmation result
        if view.value is True:
            last_msg_id = existing_stick.get("last_msg_id")
            if last_msg_id and target_channel:
                try:
                    old_msg = await target_channel.fetch_message(last_msg_id)
                    await old_msg.delete()
                except (discord.NotFound, discord.HTTPException):
                    pass

            # AWAITED: Non-blocking DB deletion
            await self.collection.delete_one({"_id": target_id_str})

            success_embed = discord.Embed(
                title="✅ Sticky Removed",
                description=f"Successfully removed the sticky message from {ch_display}.",
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