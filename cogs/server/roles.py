import re
import time
import typing
import discord
from discord import app_commands
from discord.ext import commands, tasks

# Import ConfirmView from views/common.py
from views.common_views import confirm
from views.embeds import ok_embed, err_embed, handle_common_error, send_usage
from views.loading import loading
from views.prompts import raw_args, strip_quotes


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


_ROLE_REF = re.compile(r"<@&(\d+)>|(\d{15,25})")
_FIRST_ARG = re.compile(r"^(?:\"[^\"]*\"|\u201c[^\u201d]*\u201d|'[^']*'|\S+)\s*(.*)$", re.S)
_MEMBER_REF = re.compile(r"<@!?\d+>|\d{15,25}")
_LEVELS = ("admin", "basic", "none")
_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}|(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{6}")
COLOR_MAP = {
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
    "none": discord.Color.default(),
}


def find_role(guild: discord.Guild, text: str) -> typing.Optional[discord.Role]:
    """A role by <@&mention>, ID or exact name (spaces allowed, case-insensitive). @everyone is never returned."""
    text = strip_quotes(text)
    if not text:
        return None
    match = _ROLE_REF.fullmatch(text)
    if match:
        role = guild.get_role(int(match.group(1) or match.group(2)))
        return role if role is not None and not role.is_default() else None
    wanted = " ".join(text.lower().split())
    matches = [r for r in guild.roles if not r.is_default() and " ".join(r.name.lower().split()) == wanted]
    return max(matches, key=lambda r: r.position) if matches else None


def after_first_arg(args: str) -> str:
    """Drops the first (possibly quoted) word of the raw argument text."""
    match = _FIRST_ARG.match(args.strip())
    return match.group(1).strip() if match else ""


async def role_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    guild = interaction.guild
    if guild is None:
        return []
    wanted = current.lower().strip()
    roles = [r for r in reversed(guild.roles) if not r.is_default() and wanted in r.name.lower()]
    return [app_commands.Choice(name=r.name[:100], value=str(r.id)) for r in roles[:25]]


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

    @staticmethod
    def _parse_createrole_args(args: str):
        """Splits `<name> [color] [level] [members...]` into (name, color, level, member refs)."""
        args = args.strip()
        quoted = re.match(r"^([\"'\u201c\u2018])(.+?)[\"'\u201d\u2019](?:\s+(.*))?$", args, re.S)
        if quoted:
            name, tokens, keep_one = quoted.group(2).strip(), (quoted.group(3) or "").split(), 0
        else:
            name, tokens, keep_one = "", args.split(), 1

        members = []
        while len(tokens) > keep_one and _MEMBER_REF.fullmatch(tokens[-1]):
            members.insert(0, tokens.pop())

        level = "none"
        if len(tokens) > keep_one and tokens[-1].lower() in _LEVELS:
            level = tokens.pop().lower()

        color = "default"
        if len(tokens) > keep_one:
            last = tokens[-1].lower().replace("-", " ")
            if len(tokens) > keep_one + 1 and f"{tokens[-2].lower()} {last}" in COLOR_MAP:
                color = f"{tokens[-2].lower()} {last}"
                del tokens[-2:]
            elif last in COLOR_MAP or _HEX_COLOR.fullmatch(tokens[-1]):
                color = tokens.pop()
            elif keep_one == 0 and tokens:
                color = tokens[-1]

        if not quoted:
            name = " ".join(tokens)
        return name, color, level, members

    async def confirm_action(self, ctx, prompt: str) -> bool:
        return await confirm(
            ctx,
            prompt,
            title="⚠️ Confirm",
            cancel_text="❌ Cancelled",
            timeout_text="⏱️ Timed out",
        )

    @commands.hybrid_command(
        name="giverole", 
        aliases=["gr"], 
        description="Assigns a role to a server member, optionally for a limited time.",
        with_app_command=True
    )
    @app_commands.describe(
        member="The server member to give the role to.",
        role="The role to assign (name, mention or ID).",
        duration="Optional time before the role is removed again (e.g. 30m, 2h, 1d, 1d12h)."
    )
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def giverole(
        self,
        ctx,
        member: discord.Member = commands.parameter(description="The member to give the role to (mention or ID)."),
        role: str = commands.parameter(description="The role to give (mention, ID or name, spaces allowed)."),
        duration: typing.Optional[str] = commands.parameter(default=None, description="How long they keep it, e.g. 30m, 2h, 1d, 1d12h. Leave blank for permanent."),
    ):
        if ctx.interaction is None:
            # Prefix command: the role name may contain spaces and may be followed by a duration.
            rest = after_first_arg(raw_args(ctx))
            role_obj = find_role(ctx.guild, rest)
            duration = None
            if role_obj is None:
                role_text, _, last = rest.rpartition(" ")
                if role_text and parse_duration(last) is not None:
                    role_obj = find_role(ctx.guild, role_text)
                    duration = last
            if role_obj is None:
                raise commands.RoleNotFound(strip_quotes(rest))
            role = role_obj
        else:
            role_obj = find_role(ctx.guild, role)
            if role_obj is None:
                raise commands.RoleNotFound(strip_quotes(role))
            role = role_obj

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
        role="The role to remove (name, mention or ID)."
    )
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def removerole(
        self,
        ctx,
        member: discord.Member = commands.parameter(description="The member to remove the role from (mention or ID)."),
        *,
        role: str = commands.parameter(description="The role to remove (mention, ID or name, spaces allowed)."),
    ):
        role_obj = find_role(ctx.guild, role)
        if role_obj is None:
            raise commands.RoleNotFound(strip_quotes(role))
        role = role_obj
        if role >= ctx.guild.me.top_role:
            return await ctx.reply(embed=err_embed("Action Denied", "I cannot remove a role that is higher than or equal to my highest role."), mention_author=False)
        if role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.reply(embed=err_embed("Action Denied", "You cannot remove a role higher than or equal to your own highest role."), mention_author=False)
        
        await member.remove_roles(role)
        await self.timed_roles.delete_one({"guild_id": ctx.guild.id, "user_id": member.id, "role_id": role.id})
        await ctx.reply(embed=ok_embed("Role Removed", f"Successfully removed **{role.name}** from {member.mention}."), mention_author=False)

    @commands.hybrid_command(
        name="setperms",
        description="Set permissions for roles/members (admin, member, or blocked)."
    )
    @app_commands.describe(
        level="Access level to set: admin, member or blocked.",
        targets="A role or member (mention or ID). Text commands accept several, separated by spaces.",
    )
    @app_commands.choices(level=[
        app_commands.Choice(name="admin", value="admin"),
        app_commands.Choice(name="member", value="member"),
        app_commands.Choice(name="blocked", value="blocked"),
    ])
    @commands.has_permissions(administrator=True)
    @loading
    async def setperms(
        self,
        ctx,
        level: str = commands.parameter(description="Access level to set: admin, member or blocked."),
        targets: commands.Greedy[typing.Union[discord.Role, discord.Member]] = commands.parameter(description="One or more roles or members (mentions or IDs), separated by spaces."),
    ):
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
        name="The name of the new role (spaces allowed).",
        color_input="Color name (e.g. red, light-blue), hex code (#FF0000), or default.",
        level="Role permission level: admin, basic, or none.",
        targets="Members to immediately assign the role to (optional)."
    )
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    @loading
    async def createrole(
        self, 
        ctx, 
        name: str = commands.parameter(description="The name of the new role (spaces allowed)."),
        color_input: typing.Optional[str] = commands.parameter(default="default", description="Color name or hex code."),
        level: typing.Optional[typing.Literal["admin", "basic", "none"]] = commands.parameter(default="none", description="Permission level: admin, basic, or none."),
        targets: commands.Greedy[discord.Member] = commands.parameter(default=None, description="Members to assign the role to.")
    ):
        assigned_members = []
        if isinstance(targets, list):
            assigned_members.extend(targets)
        elif isinstance(targets, discord.Member):
            assigned_members.append(targets)

        if ctx.interaction is None:
            # Prefix command: `.cr <name with spaces> [color] [level] [@members...]`. The optional
            # parts are read from the end, so the name is whatever is left. Put the name in quotes
            # when it ends with a color / level word (e.g. `.cr "Dark Red" blue`).
            name, color_input, level, member_refs = self._parse_createrole_args(raw_args(ctx))
            assigned_members = []
            for ref in member_refs:
                member_id = int(re.sub(r"\D", "", ref))
                member = ctx.guild.get_member(member_id)
                if member is None:
                    try:
                        member = await ctx.guild.fetch_member(member_id)
                    except discord.HTTPException:
                        return await ctx.reply(embed=err_embed(f"Couldn't find the member {ref}."), mention_author=False)
                assigned_members.append(member)
            if not name:
                raise commands.MissingRequiredArgument(ctx.command.clean_params["name"])

        color_map = COLOR_MAP
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
        role="The role to delete (name, mention or ID)."
    )
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def deleterole(self, ctx, *, role: str = commands.parameter(description="The role to delete (mention, ID or name, spaces allowed).")):
        role_obj = find_role(ctx.guild, role)
        if role_obj is None:
            raise commands.RoleNotFound(strip_quotes(role))
        role = role_obj
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
            await confirmed.show(ok_embed("Role Deleted", f"Successfully deleted the role **{role_name}**.", emoji="🗑️"))
        except discord.HTTPException:
            await confirmed.show(err_embed("Delete Failed", "Failed to delete the role. Check my permissions."))

    @giverole.autocomplete("role")
    async def giverole_role_autocomplete(self, interaction: discord.Interaction, current: str):
        return await role_autocomplete(interaction, current)

    @removerole.autocomplete("role")
    async def removerole_role_autocomplete(self, interaction: discord.Interaction, current: str):
        return await role_autocomplete(interaction, current)

    @deleterole.autocomplete("role")
    async def deleterole_role_autocomplete(self, interaction: discord.Interaction, current: str):
        return await role_autocomplete(interaction, current)


async def setup(bot):
    await bot.add_cog(Role(bot))