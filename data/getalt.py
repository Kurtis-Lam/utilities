"""Owner-only .getalt command: builds alt.json (shortest alt name per Pokémon)."""
import asyncio
import json
import re
import unicodedata
from pathlib import Path

from discord.ext import commands

# --- Alt-name helpers ---------------------------------------------------------
# Alt names live in the "names" list of each Pokédex entry (the same list the
# "Names" field of .dex displays). These helpers turn those raw strings into
# normalized lookup keys so that "ghos", "Ghos", "ゴース" etc. all resolve.

_ALT_LEADING_JUNK_RE = re.compile(r"^[^\w]+")
_ALT_SPLIT_RE = re.compile(r"[/;|\n]")
_ALT_PAREN_RE = re.compile(r"[(\[（]([^)\]）]*)[)\]）]")


def _alt_strip_accents(text: str) -> str:
    """Drops accents from Latin letters only (keeps kana dakuten etc. intact)."""
    out = []
    for ch in unicodedata.normalize("NFD", text):
        if unicodedata.combining(ch) and out and ord(out[-1]) < 0x250:
            continue
        out.append(ch)
    return unicodedata.normalize("NFC", "".join(out))


def _alt_is_latin(text: str) -> str:
    return all(
        ch.isascii() or ch in "’♀♂" or unicodedata.name(ch, "").startswith("LATIN")
        for ch in text
    )


def _alt_extract_name_variants(entry) -> list:
    """
    Cleans one raw entry of a Pokémon's "names" list into usable name strings.
    Handles flag/emoji prefixes, "Language: name" labels, "a / b" lists and
    "ゴース (Gōsu)" style parentheses.
    """
    if not isinstance(entry, str):
        return []
    variants = []

    def add(text):
        text = _ALT_LEADING_JUNK_RE.sub("", text.strip()).strip()
        if text and text not in variants:
            variants.append(text)

    for part in _ALT_SPLIT_RE.split(entry):
        if ":" in part:
            part = part.split(":", 1)[1]
        outer = _ALT_PAREN_RE.sub(" ", part)
        # Parenthesised romanization only counts when the outer text is non-Latin
        # (otherwise the parentheses are just a label like "(French)").
        if not _alt_is_latin(_ALT_LEADING_JUNK_RE.sub("", outer.strip())):
            for inner in _ALT_PAREN_RE.findall(part):
                add(inner)
        add(outer)
    return variants


ALT_JSON_PATH = Path("alt.json")

# "Alolan Raichu" / "Galarian Meowth" / "Hisuian Zorua" / "Paldean Tauros" -> base name
_ALT_REGIONAL_RE = re.compile(r"^(?:alolan|galarian|hisuian|paldean)\s+(.+)$")


