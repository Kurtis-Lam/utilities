from __future__ import annotations

import asyncio
import io
import re
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from views.common_views import ConfirmLayout, error_embed, make_embed, success_embed
from views.embeds import handle_command_error
from views.ticket_transcript import build_ticket_transcript
from views.tickets_views import TicketControlView, TicketDashboardView, TicketPanelView


def _defaults(guild_id: int) -> dict:
    return {
        "_id": str(guild_id),
        "panel_title": "Need help?",
        "panel_description": "Choose a category below to open a private support ticket.",
        "panel_button_label": "Create Ticket",
        "choose_option_message": "Choose the category that best matches your request.",
        "default_category_id": None,
        "staff_role_ids": [],
        "ping_role_ids": [],
        "log_channel_id": None,
        "max_open_tickets": 3,
        "channel_name_template": "ticket-{username}-{number}",
        "open_message": "Hello {user}, the team will be with you shortly.\n**Reason:** {reason}",
        "close_message": "Ticket closed by {user}.",
        "reopen_message": "Ticket reopened by {user}.",
        "claim_message": "Ticket claimed by {user}.",
        "closed_topic_suffix": "(closed)",
        "allow_user_close": True,
        "allow_claim": True,
        "transcripts_enabled": True,
        "delete_after_close_minutes": 0,
        "options": [
            {"key": "general", "label": "General Support", "description": "Ask a general question or request help.", "emoji": "🎫", "category_id": None, "ping_role_ids": [], "open_message": ""}
        ],
        "panels": [],
    }


def _format_ticket(template: str, *, user: discord.abc.User, guild: discord.Guild, channel=None,
                   reason: str = "", option: str = "", staff: str = "", ticket_id: int | None = None) -> str:
    values = {
        "{user}": user.mention,
        "{username}": user.name,
        "{display_name}": getattr(user, "display_name", user.name),
        "{user_id}": str(user.id),
        "{guild}": guild.name,
        "{guild_id}": str(guild.id),
        "{ticket}": channel.mention if channel else "ticket",
        "{ticket_id}": str(ticket_id or (channel.id if channel else "")),
        "{reason}": reason,
        "{option}": option,
        "{staff}": staff,
    }
    for key, value in values.items():
        template = template.replace(key, value)
    return template


def _channel_slug(template: str, user: discord.abc.User, number: int, option_label: str) -> str:
    values = {
        "{username}": user.name.lower(),
        "{display_name}": getattr(user, "display_name", user.name).lower(),
        "{user_id}": str(user.id),
        "{number}": f"{number:04d}",
        "{option}": option_label.lower(),
    }
    name = template
    for key, value in values.items():
        name = name.replace(key, value)
    name = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return (name or f"ticket-{user.id}")[:95]


@app_commands.default_permissions(administrator=True)
@commands.hybrid_command(name="tickets", aliases=["ticket", "ticketsetup"], description="Configure and publish support ticket panels.")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def tickets_command(ctx: commands.Context):
    cog = ctx.bot.get_cog("Tickets")
    if cog is None:
        return await ctx.reply(embed=error_embed("Ticket system is not loaded."), mention_author=False)
    if not cog._db_ready():
        return await ctx.reply(embed=error_embed("MongoDB is not configured; ticket settings are unavailable."), mention_author=False)
    config = await cog.get_config(ctx.guild.id)
    view = TicketDashboardView(cog, config, ctx.guild, ctx.author.id)
    # EmbedLayout renders its embed inside a Components V2 container; do not pass embed= separately.
    view.message = await ctx.reply(view=view, mention_author=False)


@tickets_command.error
async def tickets_command_error(ctx: commands.Context, error: Exception):
    await handle_command_error(ctx, error)


