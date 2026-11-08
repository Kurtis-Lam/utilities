"""Self-role panels (administrators only).

Commands (hybrid: work as ``.selfrole ...`` and as ``/selfrole ...``):

* ``selfrole``                                       open the dashboard (publish / edit the button or reaction panel)
* ``selfrole add [message] <emoji> <role> ...``      add reaction roles to a message
* ``selfrole remove [message]``                      remove emoji-to-role mappings from a panel message

``[message]`` is a message ID or link. Leave it out and either reply to the target message when running the
command, or the bot asks for the ID (or for a reply to the message with anything) and waits for your answer.
"""
from __future__ import annotations

import re
import unicodedata

import discord
from discord import app_commands
from discord.ext import commands

from cogs.server.roles import find_role
from views.common_views import error_embed as default_error_embed, make_embed as default_make_embed, success_embed as default_success_embed
from views.embeds import err_embed, handle_common_error, info_embed, ok_embed
from views.prompts import ask, raw_args, strip_quotes
from views.selfrole_views import SelfRoleDashboardView, SelfRolePanelView, SelfRoleRemoveView, selfrole_panel_embed

_CUSTOM_EMOJI_TOKEN = re.compile(r"<a?:[A-Za-z0-9_]{2,32}:\d{15,25}>")
_KEYCAP_EMOJI_TOKEN = re.compile(r"[0-9#*]\ufe0f?\u20e3")
_EMOJI_BASE_RANGES = (
    (0x1F000, 0x1FAFF),
    (0x2600, 0x27BF),
    (0x2300, 0x23FF),
    (0x00A9, 0x00A9),
    (0x00AE, 0x00AE),
    (0x203C, 0x203C),
    (0x2049, 0x2049),
    (0x2122, 0x2122),
    (0x2139, 0x2139),
)


_MESSAGE_ID = re.compile(r"\d{15,25}")
_MESSAGE_LINK = re.compile(r"https?://(?:\w+\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)")
ASK_MESSAGE_PROMPT = (
    "Which message is it?\n"
    "Send its **message ID** (or a message link), or **reply to that message** with anything."
)


def message_id_from_text(text: str, guild_id: int | None = None) -> int | None:
    """A message ID or a message link of this server -> the message ID."""
    text = (text or "").strip()
    link = _MESSAGE_LINK.search(text)
    if link:
        if guild_id is not None and int(link.group(1)) != guild_id:
            return None
        return int(link.group(3))
    return int(text) if _MESSAGE_ID.fullmatch(text) else None


def _is_emoji_token(token: str) -> bool:
    """Recognises a single Unicode or custom Discord emoji token in a command string."""
    if _CUSTOM_EMOJI_TOKEN.fullmatch(token) or _KEYCAP_EMOJI_TOKEN.fullmatch(token):
        return True
    if not token:
        return False

    def is_emoji_base(char: str) -> bool:
        codepoint = ord(char)
        return any(start <= codepoint <= end for start, end in _EMOJI_BASE_RANGES)

    if not is_emoji_base(token[0]):
        return False
    for char in token[1:]:
        codepoint = ord(char)
        if (
            is_emoji_base(char)
            or 0x1F3FB <= codepoint <= 0x1F3FF
            or 0x1F1E6 <= codepoint <= 0x1F1FF
            or 0xE0020 <= codepoint <= 0xE007F
            or codepoint in {0x200D, 0x20E3, 0xFE0E, 0xFE0F}
            or unicodedata.combining(char)
        ):
            continue
        return False
    return True