class GetAlt(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="getalt", description="Owner only: build alt.json (shortest alt name per Pokémon).")
    @commands.is_owner()
    async def getalt(self, ctx: commands.Context):
        """
        Owner only. For every Pokémon in pokevars (in Pokédex order) finds the
        shortest Latin-script alt name and saves {"gastly": "ghos", ...} to alt.json.
        Non-Latin names (Japanese kana/kanji, Chinese, Korean...) are ignored, and on
        a length tie the first alt name listed wins (the real name counts as a candidate).
        Regional forms (Alolan/Galarian/Hisuian/Paldean xxx) are saved as just "xxx", and
        an alt that would be longer than the original name, or equal to it, is stored as
        the original name.
        Pokémon with no Latin-script alt name are listed in the result message.
        """
        status = await ctx.reply("⏳ Building alt-name map...")
        constdata = self.bot.mongo_client["utilities"]["constdata"]

        pokedex_doc = await constdata.find_one({"_id": "pokedex"})
        pokedex = (pokedex_doc or {}).get("data") or {}
        if not pokedex:
            return await status.edit(content="❌ Pokédex data not found in MongoDB.")

        # Pokémon list: DB doc first (same source pings.py uses), pokevars.json as fallback.
        pokevars_doc = await constdata.find_one({"_id": "pokevars"})
        pokevars = (pokevars_doc or {}).get("data") or []

        # Check train/pokevars.json or pokevars.json
        pokevars_json_path = Path("train/pokevars.json") if Path("train/pokevars.json").exists() else Path("pokevars.json")
        if pokevars_json_path.exists():
            loaded = json.loads(pokevars_json_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw_list = loaded.get("data", []) if "data" in loaded else list(loaded.keys())
            elif isinstance(loaded, list):
                raw_list = loaded
            else:
                raw_list = []
            train_pokevars_set = set(str(k).lower() for k in raw_list)
        else:
            train_pokevars_set = set()

        if not pokevars and train_pokevars_set:
            pokevars = list(train_pokevars_set)

        if not pokevars:
            return await status.edit(content="❌ pokevars not found (MongoDB or pokevars.json).")

        # Set of all valid Pokémon names in lower case for word checks
        all_pokevars_set = train_pokevars_set or set(str(k).lower() for k in pokevars)

        pokedex_lookup = {str(n).lower(): info for n, info in pokedex.items()}
        NO_DEX = 10 ** 9

        def dex_number(info) -> int:
            match = re.search(r"\d+", str(info.get("pokedex_number", ""))) if info else None
            return int(match.group()) if match else NO_DEX

        # Dex #1 first; stable sort keeps pokevars order for forms sharing a number.
        ordered = sorted(
            enumerate(pokevars),
            key=lambda pair: (dex_number(pokedex_lookup.get(str(pair[1]).lower())), pair[0]),
        )

        result = {}
        no_alt_names = []
        for _, name in ordered:
            key = str(name).lower()

            # Regional forms: "alolan raichu" -> "raichu"
            regional = _ALT_REGIONAL_RE.match(key)
            if regional:
                result.setdefault(key, regional.group(1))
                continue

            # Multi-word check: split words and check if any word exists as a valid Pokémon in pokevars
            words = key.split()
            if len(words) > 1:
                matched_word = None
                for word in words:
                    if word in all_pokevars_set:
                        matched_word = word
                        break
                if matched_word:
                    result.setdefault(key, matched_word)
                    continue

            info = pokedex_lookup.get(key)
            raw_names = info.get("names", []) if isinstance(info, dict) else []
            if isinstance(raw_names, str):
                raw_names = [raw_names]

            best = None
            for entry in raw_names or []:
                for variant in _alt_extract_name_variants(entry):
                    if not _alt_is_latin(variant):
                        continue  # skip Japanese / any non-Latin script
                    folded = _alt_strip_accents(variant).casefold()
                    if not folded:
                        continue
                    if best is None or len(folded) < len(best):  # strict < => first wins ties
                        best = folded

            if best:
                # Use the real name when the shortest alt is the real name itself
                # (accent-insensitive) or is longer than it - an alt must be shorter.
                if best == _alt_strip_accents(key) or len(best) > len(key):
                    best = key
                result.setdefault(key, best)
            else:
                no_alt_names.append(str(name))

        await asyncio.to_thread(
            ALT_JSON_PATH.write_text,
            json.dumps(result, ensure_ascii=False, indent=2),
            "utf-8",
        )
        summary = f"✅ Saved **{len(result)}** alt names to `{ALT_JSON_PATH}`."
        if not no_alt_names:
            return await status.edit(content=summary)

        header = f"{summary}\n⚠️ **{len(no_alt_names)}** Pokémon have no Latin-script alt name:"
        full = f"{header}\n" + ", ".join(no_alt_names)
        if len(full) <= 1900:
            return await status.edit(content=full)

        # Too long for one message: summary first, then the list in chunks.
        await status.edit(content=header)
        chunk = ""
        for entry in no_alt_names:
            piece = entry + ", "
            if len(chunk) + len(piece) > 1900:
                await ctx.send(chunk.rstrip(", "))
                chunk = ""
            chunk += piece
        if chunk:
            await ctx.send(chunk.rstrip(", "))


async def setup(bot):
    await bot.add_cog(GetAlt(bot))