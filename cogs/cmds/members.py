import asyncio
import re
import typing
import discord
from discord import app_commands
from discord.ext import commands

# Import ConfirmView from views/common.py
from views.common import ConfirmView

class Members(commands.Cog):
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

    def parse_time(self, time_str: str) -> int:
        match = re.match(r"^(\d+)([smhd])$", time_str.lower())
        if not match:
            return None
        amount, unit = int(match.group(1)), match.group(2)
        multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        return amount * multipliers[unit]

    async def get_or_create_muted_role(self, guild: discord.Guild) -> discord.Role:
        role = discord.utils.get(guild.roles, name="muted")
        if not role:
            role = discord.utils.get(guild.roles, name="Muted")
        
        if not role:
            try:
                role = await guild.create_role(name="muted", reason="Created automatically for server mutes.")
                for channel in guild.channels:
                    await channel.set_permissions(role, send_messages=False, add_reactions=False)
            except discord.HTTPException:
                return None
        return role

    @commands.hybrid_command(name="kick", with_app_command=True, description="Removes a target user from the server.")
    @app_commands.describe(
        user="The server member to kick.",
        reason="The reason behind removing this user."
    )
    @commands.has_permissions(kick_members=True)
    async def kick(
        self, 
        ctx, 
        user: discord.Member, 
        *, 
        reason: str = "No reason provided"
    ):
        if user.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            embed = discord.Embed(title="❌ Action Denied", description="You cannot kick someone with a role higher than or equal to yours.", color=discord.Color.red())
            return await ctx.send(embed=embed)

        dm_embed = discord.Embed(title=f"You have been kicked from {ctx.guild.name}", color=discord.Color.orange())
        dm_embed.add_field(name="Reason", value=reason)

        try:
            await user.send(embed=dm_embed)
        except discord.HTTPException:
            pass

        await user.kick(reason=reason)
        
        success_embed = discord.Embed(
            title="👢 Member Kicked",
            description=f"**{user.name}** has been kicked from the server.",
            color=discord.Color.orange()
        )
        success_embed.add_field(name="Reason", value=reason, inline=False)
        await ctx.send(embed=success_embed)

    @commands.hybrid_command(name="ban", with_app_command=True, description="Bans a member from the server permanently.")
    @app_commands.describe(
        user="The server member to ban.",
        reason="The reason behind banning this user."
    )
    @commands.has_permissions(ban_members=True)
    async def ban(
        self, 
        ctx, 
        user: discord.Member, 
        *, 
        reason: str = "No reason provided"
    ):
        if user.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            embed = discord.Embed(title="❌ Action Denied", description="You cannot ban someone with a role higher than or equal to yours.", color=discord.Color.red())
            return await ctx.send(embed=embed)

        confirmed = await self.confirm_action(ctx, f"Are you sure you want to ban **{user}**? This cannot be undone easily.")
        if not confirmed:
            return

        dm_embed = discord.Embed(title=f"You have been banned from {ctx.guild.name}", color=discord.Color.red())
        dm_embed.add_field(name="Reason", value=reason)

        try:
            await user.send(embed=dm_embed)
        except discord.HTTPException:
            pass

        await user.ban(reason=reason)
        
        success_embed = discord.Embed(
            title="🔨 Member Banned",
            description=f"**{user.name}** has been banned from the server.",
            color=discord.Color.red()
        )
        success_embed.add_field(name="Reason", value=reason, inline=False)
        await ctx.send(embed=success_embed)

    @commands.hybrid_command(name="nick", with_app_command=True, description="Changes the nickname of a target server member.")
    @app_commands.describe(
        user="The server member whose nickname to change.",
        new_name="The new nickname string (leave blank/none to reset)."
    )
    @commands.has_permissions(manage_nicknames=True)
    @commands.bot_has_permissions(manage_nicknames=True)
    async def nick(
        self, 
        ctx, 
        user: discord.Member, 
        *, 
        new_name: str
    ):
        if user.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            embed = discord.Embed(title="❌ Action Denied", description="You cannot change the nickname of someone with a role higher than or equal to yours.", color=discord.Color.red())
            return await ctx.send(embed=embed)
            
        if user.top_role >= ctx.guild.me.top_role:
            embed = discord.Embed(title="❌ Action Denied", description="I cannot change this user's nickname because their top role is higher than or equal to mine.", color=discord.Color.red())
            return await ctx.send(embed=embed)

        old_name = user.display_name
        try:
            target_nick = None if new_name.lower() in ["none", "reset", ""] else new_name
            await user.edit(nick=target_nick, reason=f"Changed by {ctx.author} ({ctx.author.id})")
            
            if target_nick is None:
                embed = discord.Embed(
                    title="✏️ Nickname Reset",
                    description=f"Successfully reset the nickname for **{user.name}** (was `{old_name}`).",
                    color=discord.Color.blue()
                )
            else:
                embed = discord.Embed(
                    title="✏️ Nickname Changed",
                    description=f"Successfully changed nickname for **{user.name}** from `{old_name}` to `{target_nick}`.",
                    color=discord.Color.blue()
                )
            await ctx.send(embed=embed)
        except discord.Forbidden:
            embed = discord.Embed(title="❌ Error", description="I lack the permissions to change this user's nickname.", color=discord.Color.red())
            await ctx.send(embed=embed)
        except discord.HTTPException as e:
            embed = discord.Embed(title="❌ Error", description=f"Failed to change nickname due to an error: {e}", color=discord.Color.red())
            await ctx.send(embed=embed)

    @commands.hybrid_command(name="mute", with_app_command=True, description="Mutes a user globally using a muted role for a set duration.")
    @app_commands.describe(
        user="The server member to mute.",
        duration="Duration of the mute (e.g. 30s, 10m, 2h, 1d). Leave blank for permanent.",
        reason="The reason for muting."
    )
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def mute(
        self,
        ctx,
        user: discord.Member,
        duration: typing.Optional[str] = None,
        *,
        reason: str = "No reason provided"
    ):
        if user.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            embed = discord.Embed(title="❌ Action Denied", description="You cannot mute someone with a role higher than or equal to yours.", color=discord.Color.red())
            return await ctx.send(embed=embed)

        muted_role = await self.get_or_create_muted_role(ctx.guild)
        if not muted_role:
            embed = discord.Embed(title="❌ Error", description="Could not find or create the `muted` role.", color=discord.Color.red())
            return await ctx.send(embed=embed)

        seconds = self.parse_time(duration) if duration else None
        if duration and seconds is None:
            embed = discord.Embed(title="❌ Invalid Duration", description="Please use a valid time format like `30s`, `10m`, `2h`, or `1d`.", color=discord.Color.red())
            return await ctx.send(embed=embed)

        try:
            await user.add_roles(muted_role, reason=f"Muted by {ctx.author}: {reason}")
        except discord.HTTPException:
            embed = discord.Embed(title="❌ Error", description="Failed to assign the muted role to the user.", color=discord.Color.red())
            return await ctx.send(embed=embed)

        dm_embed = discord.Embed(title=f"You have been muted in {ctx.guild.name}", color=discord.Color.dark_grey())
        dm_embed.add_field(name="Reason", value=reason)
        if duration:
            dm_embed.add_field(name="Duration", value=duration)
        try:
            await user.send(embed=dm_embed)
        except discord.HTTPException:
            pass

        embed = discord.Embed(
            title="🔇 Member Muted",
            description=f"**{user.mention}** has been muted globally.",
            color=discord.Color.dark_grey()
        )
        embed.add_field(name="Reason", value=reason, inline=False)
        if duration:
            embed.add_field(name="Duration", value=duration, inline=False)
        await ctx.send(embed=embed)

        if seconds:
            await asyncio.sleep(seconds)
            if muted_role in user.roles:
                await user.remove_roles(muted_role, reason="Mute duration expired.")
                unmute_dm = discord.Embed(title=f"You have been unmuted in {ctx.guild.name}", color=discord.Color.green())
                try:
                    await user.send(embed=unmute_dm)
                except discord.HTTPException:
                    pass

    @commands.hybrid_command(name="unmute", with_app_command=True, description="Unmutes a user by removing the muted role.")
    @app_commands.describe(
        user="The server member to unmute."
    )
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def unmute(
        self,
        ctx,
        user: discord.Member
    ):
        muted_role = discord.utils.get(ctx.guild.roles, name="muted") or discord.utils.get(ctx.guild.roles, name="Muted")
        
        if not muted_role or muted_role not in user.roles:
            embed = discord.Embed(title="❌ Error", description="This user is not currently muted.", color=discord.Color.red())
            return await ctx.send(embed=embed)

        await user.remove_roles(muted_role, reason=f"Unmuted by {ctx.author}")

        dm_embed = discord.Embed(title=f"You have been unmuted in {ctx.guild.name}", color=discord.Color.green())
        try:
            await user.send(embed=dm_embed)
        except discord.HTTPException:
            pass

        embed = discord.Embed(
            title="🔊 Member Unmuted",
            description=f"**{user.mention}** has been unmuted.",
            color=discord.Color.green()
        )
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Members(bot))