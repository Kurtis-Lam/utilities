import asyncio
import io
from collections import OrderedDict
import re
import unicodedata
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from views.dex_views import ShinyView
from views.embeds import err_embed, handle_command_error, make_embed, send_usage, warn_embed
from views.loading import loading

TYPE_EMOJIS = {
    "normal": "🔘",
    "fire": "🔥",
    "water": "💧",
    "electric": "⚡",
    "grass": "🌿",
    "ice": "❄️",
    "fighting": "🥊",
    "poison": "☠️️",
    "ground": "⏳",
    "flying": "🕊️",
    "psychic": "🔮",
    "bug": "🐛",
    "rock": "🪨",
    "ghost": "👻",
    "dragon": "🐉",
    "dark": "🌙",
    "steel": "⚙️",
    "fairy": "🌸",
}


# --- Alt-name & Key Normalization Helpers -------------------------------------

_LEADING_JUNK_RE = re.compile(r"^[^\w]+")
_SPLIT_RE = re.compile(r"[/;|\n]")
_PAREN_RE = re.compile(r"[(\[（]([^)\]）]*)[)\]）]")


def normalize_key(name: str) -> str:
    """
    Normalizes a name/key by stripping accents (é -> e), removing special characters,
    and converting to lower case for database image lookups.
    """
    if not name:
        return ""
    nfd = unicodedata.normalize("NFD", name)
    without_accents = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    cleaned = re.sub(r"[^\w\s-]", "", without_accents)
    return " ".join(cleaned.split()).lower()


def image_id(name: str) -> str:
    """
    Canonical pokeimgs id / filename stem: lowercase, accents -> plain letters (é -> e),
    Nidoran symbols -> -m / -f. Same rule is used by rename_shinies.py and mongo_sync.py.
    """
    if not name:
        return ""
    text = unicodedata.normalize("NFC", name).replace("♂", "-m").replace("♀", "-f")
    nfd = unicodedata.normalize("NFD", text)
    stripped = "".join(c for c in nfd if unicodedata.category(c) != "Mn")  # also drops U+FE0F
    return " ".join(unicodedata.normalize("NFC", stripped).lower().split())


def image_ids(name: str) -> list:
    """Ordered, de-duplicated candidate pokeimgs ids for a Pokémon name (best match first)."""
    variants = [name, name.replace("’", "'"), name.replace("’", "").replace("'", "")]
    out = []
    for variant in variants:
        for key in (image_id(variant), variant.lower()):
            if key and key not in out:
                out.append(key)
    legacy = normalize_key(name)
    if legacy and legacy not in out and "♂" not in name and "♀" not in name:
        out.append(legacy)
    return out


def _strip_accents(text: str) -> str:
    """Drops accents from Latin letters only (keeps kana dakuten etc. intact)."""
    out = []
    for ch in unicodedata.normalize("NFD", text):
        if unicodedata.combining(ch) and out and ord(out[-1]) < 0x250:
            continue
        out.append(ch)
    return unicodedata.normalize("NFC", "".join(out))


def _is_latin(text: str) -> str:
    return all(
        ch.isascii() or ch in "’♀♂" or unicodedata.name(ch, "").startswith("LATIN")
        for ch in text
    )


def name_keys(text: str) -> set:
    """Normalized lookup keys for a name (case/width/accent-insensitive)."""
    base = unicodedata.normalize("NFKC", text).replace("’", "'")
    base = " ".join(base.split()).casefold()
    if not base:
        return set()
    return {base, _strip_accents(base), normalize_key(base)}


def extract_name_variants(entry) -> list:
    """
    Cleans one raw entry of a Pokémon's "names" list into usable name strings.
    """
    if not isinstance(entry, str):
        return []
    variants = []

    def add(text):
        text = _LEADING_JUNK_RE.sub("", text.strip()).strip()
        if text and text not in variants:
            variants.append(text)

    for part in _SPLIT_RE.split(entry):
        if ":" in part:
            part = part.split(":", 1)[1]
        outer = _PAREN_RE.sub(" ", part)
        if not _is_latin(_LEADING_JUNK_RE.sub("", outer.strip())):
            for inner in _PAREN_RE.findall(part):
                add(inner)
        add(outer)
    return variants


def build_alt_index(pokedex: dict) -> dict:
    """Maps every normalized alt name (any language) -> canonical Pokédex key."""
    index = {}
    for name, info in pokedex.items():
        if not isinstance(info, dict):
            continue
        raw_names = info.get("names", [])
        if isinstance(raw_names, str):
            raw_names = [raw_names]
        for entry in raw_names or []:
            for variant in extract_name_variants(entry):
                for key in name_keys(variant):
                    index.setdefault(key, name)
    return index


