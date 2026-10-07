import asyncio
import re
import time
from dataclasses import dataclass, field

import discord
from discord.ext import commands

from views.common import EMBED_COLOR, error_embed
from views.embeds import handle_command_error
from views.timerview import (
    DEFAULT_SECONDS,
    TIMER_CATEGORIES,
    TIMER_DESCRIPTIONS,
    TIMER_LABELS,
    TIMER_NAMES,
    build_main_page,
)
from .baseconfigs import config_group

POKETWO_ID = 716390085896962058

READY_TEXT = "You can catch the pokémon now!"
STEAL_TEXT = "{mention} you have **{short}** stole **{pokemon}**!"


def _default_category() -> dict:
    return {"enabled": False, "seconds": DEFAULT_SECONDS, "whitelist": []}


def _label(category: str) -> str:
    return TIMER_LABELS.get(category, category.upper())


def _lock_whitelist_covers(channel, whitelist: list) -> bool:
    """Same rule AutoLock uses: empty == nothing, '*' == whole server,
    otherwise the channel itself or its category must be listed."""
    if not whitelist:
        return False
    entries = {str(w) for w in whitelist}
    if "*" in entries or str(channel.id) in entries:
        return True
    category_id = getattr(channel, "category_id", None)
    return bool(category_id and str(category_id) in entries)


@dataclass
class ActiveTimer:
    category: str
    pokemon: str
    hunters: set[int]
    message: discord.Message | None = None
    task: asyncio.Task | None = None


@config_group.command(
    name="timer",
    aliases=["t"],
    description="Configure catch timers.",
)
@commands.has_permissions(administrator=True)
async def timerconfig(ctx: commands.Context):
    cog = ctx.bot.get_cog("Timer")

    if cog is None:
        return await ctx.send(embed=error_embed("Timer is not loaded."))

    view = await build_main_page(cog, ctx.guild, ctx.guild.id, ctx.author.id)
    view.message = await ctx.send(view=view)


@timerconfig.error
async def timerconfig_error(ctx: commands.Context, error: Exception):
    await handle_command_error(ctx, error)


