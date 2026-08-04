import asyncio
import typing
import discord
from discord import app_commands
from discord.ext import commands

class Channels(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            perms = ", ".join([f"`{p}`" for p in error.missing_permissions])
            embed = discord.Embed(
                title="❌ Missing Permissions",
                description=f"You lack the required permissions to use this command: {perms}",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
        elif isinstance(error, commands.BotMissingPermissions):
            perms = ", ".join([f"`{p}`" for p in error.missing_permissions])
            embed = discord.Embed(
                title="❌ Bot Missing Permissions",
                description=f"I am missing permissions to do this. Please give me: {perms}",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
        else:
            raise error

    async def confirm_action(self, ctx, prompt: str) -> bool:
        embed = discord.Embed(
            title="⚠️ Confirmation Required",
            description=f"{prompt}\n\nType `yes` to confirm or `no` to cancel.",
            color=discord.Color.gold()
        )
        await ctx.send(embed=embed)
        
        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel and m.content.lower() in ['yes', 'no']
        
        try:
            msg = await self.bot.wait_for('message', check=check, timeout=30.0)
            if msg.content.lower() == 'yes':
                return True
            else:
                cancel_embed = discord.Embed(title="❌ Action Cancelled", color=discord.Color.red())
                await ctx.send(embed=cancel_embed)
                return False
        except asyncio.TimeoutError:
            timeout_embed = discord.Embed(title="⏱️ Timeout", description="You took too long to reply. Action cancelled.", color=discord.Color.red())
            await ctx.send(embed=timeout_embed)
            return False

    @commands.hybrid_command(aliases=["cch"], name="createchannel", with_app_command=True)
    @commands.has_permissions(manage_channels=True)
    async def createchannel(
        self, 
        ctx, 
        name: str = commands.parameter(description = "The name of the new channel"),
        category: discord.CategoryChannel = commands.parameter(description = "The category to create the channel in. Defaults to none.", default = None)
    ):
        guild = ctx.guild
        new_channel = await guild.create_text_channel(name=name, category=category, reason=f"Created by {ctx.author}")
        
        embed = discord.Embed(
            title="✨ Channel Created",
            description=f"Successfully created channel {new_channel.mention}.",
            color=discord.Color.green()
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(aliases=["dch"], name="deletechannel", with_app_command=True)
    @commands.has_permissions(manage_channels=True)
    async def deletechannel(
        self, 
        ctx, 
        channel: discord.TextChannel = commands.parameter(description = "The channel to delete. Defaults to current channel.", default = None)
    ):
        target = channel or ctx.channel
        confirmed = await self.confirm_action(ctx, f"Are you sure you want to delete {target.mention}? This cannot be undone.")
        if not confirmed:
            return

        await target.delete(reason=f"Deleted by {ctx.author}")
        if target != ctx.channel:
            embed = discord.Embed(
                title="🗑️ Channel Deleted",
                description=f"Successfully deleted `{target.name}`.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)

    @commands.hybrid_command(aliases=["rch"], name="renamechannel", with_app_command=True)
    @commands.has_permissions(manage_channels=True)
    async def renamechannel(
        self,
        ctx,
        new_name: str = commands.parameter(description = "The new name for the channel"),
        channel: discord.TextChannel = commands.parameter(description = "The channel to rename. Defaults to the current channel.", default = None)
    ):
        target_channel = channel or ctx.channel
        old_name = target_channel.name
        
        await target_channel.edit(name=new_name, reason=f"Renamed by {ctx.author}")
        
        embed = discord.Embed(
            title="✏️ Channel Renamed",
            description=f"Successfully renamed {target_channel.mention} from `{old_name}` to `{new_name}`.",
            color=discord.Color.green()
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(aliases=["lch"], name="lockchannel", with_app_command=True)
    @commands.has_permissions(manage_channels=True)
    async def lockchannel(
        self, 
        ctx, 
        target: typing.Optional[typing.Union[discord.Member, discord.Role]] = commands.parameter(default = None, description = "The member or role to lock. Defaults to everyone."),
        channel: discord.TextChannel = commands.parameter(description = "The channel to lock. Defaults to current channel.", default = None)
    ):
        target_channel = channel or ctx.channel
        role_or_member = target or ctx.guild.default_role
        await target_channel.set_permissions(role_or_member, send_messages=False)
        
        embed = discord.Embed(
            title="🔒 Channel Locked",
            description=f"{target_channel.mention} has been locked for {role_or_member.mention}.",
            color=discord.Color.dark_grey()
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(aliases=["uch"], name="unlockchannel", with_app_command=True)
    @commands.has_permissions(manage_channels=True)
    async def unlockchannel(
        self, 
        ctx, 
        target: typing.Optional[typing.Union[discord.Member, discord.Role]] = commands.parameter(default = None, description = "The member or role to unlock. Defaults to everyone."),
        channel: discord.TextChannel = commands.parameter(description = "The channel to unlock. Defaults to current channel.", default = None)
    ):
        target_channel = channel or ctx.channel
        role_or_member = target or ctx.guild.default_role
        await target_channel.set_permissions(role_or_member, send_messages=True)
        
        embed = discord.Embed(
            title="🔓 Channel Unlocked",
            description=f"{target_channel.mention} has been unlocked for {role_or_member.mention}.",
            color=discord.Color.green()
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(aliases=["hch"], name="hidechannel", with_app_command=True)
    @commands.has_permissions(manage_channels=True)
    async def hidechannel(
        self, 
        ctx, 
        target: typing.Optional[typing.Union[discord.Member, discord.Role]] = commands.parameter(default = None, description = "The member or role to hide the channel from. Defaults to everyone."),
        channel: discord.TextChannel = commands.parameter(description = "The channel to hide. Defaults to the current channel", default = None)
    ):
        target_channel = channel or ctx.channel
        role_or_member = target or ctx.guild.default_role
        await target_channel.set_permissions(role_or_member, view_channel=False)
        
        embed = discord.Embed(
            title="🙈 Channel Hidden",
            description=f"{target_channel.mention} is now hidden from {role_or_member.mention}.",
            color=discord.Color.dark_grey()
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(aliases=["uhch"], name="unhidechannel", with_app_command=True)
    @commands.has_permissions(manage_channels=True)
    async def unhidechannel(
        self, 
        ctx, 
        target: typing.Optional[typing.Union[discord.Member, discord.Role]] = commands.parameter(default = None, description = "The member or role to unhide the channel for. Defaults to everyone."),
        channel: discord.TextChannel = commands.parameter(description = "The channel to unhide. Defaults to the current channel.", default = None)
    ):
        target_channel = channel or ctx.channel
        role_or_member = target or ctx.guild.default_role
        await target_channel.set_permissions(role_or_member, view_channel=True)
        
        embed = discord.Embed(
            title="👁️ Channel Unhidden",
            description=f"{target_channel.mention} is now visible to {role_or_member.mention}.",
            color=discord.Color.green()
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="nuke", with_app_command=True)
    @commands.has_permissions(manage_channels=True)
    async def nuke(
        self, 
        ctx, 
        channel: discord.TextChannel = commands.parameter(description = "The channel to nuke. Defaults to the current channel.", default = None)
    ):
        target = channel or ctx.channel
        confirmed = await self.confirm_action(ctx, f"Are you sure you want to nuke {target.mention}? This cannot be undone.")
        if not confirmed: 
            return

        pos = target.position
        category = target.category
        new_channel = await target.clone(reason="Channel Nuked")
        await target.delete()
        await new_channel.edit(position=pos, category=category)
        
        embed = discord.Embed(
            title="💥 Channel Nuked",
            description=f"Nuked by {ctx.author.mention}",
            color=discord.Color.red()
        )
        await new_channel.send(embed=embed)

    @commands.hybrid_command(name="sac", aliases=["syncallchannels"], description="Sync all channel permissions with their categories", with_app_command=True)
    @commands.has_permissions(administrator=True)
    async def sac(self, ctx):
        synced_count = 0
        skipped_count = 0

        # Defer response in case it takes a moment to iterate through channels
        await ctx.defer()

        for channel in ctx.guild.channels:
            # Check if the channel has a category
            if channel.category is not None:
                try:
                    await channel.edit(sync_permissions=True)
                    synced_count += 1
                except discord.HTTPException:
                    skipped_count += 1
            else:
                skipped_count += 1

        await ctx.send(f"Successfully synced **{synced_count}** channels with their categories. Skipped **{skipped_count}** channels.")

async def setup(bot):
    await bot.add_cog(Channels(bot))