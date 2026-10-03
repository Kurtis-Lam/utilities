import asyncio
import time
import typing
import discord
from discord import app_commands
from discord.ext import commands

# Import ConfirmView from views/common.py
from views.common import ConfirmView
from views.embeds import ok_embed, err_embed, info_embed, handle_common_error, send_usage


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

    @property
    def snipes_collection(self):
        """One document per channel: {_id: channel_id, guild_id, entries: [newest ... oldest]} (max 10)."""
        return self.db["snipes"]

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
            # (kept as plain text on purpose: it mirrors the content you set with .stick)
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
        if not await handle_common_error(ctx, error):
            raise error

    async def _resolve_channel(self, ctx, channel_str: str) -> typing.Optional[discord.TextChannel]:
        """Resolves a channel from a mention (<#ID>), raw ID, or channel name."""
        if not channel_str:
            return None
        cleaned = channel_str.strip("<#> ")
        if cleaned.isdigit():
            ch = ctx.guild.get_channel(int(cleaned))
            if ch and isinstance(ch, discord.TextChannel):
                return ch
            try:
                ch = await self.bot.fetch_channel(int(cleaned))
                if isinstance(ch, discord.TextChannel):
                    return ch
            except (discord.NotFound, discord.HTTPException):
                pass

        ch = discord.utils.get(ctx.guild.text_channels, name=channel_str.lstrip("#"))
        if ch:
            return ch

        try:
            return await commands.TextChannelConverter().convert(ctx, channel_str)
        except commands.BadArgument:
            return None

    async def _fetch_target_message(self, ctx, msg_ref: str) -> typing.Optional[discord.Message]:
        """Fetches a message by link or message ID across channels."""
        if not msg_ref:
            return None
        try:
            if "/" in msg_ref:
                parts = msg_ref.split("/")
                msg_id = int(parts[-1])
                ch_id = int(parts[-2])
                ch = ctx.guild.get_channel(ch_id)
                if not ch:
                    try:
                        ch = await self.bot.fetch_channel(ch_id)
                    except (discord.NotFound, discord.HTTPException):
                        ch = None
                if ch:
                    return await ch.fetch_message(msg_id)
                return await ctx.channel.fetch_message(msg_id)
            else:
                msg_id = int(msg_ref)
                try:
                    return await ctx.channel.fetch_message(msg_id)
                except (discord.NotFound, discord.HTTPException):
                    for ch in ctx.guild.text_channels:
                        if ch.id != ctx.channel.id:
                            try:
                                return await ch.fetch_message(msg_id)
                            except (discord.NotFound, discord.HTTPException):
                                continue
                    return None
        except (ValueError, discord.NotFound, discord.HTTPException):
            return None

    async def _forward_message(self, target_msg: discord.Message, target_channel: discord.TextChannel) -> bool:
        """Forwards native message or falls back to sending message contents, attachments, and embeds."""
        if hasattr(target_msg, "forward"):
            try:
                await target_msg.forward(target_channel)
                return True
            except (discord.HTTPException, discord.Forbidden, AttributeError):
                pass

        try:
            files = []
            for attachment in target_msg.attachments:
                try:
                    files.append(await attachment.to_file())
                except (discord.HTTPException, discord.NotFound):
                    pass

            embeds = target_msg.embeds.copy() if target_msg.embeds else []
            content = target_msg.content or None

            if not content and not embeds and not files:
                return False

            await target_channel.send(content=content, embeds=embeds, files=files)
            return True
        except discord.HTTPException:
            return False

    @commands.hybrid_command(name="forward", aliases=["fwd"], description="Forwards a message to a channel (reply to a message or provide message ID and channel).")
    @app_commands.describe(
        target_or_msg="Target channel (if replying) OR message ID/link to forward.",
        channel_ref="Target channel (if message ID/link was provided as the first argument)."
    )
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def forward(
        self,
        ctx,
        target_or_msg: typing.Optional[str] = commands.parameter(default=None, description="Target channel or message ID/link."),
        channel_ref: typing.Optional[str] = commands.parameter(default=None, description="Target channel (if message ID was provided first).")
    ):
        target_msg = None
        target_channel = None

        has_reply = bool(ctx.message and ctx.message.reference and ctx.message.reference.message_id)

        if target_or_msg and channel_ref:
            # Usage: .forward {message_id} {channel}
            target_msg = await self._fetch_target_message(ctx, target_or_msg)
            if not target_msg:
                return await ctx.reply(embed=err_embed("Message Not Found", "Could not find a valid message with that ID or link."), mention_author=False)

            target_channel = await self._resolve_channel(ctx, channel_ref)
            if not target_channel:
                return await ctx.reply(embed=err_embed("Channel Not Found", "Could not resolve the specified target channel."), mention_author=False)

        elif target_or_msg:
            if has_reply:
                # Usage: Replying to message with .forward {channel}
                target_channel = await self._resolve_channel(ctx, target_or_msg)
                if not target_channel:
                    return await ctx.reply(embed=err_embed("Channel Not Found", "Could not resolve the specified target channel."), mention_author=False)

                try:
                    ref = ctx.message.reference
                    ref_ch = ctx.guild.get_channel(ref.channel_id) or ctx.channel
                    target_msg = await ref_ch.fetch_message(ref.message_id)
                except (discord.NotFound, discord.HTTPException):
                    return await ctx.reply(embed=err_embed("Message Not Found", "Could not find the referenced message."), mention_author=False)
            else:
                return await send_usage(ctx, note="Please reply to a message with a target channel, or provide both a message ID and a target channel.")
        else:
            return await send_usage(ctx, note="Reply to a message with a target channel, or provide both a message ID and a target channel.")

        if not target_msg or not target_channel:
            return await send_usage(ctx, note="Invalid parameters provided.")

        if not target_channel.permissions_for(ctx.guild.me).send_messages:
            return await ctx.reply(embed=err_embed("Permission Error", f"I do not have permission to send messages in {target_channel.mention}."), mention_author=False)

        success = await self._forward_message(target_msg, target_channel)
        if success:
            await ctx.reply(embed=ok_embed("Message Forwarded", f"Successfully forwarded message to {target_channel.mention}.", emoji="↗️"), mention_author=False)
        else:
            await ctx.reply(embed=err_embed("Forward Failed", f"Failed to forward the message to {target_channel.mention}."), mention_author=False)

    # ------------------------------------------------------------------
    # Snipe: the last 10 deleted messages per channel (saved in MongoDB)
    # ------------------------------------------------------------------

    SNIPE_LIMIT = 10

    @commands.Cog.listener()
    async def on_message_delete(self, message):
        if message.guild is None or message.author.bot:
            return

        # Don't record command invocations (the bot deletes some of them itself).
        try:
            prefixes = await self.bot.get_prefix(message)
            if isinstance(prefixes, str):
                prefixes = [prefixes]
            prefixes = tuple(p for p in prefixes if p)
            if prefixes and message.content.startswith(prefixes):
                return
        except Exception:
            pass

        if not message.content and not message.attachments:
            return

        entry = {
            "author_id": message.author.id,
            "author_name": str(message.author),
            "content": (message.content or "")[:2000],
            "attachments": len(message.attachments),
            "deleted_at": int(time.time()),
        }

        try:
            # One atomic update: put the new entry first and keep only the newest 10,
            # so anything past 10 is deleted automatically.
            await self.snipes_collection.update_one(
                {"_id": message.channel.id},
                {
                    "$set": {"guild_id": message.guild.id},
                    "$push": {"entries": {"$each": [entry], "$position": 0, "$slice": self.SNIPE_LIMIT}},
                },
                upsert=True,
            )
        except Exception as e:
            print(f"Messages Cog: failed to save snipe entry: {e}")

    @commands.hybrid_command(name="snipe", description="Shows the most recently deleted messages in this channel (up to 10).")
    async def snipe(self, ctx):
        try:
            doc = await self.snipes_collection.find_one({"_id": ctx.channel.id})
        except Exception as e:
            print(f"Messages Cog: failed to load snipes: {e}")
            return await ctx.reply(embed=err_embed("Couldn't load deleted messages right now."), mention_author=False)

        entries = (doc or {}).get("entries", [])[:self.SNIPE_LIMIT]
        if not entries:
            return await ctx.reply(embed=info_embed("Nothing to Snipe", "No deleted messages were recorded in this channel.", emoji="🔍"), mention_author=False)

        lines = []
        for n, e in enumerate(entries, 1):
            text = (e.get("content") or "*No text content*").replace("\n", " ")
            if len(text) > 200:
                text = text[:197] + "..."
            if e.get("attachments"):
                text += f" 📎 {e['attachments']} attachment(s)"
            ts = e["deleted_at"]
            lines.append(f"**{n}.** <@{e['author_id']}> (`{e['author_name']}`) · deleted <t:{ts}:T> (<t:{ts}:R>)\n> {text}")

        embed = discord.Embed(
            title=f"🔍 Deleted Messages ({len(entries)})",
            description="\n\n".join(lines),
            color=discord.Color.blurple()
        )
        embed.set_footer(text="Newest first · only the last 10 are kept")
        await ctx.reply(embed=embed, mention_author=False)

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
                return await send_usage(ctx, note="Amount must be a valid number or `*`.")
            if amount <= 0:
                return await ctx.reply(embed=err_embed("Invalid Amount", "Amount must be greater than 0."), mention_author=False)
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
        # Plain send on purpose: the invoking message was just purged, so it can't be replied to.
        await ctx.reply(
            embed=ok_embed("Messages Purged", f"Purged **{count}** message(s){target_str}.", emoji="🧹"),
            delete_after=3, mention_author=False
        )

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
                return await ctx.reply(embed=err_embed("Message Not Found", "Could not find a valid message with that ID or link."), mention_author=False)
        elif ctx.message and ctx.message.reference and ctx.message.reference.message_id:
            try:
                target_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
            except discord.NotFound:
                return await ctx.reply(embed=err_embed("Message Not Found", "Could not find the referenced message."), mention_author=False)
        
        if not target_msg:
            return await send_usage(ctx, note="Reply to a message or provide a message ID/link to pin.")

        try:
            await target_msg.pin(reason=f"Pinned by {ctx.author}")
            if ctx.message and ctx.message.reference:
                try:
                    await ctx.message.delete()
                except discord.HTTPException:
                    pass
            else:
                await ctx.reply(embed=ok_embed("Message Pinned", "Successfully pinned the message.", emoji="📌"), delete_after=3, mention_author=False)
        except discord.HTTPException:
            await ctx.reply(embed=err_embed("Pin Failed", "Failed to pin the message. It might already be pinned or the pin limit (50) was reached."), mention_author=False)

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
                return await ctx.reply(embed=err_embed("Message Not Found", "Could not find a valid message with that ID or link."), mention_author=False)
        elif ctx.message and ctx.message.reference and ctx.message.reference.message_id:
            try:
                target_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
            except discord.NotFound:
                return await ctx.reply(embed=err_embed("Message Not Found", "Could not find the referenced message."), mention_author=False)
        
        if not target_msg:
            return await send_usage(ctx, note="Reply to a message or provide a message ID/link to unpin.")

        try:
            await target_msg.unpin(reason=f"Unpinned by {ctx.author}")
            if ctx.message and ctx.message.reference:
                try:
                    await ctx.message.delete()
                except discord.HTTPException:
                    pass
            else:
                await ctx.reply(embed=ok_embed("Message Unpinned", "Successfully unpinned the message.", emoji="📌"), delete_after=3, mention_author=False)
        except discord.HTTPException:
            await ctx.reply(embed=err_embed("Unpin Failed", "Failed to unpin the message. It might not be pinned."), mention_author=False)

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
            return await ctx.reply(embed=err_embed("Invalid Argument", "Use `channel` (`ch`) or `server` (`guild`)."), mention_author=False)

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
            return await ctx.reply(embed=info_embed("No Pins", f"No pinned messages found {title_scope}.", emoji="📌"), mention_author=False)

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

        await ctx.reply(embed=embed, mention_author=False)

    @commands.command(name="echo", description="Repeats whatever text message is supplied.")
    @commands.has_permissions(administrator=True)
    async def echo(self, ctx, *, msg: str = commands.parameter(description="The message to mirror back.")):
        try:
            await ctx.message.delete()
        except (discord.HTTPException, AttributeError):
            pass
        # Plain text on purpose: echo's whole job is to repeat your text as-is.
        await ctx.channel.send(msg)

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
            return await ctx.reply(embed=err_embed("No Sticky Found", f"There is no sticky message active in {target_channel.mention}."), mention_author=False)

        last_msg_id = existing_stick.get("last_msg_id")
        if last_msg_id:
            try:
                old_msg = await target_channel.fetch_message(last_msg_id)
                await old_msg.delete()
            except (discord.NotFound, discord.HTTPException):
                pass

        await self.collection.delete_one({"_id": channel_id_str})
        await ctx.reply(embed=ok_embed("Sticky Removed", f"Successfully removed the sticky message from {target_channel.mention}.", emoji="📌"), mention_author=False)

    @commands.hybrid_group(name="sticks", invoke_without_command=True, description="View current server sticky messages.")
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def sticks(self, ctx):
        # AWAITED: Changed to async iteration format using to_list()
        guild_sticks = await self.collection.find({"guild_id": ctx.guild.id}).to_list(length=None)

        if not guild_sticks:
            return await ctx.reply(embed=err_embed("No Sticky Messages", "There are no active sticky messages in this server."), mention_author=False)

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
        await ctx.reply(embed=embed, mention_author=False)

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
            return await ctx.reply(embed=err_embed("Channel Not Found", "Could not resolve the specified channel."), mention_author=False)

        # AWAITED
        existing_stick = await self.collection.find_one({"_id": target_id_str})
        if not existing_stick:
            ch_str = target_channel.mention if target_channel else f"ID `{target_id_str}`"
            return await ctx.reply(embed=err_embed("No Sticky Found", f"There is no sticky message active in {ch_str}."), mention_author=False)

        # Step 1: Prompt for confirmation
        view = ConfirmView(ctx.author)
        ch_display = target_channel.mention if target_channel else f"channel `{target_id_str}`"
        confirm_embed = discord.Embed(
            title="⚠️ Confirmation Required",
            description=f"Are you sure you want to remove the sticky message in {ch_display}?",
            color=discord.Color.gold()
        )
        msg = await ctx.reply(embed=confirm_embed, view=view, mention_author=False)
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