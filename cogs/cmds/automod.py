"""Automod trap channel (administrators only).

Commands (hybrid: work as ``.automod ...`` and as ``/automod ...``):

* ``automod``                           open the admin panel (send the warning message, switch kick/ban)
* ``automod set <channel> <kick|ban>``  choose the trap channel and the punishment
* ``automod action <kick|ban>``         change only the punishment
* ``automod warn``                      post the red warning message in the trap channel
* ``automod disable``                   turn the trap off
* ``clearautomodlogs <guild_id> [kick|ban|all]``  owner only: wipe the stored punishment logs of a server

Anyone who is not an administrator and sends a message in the trap channel is punished at once.
Every punishment is stored in MongoDB (``utilities.automod_logs``); the counter button on the
warning message ("Kicks: xx" / "Bans: xx") shows who was punished and when to anyone who presses it.
"""
import asyncio
import time
import typing

import discord
from discord import app_commands
from discord.ext import commands

from views.common_views import confirm
from views.automod_views import ACTION_WORDS, AutomodPanelView, AutomodWarningView
from views.embeds import BRAND_COLOR, err_embed, handle_command_error, info_embed, ok_embed
from views.navigate import PaginatorView

LOG_PAGE_SIZE = 10
LOG_FETCH_LIMIT = 500
REFRESH_DELAY = 5
_NO_PINGS = discord.AllowedMentions.none()

Action = typing.Literal["kick", "ban"]
ClearAction = typing.Literal["kick", "ban", "all"]


