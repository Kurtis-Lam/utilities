import io
import re
import unicodedata

import discord
from discord.ext import commands

from views.embeds import err_embed, handle_command_error, make_embed, send_usage, warn_embed

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
        """Warms up the database connection and loads the Pokédex asynchronously on boot."""
        try:
            await self.mongo_client.admin.command('ping')
            await self.load_pokedex()
        except Exception as e:
            print(f"Dex Cog: MongoDB warmup or Pokédex loading failed: {e}")

    async def load_pokedex(self):
        """Loads pokedex document from MongoDB Atlas into memory asynchronously."""
        try:
            doc = await self.collection.find_one({"_id": "pokedex"})
            if doc and "data" in doc:
                self.pokedex = doc["data"]
            else:
                self.pokedex = {}
            self.alt_index = build_alt_index(self.pokedex)
        except Exception as e:
            print(f"❌ Failed to load Pokédex from MongoDB: {e}")
            self.pokedex = {}
            self.alt_index = {}

    @commands.command(
        name="dex",
        aliases=["pokedex"],
        usage="<pokémon name, alt name or dex number>",
        description="Look up a Pokémon in the Pokédex by name, alt name (any language) or dex number.",
    )
    async def dex_cmd(self, ctx: commands.Context, *, query: str = None):
        if query is None or not query.strip():
            return await send_usage(ctx, note="Missing required argument: `query`")

        query_clean = query.strip().lower().lstrip("#")

        if not self.pokedex:
            return await ctx.reply(embed=warn_embed(
                "Pokédex Unavailable",
                "The Pokédex hasn't been loaded yet. Please try again in a moment."))

        matched_name = None
        data = None

        for name, info in self.pokedex.items():
            if name.lower() == query_clean or normalize_key(name) == normalize_key(query_clean):
                matched_name = name
                data = info
                break

            dex_num = str(info.get("pokedex_number", "")).lstrip("#")
            if dex_num == query_clean:
                matched_name = name
                data = info
                break

        if not data:
            for key in name_keys(query.strip().lstrip("#")):
                alt_target = self.alt_index.get(key)
                if alt_target and alt_target in self.pokedex:
                    matched_name = alt_target
                    data = self.pokedex[alt_target]
                    break

        if not data:
            await ctx.reply(embed=err_embed(
                "Pokémon Not Found",
                f"Couldn't find `{query.strip()[:100]}` in the Pokédex.\n"
                f"-# Try the name, an alt name or the dex number, e.g. `{ctx.clean_prefix}dex pikachu` or `{ctx.clean_prefix}dex 25`."))
            return

        dex_num = str(data.get("pokedex_number", "???")).lstrip("#")
        embed_title = f"#{dex_num} — {matched_name}"

        description = data.get("description", "")
        evolution = data.get("evolution", "")
        if evolution:
            description += f"\n\n**Evolution**\n{evolution}"

        embed = make_embed(
            title=embed_title,
            description=description
        )

        # Explicit image lookup key (maps "MissingNo." or "missingno" -> "missingno")
        img_lookup_keys = [matched_name.lower(), normalize_key(matched_name)]
        if normalize_key(matched_name) == "missingno":
            img_lookup_keys.insert(0, "missingno")

        img_doc = None
        for key in img_lookup_keys:
            img_doc = await self.img_collection.find_one({"_id": key})
            if img_doc and "image" in img_doc:
                break

        file = None

        if img_doc and "image" in img_doc:
            image_bytes = img_doc["image"]
            filename = f"{normalize_key(matched_name)}.png"

            image_stream = io.BytesIO(image_bytes)
            file = discord.File(fp=image_stream, filename=filename)

            embed.set_thumbnail(url=f"attachment://{filename}")

        # 1. Types (safely handles null / None)
        raw_types = data.get("types")
        if isinstance(raw_types, list):
            types_formatted = "\n".join(
                f"{TYPE_EMOJIS.get(t.lower(), '')} {t}".strip()
                for t in raw_types
            )
        elif isinstance(raw_types, str):
            types_formatted = raw_types
        else:
            types_formatted = "N/A"

        embed.add_field(
            name="Types", value=types_formatted, inline=True
        )

        # 2. Region
        embed.add_field(
            name="Region", value=data.get("region", "N/A"), inline=True
        )

        # 3. Catchable
        catchable = data.get("catchable")
        catchable_str = (
            "✅ Yes"
            if catchable is True
            else ("❌ No" if catchable is False else str(catchable))
        )
        embed.add_field(name="Catchable", value=catchable_str, inline=True)

        # 4. Base Stats
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
        embed.add_field(name="Base Stats", value=stats_str, inline=True)

        # 5. Names
        raw_names = data.get("names", [])
        names_str = (
            "\n".join(raw_names)
            if isinstance(raw_names, list)
            else str(raw_names)
        )
        embed.add_field(name="Names", value=names_str or "N/A", inline=True)

        # 6. Appearance
        app = data.get("appearance", {})
        if isinstance(app, dict):
            app_str = f"Height: {app.get('height', 'N/A')}\nWeight: {app.get('weight', 'N/A')}"
        else:
            app_str = str(app)
        embed.add_field(name="Appearance", value=app_str, inline=True)

        # 7. Gender Ratio
        gr = data.get("gender_ratio", {})
        if isinstance(gr, dict):
            gr_str = f"♂ {gr.get('male', 'N/A')} - ♀ {gr.get('female', 'N/A')}"
        else:
            gr_str = str(gr)
        embed.add_field(name="Gender Ratio", value=gr_str, inline=True)

        # 8. Egg Groups
        raw_egg = data.get("egg_group", [])
        egg_str = (
            "\n".join(raw_egg) if isinstance(raw_egg, list) else str(raw_egg)
        )
        embed.add_field(name="Egg Groups", value=egg_str or "N/A", inline=True)

        # 9. Hatch Time
        embed.add_field(
            name="Hatch Time", value=data.get("hatch_time", "N/A"), inline=True
        )

        if file:
            await ctx.reply(embed=embed, file=file)
        else:
            await ctx.reply(embed=embed)


async def setup(bot):
    await bot.add_cog(Dex(bot))