class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._delete_tasks: dict[int, asyncio.Task] = {}

    @property
    def config_collection(self):
        return self.bot.mongo_client["utilities"]["tickets_config"]

    @property
    def tickets_collection(self):
        return self.bot.mongo_client["utilities"]["tickets"]

    async def cog_load(self):
        self.bot.add_view(TicketPanelView(self))
        self.bot.add_view(TicketControlView(self, mode="all"))
        if self.bot.mongo_client is None:
            print("Tickets Cog: MongoDB is not configured; ticket creation will be unavailable.")
            return
        try:
            await self.tickets_collection.create_index([("guild_id", 1), ("opener_id", 1), ("closed", 1)])
            await self.tickets_collection.create_index([("closed", 1), ("closed_at", 1)])
        except Exception as exc:
            print(f"Tickets Cog: could not create indexes: {exc}")

    async def cog_unload(self):
        for task in self._delete_tasks.values():
            task.cancel()
        self._delete_tasks.clear()

    async def get_config(self, guild_id: int) -> dict:
        if not self._db_ready():
            raise RuntimeError("MongoDB is not configured; ticket settings are unavailable.")
        defaults = _defaults(guild_id)
        config = await self.config_collection.find_one({"_id": str(guild_id)})
        if config is None:
            await self.config_collection.update_one({"_id": str(guild_id)}, {"$setOnInsert": defaults}, upsert=True)
            return defaults
        missing = {key: value for key, value in defaults.items() if key not in config}
        if missing:
            await self.config_collection.update_one({"_id": str(guild_id)}, {"$set": missing}, upsert=True)
            config.update(missing)
        return config

    async def save_config_fields(self, guild_id: int, values: dict):
        if not self._db_ready():
            raise RuntimeError("MongoDB is not configured; ticket settings cannot be saved.")
        await self.config_collection.update_one({"_id": str(guild_id)}, {"$set": values}, upsert=True)

    def _db_ready(self) -> bool:
        return self.bot.mongo_client is not None

    async def create_ticket(self, interaction: discord.Interaction, option_key: str, config: dict, panel: dict):
        if interaction.guild is None or interaction.user.bot:
            return await interaction.response.send_message(embed=error_embed("Tickets can only be created by server members."), ephemeral=True)
        if not self._db_ready():
            return await interaction.response.send_message(embed=error_embed("MongoDB is not configured; ticket creation is unavailable."), ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        config = await self.get_config(guild.id)
        option = next((item for item in config.get("options", []) if item.get("key") == option_key), None)
        if option is None:
            return await interaction.followup.send(embed=error_embed("That ticket option was removed. Open the panel again."), ephemeral=True)
        max_open = int(config.get("max_open_tickets", 3) or 0)
        if max_open > 0:
            count = await self.tickets_collection.count_documents({"guild_id": guild.id, "opener_id": interaction.user.id, "closed": False})
            if count >= max_open:
                return await interaction.followup.send(embed=error_embed(f"You already have the maximum of `{max_open}` open ticket(s)."), ephemeral=True)

        category_id = option.get("category_id") or config.get("default_category_id")
        category = guild.get_channel(int(category_id)) if category_id else None
        if category_id and not isinstance(category, discord.CategoryChannel):
            return await interaction.followup.send(embed=error_embed("The configured ticket category no longer exists. Ask an administrator to update it."), ephemeral=True)

        support_ids = [int(x) for x in config.get("staff_role_ids", []) if str(x).isdigit()]
        ping_ids = list(dict.fromkeys(support_ids + [int(x) for x in config.get("ping_role_ids", []) if str(x).isdigit()] + [int(x) for x in option.get("ping_role_ids", []) if str(x).isdigit()]))
        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True),
        }
        for role_id in support_ids:
            role = guild.get_role(role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True, attach_files=True, embed_links=True)
        bot_member = guild.get_member(self.bot.user.id) if self.bot.user else None
        if bot_member is None and self.bot.user:
            try:
                bot_member = await guild.fetch_member(self.bot.user.id)
            except discord.HTTPException:
                bot_member = None
        if bot_member is not None:
            overwrites[bot_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, manage_messages=True, attach_files=True, embed_links=True)

        ticket_number = await self.tickets_collection.count_documents({"guild_id": guild.id}) + 1
        channel_name = _channel_slug(str(config.get("channel_name_template", "ticket-{username}-{number}")), interaction.user, ticket_number, str(option.get("label", "ticket")))
        topic = f"Ticket creator ID: {interaction.user.id} | Option: {option.get('label', 'Ticket')} | Opened: {int(time.time())}"
        try:
            channel = await guild.create_text_channel(
                channel_name,
                category=category,
                overwrites=overwrites,
                topic=topic[:1024],
                reason=f"Ticket opened by {interaction.user} ({interaction.user.id})",
            )
        except discord.HTTPException as exc:
            return await interaction.followup.send(embed=error_embed(f"Could not create the ticket channel: {exc}"), ephemeral=True)

        record = {
            "_id": channel.id,
            "guild_id": guild.id,
            "channel_id": channel.id,
            "opener_id": interaction.user.id,
            "opener_name": str(interaction.user),
            "option_key": option_key,
            "option_label": str(option.get("label", "Ticket")),
            "ticket_number": ticket_number,
            "panel_message_id": int(panel.get("message_id", 0)),
            "opened_at": int(time.time()),
            "closed": False,
            "claimed_by": None,
            "control_message_id": None,
        }
        try:
            await self.tickets_collection.insert_one(record)
        except Exception as exc:
            try:
                await channel.delete(reason="Could not persist ticket record")
            except discord.HTTPException:
                pass
            print(f"Tickets Cog: could not save ticket {channel.id}: {exc}")
            return await interaction.followup.send(embed=error_embed("Ticket creation failed while saving its record. Please try again."), ephemeral=True)

        opening_template = str(option.get("open_message") or config.get("open_message", "Hello {user}, the team will be with you shortly.\n**Reason:** {reason}"))
        description = _format_ticket(opening_template, user=interaction.user, guild=guild, channel=channel, reason=str(option.get("label", "Ticket")), option=str(option.get("label", "Ticket")), ticket_id=channel.id)
        embed = make_embed(f"{option.get('emoji') or '🎫'} {option.get('label', 'Ticket')}", description)
        embed.add_field(name="Created by", value=interaction.user.mention, inline=True)
        embed.add_field(name="Ticket ID", value=f"`{channel.id}`", inline=True)
        role_mentions = [f"<@&{role_id}>" for role_id in ping_ids if guild.get_role(role_id)]
        allowed_mentions = discord.AllowedMentions(users=[interaction.user], roles=[discord.Object(id=role_id) for role_id in ping_ids if guild.get_role(role_id)], everyone=False, replied_user=False)
        try:
            control_message = await channel.send(
                content=" ".join(role_mentions) or None,
                embed=embed,
                view=TicketControlView(self),
                allowed_mentions=allowed_mentions,
            )
            await self.tickets_collection.update_one({"_id": channel.id}, {"$set": {"control_message_id": control_message.id}})
        except discord.HTTPException as exc:
            print(f"Tickets Cog: ticket {channel.id} created but opening message failed: {exc}")
        await interaction.followup.send(
            embed=success_embed(f"Your ticket has been created: {channel.mention}\n[Open ticket]({channel.jump_url if hasattr(channel, 'jump_url') else f'https://discord.com/channels/{guild.id}/{channel.id}'})"),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    def _is_staff(self, member: discord.Member, config: dict) -> bool:
        if member.guild_permissions.administrator or member.guild_permissions.manage_guild or member.guild_permissions.manage_channels:
            return True
        role_ids = {int(x) for x in config.get("staff_role_ids", []) if str(x).isdigit()}
        return any(role.id in role_ids for role in member.roles)

    async def handle_ticket_control(self, interaction: discord.Interaction, action: str):
        channel = interaction.channel
        if interaction.guild is None or not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message(embed=error_embed("These controls only work in ticket text channels."), ephemeral=True)
        if not self._db_ready():
            return await interaction.response.send_message(embed=error_embed("Ticket storage is unavailable."), ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ticket = await self.tickets_collection.find_one({"_id": channel.id, "guild_id": interaction.guild.id})
        if ticket is None:
            return await interaction.followup.send(embed=error_embed("This channel is not registered as a ticket."), ephemeral=True)
        config = await self.get_config(interaction.guild.id)
        member = interaction.user if isinstance(interaction.user, discord.Member) else await interaction.guild.fetch_member(interaction.user.id)
        staff = self._is_staff(member, config)
        opener = member.id == int(ticket.get("opener_id", 0))

        if action == "claim":
            if not config.get("allow_claim", True):
                return await interaction.followup.send(embed=error_embed("Ticket claiming is disabled by an administrator."), ephemeral=True)
            if not staff:
                return await interaction.followup.send(embed=error_embed("Only support staff can claim tickets."), ephemeral=True)
            if ticket.get("claimed_by"):
                return await interaction.followup.send(embed=error_embed(f"This ticket is already claimed by <@{ticket['claimed_by']}>."), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            claim_result = await self.tickets_collection.update_one(
                {"_id": channel.id, "claimed_by": None},
                {"$set": {"claimed_by": member.id, "claimed_by_name": member.display_name, "claimed_at": int(time.time())}},
            )
            if claim_result.modified_count == 0:
                latest = await self.tickets_collection.find_one({"_id": channel.id}) or {}
                owner = f"<@{latest['claimed_by']}>" if latest.get("claimed_by") else "another staff member"
                return await interaction.followup.send(embed=error_embed(f"This ticket is already claimed by {owner}."), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            await self._mark_claimed(interaction.message, member)
            message = _format_ticket(str(config.get("claim_message", "Ticket claimed by {user}.")), user=member, guild=interaction.guild, channel=channel, option=ticket.get("option_label", "Ticket"), ticket_id=channel.id)
            await channel.send(embed=make_embed("🖐️ Ticket Claimed", message), allowed_mentions=discord.AllowedMentions.none())
            return await interaction.followup.send(embed=success_embed("You claimed this ticket."), ephemeral=True)

        if action == "close":
            if ticket.get("closed"):
                return await interaction.followup.send(embed=error_embed("This ticket is already closed."), ephemeral=True)
            if opener and not config.get("allow_user_close", True) and not staff:
                return await interaction.followup.send(embed=error_embed("Only support staff can close tickets in this server."), ephemeral=True)
            if not opener and not staff:
                return await interaction.followup.send(embed=error_embed("Only the ticket creator or support staff can close this ticket."), ephemeral=True)
            confirm = ConfirmLayout(
                member,
                "Are you sure you want to close this ticket?\nThe ticket creator will lose access to the channel. Support staff can reopen or delete it afterwards.",
                title="🔒 Close this ticket?",
                confirm_label="Close Ticket",
                cancel_label="Cancel",
                cancel_text="❌ Ticket close cancelled",
                timeout=60.0,
            )
            try:
                confirm.message = await interaction.followup.send(view=confirm, ephemeral=True, wait=True)
            except discord.HTTPException as exc:
                print(f"Tickets Cog: could not send close confirmation for {channel.id}: {exc}")
                return
            await confirm.wait()
            if not confirm:
                return
            latest = await self.tickets_collection.find_one({"_id": channel.id, "guild_id": interaction.guild.id})
            if latest is None or latest.get("closed"):
                return await confirm.show(error_embed("This ticket is already closed."))
            await confirm.show(success_embed("Closing ticket…"))
            await self._close_ticket(channel, latest, config, member)
            return

        if action == "reopen":
            if not staff:
                return await interaction.followup.send(embed=error_embed("Only support staff can reopen tickets."), ephemeral=True)
            if not ticket.get("closed"):
                return await interaction.followup.send(embed=error_embed("This ticket is already open."), ephemeral=True)
            opener_member = interaction.guild.get_member(int(ticket["opener_id"]))
            if opener_member is None:
                try:
                    opener_member = await interaction.guild.fetch_member(int(ticket["opener_id"]))
                except discord.HTTPException:
                    opener_member = None
            if opener_member:
                overwrite = channel.overwrites_for(opener_member)
                overwrite.view_channel = True
                overwrite.send_messages = True
                overwrite.read_message_history = True
                await channel.set_permissions(opener_member, overwrite=overwrite, reason="Ticket reopened")
            pending_delete = self._delete_tasks.pop(channel.id, None)
            if pending_delete is not None:
                pending_delete.cancel()
            await self.tickets_collection.update_one({"_id": channel.id}, {"$set": {"closed": False, "reopened_at": int(time.time())}, "$unset": {"closed_at": "", "closed_by": "", "closed_by_name": ""}})
            new_name = channel.name.removeprefix("closed-")
            try:
                await channel.edit(name=new_name[:100], topic=(channel.topic or "").replace(str(config.get("closed_topic_suffix", "(closed)")), "").strip(), reason=f"Ticket reopened by {member}")
            except discord.HTTPException:
                pass
            message = _format_ticket(str(config.get("reopen_message", "Ticket reopened by {user}.")), user=member, guild=interaction.guild, channel=channel, option=ticket.get("option_label", "Ticket"), ticket_id=channel.id)
            await channel.send(embed=make_embed("🔓 Ticket Reopened", message), allowed_mentions=discord.AllowedMentions.none())
            return await interaction.followup.send(embed=success_embed("Ticket reopened."), ephemeral=True)

        if action == "delete":
            if not staff:
                return await interaction.followup.send(embed=error_embed("Only support staff can delete tickets."), ephemeral=True)
            if not ticket.get("closed"):
                return await interaction.followup.send(embed=error_embed("Close this ticket before deleting it."), ephemeral=True)
            await interaction.followup.send(embed=success_embed("Deleting closed ticket…"), ephemeral=True)
            await self._delete_ticket(channel, ticket, config, deleter=member, interaction=interaction)
            return

    async def _mark_claimed(self, message: discord.Message | None, member: discord.Member):
        if message is None:
            return
        try:
            base = message.embeds[0] if message.embeds else make_embed("🎫 Ticket")
            embed = discord.Embed.from_dict(base.to_dict())
            for index in reversed(range(len(embed.fields))):
                if embed.fields[index].name == "Claimed by":
                    embed.remove_field(index)
            embed.add_field(name="Claimed by", value=member.mention, inline=True)
            await message.edit(embed=embed, view=TicketControlView(self, mode="claimed"), allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            print(f"Tickets Cog: could not update claimed ticket message {message.id}: {exc}")

    async def _close_ticket(self, channel: discord.TextChannel, ticket: dict, config: dict, closer: discord.Member):
        guild = channel.guild
        log_channel = guild.get_channel(int(config["log_channel_id"])) if config.get("log_channel_id") else None
        if log_channel and isinstance(log_channel, (discord.TextChannel, discord.Thread)):
            try:
                log_embed = make_embed("🔒 Ticket Closed", f"**Channel:** {channel.mention}\n**Creator:** <@{ticket['opener_id']}>\n**Closed by:** {closer.mention}\n**Option:** {ticket.get('option_label', 'Ticket')}\n**Ticket ID:** `{channel.id}`")
                await log_channel.send(embed=log_embed, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as exc:
                print(f"Tickets Cog: could not log closed ticket {channel.id}: {exc}")

        opener = guild.get_member(int(ticket["opener_id"]))
        if opener is None:
            try:
                opener = await guild.fetch_member(int(ticket["opener_id"]))
            except discord.HTTPException:
                opener = None
        if opener:
            overwrite = channel.overwrites_for(opener)
            overwrite.view_channel = False
            overwrite.send_messages = False
            overwrite.read_message_history = False
            try:
                await channel.set_permissions(opener, overwrite=overwrite, reason=f"Ticket closed by {closer}")
            except discord.HTTPException:
                pass
        await self.tickets_collection.update_one({"_id": channel.id}, {"$set": {"closed": True, "closed_at": int(time.time()), "closed_by": closer.id, "closed_by_name": closer.display_name}})
        try:
            suffix = str(config.get("closed_topic_suffix", "(closed)")).strip()
            topic = (channel.topic or "").replace(suffix, "").strip()
            if suffix:
                topic = f"{topic} {suffix}".strip()
            name = channel.name if channel.name.startswith("closed-") else f"closed-{channel.name}"
            await channel.edit(name=name[:100], topic=topic[:1024], reason=f"Ticket closed by {closer}")
        except discord.HTTPException:
            pass
        close_text = _format_ticket(str(config.get("close_message", "Ticket closed by {user}.")), user=closer, guild=guild, channel=channel, option=ticket.get("option_label", "Ticket"), ticket_id=channel.id)
        await channel.send(embed=make_embed("🔒 Ticket Closed", close_text), view=TicketControlView(self, mode="closed"), allowed_mentions=discord.AllowedMentions.none())
        delay_minutes = int(config.get("delete_after_close_minutes", 0) or 0)
        if delay_minutes > 0:
            task = asyncio.create_task(self._delete_channel_after(channel.id, delay_minutes * 60))
            old = self._delete_tasks.pop(channel.id, None)
            if old:
                old.cancel()
            self._delete_tasks[channel.id] = task
            task.add_done_callback(lambda _task, channel_id=channel.id: self._delete_tasks.pop(channel_id, None))

    async def _delete_ticket(self, channel: discord.TextChannel, ticket: dict, config: dict, *, deleter: discord.Member | None = None, interaction: discord.Interaction | None = None) -> bool:
        guild = channel.guild
        deleted_at = datetime.now(timezone.utc)
        pending = self._delete_tasks.pop(channel.id, None)
        if pending is not None and pending is not asyncio.current_task():
            pending.cancel()
        if deleter is not None:
            deleted_by_text = f"{deleter.display_name} ({deleter.id})"
            deleted_by_mention = deleter.mention
            reason = f"Ticket deleted by {deleter} ({deleter.id})"
        else:
            deleted_by_text = deleted_by_mention = "Automatic deletion (close timer)"
            reason = "Ticket auto-deleted after configured close delay"

        transcript_bytes: bytes | None = None
        transcript_name = "transcript.html"
        message_count = 0
        if config.get("transcripts_enabled", True):
            try:
                built, message_count = await build_ticket_transcript(channel, ticket, deleted_by=deleted_by_text, deleted_at=deleted_at)
                transcript_bytes, transcript_name = built.fp.getvalue(), built.filename
            except Exception as exc:
                print(f"Tickets Cog: could not build transcript for {channel.id}: {exc}")

        def transcript_file() -> discord.File:
            return discord.File(io.BytesIO(transcript_bytes or b""), filename=transcript_name)

        try:
            await channel.delete(reason=reason)
        except discord.HTTPException as exc:
            print(f"Tickets Cog: could not delete ticket {channel.id}: {exc}")
            if interaction is not None:
                try:
                    await interaction.followup.send(embed=error_embed(f"Could not delete this ticket: {exc}"), ephemeral=True)
                except discord.HTTPException:
                    pass
            return False

        try:
            await self.tickets_collection.update_one(
                {"_id": channel.id},
                {"$set": {"closed": True, "deleted": True, "deleted_at": int(deleted_at.timestamp()), "deleted_by": deleter.id if deleter else None}},
            )
        except Exception as exc:
            print(f"Tickets Cog: could not mark ticket {channel.id} deleted: {exc}")

        number = ticket.get("ticket_number")
        lines = []
        if number:
            lines.append(f"**Ticket:** #{int(number):04d}")
        lines.extend([
            f"**Option:** {ticket.get('option_label', 'Ticket')}",
            f"**Ticket ID:** `{channel.id}`",
            f"**Creator:** <@{ticket['opener_id']}>",
        ])
        if ticket.get("claimed_by"):
            lines.append(f"**Claimed by:** <@{ticket['claimed_by']}>")
        if ticket.get("closed_by"):
            lines.append(f"**Closed by:** <@{ticket['closed_by']}>")
        lines.append(f"**Deleted by:** {deleted_by_mention}")
        if transcript_bytes is not None:
            lines.append(f"**Messages archived:** {message_count}")
        log_embed = make_embed("🗑️ Ticket Deleted", "\n".join(lines))

        log_channel = guild.get_channel(int(config["log_channel_id"])) if config.get("log_channel_id") else None
        sent = False
        if log_channel and isinstance(log_channel, (discord.TextChannel, discord.Thread)):
            try:
                kwargs = {"embed": log_embed, "allowed_mentions": discord.AllowedMentions.none()}
                if transcript_bytes is not None:
                    kwargs["file"] = transcript_file()
                await log_channel.send(**kwargs)
                sent = True
            except discord.HTTPException as exc:
                print(f"Tickets Cog: could not log deleted ticket {channel.id}: {exc}")
        if not sent and transcript_bytes is not None and interaction is not None:
            try:
                await interaction.followup.send(
                    embed=make_embed("📄 Ticket Transcript", "No log channel could be used, so the transcript is attached here for you."),
                    file=transcript_file(),
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
        return True

    async def _delete_channel_after(self, channel_id: int, seconds: int):
        await asyncio.sleep(seconds)
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            ticket = await self.tickets_collection.find_one({"_id": channel_id})
            if ticket is None or not ticket.get("closed"):
                return
            config = await self.get_config(channel.guild.id)
            await self._delete_ticket(channel, ticket, config, deleter=None)
        except Exception as exc:
            print(f"Tickets Cog: auto-delete failed for {channel_id}: {exc}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))
    if bot.get_command("tickets") is None:
        bot.add_command(tickets_command)