import asyncio
import re
import typing
import discord
from discord import app_commands
from discord.ext import commands

# Import ConfirmView from views/common.py
from views.common import ConfirmView


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
        view = ConfirmView(ctx.author)
        embed = discord.Embed(
            title="⚠️ Confirmation Required",
            description=f"{prompt}\n\nClick a button below to confirm or cancel.",
            color=discord.Color.gold()
        )
        msg = await ctx.send(embed=embed, view=view)
        await view.wait()
        
        if view.value is True:
            try:
                await msg.edit(view=None)
            except discord.HTTPException:
                pass
            return True
        elif view.value is False:
            cancel_embed = discord.Embed(title="❌ Action Cancelled", color=discord.Color.red())
            try:
                await msg.edit(embed=cancel_embed, view=None)
            except discord.HTTPException:
                pass
            return False
        else:
            timeout_embed = discord.Embed(title="⏱️ Timeout", description="You took too long to reply. Action cancelled.", color=discord.Color.red())
            try:
                await msg.edit(embed=timeout_embed, view=None)
            except discord.HTTPException:
                pass
            return False

    @commands.hybrid_command(aliases=["cch"], name="createchannel", description="Creates a new text channel.", with_app_command=True)
    @app_commands.describe(
        name="The name of the new channel.",
        category="The category to create the channel in. Defaults to the current category.",
        preaction="Automatically lock or hide the channel upon creation."
    )
    @commands.has_permissions(manage_channels=True)
    async def createchannel(
        self, 
        ctx, 
        name: str = commands.parameter(description="The name of the new channel."), 
        category: typing.Optional[discord.CategoryChannel] = commands.parameter(default=None, description="The category to create the channel in. Defaults to current category."),
        preaction: typing.Optional[typing.Literal["prelock", "prehide", "both"]] = commands.parameter(default=None, description="Apply prelock, prehide, or both upon creation.")
    ):
        prelock = False
        prehide = False

        # If no category was provided, default to the current channel's category
        if category is None and getattr(ctx.channel, "category", None) is not None:
            category = ctx.channel.category

        if ctx.interaction is None:
            content = ctx.message.content.lower()
            
            # Check and parse flags safely from the raw message content for prefix commands
            if "--prelock" in content:
                prelock = True
            if "--prehide" in content:
                prehide = True
                
            # Remove flags from the parsed name in case they were captured in quotes or text
            name = re.sub(r'(?i)--prelock', '', name).strip()
            name = re.sub(r'(?i)--prehide', '', name).strip()
            
            if not name:
                embed = discord.Embed(title="❌ Error", description="Please provide a name for the channel.", color=discord.Color.red())
                return await ctx.send(embed=embed)
        else:
            if preaction in ("prelock", "both"):
                prelock = True
            if preaction in ("prehide", "both"):
                prehide = True

        overwrites = {}

        if prelock or prehide:
            # If a category is provided or inferred, clone its overwrites so the channel still syncs
            if category:
                overwrites = {target: overwrite for target, overwrite in category.overwrites.items()}
                
            default_overwrite = overwrites.get(ctx.guild.default_role, discord.PermissionOverwrite())
            
            if prelock:
                default_overwrite.send_messages = False
            if prehide:
                default_overwrite.view_channel = False
                
            overwrites[ctx.guild.default_role] = default_overwrite

            # Ensure the bot keeps channel permissions
            if ctx.guild.me not in overwrites:
                overwrites[ctx.guild.me] = discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True)

            new_channel = await ctx.guild.create_text_channel(
                name=name, 
                category=category, 
                overwrites=overwrites, 
                reason=f"Created by {ctx.author}"
            )
        else:
            new_channel = await ctx.guild.create_text_channel(
                name=name, 
                category=category, 
                reason=f"Created by {ctx.author}"
            )
        
        # Format the success message
        status_flags = []
        if prelock: status_flags.append("🔒 Locked")
        if prehide: status_flags.append("🙈 Hidden")
        status_text = f" ({', '.join(status_flags)})" if status_flags else ""

        embed = discord.Embed(
            title="✨ Channel Created",
            description=f"Successfully created channel {new_channel.mention}{status_text}.",
            color=discord.Color.green()
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(aliases=["dch"], name="deletechannel", description="Deletes a text channel.", with_app_command=True)
    @app_commands.describe(
        channel="The channel to delete. Defaults to current channel."
    )
    @commands.has_permissions(manage_channels=True)
    async def deletechannel(
        self, 
        ctx, 
        channel: typing.Optional[discord.TextChannel] = None
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

    @commands.hybrid_command(aliases=["rch"], name="renamechannel", description="Renames a text channel.", with_app_command=True)
    @app_commands.describe(
        new_name="The new name for the channel.",
        channel="The channel to rename. Defaults to the current channel."
    )
    @commands.has_permissions(manage_channels=True)
    async def renamechannel(
        self,
        ctx,
        new_name: str,
        channel: typing.Optional[discord.TextChannel] = None
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

    @commands.hybrid_command(aliases=["lch"], name="lockchannel", description="Locks a text channel for a user or role.", with_app_command=True)
    @app_commands.describe(
        target="The member or role to lock. Defaults to everyone.",
        channel="The channel to lock. Defaults to current channel."
    )
    @commands.has_permissions(manage_channels=True)
    async def lockchannel(
        self, 
        ctx, 
        target: typing.Optional[typing.Union[discord.Member, discord.Role]] = None, 
        channel: typing.Optional[discord.TextChannel] = None
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

    @commands.hybrid_command(aliases=["uch"], name="unlockchannel", description="Unlocks a text channel for a user or role.", with_app_command=True)
    @app_commands.describe(
        target="The member or role to unlock. Defaults to everyone.",
        channel="The channel to unlock. Defaults to current channel."
    )
    @commands.has_permissions(manage_channels=True)
    async def unlockchannel(
        self, 
        ctx, 
        target: typing.Optional[typing.Union[discord.Member, discord.Role]] = None, 
        channel: typing.Optional[discord.TextChannel] = None
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

    @commands.hybrid_command(aliases=["hch"], name="hidechannel", description="Hides a text channel from a user or role.", with_app_command=True)
    @app_commands.describe(
        target="The member or role to hide the channel from. Defaults to everyone.",
        channel="The channel to hide. Defaults to the current channel."
    )
    @commands.has_permissions(manage_channels=True)
    async def hidechannel(
        self, 
        ctx, 
        target: typing.Optional[typing.Union[discord.Member, discord.Role]] = None, 
        channel: typing.Optional[discord.TextChannel] = None
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

    @commands.hybrid_command(aliases=["uhch"], name="unhidechannel", description="Unhides a text channel for a user or role.", with_app_command=True)
    @app_commands.describe(
        target="The member or role to unhide the channel for. Defaults to everyone.",
        channel="The channel to unhide. Defaults to the current channel."
    )
    @commands.has_permissions(manage_channels=True)
    async def unhidechannel(
        self, 
        ctx, 
        target: typing.Optional[typing.Union[discord.Member, discord.Role]] = None, 
        channel: typing.Optional[discord.TextChannel] = None
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

    @commands.hybrid_command(name="nuke", description="Nukes a text channel by cloning and replacing it.", with_app_command=True)
    @app_commands.describe(
        channel="The channel to nuke. Defaults to the current channel."
    )
    @commands.has_permissions(manage_channels=True)
    async def nuke(
        self, 
        ctx, 
        channel: typing.Optional[discord.TextChannel] = None
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

    @commands.hybrid_command(name="sac", aliases=["syncallchannels"], description="Sync all channel permissions with their categories.", with_app_command=True)
    @commands.has_permissions(administrator=True)
    async def sac(self, ctx):
        synced_count = 0
        skipped_count = 0

        await ctx.defer()

        for channel in ctx.guild.channels:
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