from __future__ import annotations

import asyncio
import time

import discord
from discord import app_commands
from discord.ext import commands

from views.common_views import error_embed, make_embed, success_embed
from views.embeds import handle_command_error
from views.verify_views import (
    CaptchaPromptView,
    VerificationConfigView,
    VerificationPanelView,
    generate_captcha_text,
    make_captcha_image,
    verification_config_embed,
)


def _defaults(guild_id: int) -> dict:
    return {
        "_id": str(guild_id),
        "verify_channel_id": None,
        "unverified_role_id": None,
        "verified_role_id": None,
        "captcha_length": 6,
        "max_attempts": 3,
        "captcha_expiry_seconds": 180,
        "panel_title": "Verify to access the server",
        "panel_description": "Press Verify and enter the letters and numbers shown in the CAPTCHA to unlock the server.",
        "captcha_title": "Complete verification",
        "prompt_description": "Enter the lowercase letters and numbers shown. Similar-looking characters (such as 0, o, i and l) are excluded.",
        "enter_button_label": "Enter the text",
        "success_message": "Verification complete. You now have access to the server.",
        "failure_message": "Too many incorrect attempts. Press Verify again for a new CAPTCHA.",
        "verify_button_label": "Verify",
        "verify_panel_message_id": None,
        "verification_initialized": False,
        "verify_channel_backups": {},
        "verify_role_backups": {},
    }


@app_commands.default_permissions(administrator=True)
@commands.hybrid_command(name="verify", aliases=["verification"], description="Configure the server's CAPTCHA verification system.")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def verify_command(ctx: commands.Context):
    cog = ctx.bot.get_cog("Verification")
    if cog is None:
        return await ctx.reply(embed=error_embed("Verification module is unavailable."), mention_author=False)
    if not cog._db_ready():
        return await ctx.reply(embed=error_embed("MongoDB is not configured; verification settings are unavailable."), mention_author=False)
    config = await cog.get_config(ctx.guild.id)
    view = VerificationConfigView(cog, config, ctx.guild, ctx.author.id)
    # EmbedLayout renders its embed inside a Components V2 container; do not pass embed= separately.
    view.message = await ctx.reply(view=view, mention_author=False)


@verify_command.error
async def verify_command_error(ctx: commands.Context, error: Exception):
    await handle_command_error(ctx, error)


