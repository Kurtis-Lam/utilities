import asyncio
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
from views.roleviews import RoleChoiceView
from views.selfrole_views import SelfRoleDashboardView, SelfRolePanelView, selfrole_panel_embed
from views.common_views import error_embed as default_error_embed, make_embed as default_make_embed, success_embed as default_success_embed


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


def find_roles(guild: discord.Guild, text: str) -> list[discord.Role]:
    """Find an exact role name first, then all role names containing the query."""
    text = strip_quotes(text)
    if not text:
        return []
    match = _ROLE_REF.fullmatch(text)
    if match:
        role = guild.get_role(int(match.group(1) or match.group(2)))
        return [role] if role is not None and not role.is_default() else []
    wanted = " ".join(text.casefold().split())
    roles = [r for r in guild.roles if not r.is_default()]
    exact = [r for r in roles if " ".join(r.name.casefold().split()) == wanted]
    if exact:
        return sorted(exact, key=lambda role: role.position, reverse=True)
    matches = [r for r in roles if wanted in " ".join(r.name.casefold().split())]
    return sorted(matches, key=lambda role: role.position, reverse=True)


def find_role(guild: discord.Guild, text: str) -> typing.Optional[discord.Role]:
    """Find a role by exact name, mention or ID for commands that require an unambiguous exact lookup."""
    text = strip_quotes(text)
    if not text:
        return None
    match = _ROLE_REF.fullmatch(text)
    if match:
        role = guild.get_role(int(match.group(1) or match.group(2)))
        return role if role is not None and not role.is_default() else None
    wanted = " ".join(text.casefold().split())
    matches = [
        role for role in guild.roles
        if not role.is_default() and " ".join(role.name.casefold().split()) == wanted
    ]
    return max(matches, key=lambda role: role.position) if matches else None


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
        self._selfrole_cache: dict[int, dict] = {}

    # Retrieves MongoDB instance dynamically from main.py's bot.mongo_client
    @property
    def mongo_client(self):
        return self.bot.mongo_client

    @property
    def timed_roles(self):
        """Roles given with a duration: {guild_id, user_id, role_id, ends_at, given_by}."""
        return self.mongo_client["utilities"]["timed_roles"]

    async def cog_load(self):
        if self.mongo_client is not None:
            try:
                await asyncio.gather(
                    self.timed_roles.create_index([("guild_id", 1), ("user_id", 1), ("role_id", 1)], unique=True),
                    self.timed_roles.create_index("ends_at"),
                )
                configs = await self.selfrole_collection.find({}).to_list(length=None)
                for config in configs:
                    guild_id = int(config.get("_id", 0) or 0)
                    message_id = int(config.get("message_id", 0) or 0)
                    mappings = config.get("roles", [])
                    if not guild_id:
                        continue
                    self._selfrole_cache[guild_id] = config
                    if config.get("mode") == "buttons" and message_id and mappings:
                        self.bot.add_view(
                            SelfRolePanelView(self, guild_id, mappings, self.bot.get_guild(guild_id)),
                            message_id=message_id,
                        )
            except Exception as e:
                print(f"Role Cog: MongoDB initialisation failed: {e}")
        if self.mongo_client is not None:
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


    @property
    def selfrole_collection(self):
        return self.mongo_client["utilities"]["selfrole_config"]

    async def get_selfrole_config(self, guild_id: int) -> dict:
        cached = self._selfrole_cache.get(int(guild_id))
        if cached is not None:
            return cached
        defaults = {
            "_id": int(guild_id),
            "channel_id": None,
            "message_id": None,
            "title": "Choose Your Roles",
            "description": "Use the options below to manage your server roles.",
            "mode": "buttons",
            "roles": [],
        }
        config = await self.selfrole_collection.find_one({"_id": int(guild_id)})
        if config is None:
            config = defaults.copy()
            await self.selfrole_collection.update_one({"_id": int(guild_id)}, {"$setOnInsert": config}, upsert=True)
        else:
            missing = {key: value for key, value in defaults.items() if key not in config}
            if missing:
                await self.selfrole_collection.update_one({"_id": int(guild_id)}, {"$set": missing}, upsert=True)
                config.update(missing)
        self._selfrole_cache[int(guild_id)] = config
        return config

    async def save_selfrole_config(self, guild_id: int, values: dict):
        await self.selfrole_collection.update_one({"_id": int(guild_id)}, {"$set": values}, upsert=True)
        config = self._selfrole_cache.get(int(guild_id), {"_id": int(guild_id)})
        config.update(values)
        self._selfrole_cache[int(guild_id)] = config

    async def publish_selfrole_panel(self, guild: discord.Guild, config: dict) -> discord.Message:
        channel_id = int(config.get("channel_id", 0) or 0)
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise ValueError("The configured self-role channel no longer exists.")

        mode = str(config.get("mode", "buttons")).lower()
        mappings = list(config.get("roles", []))
        embed = selfrole_panel_embed(config, guild)
        view = SelfRolePanelView(self, guild.id, mappings, guild) if mode == "buttons" else None

        message = None
        previous_message_id = int(config.get("message_id", 0) or 0)
        previous_channel_id = int(config.get("previous_channel_id") or channel_id)
        if previous_message_id and previous_channel_id == channel_id:
            try:
                message = await channel.fetch_message(previous_message_id)
                await message.edit(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
                try:
                    await message.clear_reactions()
                except discord.HTTPException:
                    pass
            except discord.NotFound:
                message = None
        elif previous_message_id and previous_channel_id != channel_id:
            old_channel = guild.get_channel(previous_channel_id)
            if isinstance(old_channel, discord.TextChannel):
                try:
                    old_message = await old_channel.fetch_message(previous_message_id)
                    old_embed = default_make_embed("Self-Role Panel Moved", "Please use the current self-role panel in the configured channel.")
                    await old_message.edit(embed=old_embed, view=None, allowed_mentions=discord.AllowedMentions.none())
                    try:
                        await old_message.clear_reactions()
                    except discord.HTTPException:
                        pass
                except discord.HTTPException:
                    pass

        if message is None:
            message = await channel.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())

        if mode == "reactions":
            for item in mappings:
                try:
                    await message.add_reaction(item["emoji"])
                except discord.HTTPException as exc:
                    raise ValueError(f"Could not add reaction {item['emoji']}. Check that the emoji is usable by the bot and that it has Add Reactions permission.") from exc

        await self.save_selfrole_config(guild.id, {
            "channel_id": channel.id,
            "message_id": message.id,
            "title": config.get("title", "Choose Your Roles"),
            "description": config.get("description", ""),
            "mode": mode,
            "roles": mappings,
        })
        return message

    async def toggle_selfrole(self, member: discord.Member, role_id: int, *, panel_message_id: int | None = None):
        config = self._selfrole_cache.get(member.guild.id)
        if panel_message_id is not None:
            if not config or int(config.get("message_id", 0) or 0) != int(panel_message_id):
                return default_error_embed("This self-role panel has been replaced. Use the latest panel.")
            if not any(int(item.get("role_id", 0) or 0) == int(role_id) for item in config.get("roles", [])):
                return default_error_embed("That role is no longer part of this self-role panel.")
        role = member.guild.get_role(int(role_id))
        bot_member = member.guild.me
        if role is None or role.is_default() or role.managed:
            return default_error_embed("That self-role is no longer available.")
        if bot_member is None or role >= bot_member.top_role:
            return default_error_embed("I cannot assign or remove that role because it is above my highest role.")
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Self-role panel selection")
                return default_success_embed(f"Removed **{role.name}** from your roles.")
            await member.add_roles(role, reason="Self-role panel selection")
            return default_success_embed(f"Added **{role.name}** to your roles.")
        except discord.Forbidden:
            return default_error_embed("I do not have permission to update that role. Check my role hierarchy and Manage Roles permission.")
        except discord.HTTPException:
            return default_error_embed("Discord could not update your roles. Please try again.")

    async def _handle_selfrole_reaction(self, payload: discord.RawReactionActionEvent, *, adding: bool):
        if payload.guild_id is None or payload.user_id == (self.bot.user.id if self.bot.user else 0):
            return
        config = self._selfrole_cache.get(int(payload.guild_id))
        if not config or config.get("mode") != "reactions" or int(config.get("message_id", 0) or 0) != payload.message_id:
            return
        emoji = str(payload.emoji)
        mapping = next((item for item in config.get("roles", []) if str(item.get("emoji")) == emoji), None)
        if mapping is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        member = payload.member if adding else guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except discord.HTTPException:
                return
        if member.bot:
            return
        role = guild.get_role(int(mapping.get("role_id", 0) or 0))
        bot_member = guild.me
        if role is None or role.is_default() or role.managed or bot_member is None or role >= bot_member.top_role:
            return
        try:
            if adding and role not in member.roles:
                await member.add_roles(role, reason="Self-role reaction panel")
            elif not adding and role in member.roles:
                await member.remove_roles(role, reason="Self-role reaction removed")
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        await self._handle_selfrole_reaction(payload, adding=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        await self._handle_selfrole_reaction(payload, adding=False)

    @commands.hybrid_command(name="selfrole", description="Configure a self-role panel with emoji reactions or buttons.")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def selfrole(self, ctx):
        if self.mongo_client is None:
            return await ctx.reply(embed=err_embed("MongoDB is not configured; self-role settings are unavailable."), mention_author=False)
        config = await self.get_selfrole_config(ctx.guild.id)
        view = SelfRoleDashboardView(self, ctx.guild, ctx.author.id, config)
        if ctx.interaction is not None:
            await ctx.interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)
            view.message = await ctx.interaction.original_response()
        else:
            view.message = await ctx.reply(embed=view.embed(), view=view, mention_author=False)


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

    async def _change_member_role(self, *, guild: discord.Guild, actor: discord.Member, member: discord.Member,
                                  role: discord.Role, adding: bool, duration: str | None = None,
                                  seconds: int | None = None):
        action = "assign" if adding else "remove"
        bot_member = guild.me
        if bot_member is None or role >= bot_member.top_role:
            return err_embed("Action Denied", f"I cannot {action} a role that is higher than or equal to my highest role.")
        if role >= actor.top_role and actor != guild.owner:
            return err_embed("Action Denied", f"You cannot {action} a role higher than or equal to your own highest role.")

        try:
            if adding:
                await member.add_roles(role, reason=f"Given by {actor}" + (f" for {duration}" if duration else ""))
                key = {"guild_id": guild.id, "user_id": member.id, "role_id": role.id}
                if seconds:
                    ends_at = time.time() + seconds
                    await self.timed_roles.update_one(
                        key,
                        {"$set": {"ends_at": ends_at, "given_by": actor.id}},
                        upsert=True,
                    )
                    return ok_embed(
                        "Role Given",
                        f"Successfully gave **{role.name}** to {member.mention} for **{duration}**.\n"
                        f"It will be removed <t:{int(ends_at)}:R>.",
                        emoji="⏳",
                    )
                await self.timed_roles.delete_one(key)
                return ok_embed("Role Given", f"Successfully gave **{role.name}** to {member.mention}.")

            await member.remove_roles(role, reason=f"Removed by {actor}")
            await self.timed_roles.delete_one({"guild_id": guild.id, "user_id": member.id, "role_id": role.id})
            return ok_embed("Role Removed", f"Successfully removed **{role.name}** from {member.mention}.")
        except discord.Forbidden:
            return err_embed("Permission Denied", "I could not update that role. Check my role hierarchy and Manage Roles permission.")
        except discord.HTTPException:
            return err_embed("Role Update Failed", "Discord could not update the member's roles. Please try again.")

    async def _resolve_role_action(self, ctx, member: discord.Member, role_text: str, *, adding: bool,
                                   duration: str | None = None, seconds: int | None = None):
        matches = find_roles(ctx.guild, role_text)
        if not matches:
            raise commands.RoleNotFound(strip_quotes(role_text))

        async def apply(interaction: discord.Interaction, selected_role: discord.Role):
            return await self._change_member_role(
                guild=ctx.guild,
                actor=ctx.author,
                member=member,
                role=selected_role,
                adding=adding,
                duration=duration,
                seconds=seconds,
            )

        if len(matches) > 1:
            view = RoleChoiceView(author_id=ctx.author.id, roles=matches, query=role_text, action_callback=apply)
            view.message = await ctx.reply(embed=view.prompt_embed(), view=view, mention_author=False)
            return

        embed = await self._change_member_role(
            guild=ctx.guild,
            actor=ctx.author,
            member=member,
            role=matches[0],
            adding=adding,
            duration=duration,
            seconds=seconds,
        )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_command(
        name="giverole",
        aliases=["gr"],
        description="Assigns a role to a server member, optionally for a limited time.",
        with_app_command=True,
    )
    @app_commands.describe(
        member="The server member to give the role to.",
        role="The role to assign (exact name preferred, otherwise a partial name).",
        duration="Optional time before the role is removed again (e.g. 30m, 2h, 1d, 1d12h).",
    )
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def giverole(
        self,
        ctx,
        member: discord.Member = commands.parameter(description="The member to give the role to (mention or ID)."),
        role: str = commands.parameter(description="The role name, mention or ID."),
        duration: typing.Optional[str] = commands.parameter(default=None, description="How long they keep it, e.g. 30m, 2h, 1d, 1d12h. Leave blank for permanent."),
    ):
        role_text = role
        if ctx.interaction is None:
            rest = after_first_arg(raw_args(ctx))
            role_text = rest
            duration = None
            role_matches = find_roles(ctx.guild, role_text)
            if not role_matches:
                possible_role, _, possible_duration = rest.rpartition(" ")
                if possible_role and parse_duration(possible_duration) is not None:
                    role_matches = find_roles(ctx.guild, possible_role)
                    if role_matches:
                        role_text = possible_role
                        duration = possible_duration
        if duration:
            seconds = parse_duration(duration)
            if seconds is None:
                return await ctx.reply(embed=err_embed("Invalid Duration", "Use a format like `30m`, `2h`, `1d` or `1d12h` (units: s, m, h, d, w)."), mention_author=False)
            if seconds > MAX_ROLE_SECONDS:
                return await ctx.reply(embed=err_embed("Too Long", "A timed role can't last longer than 365 days."), mention_author=False)
        else:
            seconds = None
        await self._resolve_role_action(ctx, member, role_text, adding=True, duration=duration, seconds=seconds)

    @commands.hybrid_command(
        name="removerole",
        aliases=["rr"],
        description="Removes a role from a server member.",
        with_app_command=True,
    )
    @app_commands.describe(
        member="The server member to remove the role from.",
        role="The role to remove (exact name preferred, otherwise a partial name).",
    )
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def removerole(
        self,
        ctx,
        member: discord.Member = commands.parameter(description="The member to remove the role from (mention or ID)."),
        *,
        role: str = commands.parameter(description="The role name, mention or ID."),
    ):
        role_text = role
        if ctx.interaction is None:
            role_text = after_first_arg(raw_args(ctx))
        await self._resolve_role_action(ctx, member, role_text, adding=False)

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