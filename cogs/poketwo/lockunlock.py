import os
import asyncio
import discord
from discord.ext import commands

class LockUnlock(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.target_user_id = 716390085896962058

    @property
    def locks_collection(self):
        """Dynamically retrieves the collection from the shared main MongoDB client."""
        return self.bot.mongo_client["utilities"]["locked_channels"]

    async def _get_target_user(self, guild: discord.Guild = None):
        """Cache-first lookup to avoid unnecessary HTTP requests to Discord API."""
        if guild:
            member = guild.get_member(self.target_user_id)
            if member:
                return member
        
        user = self.bot.get_user(self.target_user_id)
        if user:
            return user
            
        return await self.bot.fetch_user(self.target_user_id)

    @commands.hybrid_command(aliases=["u"], name="unlock", description="Unlocks the current channel.")
    async def unlock(self, ctx):
        # Motor queries return a Future, pass them directly to asyncio.gather
        user_task = self._get_target_user(ctx.guild)
        lock_doc_task = self.locks_collection.find_one({"_id": ctx.channel.id})

        user, lock_doc = await asyncio.gather(user_task, lock_doc_task)

        # Database Permission Check
        if lock_doc:
            allowed = lock_doc.get("allowed_users")
            if allowed is not None and ctx.author.id not in allowed and not ctx.author.guild_permissions.administrator:
                return await ctx.reply("⚠️ Only the user(s) who triggered this lock can unlock this channel.")
            
            # Non-blocking deletion using asyncio.ensure_future for Motor futures
            asyncio.ensure_future(self.locks_collection.delete_one({"_id": ctx.channel.id}))

        permissions = discord.PermissionOverwrite(read_messages=True, send_messages=True)
        await ctx.channel.set_permissions(user, overwrite=permissions)
        await ctx.reply(f"🔓 **{ctx.channel.mention}** was unlocked by {ctx.author.mention}")

    @commands.hybrid_command(aliases=["l"], name="lock", description="Locks the current channel.")
    async def lock(self, ctx):
        permissions = discord.PermissionOverwrite(read_messages=False, send_messages=False)
        
        # Run user fetch and Mongo upsert concurrently
        user_task = self._get_target_user(ctx.guild)
        db_task = self.locks_collection.update_one(
            {"_id": ctx.channel.id},
            {"$set": {"allowed_users": [ctx.author.id], "guild_id": ctx.guild.id}},
            upsert=True
        )

        user, _ = await asyncio.gather(user_task, db_task)
        await ctx.channel.set_permissions(user, overwrite=permissions)
        
        embed = discord.Embed(
            description=f"🔒 Channel locked by {ctx.author.mention}",
            color=discord.Color.red()
        )
        await ctx.reply(embed=embed)

    @commands.has_permissions(administrator=True)
    @commands.command(aliases=["uac"], description="Unlocks all channels in the server.")
    async def unlockallchannels(self, ctx):
        permissions = discord.PermissionOverwrite(read_messages=True, send_messages=True)
        
        # Trigger DB cleanup immediately without blocking
        asyncio.ensure_future(self.locks_collection.delete_many({"guild_id": ctx.guild.id}))
        
        user = await self._get_target_user(ctx.guild)
        msg = await ctx.send("🔓 Unlocking channels in batches...")
        
        target_channels = [
            ch for ch in ctx.guild.channels 
            if isinstance(ch, (discord.TextChannel, discord.VoiceChannel, discord.ForumChannel))
            and user in ch.overwrites
        ]

        count = 0
        
        async def unlock_single(channel):
            nonlocal count
            try:
                await channel.set_permissions(user, overwrite=permissions)
                count += 1
            except discord.HTTPException:
                pass

        # Concurrent batches of 5 to respect Discord rate limits
        chunk_size = 5
        for i in range(0, len(target_channels), chunk_size):
            chunk = target_channels[i:i + chunk_size]
            await asyncio.gather(*(unlock_single(ch) for ch in chunk))
            await asyncio.sleep(0.3)

        embed = discord.Embed(
            description=f"🔓 **{count} channels** unlocked by {ctx.author.mention}",
            color=discord.Color.green()
        )
        
        await msg.delete()
        await ctx.reply(embed=embed)

    @commands.hybrid_command(name="lockstats", aliases=["ls"], description="Shows all locked and unlocked channels.")
    async def lockstats(self, ctx):
        user = await self._get_target_user(ctx.guild)

        locked = []
        unlocked = []

        for channel in ctx.guild.text_channels:
            overwrite = channel.overwrites_for(user)
            if overwrite.send_messages is False:
                locked.append(channel)
            else:
                unlocked.append(channel)

        embed = discord.Embed(title="📊 Channel Status Overview", color=discord.Color.blue())

        def add_channel_fields(title, channel_list):
            if not channel_list:
                embed.add_field(name=f"{title} (0)", value="None", inline=False)
                return

            mentions = [c.mention for c in channel_list]
            chunks = []
            current_chunk = []
            current_length = 0

            for mention in mentions:
                if current_length + len(mention) + 1 > 950:
                    chunks.append(" ".join(current_chunk))
                    current_chunk = [mention]
                    current_length = len(mention)
                else:
                    current_chunk.append(mention)
                    current_length += len(mention) + 1

            if current_chunk:
                chunks.append(" ".join(current_chunk))

            for i, chunk in enumerate(chunks, start=1):
                field_name = f"{title} ({len(channel_list)})" if len(chunks) == 1 else f"{title} ({len(channel_list)}) - Part {i}"
                embed.add_field(name=field_name, value=chunk, inline=False)

        add_channel_fields("🔒 Locked Channels", locked)
        add_channel_fields("🔓 Unlocked Channels", unlocked)

        await ctx.reply(embed=embed)

async def setup(bot):
    await bot.add_cog(LockUnlock(bot))