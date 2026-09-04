import discord
from discord.ext import commands
import motor.motor_asyncio
import certifi

from views.autolockview import (
    AutoLockMainView,
    CATEGORY_LABELS,
    CATEGORY_ORDER,
    CATEGORY_DESCRIPTIONS,
    ROLE_CATEGORIES,
    ROLE_COMMAND_HINTS,
    RESTRICT_CATEGORIES,
)
from .base import config_group

MONGO_URI = "mongodb+srv://KurtisLam:CsHLOnDqihiU5uYG@cluster0.7rwx3oc.mongodb.net/?appName=Cluster0"

DEFAULT_DELAY = 10


def _default_category() -> dict:
    # Note: no "role" field. Roles for rare/regional/gmax/paradox/eevos live
    # in the `pings` collection (managed by PokePings) and are only read from
    # here, never written.
    return {"delay": DEFAULT_DELAY, "whitelist": [], "restrict_unlockers": False}


@config_group.command(
    name="autolock",
    aliases=["a", "al"],
    description="Configure autolock behavior for rare/regional/gmax/paradox/eevos and sh/cl/tp/rp locks.",
)
@commands.has_permissions(administrator=True)
async def autolockconfig(ctx: commands.Context):
    cog = ctx.bot.get_cog("AutoLockConfig")

    if cog is None:
        return await ctx.send("⚠️ Internal error: `AutoLockConfig` cog is not loaded.")

    embed = await cog.build_main_embed(ctx.guild)
    view = AutoLockMainView(cog, guild_id=ctx.guild.id, author_id=ctx.author.id)
    await ctx.send(embed=embed, view=view)


@autolockconfig.error
async def autolockconfig_error(ctx: commands.Context, error: Exception):
    if isinstance(error, commands.MissingPermissions):
        pass
    else:
        await ctx.send(f"⚠️ Internal error: `{error}`")


