import asyncio
import re
import typing
import discord
from discord import app_commands
from discord.ext import commands

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
        
    @commands.hybrid_command(name="kick", with_app_command=True, description="Removes a target user from the server while checking structural hierarchy safety.")
    @commands.has_permissions(kick_members=True)
    async def kick(
        self, 
        ctx, 
        user: discord.Member = commands.parameter(description="The server member to kick."), 
        *, 
        reason: str = commands.parameter(default="No reason provided", description="The reason behind removing this user.")
    ):
        if user.top_role >= ctx.author.top_role:
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

    @commands.hybrid_command(name="ban", with_app_command=True, description="Bans a member from the server permanently unless explicitly unbanned.")
    @commands.has_permissions(ban_members=True)
    async def ban(
        self, 
        ctx, 
        user: discord.Member = commands.parameter(description="The server member to ban."), 
        *, 
        reason: str = commands.parameter(default="No reason provided", description="The reason behind banning this user.")
    ):
        if user.top_role >= ctx.author.top_role:
            embed = discord.Embed(title="❌ Action Denied", description="You cannot ban someone with a role higher than or equal to yours.", color=discord.Color.red())
            return await ctx.send(embed=embed)

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
    @commands.has_permissions(manage_nicknames=True)
    @commands.bot_has_permissions(manage_nicknames=True)
    async def nick(
        self, 
        ctx, 
        user: discord.Member = commands.parameter(description="The server member whose nickname to change."), 
        *, 
        new_name: str = commands.parameter(description="The new nickname string (leave blank to reset).")
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

    @commands.hybrid_command(name="mute", with_app_command=True, description="Mutes a user by stripping send messages and add reaction permissions in the channel.")
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def mute(
        self,
        ctx,
        user: discord.Member = commands.parameter(description="The server member to mute."),
        channel: typing.Optional[discord.TextChannel] = commands.parameter(default=None, description="The channel to mute them in. Defaults to current channel."),
        *,
        reason: str = commands.parameter(default="No reason provided", description="The reason for muting.")
    ):
        target_channel = channel or ctx.channel

        if user.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            embed = discord.Embed(title="❌ Action Denied", description="You cannot mute someone with a role higher than or equal to yours.", color=discord.Color.red())
            return await ctx.send(embed=embed)

        await target_channel.set_permissions(
            user, 
            send_messages=False, 
            add_reactions=False, 
            reason=f"Muted by {ctx.author}: {reason}"
        )

        embed = discord.Embed(
            title="🔇 Member Muted",
            description=f"**{user.mention}** has been muted in {target_channel.mention}.",
            color=discord.Color.dark_grey()
        )
        embed.add_field(name="Reason", value=reason, inline=False)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="unmute", with_app_command=True, description="Unmutes a user by clearing their channel override.")
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def unmute(
        self,
        ctx,
        user: discord.Member = commands.parameter(description="The server member to unmute."),
        channel: typing.Optional[discord.TextChannel] = commands.parameter(default=None, description="The channel to unmute them in. Defaults to current channel.")
    ):
        target_channel = channel or ctx.channel

        await target_channel.set_permissions(user, overwrite=None, reason=f"Unmuted by {ctx.author}")

        embed = discord.Embed(
            title="🔊 Member Unmuted",
            description=f"**{user.mention}** has been unmuted in {target_channel.mention}.",
            color=discord.Color.green()
        )
        await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(Members(bot))