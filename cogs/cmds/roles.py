import re
import typing
import discord
from discord import app_commands
from discord.ext import commands

# Import ConfirmView from views/common.py
from views.common import ConfirmView
from views.embeds import ok_embed, err_embed, handle_common_error, send_usage


class RoleCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_command_error(self, ctx, error):
        if not await handle_common_error(ctx, error):
            raise error

    async def confirm_action(self, ctx, prompt: str) -> bool:
        view = ConfirmView(ctx.author)
        embed = discord.Embed(
            title="⚠️ Confirm",
            description=prompt,
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
            cancel_embed = discord.Embed(title="❌ Cancelled", color=discord.Color.red())
            try:
                await msg.edit(embed=cancel_embed, view=None)
            except discord.HTTPException:
                pass
            return False
        else:
            timeout_embed = discord.Embed(title="⏱️ Timed out", color=discord.Color.red())
            try:
                await msg.edit(embed=timeout_embed, view=None)
            except discord.HTTPException:
                pass
            return False

    @commands.hybrid_command(
        name="giverole", 
        aliases=["gr"], 
        description="Assigns a role to a server member.",
        with_app_command=True
    )
    @app_commands.describe(
        member="The server member to give the role to.",
        role="The role to assign."
    )
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def giverole(self, ctx, member: discord.Member, role: discord.Role):
        if role >= ctx.guild.me.top_role:
            return await ctx.send(embed=err_embed("Action Denied", "I cannot assign a role that is higher than or equal to my highest role."))
        if role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send(embed=err_embed("Action Denied", "You cannot assign a role higher than or equal to your own highest role."))
        
        await member.add_roles(role)
        await ctx.send(embed=ok_embed("Role Given", f"Successfully gave **{role.name}** to {member.mention}."))

    @commands.hybrid_command(
        name="removerole", 
        aliases=["rr"], 
        description="Removes a role from a server member.",
        with_app_command=True
    )
    @app_commands.describe(
        member="The server member to remove the role from.",
        role="The role to remove."
    )
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def removerole(self, ctx, member: discord.Member, role: discord.Role):
        if role >= ctx.guild.me.top_role:
            return await ctx.send(embed=err_embed("Action Denied", "I cannot remove a role that is higher than or equal to my highest role."))
        if role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send(embed=err_embed("Action Denied", "You cannot remove a role higher than or equal to your own highest role."))
        
        await member.remove_roles(role)
        await ctx.send(embed=ok_embed("Role Removed", f"Successfully removed **{role.name}** from {member.mention}."))

    @commands.command(
        name="setperms", 
        description="Set permissions for multiple roles/members (admin, member, or blocked)."
    )
    @commands.has_permissions(administrator=True)
    async def setperms(self, ctx, level: str, targets: commands.Greedy[discord.Role | discord.Member]):
        level = level.lower()
        if level not in ["admin", "member", "blocked"]:
            return await send_usage(ctx, note="Invalid level! Please choose **admin**, **member**, or **blocked**.")

        if not targets:
            return await send_usage(ctx, note="Please specify at least one role or member.")

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

        destination = ctx.channel.category if ctx.channel.category is not None else ctx.channel
        dest_type = "category" if ctx.channel.category is not None else "channel"

        if level != "blocked":
            await destination.set_permissions(ctx.guild.default_role, view_channel=False)

        target_names = []
        for target in targets:
            await destination.set_permissions(target, overwrite=perms)
            target_names.append(target.name)

        names_str = ", ".join(f"**{name}**" for name in target_names)
        embed = ok_embed(
            "Permissions Updated",
            f"Set {dest_type} **{destination.name}** and granted **{level_desc}** permissions to: {names_str}.",
            emoji="🔐"
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="createrole", 
        aliases=["cr"], 
        description="Creates a new role with optional color, permissions, and initial member assignments.",
        with_app_command=True
    )
    @app_commands.describe(
        name="The name of the new role (use quotes for multi-word names in text commands).",
        color_input="Color name (e.g. red, light-blue), hex code (#FF0000), or default.",
        level="Role permission level: admin, basic, or none.",
        targets="Members to immediately assign the role to (optional)."
    )
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def createrole(
        self, 
        ctx, 
        name: str = commands.parameter(description="The name of the new role."), 
        color_input: typing.Optional[str] = commands.parameter(default="default", description="Color name or hex code."),
        level: typing.Optional[typing.Literal["admin", "basic", "none"]] = commands.parameter(default="none", description="Permission level: admin, basic, or none."),
        targets: commands.Greedy[discord.Member] = commands.parameter(default=None, description="Members to assign the role to.")
    ):
        assigned_members = []
        if isinstance(targets, list):
            assigned_members.extend(targets)
        elif isinstance(targets, discord.Member):
            assigned_members.append(targets)

        # Handle optional positional shifting for prefix commands if user skips color or level
        if ctx.interaction is None:
            # Case 1: User skipped color and passed level directly (e.g. .cr "Role" admin)
            if color_input and color_input.lower() in ["admin", "basic", "none"] and level == "none":
                level = color_input.lower()
                color_input = "default"

            # Case 2: User skipped color/level and passed a member mention directly (e.g. .cr "Role" @User)
            elif color_input and (color_input.startswith("<@") or color_input.isdigit()):
                try:
                    member_id = int(re.sub(r"\D", "", color_input))
                    member = ctx.guild.get_member(member_id)
                    if member:
                        assigned_members.append(member)
                        color_input = "default"
                        level = "none"
                except ValueError:
                    pass

            # Case 3: User provided color, skipped level, passed a member in level position (e.g. .cr "Role" red @User)
            if level and (level.startswith("<@") or level.isdigit()):
                try:
                    member_id = int(re.sub(r"\D", "", level))
                    member = ctx.guild.get_member(member_id)
                    if member:
                        assigned_members.append(member)
                        level = "none"
                except ValueError:
                    pass

        # Color Parsing
        color_map = {
            "red": discord.Color.red(),
            "orange": discord.Color.orange(),
            "yellow": discord.Color.gold(),
            "green": discord.Color.green(),
            "blue": discord.Color.blue(),
            "purple": discord.Color.purple(),
            "pink": discord.Color.magenta(),
            "black": discord.Color.from_rgb(1, 1, 1),
            "white": discord.Color.from_rgb(255, 255, 255),
            "light red": discord.Color.from_rgb(255, 100, 100),
            "light blue": discord.Color.from_rgb(100, 149, 237),
            "light green": discord.Color.from_rgb(144, 238, 144),
            "light orange": discord.Color.from_rgb(255, 165, 0),
            "light purple": discord.Color.from_rgb(216, 191, 216),
            "light yellow": discord.Color.from_rgb(255, 255, 224),
            "light pink": discord.Color.from_rgb(255, 182, 193),
            "grey": discord.Color.greyple(),
            "gray": discord.Color.greyple(),
            "default": discord.Color.default(),
            "none": discord.Color.default()
        }

        color_input_clean = color_input.strip().lower().replace("-", " ") if color_input else "default"
        role_color = discord.Color.default()

        if color_input_clean in color_map:
            role_color = color_map[color_input_clean]
        elif color_input_clean.startswith("#") or len(color_input_clean) == 6:
            try:
                hex_val = int(color_input_clean.lstrip("#"), 16)
                role_color = discord.Color(hex_val)
            except ValueError:
                return await ctx.send(embed=err_embed("Invalid Color", "Invalid hex color code provided. Example: `#FF0000`."))
        elif color_input_clean not in ["default", "none"]:
            return await ctx.send(embed=err_embed(
                "Invalid Color",
                "Choose a standard color (e.g. `light-blue`, `red`), `default`, or a hex code (e.g. `#FF0000`)."
            ))

        # Permission Level Parsing
        level_clean = level.strip().lower() if level else "none"
        if level_clean == "admin":
            perms = discord.Permissions(administrator=True)
        elif level_clean == "basic":
            perms = discord.Permissions(send_messages=True, view_channel=True)
        elif level_clean == "none":
            perms = discord.Permissions.none()
        else:
            return await ctx.send(embed=err_embed("Invalid Level", "Please choose **admin**, **basic**, or **none**."))

        # Create Role
        try:
            role = await ctx.guild.create_role(
                name=name, 
                color=role_color, 
                permissions=perms,
                reason=f"Created by {ctx.author}"
            )
        except discord.HTTPException:
            return await ctx.send(embed=err_embed("Role Creation Failed", "Failed to create the role. Check my permissions."))

        # Assign Role to Targets
        assigned_count = 0
        if assigned_members:
            unique_members = list(dict.fromkeys(assigned_members))
            for member in unique_members:
                try:
                    await member.add_roles(role, reason=f"Role auto-assigned during creation by {ctx.author}")
                    assigned_count += 1
                except discord.HTTPException:
                    pass

        # Send Success Response
        color_display = color_input if color_input_clean not in ["default", "none"] else "default (gray)"
        embed = ok_embed(
            "Role Created",
            f"Successfully created role **{role.name}**.",
            emoji="🎨"
        )
        embed.add_field(name="🎨 Color", value=f"`{color_display}`", inline=True)
        embed.add_field(name="🔐 Permissions", value=f"`{level_clean}`", inline=True)
        if assigned_count > 0:
            embed.add_field(name="👥 Assigned To", value=f"{assigned_count} member(s)", inline=True)
            
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="deleterole", 
        aliases=["dr"], 
        description="Deletes a role from the server by mention or ID.",
        with_app_command=True
    )
    @app_commands.describe(
        role="The role to delete (mention or ID)."
    )
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def deleterole(self, ctx, role: discord.Role):
        if role >= ctx.guild.me.top_role:
            return await ctx.send(embed=err_embed("Action Denied", "I cannot delete a role that is higher than or equal to my highest role."))
        if role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send(embed=err_embed("Action Denied", "You cannot delete a role higher than or equal to your own highest role."))

        role_name = role.name
        
        # Trigger Confirmation
        confirmed = await self.confirm_action(ctx, f"Delete role **{role_name}**?")
        if not confirmed:
            return

        try:
            await role.delete(reason=f"Deleted by {ctx.author}")
            await ctx.send(embed=ok_embed("Role Deleted", f"Successfully deleted the role **{role_name}**.", emoji="🗑️"))
        except discord.HTTPException:
            await ctx.send(embed=err_embed("Delete Failed", "Failed to delete the role. Check my permissions."))


async def setup(bot):
    await bot.add_cog(RoleCog(bot))