import re
import time
import typing
import discord
from discord import app_commands
from discord.ext import commands, tasks

# Import ConfirmView from views/common.py
from views.common import ConfirmView
from views.embeds import ok_embed, err_embed, handle_common_error, send_usage


_DURATION_FULL = re.compile(r"^(?:\d+[smhdw])+$")
_DURATION_PART = re.compile(r"(\d+)([smhdw])")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
MAX_ROLE_SECONDS = 365 * 86400


def parse_duration(text: str) -> typing.Optional[int]:
    """'30m' / '2h' / '1d12h' -> seconds, or None if the format is invalid."""
    text = text.strip().lower()
    if not _DURATION_FULL.match(text):
        return None
    return sum(int(n) * _DURATION_UNITS[u] for n, u in _DURATION_PART.findall(text))


class Role(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # Retrieves MongoDB instance dynamically from main.py's bot.mongo_client
    @property
    def mongo_client(self):
        return self.bot.mongo_client

    @property
    def timed_roles(self):
        """Roles given with a duration: {guild_id, user_id, role_id, ends_at, given_by}."""
        return self.mongo_client["utilities"]["timed_roles"]

    async def cog_load(self):
        try:
            await self.mongo_client.admin.command("ping")
            await self.timed_roles.create_index([("guild_id", 1), ("user_id", 1), ("role_id", 1)], unique=True)
            await self.timed_roles.create_index("ends_at")
        except Exception as e:
            print(f"Role Cog: MongoDB warmup failed: {e}")
        self.check_timed_roles.start()

    def cog_unload(self):
        self.check_timed_roles.cancel()

    @tasks.loop(seconds=15)
    async def check_timed_roles(self):
        """Removes every role whose time is up (also catches ones that expired while the bot was offline)."""
        try:
            due = await self.timed_roles.find({"ends_at": {"$lte": time.time()}}).to_list(length=None)
        except Exception as e:
            print(f"Role Cog: failed to query timed roles: {e}")
            return

        for doc in due:
            try:
                guild = self.bot.get_guild(doc["guild_id"])
                if guild is None:
                    continue  # not available right now; try again next tick

                role = guild.get_role(doc["role_id"])
                member = guild.get_member(doc["user_id"])
                if member is None:
                    try:
                        member = await guild.fetch_member(doc["user_id"])
                    except discord.NotFound:
                        member = None

                if role is not None and member is not None and role in member.roles:
                    try:
                        await member.remove_roles(role, reason="Timed role expired")
                    except discord.Forbidden:
                        print(f"Role Cog: missing permission to remove {role.id} from {member.id}")
                    except discord.HTTPException:
                        continue  # temporary failure; retry next tick

                await self.timed_roles.delete_one({"_id": doc["_id"]})
            except Exception as e:
                print(f"Role Cog: error while expiring timed role {doc.get('_id')}: {e}")

    @check_timed_roles.before_loop
    async def before_check_timed_roles(self):
        await self.bot.wait_until_ready()

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
        msg = await ctx.reply(embed=embed, view=view, mention_author=False)
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
        description="Assigns a role to a server member, optionally for a limited time.",
        with_app_command=True
    )
    @app_commands.describe(
        member="The server member to give the role to.",
        role="The role to assign.",
        duration="Optional time before the role is removed again (e.g. 30m, 2h, 1d, 1d12h)."
    )
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def giverole(self, ctx, member: discord.Member, role: discord.Role, duration: typing.Optional[str] = None):
        if role >= ctx.guild.me.top_role:
            return await ctx.reply(embed=err_embed("Action Denied", "I cannot assign a role that is higher than or equal to my highest role."), mention_author=False)
        if role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.reply(embed=err_embed("Action Denied", "You cannot assign a role higher than or equal to your own highest role."), mention_author=False)

        seconds = None
        if duration:
            seconds = parse_duration(duration)
            if seconds is None:
                return await ctx.reply(embed=err_embed("Invalid Duration", "Use a format like `30m`, `2h`, `1d` or `1d12h` (units: s, m, h, d, w)."), mention_author=False)
            if seconds > MAX_ROLE_SECONDS:
                return await ctx.reply(embed=err_embed("Too Long", "A timed role can't last longer than 365 days."), mention_author=False)

        await member.add_roles(role, reason=f"Given by {ctx.author}" + (f" for {duration}" if duration else ""))

        key = {"guild_id": ctx.guild.id, "user_id": member.id, "role_id": role.id}
        if seconds:
            ends_at = time.time() + seconds
            await self.timed_roles.update_one(
                key,
                {"$set": {"ends_at": ends_at, "given_by": ctx.author.id}},
                upsert=True
            )
            await ctx.reply(embed=ok_embed(
                "Role Given",
                f"Successfully gave **{role.name}** to {member.mention} for **{duration}**.\nIt will be removed <t:{int(ends_at)}:R>.",
                emoji="⏳"
            ), mention_author=False)
        else:
            # Given permanently: cancel any earlier timer for this role.
            await self.timed_roles.delete_one(key)
            await ctx.reply(embed=ok_embed("Role Given", f"Successfully gave **{role.name}** to {member.mention}."), mention_author=False)

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
            return await ctx.reply(embed=err_embed("Action Denied", "I cannot remove a role that is higher than or equal to my highest role."), mention_author=False)
        if role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.reply(embed=err_embed("Action Denied", "You cannot remove a role higher than or equal to your own highest role."), mention_author=False)
        
        await member.remove_roles(role)
        await self.timed_roles.delete_one({"guild_id": ctx.guild.id, "user_id": member.id, "role_id": role.id})
        await ctx.reply(embed=ok_embed("Role Removed", f"Successfully removed **{role.name}** from {member.mention}."), mention_author=False)

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
        await ctx.reply(embed=embed, mention_author=False)

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
                return await ctx.reply(embed=err_embed("Invalid Color", "Invalid hex color code provided. Example: `#FF0000`."), mention_author=False)
        elif color_input_clean not in ["default", "none"]:
            return await ctx.reply(embed=err_embed(
                "Invalid Color",
                "Choose a standard color (e.g. `light-blue`, `red`), `default`, or a hex code (e.g. `#FF0000`)."
            ), mention_author=False)

        # Permission Level Parsing
        level_clean = level.strip().lower() if level else "none"
        if level_clean == "admin":
            perms = discord.Permissions(administrator=True)
        elif level_clean == "basic":
            perms = discord.Permissions(send_messages=True, view_channel=True)
        elif level_clean == "none":
            perms = discord.Permissions.none()
        else:
            return await ctx.reply(embed=err_embed("Invalid Level", "Please choose **admin**, **basic**, or **none**."), mention_author=False)

        # Create Role
        try:
            role = await ctx.guild.create_role(
                name=name, 
                color=role_color, 
                permissions=perms,
                reason=f"Created by {ctx.author}"
            )
        except discord.HTTPException:
            return await ctx.reply(embed=err_embed("Role Creation Failed", "Failed to create the role. Check my permissions."), mention_author=False)

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
            
        await ctx.reply(embed=embed, mention_author=False)

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
            return await ctx.reply(embed=err_embed("Action Denied", "I cannot delete a role that is higher than or equal to my highest role."), mention_author=False)
        if role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.reply(embed=err_embed("Action Denied", "You cannot delete a role higher than or equal to your own highest role."), mention_author=False)

        role_name = role.name
        
        # Trigger Confirmation
        confirmed = await self.confirm_action(ctx, f"Delete role **{role_name}**?")
        if not confirmed:
            return

        try:
            await role.delete(reason=f"Deleted by {ctx.author}")
            await self.timed_roles.delete_many({"guild_id": ctx.guild.id, "role_id": role.id})
            await ctx.reply(embed=ok_embed("Role Deleted", f"Successfully deleted the role **{role_name}**.", emoji="🗑️"), mention_author=False)
        except discord.HTTPException:
            await ctx.reply(embed=err_embed("Delete Failed", "Failed to delete the role. Check my permissions."), mention_author=False)


async def setup(bot):
    await bot.add_cog(Role(bot))