class Verification(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def collection(self):
        if self.bot.mongo_client is None:
            raise RuntimeError("MongoDB is not configured; verification settings are unavailable.")
        return self.bot.mongo_client["utilities"]["verification_config"]

    def _db_ready(self) -> bool:
        return self.bot.mongo_client is not None

    async def cog_load(self):
        self.bot.add_view(VerificationPanelView(self))
        if self.bot.mongo_client is not None:
            try:
                await self.collection.create_index("verify_channel_id")
            except Exception as exc:
                print(f"Verification Cog: could not create index: {exc}")

    async def get_config(self, guild_id: int) -> dict:
        if not self._db_ready():
            raise RuntimeError("MongoDB is not configured; verification settings are unavailable.")
        defaults = _defaults(guild_id)
        config = await self.collection.find_one({"_id": str(guild_id)})
        if config is None:
            await self.collection.update_one({"_id": str(guild_id)}, {"$setOnInsert": defaults}, upsert=True)
            return defaults
        missing = {key: value for key, value in defaults.items() if key not in config}
        if missing:
            await self.collection.update_one({"_id": str(guild_id)}, {"$set": missing}, upsert=True)
            config.update(missing)
        return config

    async def save_config_fields(self, guild_id: int, values: dict):
        if not self._db_ready():
            raise RuntimeError("MongoDB is not configured; verification settings cannot be saved.")
        await self.collection.update_one({"_id": str(guild_id)}, {"$set": values}, upsert=True)

    async def _joins_cog(self):
        return self.bot.get_cog("Joins")

    async def _protect_autorole(self, guild_id: int, role_id: int, old_role_id: int | None = None):
        if not self._db_ready():
            return
        joins = await self._joins_cog()
        if joins is not None:
            if old_role_id and old_role_id != role_id:
                await joins.collection.update_one(
                    {"_id": guild_id},
                    {"$pull": {"autoroles": old_role_id, "protected_autoroles": old_role_id}},
                    upsert=True,
                )
            await joins.ensure_protected_autorole(guild_id, role_id)
            return
        update = {"$addToSet": {"autoroles": role_id, "protected_autoroles": role_id}}
        if old_role_id and old_role_id != role_id:
            update["$pull"] = {"autoroles": old_role_id, "protected_autoroles": old_role_id}
        client = self.bot.mongo_client
        if client is not None:
            await client["utilities"]["joins_config"].update_one({"_id": guild_id}, update, upsert=True)

    @staticmethod
    def _snapshot_overwrite(overwrite: discord.PermissionOverwrite, existed: bool) -> dict:
        return {
            "existed": existed,
            "view_channel": overwrite.view_channel,
            "send_messages": overwrite.send_messages,
            "read_message_history": overwrite.read_message_history,
            "connect": overwrite.connect,
        }

    @staticmethod
    def _restore_overwrite_fields(current: discord.PermissionOverwrite, snapshot: dict) -> discord.PermissionOverwrite:
        for field in ("view_channel", "send_messages", "read_message_history", "connect"):
            setattr(current, field, snapshot.get(field))
        return current

    async def _resolve_verification_channel(
        self,
        guild: discord.Guild,
        channel: object,
    ) -> discord.TextChannel:
        """Resolve UI channel-select wrappers to a real guild channel object."""
        channel_id = getattr(channel, "id", None)
        if channel_id is None:
            raise ValueError("Select a valid text channel from this server.")

        resolved = guild.get_channel(int(channel_id))
        if resolved is None:
            try:
                resolved = await guild.fetch_channel(int(channel_id))
            except discord.NotFound as exc:
                raise ValueError("The selected channel no longer exists in this server.") from exc

        if not isinstance(resolved, discord.TextChannel):
            raise ValueError("The verification channel must be a server text or announcement channel.")
        return resolved

    async def _snapshot_verify_channel(self, guild: discord.Guild, channel: discord.TextChannel, config: dict):
        backups = dict(config.get("verify_channel_backups", {}))
        key = str(channel.id)
        if key not in backups:
            data = {}
            for target, overwrite in channel.overwrites.items():
                target_type = "role" if isinstance(target, discord.Role) else "member"
                data[f"{target_type}:{target.id}"] = self._snapshot_overwrite(overwrite, True)
            backups[key] = data
            await self.save_config_fields(guild.id, {"verify_channel_backups": backups})

    async def _restore_previous_verify_channel(self, guild: discord.Guild, previous_channel_id: int, config: dict, role: discord.Role | None):
        channel = guild.get_channel(previous_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        backups = config.get("verify_channel_backups", {})
        snapshot = backups.get(str(previous_channel_id))
        if not isinstance(snapshot, dict):
            return
        # Restore the channel's pre-verification overwrites while retaining unrelated permission fields.
        for target, current in list(channel.overwrites.items()):
            key = f"{'role' if isinstance(target, discord.Role) else 'member'}:{target.id}"
            saved = snapshot.get(key)
            if saved is None:
                self._restore_overwrite_fields(current, {"view_channel": None, "send_messages": None, "read_message_history": None, "connect": None})
                try:
                    await channel.set_permissions(target, overwrite=None if current.is_empty() else current, reason="Restore previous verification channel permissions")
                except discord.HTTPException:
                    pass
            else:
                self._restore_overwrite_fields(current, saved)
                try:
                    await channel.set_permissions(target, overwrite=None if current.is_empty() and not saved.get("existed") else current, reason="Restore previous verification channel permissions")
                except discord.HTTPException:
                    pass
        if role is not None:
            current = channel.overwrites_for(role)
            current.view_channel = False
            current.send_messages = False
            try:
                await channel.set_permissions(role, overwrite=current, reason="Hide old verification channel from unverified members")
            except discord.HTTPException:
                pass

    async def _backup_role_overwrites(self, guild: discord.Guild, role: discord.Role, config: dict):
        backups = dict(config.get("verify_role_backups", {}))
        key = str(role.id)
        if key in backups:
            return
        channel_data = {}
        for channel in guild.channels:
            existed = role in channel.overwrites
            overwrite = channel.overwrites_for(role)
            channel_data[str(channel.id)] = self._snapshot_overwrite(overwrite, existed)
        backups[key] = channel_data
        await self.save_config_fields(guild.id, {"verify_role_backups": backups})

    async def _restore_role_overwrites(self, guild: discord.Guild, role: discord.Role, config: dict):
        snapshot = config.get("verify_role_backups", {}).get(str(role.id), {})
        for channel_id, saved in snapshot.items():
            channel = guild.get_channel(int(channel_id))
            if channel is None:
                continue
            overwrite = channel.overwrites_for(role)
            self._restore_overwrite_fields(overwrite, saved)
            try:
                if not saved.get("existed") and overwrite.is_empty():
                    await channel.set_permissions(role, overwrite=None, reason="Restore previous unverified-role permissions")
                else:
                    await channel.set_permissions(role, overwrite=overwrite, reason="Restore previous unverified-role permissions")
            except discord.HTTPException:
                pass

    async def set_verified_role(self, guild: discord.Guild, role: discord.Role | None):
        if role is None:
            await self.save_config_fields(guild.id, {"verified_role_id": None})
            return
        config = await self.get_config(guild.id)
        if role.is_default():
            raise ValueError("The @everyone role cannot be used as the verified role.")
        if role.managed:
            raise ValueError("Bot or integration managed roles cannot be assigned manually.")
        unverified_id = config.get("unverified_role_id")
        if unverified_id and int(unverified_id) == role.id:
            raise ValueError("The verified role must be different from the unverified role.")
        me = guild.me
        if me is None or role >= me.top_role:
            raise ValueError("Move the bot's highest role above this role so it can assign it.")
        await self.save_config_fields(guild.id, {"verified_role_id": role.id})

    async def create_unverified_role(self, guild: discord.Guild) -> discord.Role | None:
        config = await self.get_config(guild.id)
        role_id = config.get("unverified_role_id")
        existing = guild.get_role(int(role_id)) if role_id else None
        if existing:
            return existing
        try:
            role = await guild.create_role(name=str(config.get("unverified_role_name", "Unverified"))[:100], permissions=discord.Permissions.none(), mentionable=False, reason="Create the verification gate role")
        except discord.HTTPException as exc:
            print(f"Verification Cog: could not create unverified role: {exc}")
            return None
        await self.set_unverified_role(guild, role)
        return role

    async def set_unverified_role(self, guild: discord.Guild, role: discord.Role):
        if role.is_default() or role.permissions.administrator:
            raise ValueError("The unverified role cannot be @everyone or have Administrator permission.")
        bot_member = guild.me
        if bot_member is not None and role >= bot_member.top_role:
            raise ValueError("Move the bot's highest role above the selected unverified role.")
        config = await self.get_config(guild.id)
        old_id = int(config["unverified_role_id"]) if config.get("unverified_role_id") else None
        old_role = guild.get_role(old_id) if old_id else None
        if old_role and old_role.id != role.id:
            await self._restore_role_overwrites(guild, old_role, config)
        await self.save_config_fields(guild.id, {"unverified_role_id": role.id})
        await self._protect_autorole(guild.id, role.id, old_id)
        config = await self.get_config(guild.id)
        if old_role and old_role.id != role.id:
            await self._migrate_role_members(guild, old_role, role)
        if config.get("verify_channel_id"):
            await self.apply_verification_permissions(guild, config)

    async def _migrate_role_members(self, guild: discord.Guild, old_role: discord.Role, new_role: discord.Role):
        try:
            async for member in guild.fetch_members(limit=None):
                if member.bot or old_role not in member.roles:
                    continue
                try:
                    await member.add_roles(new_role, reason="Migrate to the configured unverified role")
                    await member.remove_roles(old_role, reason="Migrate to the configured unverified role")
                except discord.HTTPException:
                    pass
        except (discord.Forbidden, discord.HTTPException) as exc:
            print(f"Verification Cog: could not migrate every member to the new unverified role: {exc}")

    async def set_verification_channel(self, guild: discord.Guild, channel: object) -> discord.TextChannel:
        # ChannelSelect may return an AppCommandChannel wrapper without permission overwrites.
        # Resolve it before any snapshot or permission-management operations.
        channel = await self._resolve_verification_channel(guild, channel)
        config = await self.get_config(guild.id)
        old_channel_id = int(config["verify_channel_id"]) if config.get("verify_channel_id") else None
        role_id = int(config["unverified_role_id"]) if config.get("unverified_role_id") else None
        role = guild.get_role(role_id) if role_id else None
        if role is None:
            role = await self.create_unverified_role(guild)
            if role is None:
                raise RuntimeError("The bot could not create the Unverified role.")
            config = await self.get_config(guild.id)
        if old_channel_id and old_channel_id != channel.id:
            await self._restore_previous_verify_channel(guild, old_channel_id, config, role)
        await self._snapshot_verify_channel(guild, channel, config)
        await self.save_config_fields(guild.id, {"verify_channel_id": channel.id})
        config = await self.get_config(guild.id)
        await self.apply_verification_permissions(guild, config)
        if not config.get("verification_initialized"):
            assigned = await self._assign_unverified_to_existing_members(guild, role)
            await self.save_config_fields(guild.id, {"verification_initialized": True, "initial_members_assigned": assigned})
        return channel

    async def _assign_unverified_to_existing_members(self, guild: discord.Guild, role: discord.Role) -> int:
        assigned = 0
        try:
            async for member in guild.fetch_members(limit=None):
                if member.bot or member.guild_permissions.administrator or role in member.roles:
                    continue
                try:
                    await member.add_roles(role, reason="Initialize the server verification gate")
                    assigned += 1
                except discord.HTTPException:
                    pass
        except (discord.Forbidden, discord.HTTPException) as exc:
            print(f"Verification Cog: could not enumerate all members for initial verification: {exc}")
            for member in guild.members:
                if member.bot or member.guild_permissions.administrator or role in member.roles:
                    continue
                try:
                    await member.add_roles(role, reason="Initialize the server verification gate")
                    assigned += 1
                except discord.HTTPException:
                    pass
        return assigned

    async def apply_verification_permissions(self, guild: discord.Guild, config: dict) -> dict:
        channel_id = config.get("verify_channel_id")
        role_id = config.get("unverified_role_id")
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        role = guild.get_role(int(role_id)) if role_id else None
        if not isinstance(channel, discord.TextChannel) or role is None:
            raise ValueError("Select a valid verification text channel and unverified role first.")
        await self._backup_role_overwrites(guild, role, config)
        updated, failed = 0, 0
        for target_channel in guild.channels:
            try:
                overwrite = target_channel.overwrites_for(role)
                overwrite.view_channel = False
                overwrite.send_messages = False
                overwrite.connect = False
                if target_channel.id == channel.id:
                    overwrite.view_channel = True
                    overwrite.read_message_history = True
                    overwrite.send_messages = False
                await target_channel.set_permissions(role, overwrite=overwrite, reason="Apply verification access restrictions")
                updated += 1
            except (discord.Forbidden, discord.HTTPException):
                failed += 1

        # Hide the verification channel from everyone except the unverified role and administrators.
        try:
            everyone_overwrite = channel.overwrites_for(guild.default_role)
            everyone_overwrite.view_channel = False
            await channel.set_permissions(guild.default_role, overwrite=everyone_overwrite, reason="Hide verification channel from verified members")
        except (discord.Forbidden, discord.HTTPException):
            failed += 1
        for other_role in guild.roles:
            if other_role.is_default() or other_role.id == role.id or other_role.permissions.administrator:
                continue
            try:
                overwrite = channel.overwrites_for(other_role)
                overwrite.view_channel = False
                await channel.set_permissions(other_role, overwrite=overwrite, reason="Restrict verification channel to unverified members")
            except (discord.Forbidden, discord.HTTPException):
                failed += 1
        # Individual member overwrites are evaluated after role overwrites in Discord; deny
        # stale per-member allows too, otherwise a previously whitelisted member could bypass the gate.
        bot_user_id = self.bot.user.id if self.bot.user else None
        for target in list(channel.overwrites):
            if not isinstance(target, discord.Member) or target.id == bot_user_id:
                continue
            try:
                overwrite = channel.overwrites_for(target)
                overwrite.view_channel = False
                await channel.set_permissions(target, overwrite=overwrite, reason="Restrict verification channel to unverified members")
            except (discord.Forbidden, discord.HTTPException):
                failed += 1
        bot_member = guild.get_member(self.bot.user.id) if self.bot.user else None
        if bot_member is None and self.bot.user:
            try:
                bot_member = await guild.fetch_member(self.bot.user.id)
            except discord.HTTPException:
                bot_member = None
        if bot_member is not None:
            try:
                overwrite = channel.overwrites_for(bot_member)
                overwrite.view_channel = True
                overwrite.send_messages = True
                overwrite.read_message_history = True
                overwrite.attach_files = True
                overwrite.embed_links = True
                await channel.set_permissions(bot_member, overwrite=overwrite, reason="Allow bot to operate verification panel")
            except (discord.Forbidden, discord.HTTPException):
                failed += 1
        return {"updated": updated, "failed": failed}

    async def post_verification_panel(self, guild: discord.Guild, channel: discord.TextChannel, config: dict) -> discord.Message | None:
        title = str(config.get("panel_title", "Verify to access the server"))
        description = str(config.get("panel_description", "Press Verify and enter the letters and numbers shown in the CAPTCHA to unlock the server."))
        embed = make_embed(title, description)
        old_id = config.get("verify_panel_message_id")
        old_message = None
        if old_id:
            try:
                old_message = await channel.fetch_message(int(old_id))
            except discord.HTTPException:
                old_message = None
        view = VerificationPanelView(self, str(config.get("verify_button_label", "Verify")))
        try:
            if old_message:
                await old_message.edit(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
                message = old_message
            else:
                message = await channel.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            print(f"Verification Cog: could not post verification panel: {exc}")
            return None
        await self.save_config_fields(guild.id, {"verify_panel_message_id": message.id, "verify_channel_id": channel.id})
        return message

    async def start_captcha(self, interaction: discord.Interaction, *, test_only: bool = False):
        if interaction.guild is None:
            return await interaction.response.send_message(embed=error_embed("Use the Verify button inside the server."), ephemeral=True)
        if not self._db_ready():
            return await interaction.response.send_message(embed=error_embed("Verification storage is unavailable. Please contact an administrator."), ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        config = await self.get_config(interaction.guild.id)
        role_id = config.get("unverified_role_id")
        role = interaction.guild.get_role(int(role_id)) if role_id else None
        if not test_only:
            if not config.get("verify_channel_id") or not role:
                return await interaction.followup.send(embed=error_embed("Verification is not fully configured. Ask an administrator for help."), ephemeral=True)
            if isinstance(interaction.user, discord.Member) and role not in interaction.user.roles:
                return await interaction.followup.send(embed=make_embed("Already Verified", "You already have access to the server."), ephemeral=True)
        else:
            config["_test_only"] = True
        length = max(3, min(12, int(config.get("captcha_length", 6))))
        expected = generate_captcha_text(length)
        file = await asyncio.to_thread(make_captcha_image, expected)
        embed = make_embed(str(config.get("captcha_title", "Complete verification")), str(config.get("prompt_description", "Enter the lowercase letters and numbers shown. Similar-looking characters (such as 0, o, i and l) are excluded.")))
        embed.set_image(url="attachment://verification.png")
        view = CaptchaPromptView(self, expected, config, interaction.user.id, time.time() + max(30, min(600, int(config.get("captcha_expiry_seconds", 180)))))
        try:
            view.message = await interaction.followup.send(embed=embed, file=file, view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none(), wait=True)
        except discord.HTTPException as exc:
            print(f"Verification Cog: could not start CAPTCHA for {interaction.user.id}: {exc}")
            try:
                await interaction.followup.send(embed=error_embed("Could not start verification. Please try again."), ephemeral=True)
            except discord.HTTPException:
                pass

    async def complete_verification(self, guild: discord.Guild, user: discord.abc.User, config: dict):
        if config.get("_test_only"):
            return
        role_id = config.get("unverified_role_id")
        role = guild.get_role(int(role_id)) if role_id else None
        if role is None:
            raise ValueError("The Unverified role no longer exists. Ask an administrator to configure it again.")
        member = user if isinstance(user, discord.Member) else guild.get_member(user.id)
        if member is None:
            member = await guild.fetch_member(user.id)
        if role not in member.roles:
            return
        await member.remove_roles(role, reason="Completed CAPTCHA verification")
        verified_id = config.get("verified_role_id")
        verified_role = guild.get_role(int(verified_id)) if verified_id else None
        if verified_role is not None and verified_role.id != role.id and verified_role not in member.roles:
            try:
                await member.add_roles(verified_role, reason="Completed CAPTCHA verification")
            except (discord.Forbidden, discord.HTTPException) as exc:
                print(f"Verification Cog: could not assign verified role {verified_role.id} to {member.id}: {exc}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Verification(bot))
    if bot.get_command("verify") is None:
        bot.add_command(verify_command)