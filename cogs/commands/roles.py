import discord
from discord.ext import commands

class Roles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="giverole", description="Give a role to a member", with_app_command=True)
    @commands.has_permissions(manage_roles=True)
    async def giverole(self, ctx, member: discord.Member, role: discord.Role):
        if role >= ctx.guild.me.top_role:
            return await ctx.send("I cannot assign a role that is higher than or equal to my highest role.")
        
        await member.add_roles(role)
        await ctx.send(f"Successfully gave **{role.name}** to **{member.display_name}**.")

    @commands.hybrid_command(name="removerole", description="Remove a role from a member", with_app_command=True)
    @commands.has_permissions(manage_roles=True)
    async def removerole(self, ctx, member: discord.Member, role: discord.Role):
        if role >= ctx.guild.me.top_role:
            return await ctx.send("I cannot remove a role that is higher than or equal to my highest role.")
        
        await member.remove_roles(role)
        await ctx.send(f"Successfully removed **{role.name}** from **{member.display_name}**.")

    @commands.hybrid_command(name="setperms", description="Set permissions for multiple roles/members (admin, member, or blocked)", with_app_command=True)
    @commands.has_permissions(administrator=True)
    async def setperms(self, ctx, level: str, targets: commands.Greedy[discord.Role | discord.Member]):
        level = level.lower()
        if level not in ["admin", "member", "blocked"]:
            return await ctx.send("Invalid level! Please choose **admin**, **member**, or **blocked**.")

        if not targets:
            return await ctx.send("Please specify at least one role or member.")

        # Determine permissions based on level
        if level == "admin":
            perms = discord.PermissionOverwrite(
                administrator=True,
                view_channel=True,
                send_messages=True
            )
            level_desc = "administrator"
        elif level == "member":
            perms = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True
            )
            level_desc = "view channel and send messages"
        else:  # blocked
            perms = discord.PermissionOverwrite(
                view_channel=False,
                send_messages=False
            )
            level_desc = "blocked (no view/send)"

        # Determine destination (Category or Channel)
        destination = ctx.channel.category if ctx.channel.category is not None else ctx.channel
        dest_type = "category" if ctx.channel.category is not None else "channel"

        # Set @everyone to private if not blocked level, or apply overwrites
        if level != "blocked":
            await destination.set_permissions(ctx.guild.default_role, view_channel=False)

        # Apply permissions for all targets
        target_names = []
        for target in targets:
            await destination.set_permissions(target, overwrite=perms)
            target_names.append(target.name)

        names_str = ", ".join(f"**{name}**" for name in target_names)
        await ctx.send(f"Set {dest_type} **{destination.name}** and granted **{level_desc}** permissions to: {names_str}.")

async def setup(bot):
    await bot.add_cog(Roles(bot))