class AutoLockConfig(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI, tlsCAFile=certifi.where())
        self.db = self.mongo_client["utilities"]
        self.collection = self.db["autolock"]
        # Same collection PokePings (pings.py) writes rare/regional/gmax/
        # paradox/eevos role IDs to. We only ever read from it.
        self.pings_collection = self.db["pings"]

    async def cog_load(self):
        try:
            await self.mongo_client.admin.command("ping")
        except Exception as e:
            print(f"AutoLockConfig Cog: MongoDB warmup failed: {e}")

    # --- storage helpers (autolock settings) ----------------------------------

    async def get_guild_config(self, guild_id: int) -> dict:
        gid = str(guild_id)
        doc = await self.collection.find_one({"_id": gid})

        if not doc:
            doc = {"_id": gid, **{cat: _default_category() for cat in CATEGORY_ORDER}}
            await self.collection.insert_one(doc)
            return doc

        # Backfill any missing categories/fields so older docs stay compatible
        # if new categories or settings are added later (e.g. gmax/paradox/eevos).
        needs_update = False
        for cat in CATEGORY_ORDER:
            if cat not in doc:
                doc[cat] = _default_category()
                needs_update = True
            else:
                for key, val in _default_category().items():
                    if key not in doc[cat]:
                        doc[cat][key] = val
                        needs_update = True
                # Drop any legacy "role" field left over from an older schema.
                doc[cat].pop("role", None)

        if needs_update:
            await self.collection.update_one(
                {"_id": gid}, {"$set": {cat: doc[cat] for cat in CATEGORY_ORDER}}, upsert=True
            )

        return doc

    async def get_category_config(self, guild_id: int, category: str) -> dict:
        doc = await self.get_guild_config(guild_id)
        return doc.get(category, _default_category())

    async def set_delay(self, guild_id: int, category: str, delay: int):
        await self.get_guild_config(guild_id)
        await self.collection.update_one(
            {"_id": str(guild_id)}, {"$set": {f"{category}.delay": delay}}, upsert=True
        )

    async def add_whitelist(self, guild_id: int, category: str, target_id: int):
        await self.get_guild_config(guild_id)
        await self.collection.update_one(
            {"_id": str(guild_id)},
            {"$addToSet": {f"{category}.whitelist": target_id}},
            upsert=True,
        )

    async def remove_whitelist(self, guild_id: int, category: str, target_id: int):
        await self.get_guild_config(guild_id)
        await self.collection.update_one(
            {"_id": str(guild_id)},
            {"$pull": {f"{category}.whitelist": target_id}},
            upsert=True,
        )

    async def toggle_restrict(self, guild_id: int, category: str) -> bool:
        cfg = await self.get_category_config(guild_id, category)
        new_val = not cfg.get("restrict_unlockers", False)
        await self.collection.update_one(
            {"_id": str(guild_id)},
            {"$set": {f"{category}.restrict_unlockers": new_val}},
            upsert=True,
        )
        return new_val

    # --- reading roles from PokePings (never written here) --------------------

    async def get_ping_role(self, guild: discord.Guild, category: str) -> discord.Role | None:
        doc = await self.pings_collection.find_one({"_id": str(guild.id)})
        role_id = (doc or {}).get("roles", {}).get(category)
        if not role_id:
            return None
        try:
            return guild.get_role(int(role_id))
        except (TypeError, ValueError):
            return None

    # --- embed builders --------------------------------------------------------

    def _format_whitelist(self, guild: discord.Guild, whitelist: list) -> str:
        if not whitelist:
            return "None (applies to *all* channels)"

        parts = []
        for wid in whitelist:
            ch = guild.get_channel(wid)
            if ch is None:
                parts.append(f"`{wid}` (unknown/deleted)")
            elif isinstance(ch, discord.CategoryChannel):
                parts.append(f"📁 {ch.name}")
            else:
                parts.append(ch.mention)
        return "\n".join(parts)

    async def build_main_embed(self, guild: discord.Guild) -> discord.Embed:
        doc = await self.get_guild_config(guild.id)

        embed = discord.Embed(
            title=f"🔒 AutoLock Configuration — {guild.name}",
            description="Select a category below to configure its lock behavior.",
            color=discord.Color.blurple(),
        )

        for cat in CATEGORY_ORDER:
            cfg = doc.get(cat, _default_category())
            lines = [
                f"Delay: `{cfg['delay']}s`",
                f"Whitelist: `{len(cfg['whitelist'])}` entr{'y' if len(cfg['whitelist']) == 1 else 'ies'}",
            ]
            if cat in ROLE_CATEGORIES:
                role = await self.get_ping_role(guild, cat)
                lines.append(f"Role: {role.mention if role else 'Not set'}")
            if cat in RESTRICT_CATEGORIES:
                lines.append(f"Restrict Unlockers: `{cfg.get('restrict_unlockers', False)}`")

            embed.add_field(name=CATEGORY_LABELS[cat], value="\n".join(lines), inline=True)

        return embed

    async def build_category_embed(self, guild: discord.Guild, category: str) -> discord.Embed:
        cfg = await self.get_category_config(guild.id, category)

        embed = discord.Embed(
            title=f"⚙️ {CATEGORY_LABELS[category]} Settings — {guild.name}",
            color=discord.Color.blurple(),
        )

        if category in CATEGORY_DESCRIPTIONS:
            embed.description = CATEGORY_DESCRIPTIONS[category]

        embed.add_field(name="Lock Delay", value=f"`{cfg['delay']}` seconds", inline=False)
        embed.add_field(name="Whitelist", value=self._format_whitelist(guild, cfg["whitelist"]), inline=False)

        if category in ROLE_CATEGORIES:
            role = await self.get_ping_role(guild, category)
            embed.add_field(
                name="Ping Role (read-only here)",
                value=(
                    f"{role.mention if role else 'Not set'}\n"
                    f"-# Set with {ROLE_COMMAND_HINTS[category]}, not here."
                ),
                inline=False,
            )

        if category in RESTRICT_CATEGORIES:
            embed.add_field(
                name="Restrict Unlockers",
                value=(
                    f"`{cfg.get('restrict_unlockers', False)}` — when enabled, only the user(s) "
                    "pinged in the trigger message may unlock the channel (overrides the "
                    "\"anyone can unlock\" behavior of rare/regional/gmax/paradox/eevos)."
                ),
                inline=False,
            )

        return embed


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoLockConfig(bot))