class Automod(commands.Cog):
    """Kick or ban anyone who talks in a trap channel."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._config: dict[int, dict] = {}
        self._busy: set[tuple[int, int]] = set()
        self._refresh_tasks: dict[int, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # Setup / teardown
    # ------------------------------------------------------------------

    @property
    def configs(self):
        return self.bot.mongo_client["utilities"]["automod_config"]

    @property
    def logs(self):
        return self.bot.mongo_client["utilities"]["automod_logs"]

    async def cog_load(self):
        if self.bot.mongo_client is None:
            print("Automod Cog: MongoDB is not configured, automod is disabled.")
            return
        try:
            await self.logs.create_index([("guild_id", 1), ("action", 1), ("at", -1)])
            async for doc in self.configs.find({}):
                self._config[doc["_id"]] = doc
        except Exception as e:
            print(f"Automod Cog: could not load configuration: {e}")
        self.bot.add_view(AutomodWarningView(self))

    def cog_unload(self):
        for task in self._refresh_tasks.values():
            task.cancel()
        self._refresh_tasks.clear()

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        await handle_command_error(ctx, error)

    def _db_ready(self) -> bool:
        return self.bot.mongo_client is not None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _save_config(self, guild_id: int, **fields):
        doc = {**self._config.get(guild_id, {"_id": guild_id}), **fields}
        self._config[guild_id] = doc
        await self.configs.update_one({"_id": guild_id}, {"$set": fields}, upsert=True)

    async def _count(self, guild_id: int, action: str) -> int:
        return await self.logs.count_documents({"guild_id": guild_id, "action": action})

    def _warning_view(self, action: str, count: int) -> AutomodWarningView:
        avatar = self.bot.user.display_avatar.url if self.bot.user else None
        return AutomodWarningView(self, action=action, count=count, avatar_url=avatar)

    async def _panel_embed(self, guild: discord.Guild) -> discord.Embed:
        cfg = self._config.get(guild.id) or {}
        channel = guild.get_channel(cfg.get("channel_id", 0))
        action = cfg.get("action", "kick")
        kicks, bans = await asyncio.gather(self._count(guild.id, "kick"), self._count(guild.id, "ban"))
        embed = discord.Embed(title="🛡️ Automod", color=BRAND_COLOR)
        if channel is None:
            embed.description = "No trap channel is set. Use `automod set <channel> <kick|ban>`."
        else:
            embed.description = (
                f"**Channel:** {channel.mention}\n"
                f"**Punishment:** {ACTION_WORDS[action]['verb']}\n"
                f"**Kicks:** {kicks:,} • **Bans:** {bans:,}"
            )
        return embed

    def _panel_view(self, guild: discord.Guild, author_id: int) -> AutomodPanelView:
        cfg = self._config.get(guild.id) or {}
        return AutomodPanelView(
            self,
            author_id=author_id,
            action=cfg.get("action", "kick"),
            has_channel=guild.get_channel(cfg.get("channel_id", 0)) is not None,
        )

    async def send_warning(self, guild: discord.Guild) -> tuple[bool, str]:
        cfg = self._config.get(guild.id)
        channel = guild.get_channel(cfg["channel_id"]) if cfg else None
        if channel is None:
            return False, "No trap channel is set."
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages):
            return False, f"I need View Channel and Send Messages in {channel.mention}."

        action = cfg.get("action", "kick")
        count = await self._count(guild.id, action)
        try:
            message = await channel.send(view=self._warning_view(action, count), allowed_mentions=_NO_PINGS)
        except discord.HTTPException as e:
            return False, f"Couldn't send the warning: {e.text or e}"

        old_id = cfg.get("warning_message_id")
        if old_id and old_id != message.id:
            try:
                await channel.get_partial_message(old_id).delete()
            except discord.HTTPException:
                pass
        await self._save_config(guild.id, warning_message_id=message.id)
        return True, channel.mention

    def schedule_refresh(self, guild_id: int):
        """Update the counter on the warning message (several punishments merge into one edit)."""
        if guild_id in self._refresh_tasks:
            return
        task = asyncio.create_task(self._refresh_later(guild_id))
        self._refresh_tasks[guild_id] = task
        task.add_done_callback(lambda _t, gid=guild_id: self._refresh_tasks.pop(gid, None))

    async def _refresh_later(self, guild_id: int):
        await asyncio.sleep(REFRESH_DELAY)
        await self.refresh_warning(guild_id)

    async def refresh_warning(self, guild_id: int):
        cfg = self._config.get(guild_id)
        if not cfg or not cfg.get("warning_message_id"):
            return
        channel = self.bot.get_channel(cfg["channel_id"])
        if channel is None:
            return
        action = cfg.get("action", "kick")
        try:
            count = await self._count(guild_id, action)
            await channel.get_partial_message(cfg["warning_message_id"]).edit(view=self._warning_view(action, count))
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------
    # Punishing
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        guild = message.guild
        if guild is None or message.author.bot:
            return
        cfg = self._config.get(guild.id)
        if not cfg or message.channel.id != cfg["channel_id"]:
            return

        member = message.author
        if not isinstance(member, discord.Member):
            return
        if member.guild_permissions.administrator or member.id == guild.owner_id:
            return

        action = cfg.get("action", "kick")
        me = guild.me
        needed = me.guild_permissions.ban_members if action == "ban" else me.guild_permissions.kick_members
        if not needed or member.top_role >= me.top_role:
            return

        key = (guild.id, member.id)
        if key in self._busy:
            return
        self._busy.add(key)
        try:
            await self._punish(message, member, action)
        finally:
            self._busy.discard(key)

    async def _punish(self, message: discord.Message, member: discord.Member, action: str):
        guild = member.guild
        channel = message.channel
        words = ACTION_WORDS[action]
        reason = f"Automod: sent a message in #{channel.name}"

        try:
            await message.delete()
        except discord.HTTPException:
            pass

        try:
            await member.send(
                embed=err_embed(f"You were {words['verb']} from {guild.name} for sending a message in #{channel.name}.")
            )
        except discord.HTTPException:
            pass

        try:
            if action == "ban":
                await guild.ban(member, reason=reason, delete_message_seconds=0)
            else:
                await guild.kick(member, reason=reason)
        except discord.NotFound:
            return
        except discord.HTTPException as e:
            print(f"Automod Cog: could not {action} {member.id} in {guild.id}: {e}")
            return

        try:
            await self.logs.insert_one({
                "guild_id": guild.id,
                "user_id": member.id,
                "user_name": str(member),
                "action": action,
                "channel_id": channel.id,
                "at": int(time.time()),
            })
        except Exception as e:
            print(f"Automod Cog: could not log {action} of {member.id}: {e}")
        self.schedule_refresh(guild.id)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        cfg = self._config.get(channel.guild.id)
        if cfg and cfg["channel_id"] == channel.id:
            self._config.pop(channel.guild.id, None)
            try:
                await self.configs.delete_one({"_id": channel.guild.id})
            except Exception as e:
                print(f"Automod Cog: could not remove config of {channel.guild.id}: {e}")

    # ------------------------------------------------------------------
    # Counter button + panel callbacks
    # ------------------------------------------------------------------

    async def handle_log(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None or not self._db_ready():
            return await interaction.response.send_message(embed=err_embed("Unavailable right now."), ephemeral=True)

        action = (self._config.get(guild.id) or {}).get("action", "kick")
        words = ACTION_WORDS[action]
        total = await self._count(guild.id, action)
        if not total:
            return await interaction.response.send_message(
                embed=info_embed("Nobody yet", f"Nobody has been {words['verb']} so far.", emoji="📭"), ephemeral=True
            )

        docs = await self.logs.find({"guild_id": guild.id, "action": action}).sort("at", -1).to_list(length=LOG_FETCH_LIMIT)
        lines = [
            f"{i}. <@{d['user_id']}> (`{d.get('user_name', d['user_id'])}`) • <t:{d['at']}:f> (<t:{d['at']}:R>)"
            for i, d in enumerate(docs, start=1)
        ]
        pages = []
        page_count = (len(lines) + LOG_PAGE_SIZE - 1) // LOG_PAGE_SIZE
        for number in range(page_count):
            chunk = lines[number * LOG_PAGE_SIZE:(number + 1) * LOG_PAGE_SIZE]
            embed = discord.Embed(
                title=f"{words['noun']}: {total:,}",
                description="\n".join(chunk),
                color=discord.Color(0xE02B2B),
            )
            embed.set_footer(text=f"Page {number + 1}/{page_count} • newest first")
            pages.append(embed)

        if len(pages) == 1:
            return await interaction.response.send_message(embed=pages[0], ephemeral=True, allowed_mentions=_NO_PINGS)
        view = PaginatorView(pages, user_id=interaction.user.id)
        await interaction.response.send_message(embed=pages[0], view=view, ephemeral=True, allowed_mentions=_NO_PINGS)

    async def panel_send_warning(self, interaction: discord.Interaction):
        ok, detail = await self.send_warning(interaction.guild)
        if ok:
            embed = ok_embed("Warning Sent", f"Posted the warning in {detail}.", emoji="⚠️")
        else:
            embed = err_embed(detail)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def panel_toggle_action(self, interaction: discord.Interaction, view: AutomodPanelView):
        guild = interaction.guild
        current = (self._config.get(guild.id) or {}).get("action", "kick")
        await self._save_config(guild.id, action="ban" if current == "kick" else "kick")
        self.schedule_refresh(guild.id)
        await view.swap(
            interaction,
            embed=await self._panel_embed(guild),
            view=self._panel_view(guild, interaction.user.id),
        )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def _require_db(self, ctx: commands.Context) -> bool:
        if self._db_ready():
            return True
        await ctx.reply(embed=err_embed("MongoDB isn't configured, automod is unavailable.", emoji="⚠️"), mention_author=False)
        return False

    @commands.hybrid_group(
        name="automod",
        aliases=["am"],
        invoke_without_command=True,
        description="Kick or ban anyone who talks in a trap channel (administrators only).",
    )
    @app_commands.default_permissions(administrator=True)
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def automod(self, ctx: commands.Context):
        if not await self._require_db(ctx):
            return
        view = self._panel_view(ctx.guild, ctx.author.id)
        view.message = await ctx.reply(embed=await self._panel_embed(ctx.guild), view=view, mention_author=False)

    @automod.command(name="set", usage="<channel> <kick|ban>", description="Sets the trap channel and the punishment.")
    @app_commands.describe(channel="The trap channel.", action="Kick or ban whoever sends a message there.")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def automod_set(self, ctx: commands.Context, channel: discord.TextChannel, action: Action):
        if not await self._require_db(ctx):
            return
        await self._save_config(ctx.guild.id, channel_id=channel.id, action=action, warning_message_id=None)
        note = ""
        me = ctx.guild.me.guild_permissions
        if (action == "kick" and not me.kick_members) or (action == "ban" and not me.ban_members):
            note = f"\n⚠️ I don't have the {'Kick' if action == 'kick' else 'Ban'} Members permission yet."
        await ctx.reply(
            embed=ok_embed(
                "Automod Set",
                f"Anyone who sends a message in {channel.mention} will be **{ACTION_WORDS[action]['verb']}**.\n"
                f"Use `{ctx.clean_prefix}automod` to post the warning message.{note}",
                emoji="🛡️",
            ),
            mention_author=False,
        )

    @automod.command(name="action", usage="<kick|ban>", description="Changes the punishment.")
    @app_commands.describe(action="Kick or ban whoever sends a message in the trap channel.")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def automod_action(self, ctx: commands.Context, action: Action):
        if not await self._require_db(ctx):
            return
        if ctx.guild.id not in self._config:
            return await ctx.reply(embed=err_embed("Set a trap channel first with `automod set`."), mention_author=False)
        await self._save_config(ctx.guild.id, action=action)
        self.schedule_refresh(ctx.guild.id)
        await ctx.reply(
            embed=ok_embed("Punishment Changed", f"Trap channel violators will now be **{ACTION_WORDS[action]['verb']}**.", emoji="🛡️"),
            mention_author=False,
        )

    @automod.command(name="warn", aliases=["warning"], description="Posts the warning message in the trap channel.")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def automod_warn(self, ctx: commands.Context):
        if not await self._require_db(ctx):
            return
        ok, detail = await self.send_warning(ctx.guild)
        embed = ok_embed("Warning Sent", f"Posted the warning in {detail}.", emoji="⚠️") if ok else err_embed(detail)
        await ctx.reply(embed=embed, mention_author=False)

    @automod.command(name="disable", aliases=["off"], description="Turns the trap channel off.")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def automod_disable(self, ctx: commands.Context):
        if not await self._require_db(ctx):
            return
        if self._config.pop(ctx.guild.id, None) is None:
            return await ctx.reply(embed=err_embed("Automod isn't set up."), mention_author=False)
        await self.configs.delete_one({"_id": ctx.guild.id})
        await ctx.reply(embed=ok_embed("Automod Disabled", "The trap channel is no longer watched.", emoji="🛡️"), mention_author=False)


    @commands.hybrid_command(
        name="clearautomodlogs",
        aliases=["clearamlogs"],
        usage="<guild_id> [kick|ban|all]",
        description="Clears the automod kick/ban logs of a server (owner only).",
    )
    @app_commands.describe(guild_id="The ID of the server whose logs should be cleared.", action="Which logs to clear (default: all).")
    @commands.is_owner()
    async def clearautomodlogs(self, ctx: commands.Context, guild_id: str, action: ClearAction = "all"):
        if not await self._require_db(ctx):
            return
        try:
            target_id = int(guild_id.strip())
        except ValueError:
            return await ctx.reply(embed=err_embed("The guild ID must be a number."), mention_author=False)

        query = {"guild_id": target_id}
        if action != "all":
            query["action"] = action

        total = await self.logs.count_documents(query)
        guild = self.bot.get_guild(target_id)
        name = f"{guild.name} (`{target_id}`)" if guild else f"`{target_id}`"
        if not total:
            return await ctx.reply(embed=info_embed("Nothing to clear", f"There are no {action} logs for {name}.", emoji="📭"), mention_author=False)

        label = "kick and ban" if action == "all" else action
        result = await confirm(
            ctx,
            f"Delete **{total:,}** {label} log entr{'y' if total == 1 else 'ies'} of {name}?\nThis can't be undone.",
            title="⚠️ Clear Automod Logs",
            cancel_text="❌ Cancelled",
            timeout_text="⏱️ Timeout\nYou took too long to reply. Nothing was deleted.",
        )
        if not result:
            return

        try:
            deleted = (await self.logs.delete_many(query)).deleted_count
        except Exception as e:
            return await result.show(err_embed(f"Could not clear the logs: {e}"))
        self.schedule_refresh(target_id)
        await result.show(ok_embed("Logs Cleared", f"Deleted **{deleted:,}** {label} log entr{'y' if deleted == 1 else 'ies'} of {name}.", emoji="🧹"))


async def setup(bot: commands.Bot):
    await bot.add_cog(Automod(bot))