def parse_selfrole_add_args(guild: discord.Guild, raw: str) -> tuple[int, list[dict]]:
    """Parse `<message ID> <emoji> <role name with spaces> [<emoji> <role> ...]`."""
    tokens = raw.split()
    if not tokens or not re.fullmatch(r"\d{15,25}", tokens[0]):
        raise ValueError("Start with the message ID, followed by emoji and role pairs.")

    message_id = int(tokens[0])
    mappings = []
    seen_emojis = set()
    index = 1
    while index < len(tokens):
        emoji_text = tokens[index]
        if not _is_emoji_token(emoji_text):
            raise ValueError(f"`{emoji_text}` is not a recognised emoji. Put an emoji before each role name.")
        index += 1

        role_tokens = []
        while index < len(tokens) and not _is_emoji_token(tokens[index]):
            role_tokens.append(tokens[index])
            index += 1
        role_text = strip_quotes(" ".join(role_tokens))
        if not role_text:
            raise ValueError(f"No role was supplied after `{emoji_text}`.")

        try:
            emoji = discord.PartialEmoji.from_str(emoji_text)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"`{emoji_text}` is not a valid Discord emoji.") from exc
        emoji_value = str(emoji)
        if emoji_value in seen_emojis:
            raise ValueError(f"The emoji `{emoji_value}` appears more than once.")
        role = find_role(guild, role_text)
        if role is None:
            raise ValueError(f"Couldn't find the role `{role_text}`. Role names with spaces are supported.")
        if role.managed or role.is_default():
            raise ValueError(f"The role `{role.name}` cannot be self-assigned.")
        seen_emojis.add(emoji_value)
        mappings.append({"emoji": emoji_value, "role_id": role.id, "role_name": role.name})

    if not mappings:
        raise ValueError("Add at least one emoji and role pair after the message ID.")
    return message_id, mappings