class Dex(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self.pokedex = {}
        self.alt_index = {}
        self._lookup = {}
        self._numbers = {}
        self._img_cache = OrderedDict()
        self._image_locks: dict[str, asyncio.Lock] = {}
        self._image_cache_limit = 4096

    async def cog_command_error(self, ctx: commands.Context, error):
        await handle_command_error(ctx, error)

    @property
    def mongo_client(self):
        return self.bot.mongo_client

    @property
    def db(self):
        return self.mongo_client["utilities"]

    @property
    def collection(self):
        return self.db["constdata"]

    @property
    def img_collection(self):
        return self.db["pokeimgs"]

    async def cog_load(self):
        """Loads the Pokédex after the shared MongoDB client has been warmed up by the bot."""
        if self.mongo_client is None:
            print("Dex Cog: MongoDB is not configured; Pokédex lookups are unavailable.")
            return
        await self.load_pokedex()

    async def load_pokedex(self):
        """Loads pokedex document from MongoDB Atlas into memory asynchronously."""
        try:
            doc = await self.collection.find_one({"_id": "pokedex"}, {"data": 1})
            if doc and "data" in doc:
                self.pokedex = doc["data"]
            else:
                self.pokedex = {}
            self.alt_index = build_alt_index(self.pokedex)
            self._build_lookup()
        except Exception as e:
            print(f"❌ Failed to load Pokédex from MongoDB: {e}")
            self.pokedex = {}
            self.alt_index = {}
            self._lookup = {}

    def _build_lookup(self):
        lookup = {}
        numbers = {}
        for name, info in self.pokedex.items():
            if not isinstance(info, dict):
                continue
            lookup.setdefault(name.lower(), name)
            lookup.setdefault(normalize_key(name), name)
            number = str(info.get("pokedex_number", "")).lstrip("#")
            if number:
                numbers.setdefault(number, name)
        self._lookup = lookup
        self._numbers = numbers

    async def _fetch_pokemon_images(self, matched_name: str) -> tuple[Optional[tuple], Optional[tuple]]:
        """Fetch normal and shiny images in one MongoDB round trip, then retain positive and negative cache entries."""
        normal_keys = image_ids(matched_name)
        if normalize_key(matched_name) == "missingno" and "missingno" not in normal_keys:
            normal_keys.insert(0, "missingno")
        shiny_keys = [f"shiny {key}" for key in image_ids(matched_name)]
        all_keys = list(dict.fromkeys(normal_keys + shiny_keys))
        if not all_keys:
            return None, None

        lock_key = normalize_key(matched_name) or matched_name.casefold()
        image_lock = self._image_locks.setdefault(lock_key, asyncio.Lock())
        async with image_lock:
            missing = []
            for key in all_keys:
                if key in self._img_cache:
                    self._img_cache.move_to_end(key)
                else:
                    missing.append(key)
            if missing:
                found = {}
                cursor = self.img_collection.find(
                    {"_id": {"$in": missing}},
                    {"image": 1},
                )
                documents = await cursor.to_list(length=len(missing))
                for doc in documents:
                    image_data = doc.get("image")
                    if image_data is not None:
                        found[doc["_id"]] = bytes(image_data)
                for key in missing:
                    self._img_cache[key] = found.get(key)
                while len(self._img_cache) > self._image_cache_limit:
                    self._img_cache.popitem(last=False)

            normal_result = None
            for key in normal_keys:
                value = self._img_cache.get(key)
                if value is not None:
                    self._img_cache.move_to_end(key)
                    normal_result = (key, value)
                    break

            shiny_result = None
            for key in shiny_keys:
                value = self._img_cache.get(key)
                if value is not None:
                    self._img_cache.move_to_end(key)
                    filename = f"shiny_{normalize_key(matched_name).replace(' ', '_')}.png"
                    shiny_result = (filename, value)
                    break

        return normal_result, shiny_result

    @commands.hybrid_command(
        name="dex",
        aliases=["pokedex"],
        usage="<name or dex number>",
        description="Look up a Pokémon by name or dex number.",
    )
    @app_commands.describe(query="Pokémon name or Pokédex number (e.g. pikachu, 25, #25).")
    @loading
    async def dex_cmd(self, ctx: commands.Context, *, query: str = commands.parameter(default=None, description="Pokémon name or Pokédex number (e.g. pikachu, 25, #25).")):
        if query is None or not query.strip():
            return await send_usage(ctx, title="Missing arg: `query`")

        query_clean = query.strip().lower().lstrip("#")

        if not self.pokedex:
            return await ctx.reply(embed=warn_embed("Pokédex not loaded yet."), mention_author=False)

        matched_name = self._lookup.get(query_clean) or self._lookup.get(normalize_key(query_clean))
        if matched_name is None:
            matched_name = self._numbers.get(query_clean)
        data = self.pokedex.get(matched_name) if matched_name else None

        if not data:
            for key in name_keys(query.strip().lstrip("#")):
                alt_target = self.alt_index.get(key)
                if alt_target and alt_target in self.pokedex:
                    matched_name = alt_target
                    data = self.pokedex[alt_target]
                    break

        if not data:
            await ctx.reply(embed=err_embed(f"Couldn't find {query.strip()[:100]}."), mention_author=False)
            return

        dex_num = str(data.get("pokedex_number", "???")).lstrip("#")
        embed_title = f"#{dex_num} - {matched_name}"

        description = str(data.get("description") or "")
        evolution = data.get("evolution", "")
        if evolution:
            description = f"{description}\n\n**Evolution**\n{evolution}" if description else f"**Evolution**\n{evolution}"
        description = description[:4096]

        embed = make_embed(title=embed_title, description=description or None)

        normal_result, shiny_img = await self._fetch_pokemon_images(matched_name)

        file = None
        normal_img = None

        if normal_result:
            image_bytes = normal_result[1]
            filename = f"{normalize_key(matched_name)}.png"
            normal_img = (filename, image_bytes)
            file = discord.File(fp=io.BytesIO(image_bytes), filename=filename)
            embed.set_thumbnail(url=f"attachment://{filename}")

        # Keep fields in three inline columns, matching a compact Pokédex card.
        raw_types = data.get("types")
        if isinstance(raw_types, list):
            types_formatted = "\n".join(str(t) for t in raw_types)
        elif isinstance(raw_types, str):
            types_formatted = raw_types
        else:
            types_formatted = "N/A"
        embed.add_field(name="Types", value=types_formatted[:1024] or "N/A", inline=True)
        embed.add_field(name="Region", value=str(data.get("region") or "N/A")[:1024], inline=True)

        catchable = data.get("catchable")
        catchable_str = (
            "Yes"
            if catchable is True
            else ("No" if catchable is False else str(catchable or "N/A"))
        )
        embed.add_field(name="Catchable", value=catchable_str, inline=True)

        stats = data.get("base_stats", {})
        if isinstance(stats, dict):
            stats_str = (
                f"**HP:** {stats.get('hp', 'N/A')}\n"
                f"**Attack:** {stats.get('attack', 'N/A')}\n"
                f"**Defense:** {stats.get('defense', 'N/A')}\n"
                f"**Sp. Atk:** {stats.get('sp_atk', 'N/A')}\n"
                f"**Sp. Def:** {stats.get('sp_def', 'N/A')}\n"
                f"**Speed:** {stats.get('speed', 'N/A')}\n"
                f"**Total: {stats.get('total', 'N/A')}**"
            )
        else:
            stats_str = str(stats)
        embed.add_field(name="Base Stats", value=stats_str[:1024] or "N/A", inline=True)

        raw_names = data.get("names", [])
        names_str = (
            "\n".join(raw_names)
            if isinstance(raw_names, list)
            else str(raw_names)
        )
        embed.add_field(name="Names", value=(names_str or "N/A")[:1024], inline=True)

        app = data.get("appearance", {})
        if isinstance(app, dict):
            app_str = f"Height: {app.get('height', 'N/A')}\nWeight: {app.get('weight', 'N/A')}"
        else:
            app_str = str(app)
        embed.add_field(name="Appearance", value=app_str[:1024] or "N/A", inline=True)

        gr = data.get("gender_ratio", {})
        if isinstance(gr, dict):
            gr_str = f"♂ {gr.get('male', 'N/A')} - ♀ {gr.get('female', 'N/A')}"
        else:
            gr_str = str(gr)
        embed.add_field(name="Gender Ratio", value=gr_str[:1024] or "N/A", inline=True)

        raw_egg = data.get("egg_group", [])
        egg_str = (
            "\n".join(raw_egg) if isinstance(raw_egg, list) else str(raw_egg)
        )
        embed.add_field(name="Egg Groups", value=(egg_str or "N/A")[:1024], inline=True)

        embed.add_field(
            name="Hatch Time", value=str(data.get("hatch_time") or "N/A")[:1024], inline=True
        )

        # Shiny image (pokeimgs doc "shiny <name>") -> sparkles toggle button
        view = None
        if shiny_img:
            view = ShinyView(ctx.author.id, embed, normal_img, shiny_img)

        kwargs = {"embed": view.render_embed(), "view": view} if view else {"embed": embed}
        if file:
            kwargs["file"] = file
        msg = await ctx.reply(**kwargs, mention_author=False)
        if view:
            view.message = msg



async def setup(bot):
    await bot.add_cog(Dex(bot))