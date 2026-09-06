import discord
from discord.ext import commands

class LockUnlock(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.target_user_id = 716390085896962058

    @commands.hybrid_command(aliases=["u"], name="unlock", description="Unlocks the current channel.")
    async def unlock(self, ctx):
        permissions = discord.PermissionOverwrite(
            read_messages=True,
            send_messages=True
        )
        channel = ctx.channel 
        user = await self.bot.fetch_user(self.target_user_id)

        await channel.set_permissions(user, overwrite=permissions)
        
        await ctx.reply(f"🔓 **{channel.mention}** was unlocked by {ctx.author.mention}")

    @commands.hybrid_command(aliases=["l"], name="lock", description="Locks the current channel.")
    async def lock(self, ctx):
        permissions = discord.PermissionOverwrite(
            read_messages=False,
            send_messages=False
        )
        channel = ctx.channel 
        user = await self.bot.fetch_user(self.target_user_id)

        await channel.set_permissions(user, overwrite=permissions)
        
        embed = discord.Embed(
            description=f"🔒 Channel locked by {ctx.author.mention}",
            color=discord.Color.red()
        )
        await ctx.reply(embed=embed)

    @commands.has_permissions(administrator=True)
    @commands.command(aliases=["uac"], description="Unlocks all channels in the server.")
    async def unlockallchannels(self, ctx):
        permissions = discord.PermissionOverwrite(
            read_messages=True,
            send_messages=True
        )
        user = await self.bot.fetch_user(self.target_user_id)
        
        msg = await ctx.send("Unlocking all channels...\nThis might take some time...")
        for channel in ctx.guild.channels:
            await channel.set_permissions(user, overwrite=permissions)
            
        embed = discord.Embed(
            description=f"🔓 **All channels** unlocked by {ctx.author.mention}",
            color=discord.Color.green()
        )
        
        await msg.delete()
        await ctx.reply(embed=embed)

    @commands.hybrid_command(name="stats", description="Shows all locked and unlocked channels.")
    async def stats(self, ctx):
        user = ctx.guild.get_member(self.target_user_id) or await self.bot.fetch_user(self.target_user_id)

        locked = []
        unlocked = []

        # Check permissions for text channels in the guild
        for channel in ctx.guild.text_channels:
            overwrite = channel.overwrites_for(user)
            if overwrite.send_messages is False:
                locked.append(channel)
            else:
                unlocked.append(channel)

        embed = discord.Embed(
            title="📊 Channel Status Overview",
            color=discord.Color.blue()
        )

        def add_channel_fields(title, channel_list):
            if not channel_list:
                embed.add_field(name=f"{title} (0)", value="None", inline=False)
                return

            chunks = []
            current_chunk = ""

            for channel in channel_list:
                mention = f"{channel.mention} "
                # Ensure value stays well below the 1024-character field limit
                if len(current_chunk) + len(mention) > 1000:
                    chunks.append(current_chunk.strip())
                    current_chunk = mention
                else:
                    current_chunk += mention

            if current_chunk:
                chunks.append(current_chunk.strip())

            for i, chunk in enumerate(chunks, start=1):
                field_name = f"{title} ({len(channel_list)})" if len(chunks) == 1 else f"{title} ({len(channel_list)}) - Part {i}"
                embed.add_field(name=field_name, value=chunk, inline=False)

        # Add locked and unlocked fields dynamically
        add_channel_fields("🔴 Locked Channels", locked)
        add_channel_fields("🟢 Unlocked Channels", unlocked)

        await ctx.reply(embed=embed)

async def setup(bot):
    await bot.add_cog(LockUnlock(bot))