import asyncio
import typing
import discord
from discord.ext import commands

class Messages(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            perms = ", ".join([f"`{p}`" for p in error.missing_permissions])
            await ctx.send(f"❌ You lack the required permissions: {perms}")
        elif isinstance(error, commands.BotMissingPermissions):
            perms = ", ".join([f"`{p}`" for p in error.missing_permissions])
            await ctx.send(f"❌ I am missing permissions: {perms}")
        else:
            raise error

    @commands.hybrid_command(name="purge", description="Bulk deletes messages, optionally restricted to a specific user.")
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def purge(
        self, 
        ctx, 
        amt: int = commands.parameter(description="Number of messages to check/clear."), 
        user: typing.Optional[discord.Member] = commands.parameter(default=None, description="Optional user whose messages to target.")
    ):
        if amt <= 0:
            return await ctx.send("❌ Amount must be greater than 0.")
        
        # Include the command invocation message if it's a prefix command, 
        # or handle standard purge limit.
        limit = amt + 1 if ctx.interaction is None else amt
        
        def check_msg(m):
            return m.author == user if user else True

        deleted = await ctx.channel.purge(limit=limit, check=check_msg)
        
        # Adjust count if the trigger message was included in the purge
        count = len(deleted) - 1 if ctx.interaction is None else len(deleted)
        if count < 0:
            count = 0

        target_str = f" from {user.display_name}" if user else ""
        msg = await ctx.send(f"🧹 Purged {count} messages{target_str}.")
        await asyncio.sleep(1)
        await msg.delete()

    @commands.command(name="pin", description="Pins the message you are replying to.")
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def pin(self, ctx):
        if not ctx.message.reference or not ctx.message.reference.message_id:
            return await ctx.send("❌ Please reply to the message you want to pin.")
        
        try:
            target_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
            await target_msg.pin(reason=f"Pinned by {ctx.author}")
            await ctx.message.delete()
        except discord.NotFound:
            await ctx.send("❌ Could not find the referenced message.")
        except discord.HTTPException:
            await ctx.send("❌ Failed to pin the message. It might already be pinned or the pin limit (50) was reached.")

    @commands.command(name="echo", description="Repeats whatever text message is supplied.")
    @commands.has_permissions(administrator=True)
    async def echo(self, ctx, *, msg: str = commands.parameter(description="The message to mirror back.")):
        await ctx.message.delete()
        await ctx.send(msg)

    @commands.hybrid_command(name="stick", description="Sticks a message to the current channel.")
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def stick(self, ctx, *, msg: str = commands.parameter(description="The message content to stick.")):
        channel_id_str = str(ctx.channel.id)

        # Clean up existing stick message in channel if overriding
        if channel_id_str in self.sticks and self.sticks[channel_id_str].get("last_msg_id"):
            try:
                old_msg = await ctx.channel.fetch_message(self.sticks[channel_id_str]["last_msg_id"])
                await old_msg.delete()
            except (discord.NotFound, discord.HTTPException):
                pass

        # Send the initial sticky message
        sent_msg = await ctx.channel.send(f"{msg}")

        # Save to memory and JSON
        self.sticks[channel_id_str] = {
            "channel_id": ctx.channel.id,
            "guild_id": ctx.guild.id,
            "content": msg,
            "last_msg_id": sent_msg.id
        }
        self.save_sticks()

        # Delete the command invocation to keep chat clean
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass

    @commands.group(name="sticks", invoke_without_command=True, description="View current server sticks.")
    @commands.has_permissions(manage_messages=True)
    async def sticks(self, ctx):
        # Filter sticks for the current guild
        guild_sticks = [
            data for data in self.sticks.values() 
            if data.get("guild_id") == ctx.guild.id
        ]

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
    @commands.has_permissions(manage_messages=True)
    async def sticks_remove(self, ctx, channel: typing.Optional[discord.TextChannel] = None):
        target_channel = channel or ctx.channel
        channel_id_str = str(target_channel.id)

        if channel_id_str in self.sticks:
            # Try to delete the last active sticky message instance
            last_msg_id = self.sticks[channel_id_str].get("last_msg_id")
            if last_msg_id:
                try:
                    msg = await target_channel.fetch_message(last_msg_id)
                    await msg.delete()
                except (discord.NotFound, discord.HTTPException):
                    pass

            del self.sticks[channel_id_str]
            self.save_sticks()
            await ctx.send(f"✅ Successfully removed the sticky message from {target_channel.mention}.")
        else:
            await ctx.send(f"❌ There is no sticky message active in {target_channel.mention}.")

async def setup(bot):
    await bot.add_cog(Messages(bot))