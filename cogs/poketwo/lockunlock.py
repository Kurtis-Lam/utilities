import discord
from discord.ext import commands

class LockUnlock(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(aliases=["u"], name="unlock", description="Unlocks the current channel.")
    async def unlock(self, ctx):
        user_id = 716390085896962058 
        permissions = discord.PermissionOverwrite(
            read_messages=True,
            send_messages=True
        )
        channel = ctx.channel 
        user = await self.bot.fetch_user(user_id)

        await channel.set_permissions(user, overwrite=permissions)
        
        await ctx.reply(f"🔓 **{channel.mention}** was unlocked by {ctx.author.mention}")

    @commands.hybrid_command(aliases=["l"], name="lock", description="Locks the current channel.")
    async def lock(self, ctx):
        user_id = 716390085896962058 
        permissions = discord.PermissionOverwrite(
            read_messages=False,
            send_messages=False
        )
        channel = ctx.channel 
        user = await self.bot.fetch_user(user_id)

        await channel.set_permissions(user, overwrite=permissions)
        
        embed = discord.Embed(
            description=f"🔒 Channel locked by {ctx.author.mention}",
            color=discord.Color.red()
        )
        await ctx.reply(embed=embed)

    @commands.has_permissions(administrator=True)
    @commands.command(aliases=["uac"], description="Unlocks all channels in the server.")
    async def unlockallchannels(self, ctx):
        user_id = 716390085896962058 
        permissions = discord.PermissionOverwrite(
            read_messages=True,
            send_messages=True
        )
        user = await self.bot.fetch_user(user_id)
        
        msg = await ctx.send("Unlocking all channels...\nThis might take some time...")
        for channel in ctx.guild.channels:
            await channel.set_permissions(user, overwrite=permissions)
            
        embed = discord.Embed(
            description=f"🔓 **All channels** unlocked by {ctx.author.mention}",
            color=discord.Color.green()
        )
        
        await msg.delete()
        await ctx.reply(embed=embed)

async def setup(bot):
    await bot.add_cog(LockUnlock(bot))