class SelfRole(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._selfrole_cache: dict[int, dict] = {}

    @property
    def mongo_client(self):
        return self.bot.mongo_client

    async def cog_load(self):
        if self.mongo_client is None:
            return
        try:
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
            print(f"SelfRole Cog: MongoDB initialisation failed: {e}")

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

    async def _find_selfrole_message(
        self,
        guild: discord.Guild,
        message_id: int,
        preferred_channel: discord.abc.Messageable | None = None,
    ) -> discord.Message | None:
        """Find a message in this guild, checking the command channel first."""
        channels = []
        if isinstance(preferred_channel, (discord.TextChannel, discord.Thread)):
            channels.append(preferred_channel)
        channels.extend(guild.text_channels)
        channels.extend(getattr(guild, "threads", []))

        seen = set()
        bot_member = guild.me
        for channel in channels:
            if channel.id in seen:
                continue
            seen.add(channel.id)
            if bot_member is not None:
                permissions = channel.permissions_for(bot_member)
                if not permissions.view_channel or not permissions.read_message_history:
                    continue
            try:
                message = await channel.fetch_message(int(message_id))
                if message.guild is not None and message.guild.id == guild.id:
                    return message
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                continue
        return None

    async def remove_selfrole_mapping(self, guild_id: int, emoji: str, role_id: int):
        """Delete one emoji-to-role mapping and synchronise its published panel."""
        config = await self.get_selfrole_config(guild_id)
        roles = list(config.get("roles", []))
        target = next(
            (
                item for item in roles
                if str(item.get("emoji", "")) == str(emoji)
                and int(item.get("role_id", 0) or 0) == int(role_id)
            ),
            None,
        )
        if target is None:
            return default_error_embed("That emoji-to-role mapping has already been removed.")

        remaining = [item for item in roles if item is not target]
        await self.save_selfrole_config(guild_id, {"roles": remaining})

        guild = self.bot.get_guild(int(guild_id))
        if guild is not None:
            channel_id = int(config.get("channel_id", 0) or 0)
            message_id = int(config.get("message_id", 0) or 0)
            channel = guild.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await guild.fetch_channel(channel_id)
                except discord.HTTPException:
                    channel = None
            if message_id and isinstance(channel, (discord.TextChannel, discord.Thread)):
                try:
                    message = await channel.fetch_message(message_id)
                    if str(config.get("mode", "buttons")).lower() == "reactions":
                        try:
                            await message.clear_reaction(discord.PartialEmoji.from_str(emoji))
                        except discord.HTTPException:
                            pass
                    else:
                        panel_config = {**config, "roles": remaining}
                        panel_view = SelfRolePanelView(self, guild.id, remaining, guild) if remaining else None
                        await message.edit(
                            embed=selfrole_panel_embed(panel_config, guild),
                            view=panel_view,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                except discord.HTTPException:
                    pass

        role_name = target.get("role_name") or f"role `{role_id}`"
        return default_success_embed(f"Removed the `{emoji}` self-role mapping for **{role_name}**.")

    async def _ask_message_id(self, ctx, guild: discord.Guild) -> int | None:
        """Ask for the target message. Accepts a message ID / link, or a reply to the message."""
        for _ in range(3):
            answer = await ask(ctx, ASK_MESSAGE_PROMPT, timeout=60)
            if answer is None:
                return None
            reference = answer.reference
            if reference is not None and reference.message_id:
                return reference.message_id
            message_id = message_id_from_text(answer.content, guild.id)
            if message_id is not None:
                return message_id
            await ctx.send(
                embed=err_embed("That isn't a message ID", "Send the message ID or link, or reply to the message."),
                delete_after=8,
            )
        return None

    @staticmethod
    def _replied_message_id(ctx) -> int | None:
        reference = getattr(ctx.message, "reference", None)
        return reference.message_id if reference is not None and reference.message_id else None

    async def _split_message_arg(self, ctx, raw: str) -> tuple[int | None, str]:
        """``[message] rest`` -> (message ID, rest). The message comes from the text, then from a reply,
        and is asked for when neither is there. Returns ``(None, "")`` when the user cancelled."""
        raw = raw.strip()
        first, _, rest = raw.partition(" ")
        message_id = message_id_from_text(first, ctx.guild.id) if first else None
        if message_id is not None:
            return message_id, rest.strip()
        message_id = self._replied_message_id(ctx)
        if message_id is not None:
            return message_id, raw
        message_id = await self._ask_message_id(ctx, ctx.guild)
        return message_id, raw

    async def _add_selfrole_reactions(self, ctx, raw: str):
        message_id, rest = await self._split_message_arg(ctx, raw)
        if message_id is None:
            return
        if not rest:
            answer = await ask(
                ctx,
                "Which emoji and role pairs should I add?\n"
                "Send them like `🎮 Gaming Members ✨ VIP Members` (an emoji, then the role name, repeated).",
                timeout=90,
            )
            if answer is None:
                return
            rest = answer.content.strip()
        try:
            message_id, mappings = parse_selfrole_add_args(ctx.guild, f"{message_id} {rest}")
        except ValueError as exc:
            return await ctx.reply(embed=err_embed("Invalid Self-Role Mapping", str(exc)), mention_author=False)
        message = await self._find_selfrole_message(ctx.guild, message_id, ctx.channel)
        if message is None:
            return await ctx.reply(
                embed=err_embed("Message Not Found", "I couldn't find that message in this server, or I don't have permission to read its channel."),
                mention_author=False,
            )

        bot_member = ctx.guild.me
        if bot_member is None:
            return await ctx.reply(embed=err_embed("Unavailable", "I couldn't verify my server permissions. Try again shortly."), mention_author=False)
        channel_permissions = message.channel.permissions_for(bot_member)
        if not channel_permissions.add_reactions or not channel_permissions.read_message_history:
            return await ctx.reply(
                embed=err_embed("Missing Permissions", "I need **Add Reactions** and **Read Message History** in the channel containing that message."),
                mention_author=False,
            )

        for item in mappings:
            role = ctx.guild.get_role(int(item["role_id"]))
            if role is None:
                return await ctx.reply(embed=err_embed("Role Not Found", f"The role `{item['role_name']}` no longer exists."), mention_author=False)
            if role >= bot_member.top_role:
                return await ctx.reply(embed=err_embed("Role Hierarchy", f"Move my highest role above **{role.name}** before adding it as a self-role."), mention_author=False)

        current = await self.get_selfrole_config(ctx.guild.id)
        current_message_id = int(current.get("message_id", 0) or 0)
        same_message = current_message_id == message.id
        merged = list(current.get("roles", [])) if same_message else []
        by_emoji = {str(item.get("emoji")): item for item in merged}
        existing_emojis = set(by_emoji)
        for item in mappings:
            by_emoji[item["emoji"]] = item
        merged = list(by_emoji.values())
        if len(merged) > 20:
            return await ctx.reply(embed=err_embed("Too Many Mappings", "Reaction-based self-role panels support up to 20 emoji mappings."), mention_author=False)

        added_reactions = []
        try:
            for item in mappings:
                if item["emoji"] in existing_emojis:
                    continue
                await message.add_reaction(item["emoji"])
                added_reactions.append(item["emoji"])
        except discord.HTTPException as exc:
            for added in added_reactions:
                try:
                    await message.remove_reaction(added, bot_member)
                except discord.HTTPException:
                    pass
            return await ctx.reply(
                embed=err_embed("Could Not Add Reaction", f"I couldn't add `{item['emoji']}`. Check Add Reactions and Use External Emojis permissions.\n`{exc}`"),
                mention_author=False,
            )

        if same_message and str(current.get("mode", "buttons")).lower() == "buttons":
            try:
                await message.edit(view=None)
            except discord.HTTPException:
                pass

        await self.save_selfrole_config(ctx.guild.id, {
            "channel_id": message.channel.id,
            "message_id": message.id,
            "title": current.get("title", "Choose Your Roles"),
            "description": current.get("description", "Use the reactions below to manage your roles."),
            "mode": "reactions",
            "roles": merged,
        })
        role_lines = [f"{item['emoji']}  <@&{item['role_id']}>" for item in mappings]
        embed = ok_embed(
            "Self-Roles Added",
            f"Added {len(mappings)} emoji mapping(s) to [the message]({message.jump_url}).\n\n" + "\n".join(role_lines),
            emoji="✅",
        )
        await ctx.reply(embed=embed, mention_author=False)

    async def _open_selfrole_remove(self, ctx, config: dict):
        if not config.get("roles"):
            return await ctx.reply(embed=info_embed("There are no self-role mappings to remove."), mention_author=False)
        view = SelfRoleRemoveView(self, ctx.guild.id, ctx.author.id, config)
        if ctx.interaction is not None:
            if ctx.interaction.response.is_done():
                view.message = await ctx.interaction.followup.send(embed=view.embed(), view=view, ephemeral=True, wait=True)
            else:
                await ctx.interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)
                view.message = await ctx.interaction.original_response()
            return
        try:
            view.message = await ctx.author.send(embed=view.embed(), view=view)
            await ctx.reply(embed=info_embed("I sent you a private self-role removal selector."), mention_author=False, delete_after=10)
        except discord.Forbidden:
            await ctx.reply(
                embed=err_embed("Private Selector Unavailable", "I couldn't DM you. Run `/selfrole` and choose `remove` in the private slash-command response; prefix commands cannot create ephemeral messages."),
                mention_author=False,
            )

    async def _remove_selfrole(self, ctx, raw: str):
        message_id, _ = await self._split_message_arg(ctx, raw)
        if message_id is None:
            return
        config = await self.get_selfrole_config(ctx.guild.id)
        panel_id = int(config.get("message_id", 0) or 0)
        if not panel_id or panel_id != message_id:
            hint = ""
            channel_id = int(config.get("channel_id", 0) or 0)
            if panel_id and channel_id:
                hint = f"\nThe current self-role message is [here](https://discord.com/channels/{ctx.guild.id}/{channel_id}/{panel_id})."
            return await ctx.reply(
                embed=err_embed("Not a Self-Role Message", f"I'm not managing self-roles on that message.{hint}"),
                mention_author=False,
            )
        await self._open_selfrole_remove(ctx, config)

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

    @commands.hybrid_command(name="selfrole", description="Configure, add or remove self-role mappings.")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @commands.bot_has_permissions(manage_roles=True)
    @app_commands.describe(args="Leave blank for the dashboard, or use `add [message] <emoji> <role> ...` / `remove [message]`.")
    async def selfrole(self, ctx, *, args: str = commands.parameter(default="", description="Self-role action and arguments.")):
        if self.mongo_client is None:
            return await ctx.reply(embed=err_embed("MongoDB is not configured; self-role settings are unavailable."), mention_author=False)

        raw = args.strip() if ctx.interaction is not None else raw_args(ctx)
        if raw:
            action, _, remainder = raw.partition(" ")
            action = action.casefold()
            if action == "add":
                return await self._add_selfrole_reactions(ctx, remainder.strip())
            if action in {"remove", "delete", "del", "r"}:
                return await self._remove_selfrole(ctx, remainder.strip())
            return await ctx.reply(
                embed=err_embed("Unknown Action", "Use `.selfrole` for the dashboard, `.selfrole add [message] <emoji> <role> ...`, or `.selfrole remove [message]`."),
                mention_author=False,
            )

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


async def setup(bot):
    await bot.add_cog(SelfRole(bot))