class Timer(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active: dict[int, ActiveTimer] = {}  # channel_id -> running timer

    async def cog_unload(self):
        for channel_id in list(self.active):
            await self._finish(channel_id, delete=False)

    # --- Mongo ----------------------------------------------------------------

    @property
    def collection(self):
        return self.bot.mongo_client["utilities"]["timer"]

    async def get_guild_config(self, guild_id: int) -> dict:
        gid = str(guild_id)
        doc = await self.collection.find_one({"_id": gid})

        if not doc:
            doc = {"_id": gid, **{cat: _default_category() for cat in TIMER_CATEGORIES}}
            await self.collection.insert_one(doc)
            return doc

        needs_update = False
        for cat in TIMER_CATEGORIES:
            if not isinstance(doc.get(cat), dict):
                doc[cat] = _default_category()
                needs_update = True
            else:
                for key, val in _default_category().items():
                    if key not in doc[cat]:
                        doc[cat][key] = val
                        needs_update = True

        if needs_update:
            await self.collection.update_one(
                {"_id": gid}, {"$set": {cat: doc[cat] for cat in TIMER_CATEGORIES}}, upsert=True
            )
        return doc

    async def get_category_config(self, guild_id: int, category: str) -> dict:
        doc = await self.get_guild_config(guild_id)
        return doc.get(category, _default_category())

    async def toggle_timer(self, guild_id: int, category: str) -> bool:
        cfg = await self.get_category_config(guild_id, category)
        new_val = not cfg.get("enabled", False)
        await self.collection.update_one(
            {"_id": str(guild_id)}, {"$set": {f"{category}.enabled": new_val}}, upsert=True
        )
        return new_val

    async def set_seconds(self, guild_id: int, category: str, seconds: int):
        await self.get_guild_config(guild_id)
        await self.collection.update_one(
            {"_id": str(guild_id)}, {"$set": {f"{category}.seconds": seconds}}, upsert=True
        )

    async def add_whitelist(self, guild: discord.Guild, category: str, raw_input: str):
        cfg = await self.get_category_config(guild.id, category)
        current = cfg.get("whitelist", [])
        updated = False

        for item in (i.strip() for i in str(raw_input).split(",") if i.strip()):
            if item == "*":
                if "*" not in current:
                    current.append("*")
                    updated = True
                continue

            clean_id = re.sub(r"\D", "", item)
            target = guild.get_channel(int(clean_id)) if clean_id else None
            if target is None:
                name = item.lower().lstrip("#")
                target = discord.utils.find(lambda c: c.name.lower() == name, guild.channels)

            if target and target.id not in current:
                current.append(target.id)
                updated = True

        if updated:
            await self.collection.update_one(
                {"_id": str(guild.id)}, {"$set": {f"{category}.whitelist": current}}, upsert=True
            )

    async def remove_whitelist(self, guild: discord.Guild, category: str, raw_input: str):
        cfg = await self.get_category_config(guild.id, category)
        current = cfg.get("whitelist", [])
        if not current:
            return

        ordered = self._get_ordered_whitelist(guild, current)
        to_remove: set = set()
        clear_all = False

        for item in (i.strip() for i in str(raw_input).split(",") if i.strip()):
            if item == "*":
                clear_all = True
                break

            if item.isdigit():
                val = int(item)
                if 1 <= val <= len(ordered):
                    target = ordered[val - 1]
                    to_remove.update({target, str(target)})
                to_remove.update({val, str(val)})

            clean_id = re.sub(r"\D", "", item)
            if clean_id:
                to_remove.update({int(clean_id), clean_id})

            name = item.lower().lstrip("#")
            matched = discord.utils.find(lambda c: c.name.lower() == name, guild.channels)
            if matched:
                to_remove.update({matched.id, str(matched.id)})

        new_whitelist = [] if clear_all else [
            x for x in current if x not in to_remove and str(x) not in to_remove
        ]
        await self.collection.update_one(
            {"_id": str(guild.id)}, {"$set": {f"{category}.whitelist": new_whitelist}}, upsert=True
        )

    # --- Embed builders -------------------------------------------------------

    def _get_ordered_whitelist(self, guild: discord.Guild, whitelist: list) -> list:
        categories, channels, others = [], [], []
        for item in whitelist:
            if str(item) == "*":
                others.append("*")
                continue
            try:
                wid = int(item)
            except (ValueError, TypeError):
                others.append(item)
                continue

            ch = guild.get_channel(wid)
            if ch is None:
                others.append(wid)
            elif isinstance(ch, discord.CategoryChannel):
                categories.append((ch.position, ch.name, wid))
            else:
                channels.append((ch.position, ch.name, wid))

        categories.sort(key=lambda x: (x[0], x[1]))
        channels.sort(key=lambda x: (x[0], x[1]))
        return [c[2] for c in categories] + [c[2] for c in channels] + others

    def _format_whitelist(self, guild: discord.Guild, whitelist: list) -> str:
        if not whitelist:
            return "None"

        lines = []
        for idx, item in enumerate(self._get_ordered_whitelist(guild, whitelist), start=1):
            if str(item) == "*":
                lines.append(f"`{idx}.` 🌐 Whole Server (`*`)")
                continue
            ch = guild.get_channel(int(item)) if str(item).isdigit() else None
            if ch is None:
                lines.append(f"`{idx}.` `{item}` (deleted)")
            elif isinstance(ch, discord.CategoryChannel):
                lines.append(f"`{idx}.` 📁 {ch.name}")
            else:
                lines.append(f"`{idx}.` {ch.mention}")
        return "\n".join(lines)

    async def build_main_embed(self, guild: discord.Guild) -> discord.Embed:
        doc = await self.get_guild_config(guild.id)
        lines = []
        for cat in TIMER_CATEGORIES:
            on = doc.get(cat, _default_category()).get("enabled", False)
            lines.append(f"{'✅' if on else '❌'} **{_label(cat)}** — {'On' if on else 'Off'}")

        return discord.Embed(
            title=f"⏲️ Timers — {guild.name}",
            description="Pick a timer to see its settings.\n\n" + "\n".join(lines),
            color=discord.Color.blurple(),
        )

    async def build_category_embed(self, guild: discord.Guild, category: str) -> discord.Embed:
        cfg = await self.get_category_config(guild.id, category)
        on = cfg.get("enabled", False)

        embed = discord.Embed(
            title=f"⚙️ {_label(category)} — {guild.name}",
            description=TIMER_DESCRIPTIONS.get(category),
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Timer Status", value=f"`{'Enabled ✅' if on else 'Disabled ❌'}`", inline=False)
        embed.add_field(name="Timer Length", value=f"`{cfg.get('seconds', DEFAULT_SECONDS)}s`", inline=False)
        embed.add_field(
            name="Whitelist",
            value=self._format_whitelist(guild, cfg.get("whitelist", [])),
            inline=False,
        )
        return embed

    # --- Runtime --------------------------------------------------------------

    @staticmethod
    def _channel_whitelisted(channel, whitelist: list) -> bool:
        wl = {str(x) for x in whitelist}
        if not wl:
            return False
        if "*" in wl or str(channel.id) in wl:
            return True
        for attr in ("category_id", "parent_id"):
            val = getattr(channel, attr, None)
            if val and str(val) in wl:
                return True
        parent = getattr(channel, "parent", None)  # thread inside a categorised channel
        cat_id = getattr(parent, "category_id", None)
        return bool(cat_id and str(cat_id) in wl)

    async def _lock_active(
        self,
        guild: discord.Guild,
        channel,
        activated_categories: list[str],
    ) -> bool:
        """True if AutoLock would lock this channel for this spawn.

        Mirrors AutoLock.process_autolock: a category counts when its effective
        config (guild default + per-channel override) is enabled AND its lock
        whitelist covers the channel.
        """
        config_cog = self.bot.get_cog("AutoLockConfig")
        if config_cog is None:
            return False

        for cat in dict.fromkeys(activated_categories):
            try:
                cfg = await config_cog.get_category_config_channel(guild.id, channel.id, cat)
            except Exception:
                continue
            if cfg.get("enabled", False) and _lock_whitelist_covers(channel, cfg.get("whitelist", [])):
                return True
        return False

    async def _non_afk_users(self, guild_id: int, category: str, uids: set[int]) -> set[int]:
        """Hunters who are NOT AFK. Uses SetAFK.format_ping_list (the same filter the
        ping lines use) and keeps the user ids that come back as mentions."""
        afk_cog = self.bot.get_cog("SetAFK")
        if not afk_cog or not uids:
            return set(uids)
        try:
            pings = await afk_cog.format_ping_list(set(uids), guild_id, category)
        except Exception:
            return set(uids)
        found = {int(m) for text in pings for m in re.findall(r"<@!?(\d+)>", str(text))}
        return found & set(uids)

    async def start_timer(
        self,
        channel: discord.abc.Messageable,
        guild: discord.Guild | None,
        pokemon_name: str,
        activated_categories: list[str],
        category_users: dict[str, set[int]],
    ):
        """Called by the recognizer right after it posts its detection reply."""
        if guild is None:
            return

        # A fresh spawn replaces whatever timer was running in this channel.
        await self._finish(channel.id, delete=True)

        # The only exception: if a lock is enabled for this channel, no timer.
        if await self._lock_active(guild, channel, activated_categories):
            return

        doc = await self.get_guild_config(guild.id)

        chosen = None
        for cat in TIMER_CATEGORIES:  # sh > cl > rp > tp
            if cat not in activated_categories:
                continue
            cfg = doc.get(cat, _default_category())
            if not cfg.get("enabled", False):
                continue
            if not self._channel_whitelisted(channel, cfg.get("whitelist", [])):
                continue
            users = category_users.get(cat, set())
            # Even if only 1 of 10 hunters is active, the timer still runs.
            if not await self._non_afk_users(guild.id, cat, users):
                continue
            chosen = (cat, cfg, users)
            break

        if chosen is None:
            return

        cat, cfg, users = chosen
        seconds = int(cfg.get("seconds", DEFAULT_SECONDS))
        end_ts = int(time.time()) + seconds

        embed = discord.Embed(
            description=f"**{TIMER_NAMES[cat]}** timer will end <t:{end_ts}:R>",
            color=EMBED_COLOR,
        )
        try:
            msg = await channel.send(embed=embed)
        except discord.HTTPException:
            return

        timer = ActiveTimer(category=cat, pokemon=pokemon_name, hunters=set(users), message=msg)
        timer.task = asyncio.create_task(self._run(channel, timer, seconds))
        self.active[channel.id] = timer

    async def _run(self, channel, timer: ActiveTimer, seconds: int):
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return

        # Only the timer that is still registered may announce the end.
        if self.active.get(channel.id) is not timer:
            return
        self.active.pop(channel.id, None)

        try:
            if timer.message:
                await timer.message.delete()
        except discord.HTTPException:
            pass
        try:
            await channel.send(READY_TEXT)
        except discord.HTTPException:
            pass

    async def _finish(self, channel_id: int, delete: bool = True) -> ActiveTimer | None:
        timer = self.active.pop(channel_id, None)
        if timer is None:
            return None
        if timer.task and timer.task is not asyncio.current_task():
            timer.task.cancel()
        if delete and timer.message:
            try:
                await timer.message.delete()
            except discord.HTTPException:
                pass
        return timer

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.id != POKETWO_ID or message.channel.id not in self.active:
            return

        text = " ".join(
            [message.content.lower()]
            + [
                part.lower()
                for e in message.embeds
                for part in (e.title, e.description)
                if part
            ]
        )

        if "fled" in text:
            await self._finish(message.channel.id, delete=True)
            return

        if "you caught a level" not in text:
            return

        timer = await self._finish(message.channel.id, delete=True)
        if timer is None:
            return

        match = re.search(r"<@!?(\d+)>", message.content)
        if not match:
            return
        catcher_id = int(match.group(1))

        if catcher_id in timer.hunters:
            return

        try:
            await message.channel.send(
                STEAL_TEXT.format(
                    mention=f"<@{catcher_id}>",
                    short=timer.category.upper(),
                    pokemon=timer.pokemon,
                ),
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Timer(bot))