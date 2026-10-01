import json
import re
import unicodedata
from pathlib import Path
from typing import List, Tuple, Optional
import discord
from discord.ext import commands

from views.common import EMBED_COLOR, error_embed, info_embed, make_embed, success_embed, warning_embed
from views.embeds import handle_command_error, send_usage

TYPES = [
    "Normal", "Fire", "Water", "Grass", "Electric", "Ice",
    "Fighting", "Poison", "Ground", "Flying", "Psychic", "Bug",
    "Rock", "Ghost", "Dragon", "Steel", "Dark", "Fairy"
]

REGIONS = [
    "Kanto", "Johto", "Hoenn", "Sinnoh", "Unova",
    "Kalos", "Alola", "Galar", "Hisui", "Paldea"
]

EXTRA_RP_CATEGORIES = ["Gmax", "Paradox", "Eevos"]


# --- Alt-name helpers ---------------------------------------------------------
# Alt names live in the "names" list of each Pokédex entry (the same list the
# "Names" field of .dex displays). These helpers turn those raw strings into
# normalized lookup keys so that "ghos", "Ghos", "ゴース" etc. all resolve.

_LEADING_JUNK_RE = re.compile(r"^[^\w]+")
_SPLIT_RE = re.compile(r"[/;|\n]")
_PAREN_RE = re.compile(r"[(\[（]([^)\]）]*)[)\]）]")


def _strip_accents(text: str) -> str:
    """Drops accents from Latin letters only (keeps kana dakuten etc. intact)."""
    out = []
    for ch in unicodedata.normalize("NFD", text):
        if unicodedata.combining(ch) and out and ord(out[-1]) < 0x250:
            continue
        out.append(ch)
    return unicodedata.normalize("NFC", "".join(out))


def _is_latin(text: str) -> bool:
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
    return {base, _strip_accents(base)}


def extract_name_variants(entry) -> list:
    """
    Cleans one raw entry of a Pokémon's "names" list into usable name strings.
    Handles flag/emoji prefixes, "Language: name" labels, "a / b" lists and
    "ゴース (Gōsu)" style parentheses.
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
        # Parenthesised romanization only counts when the outer text is non-Latin
        # (otherwise the parentheses are just a label like "(French)").
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


# --- UI Components for Type Pings ---

class TypePingSelect(discord.ui.Select):
    def __init__(self, user_types: list):
        options = [
            discord.SelectOption(
                label=t,
                value=t,
                default=(t in user_types)
            )
            for t in TYPES
        ]
        super().__init__(
            placeholder="Select types to toggle...",
            min_values=0,
            max_values=len(TYPES),
            options=options,
            custom_id="tp_select"
        )

    async def callback(self, interaction: discord.Interaction):
        view: TypePingView = self.view
        if interaction.user.id != view.user_id:
            return await interaction.response.send_message(
                embed=warning_embed("This menu isn't for you. Run the command yourself to get your own."), ephemeral=True
            )

        g_id = str(interaction.guild_id)
        u_id = str(interaction.user.id)

        new_types = self.values
        await view.cog.set_ping_data(g_id, "tp", u_id, new_types)

        for option in self.options:
            option.default = option.value in new_types

        embed = view.make_embed(new_types)
        await interaction.response.edit_message(embed=embed, view=view)


class TypePingView(discord.ui.View):
    def __init__(self, cog: "PokePings", user_id: int, user_types: list):
        super().__init__(timeout=180)
        self.cog = cog
        self.user_id = user_id
        self.add_item(TypePingSelect(user_types))

    def make_embed(self, user_types: list) -> discord.Embed:
        embed = discord.Embed(title="⚡ Type Pings Configuration", color=EMBED_COLOR)
        embed.description = "\n".join(
            f"{'✅' if t in user_types else '❌'} **{t}**" for t in TYPES
        )
        return embed


# --- UI Components for Region & Special Category Pings ---

class RegionPingSelect(discord.ui.Select):
    def __init__(self, user_regions: list):
        all_items = REGIONS + EXTRA_RP_CATEGORIES
        options = [
            discord.SelectOption(
                label=item,
                value=item,
                default=(item in user_regions)
            )
            for item in all_items
        ]
        super().__init__(
            placeholder="Select regions/categories to toggle...",
            min_values=0,
            max_values=len(all_items),
            options=options,
            custom_id="rp_select"
        )

    async def callback(self, interaction: discord.Interaction):
        view: RegionPingView = self.view
        if interaction.user.id != view.user_id:
            return await interaction.response.send_message(
                embed=warning_embed("This menu isn't for you. Run the command yourself to get your own."), ephemeral=True
            )

        g_id = str(interaction.guild_id)
        u_id = str(interaction.user.id)

        new_regions = self.values
        await view.cog.set_ping_data(g_id, "rp", u_id, new_regions)

        for option in self.options:
            option.default = option.value in new_regions

        embed = view.make_embed(new_regions)
        await interaction.response.edit_message(embed=embed, view=view)


class RegionPingView(discord.ui.View):
    def __init__(self, cog: "PokePings", user_id: int, user_regions: list):
        super().__init__(timeout=180)
        self.cog = cog
        self.user_id = user_id
        self.add_item(RegionPingSelect(user_regions))

    def make_embed(self, user_regions: list) -> discord.Embed:
        embed = discord.Embed(title="🌍 Region & Special Pings Configuration", color=EMBED_COLOR)
        
        region_lines = [f"{'✅' if r in user_regions else '❌'} **{r}**" for r in REGIONS]
        extra_lines = [f"{'✅' if cat in user_regions else '❌'} **{cat}**" for cat in EXTRA_RP_CATEGORIES]

        embed.add_field(name="Regions", value="\n".join(region_lines), inline=True)
        embed.add_field(name="Special Categories", value="\n".join(extra_lines), inline=True)
        return embed


# --- Cog Definition ---

class PokePings(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._alt_index = {}  # normalized alt name -> canonical Pokédex name

    @property
    def collection(self):
        """Dynamically retrieves the pings collection using main.py shared pool."""
        return self.bot.mongo_client["utilities"]["pings"]

    @property
    def constdata_collection(self):
        """Dynamically retrieves the constdata collection using main.py shared pool."""
        return self.bot.mongo_client["utilities"]["constdata"]

    async def _get_guild_doc(self, guild_id: str) -> dict:
        doc = await self.collection.find_one({"_id": guild_id})
        return doc if doc else {"_id": guild_id, "sh": {}, "cl": {}, "re": {}, "tp": {}, "rp": {}, "roles": {}}

    async def get_ping_data(self, guild_id: str, ping_type: str, user_id: str, default=None):
        doc = await self._get_guild_doc(guild_id)
        return doc.get(ping_type, {}).get(user_id, default)

    async def set_ping_data(self, guild_id: str, ping_type: str, user_id: str, value):
        await self.collection.update_one(
            {"_id": guild_id},
            {"$set": {f"{ping_type}.{user_id}": value}},
            upsert=True
        )

    async def get_guild_role(self, guild_id: str, role_key: str) -> Optional[str]:
        doc = await self._get_guild_doc(guild_id)
        return doc.get("roles", {}).get(role_key)

    async def set_guild_role(self, guild_id: str, role_key: str, role_id: str):
        await self.collection.update_one(
            {"_id": guild_id},
            {"$set": {f"roles.{role_key}": role_id}},
            upsert=True
        )

    async def clear_ping_category(self, guild_id: str, ping_type: str, user_id: Optional[str] = None):
        if user_id:
            await self.collection.update_one(
                {"_id": guild_id},
                {"$unset": {f"{ping_type}.{user_id}": ""}}
            )
        else:
            await self.collection.update_one(
                {"_id": guild_id},
                {"$set": {ping_type: {}}},
                upsert=True
            )

    async def _get_alt_index(self) -> dict:
        """Lazily builds (and caches) the alt-name index from the pokedex doc."""
        if self._alt_index:
            return self._alt_index
        try:
            doc = await self.constdata_collection.find_one({"_id": "pokedex"})
            data = doc.get("data") if doc else None
            if isinstance(data, dict):
                self._alt_index = build_alt_index(data)
        except Exception as e:
            print(f"PokePings: failed to build alt-name index: {e}")
        return self._alt_index

    async def _resolve_alt_lower(self, text: str) -> Optional[str]:
        """Returns the canonical (lowercase) Pokémon name for an alt name, or None."""
        alt_index = await self._get_alt_index()
        for key in name_keys(text):
            target = alt_index.get(key)
            if target:
                return target.lower()
        return None

    async def parse_pokemon_list(self, raw_input: str) -> Tuple[List[str], List[str]]:
        """
        Resolves a comma-separated list of names to canonical pokevars names.
        Accepts real names and alt names in any language (e.g. "ghos", "ゴース").
        Returns (matched, invalid) - matched keeps input order and has no duplicates.
        """
        requested = [p.strip() for p in raw_input.split(",") if p.strip()]
        if not requested:
            return [], []

        alt_index = await self._get_alt_index()

        # Per input: real name first, then any alt-name resolution.
        candidates: List[List[str]] = []
        all_candidates = set()
        for original in requested:
            cands = [original.lower()]
            for key in name_keys(original):
                target = alt_index.get(key)
                if target and target.lower() not in cands:
                    cands.append(target.lower())
            candidates.append(cands)
            all_candidates.update(cands)

        pipeline = [
            {"$match": {"_id": "pokevars"}},
            {"$project": {
                "matched": {
                    "$filter": {
                        "input": "$data",
                        "as": "poke",
                        "cond": {"$in": [{"$toLower": "$$poke"}, list(all_candidates)]}
                    }
                }
            }}
        ]

        db_matched = []
        cursor = self.constdata_collection.aggregate(pipeline)
        async for doc in cursor:
            db_matched = doc.get("matched", [])
            break

        lookup = {m.lower(): m for m in db_matched}

        matched, invalid, seen = [], [], set()
        for original, cands in zip(requested, candidates):
            hit = next((lookup[c] for c in cands if c in lookup), None)
            if hit is None:
                invalid.append(original.lower())
                continue
            if hit.lower() not in seen:
                seen.add(hit.lower())
                matched.append(hit)

        return matched, invalid

    async def _map_targets(self, raw_targets: set, user_list: List[str]) -> dict:
        """
        Maps removal inputs to {canonical_lowercase_name: original_input}.
        An input already present in the list is used as-is; otherwise it is
        resolved as an alt name (e.g. "ghos" -> "gastly").
        """
        in_list = {i.lower() for i in user_list}
        mapped = {}
        for target in raw_targets:
            canonical = target if target in in_list else (await self._resolve_alt_lower(target) or target)
            mapped[canonical] = target
        return mapped

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        await handle_command_error(ctx, error)

    async def _handle_role_config(self, ctx: commands.Context, role_key: str, category_name: str, role: discord.Role = None):
        g_id = str(ctx.guild.id)
        if role is None:
            current_role_id = await self.get_guild_role(g_id, role_key)
            if current_role_id:
                embed = info_embed(f"Current **{category_name}** role: <@&{current_role_id}>")
            else:
                embed = warning_embed(f"No role configured for **{category_name}**.")
            await ctx.reply(embed=embed, mention_author=False)
        else:
            await self.set_guild_role(g_id, role_key, str(role.id))
            await ctx.reply(embed=success_embed(f"Set **{category_name}** ping role to {role.mention}"), mention_author=False)

    # --- Shiny Hunt Command ---

    @commands.command(name="sh", description="View or set your Shiny Hunt target.")
    async def shiny_hunt(
        self,
        ctx: commands.Context,
        *,
        pokemon: str = commands.parameter(
            default=None,
            description="The Pokémon to hunt. Leave empty to view your current target.",
        ),
    ):
        g_id = str(ctx.guild.id)
        u_id = str(ctx.author.id)

        if not pokemon:
            current_sh = await self.get_ping_data(g_id, "sh", u_id)
            if current_sh:
                await ctx.reply(
                    embed=make_embed(description=f"✨ Your current Shiny Hunt target is **{current_sh}**."),
                    mention_author=False,
                )
            else:
                await send_usage(ctx, note="You don't have a Shiny Hunt target set.")
            return

        matched_names, _ = await self.parse_pokemon_list(pokemon)
        if not matched_names:
            await ctx.reply(embed=error_embed("Pokémon does not exist."), mention_author=False)
            return

        matched_name = matched_names[0]
        await self.set_ping_data(g_id, "sh", u_id, matched_name)

        description = f"✨ Set your Shiny Hunt target to **{matched_name}** in this server!"
        typed = pokemon.split(",")[0].strip()
        if typed.lower() != matched_name.lower():
            description += f"\n-# Recognized `{typed[:50]}` as an alt name of {matched_name}."
        await ctx.reply(embed=make_embed(description=description), mention_author=False)

    # --- Collection List Commands ---

    @commands.group(name="cl", invoke_without_command=True, description="Manage your collection list pings.")
    async def cl_group(self, ctx: commands.Context):
        passed = getattr(ctx, "subcommand_passed", None)
        await send_usage(ctx, note=f"Unknown subcommand `{passed}`." if passed else None)

    @cl_group.command(name="add", aliases=["a"], description="Add Pokémon to your collection list.")
    async def cl_add(
        self,
        ctx: commands.Context,
        *,
        pokemon: str = commands.parameter(description="Pokémon name(s), separated by commas."),
    ):
        matched_names, invalid_names = await self.parse_pokemon_list(pokemon)
        if not matched_names:
            await ctx.reply(embed=error_embed("None of the specified Pokémon exist."), mention_author=False)
            return

        g_id = str(ctx.guild.id)
        u_id = str(ctx.author.id)

        user_list = await self.get_ping_data(g_id, "cl", u_id, default=[])
        user_set = set(user_list)
        added, already_in = [], []

        for name in matched_names:
            if name not in user_set:
                user_list.append(name)
                user_set.add(name)
                added.append(name)
            else:
                already_in.append(name)

        await self.set_ping_data(g_id, "cl", u_id, user_list)

        msg_parts = []
        if added:
            msg_parts.append(f"📦 Added to collection: **{', '.join(added)}**")
        if already_in:
            msg_parts.append(f"⚠️ Already in collection: **{', '.join(already_in)}**")
        if invalid_names:
            msg_parts.append(f"❌ Invalid Pokémon: **{', '.join(invalid_names)}**")

        await ctx.reply(embed=make_embed(description="\n".join(msg_parts)), mention_author=False)

    @cl_group.command(name="remove", aliases=["r"], description="Remove Pokémon from your collection list.")
    async def cl_remove(
        self,
        ctx: commands.Context,
        *,
        pokemon: str = commands.parameter(description="Pokémon name(s), separated by commas."),
    ):
        raw_targets = {p.strip().lower() for p in pokemon.split(",") if p.strip()}
        if not raw_targets:
            return await send_usage(ctx, note="Please specify at least one Pokémon name.")

        g_id = str(ctx.guild.id)
        u_id = str(ctx.author.id)

        user_list = await self.get_ping_data(g_id, "cl", u_id, default=[])

        if not user_list:
            await ctx.reply(embed=warning_embed("Your collection list is empty."), mention_author=False)
            return

        targets = await self._map_targets(raw_targets, user_list)
        removed, not_found = [], []

        new_list = []
        for item in user_list:
            if item.lower() in targets:
                removed.append(item)
                targets.pop(item.lower())
            else:
                new_list.append(item)

        if targets:
            not_found = list(targets.values())

        if removed:
            await self.set_ping_data(g_id, "cl", u_id, new_list)

        msg_parts = []
        if removed:
            msg_parts.append(f"🗑️ Removed from collection: **{', '.join(removed)}**")
        if not_found:
            msg_parts.append(f"❌ Not found in list: **{', '.join(not_found)}**")

        await ctx.reply(embed=make_embed(description="\n".join(msg_parts)), mention_author=False)

    @cl_group.command(name="clear", aliases=["c"], description="Clear your whole collection list.")
    async def cl_clear(self, ctx: commands.Context):
        g_id = str(ctx.guild.id)
        u_id = str(ctx.author.id)

        await self.set_ping_data(g_id, "cl", u_id, [])
        await ctx.reply(embed=make_embed(description="🧹 Cleared your collection list!"), mention_author=False)

    @cl_group.command(name="list", aliases=["l"], description="Show your collection list.")
    async def cl_list(self, ctx: commands.Context):
        g_id = str(ctx.guild.id)
        u_id = str(ctx.author.id)

        user_list = await self.get_ping_data(g_id, "cl", u_id, default=[])

        embed = discord.Embed(title=f"📦 {ctx.author.display_name}'s Collection List", color=EMBED_COLOR)
        embed.description = "\n".join(f"• {name}" for name in user_list) if user_list else "*Your collection list is empty.*"

        await ctx.reply(embed=embed, mention_author=False)

    # --- Reserves Commands ---

    @commands.group(
        name="reserves",
        aliases=["reserve", "res", "re"],
        invoke_without_command=True,
        description="Show this server's reserves list.",
    )
    async def reserves(self, ctx: commands.Context):
        g_id = str(ctx.guild.id)
        doc = await self._get_guild_doc(g_id)
        re_data = doc.get("re", {})

        embed = discord.Embed(title=f"📋 {ctx.guild.name} Reserves List", color=EMBED_COLOR)
        lines = []

        for uid, plist in re_data.items():
            if plist:
                user = ctx.guild.get_member(int(uid))
                user_str = user.mention if user else f"User ID {uid}"
                lines.append(f"• {user_str}: {', '.join(plist)}")

        embed.description = "\n".join(lines) if lines else "*No active reserves in this server.*"
        await ctx.reply(embed=embed, mention_author=False)

    @reserves.command(name="add", aliases=["a"], description="Reserve Pokémon for a member (admin only).")
    @commands.has_permissions(administrator=True)
    async def re_add(
        self,
        ctx: commands.Context,
        member: discord.Member = commands.parameter(description="The member to reserve for (mention or ID)."),
        *,
        pokemon: str = commands.parameter(description="Pokémon name(s), separated by commas."),
    ):
        matched_names, invalid_names = await self.parse_pokemon_list(pokemon)
        if not matched_names:
            await ctx.reply(embed=error_embed("None of the specified Pokémon exist."), mention_author=False)
            return

        g_id = str(ctx.guild.id)
        u_id = str(member.id)

        user_list = await self.get_ping_data(g_id, "re", u_id, default=[])
        user_set = set(user_list)
        added, already_in = [], []

        for name in matched_names:
            if name not in user_set:
                user_list.append(name)
                user_set.add(name)
                added.append(name)
            else:
                already_in.append(name)

        await self.set_ping_data(g_id, "re", u_id, user_list)

        msg_parts = []
        if added:
            msg_parts.append(f"📌 Added to {member.mention}'s reserves: **{', '.join(added)}**")
        if already_in:
            msg_parts.append(f"⚠️ Already in reserves: **{', '.join(already_in)}**")
        if invalid_names:
            msg_parts.append(f"❌ Invalid Pokémon: **{', '.join(invalid_names)}**")

        await ctx.reply(embed=make_embed(description="\n".join(msg_parts)), mention_author=False)

    @reserves.command(name="remove", aliases=["r"], description="Remove reserved Pokémon from a member (admin only).")
    @commands.has_permissions(administrator=True)
    async def re_remove(
        self,
        ctx: commands.Context,
        member: discord.Member = commands.parameter(description="The member to remove reserves from (mention or ID)."),
        *,
        pokemon: str = commands.parameter(description="Pokémon name(s), separated by commas."),
    ):
        g_id = str(ctx.guild.id)
        u_id = str(member.id)

        user_list = await self.get_ping_data(g_id, "re", u_id, default=[])

        if not user_list:
            await ctx.reply(embed=warning_embed(f"{member.mention} has no reserves."), mention_author=False)
            return

        raw_targets = {p.strip().lower() for p in pokemon.split(",") if p.strip()}
        if not raw_targets:
            return await send_usage(ctx, note="Please specify at least one Pokémon name.")

        targets = await self._map_targets(raw_targets, user_list)
        removed, not_found = [], []

        new_list = []
        for item in user_list:
            if item.lower() in targets:
                removed.append(item)
                targets.pop(item.lower())
            else:
                new_list.append(item)

        if targets:
            not_found = list(targets.values())

        if removed:
            await self.set_ping_data(g_id, "re", u_id, new_list)

        msg_parts = []
        if removed:
            msg_parts.append(f"🗑️ Removed from {member.mention}'s reserves: **{', '.join(removed)}**")
        if not_found:
            msg_parts.append(f"❌ Not found in reserves: **{', '.join(not_found)}**")

        await ctx.reply(embed=make_embed(description="\n".join(msg_parts)), mention_author=False)

    @reserves.command(name="clear", aliases=["c"], description="Clear reserves for one member, or the whole server (admin only).")
    @commands.has_permissions(administrator=True)
    async def re_clear(
        self,
        ctx: commands.Context,
        member: discord.Member = commands.parameter(
            default=None,
            description="Member to clear. Leave empty to clear everyone's reserves.",
        ),
    ):
        g_id = str(ctx.guild.id)

        if member:
            u_id = str(member.id)
            await self.set_ping_data(g_id, "re", u_id, [])
            await ctx.reply(embed=make_embed(description=f"🧹 Cleared all reserves for {member.mention}!"), mention_author=False)
        else:
            await self.clear_ping_category(g_id, "re")
            await ctx.reply(embed=make_embed(description="🧹 Cleared **ALL** reserves for this server!"), mention_author=False)

    # --- Type & Region Commands ---

    @commands.command(name="tp", description="View or toggle your Type Pings.")
    async def type_pings(
        self,
        ctx: commands.Context,
        *,
        target: str = commands.parameter(
            default=None,
            description="Type(s) to toggle, separated by spaces or commas. Leave empty to open the menu.",
        ),
    ):
        g_id = str(ctx.guild.id)
        u_id = str(ctx.author.id)

        if not target:
            user_types = await self.get_ping_data(g_id, "tp", u_id, default=[])
            view = TypePingView(self, ctx.author.id, user_types)
            embed = view.make_embed(user_types)
            await ctx.reply(embed=embed, view=view, mention_author=False)
            return

        types_map = {t.lower(): t for t in TYPES}
        targets_input = [t.strip().lower() for t in target.replace(',', ' ').split() if t.strip()]

        valid_targets = []
        invalid_targets = []

        for t_in in targets_input:
            if t_in in types_map:
                valid_targets.append(types_map[t_in])
            else:
                invalid_targets.append(t_in)

        if not valid_targets:
            await send_usage(ctx, note=f"Invalid type(s). Valid types are: {', '.join(TYPES)}")
            return

        user_types = await self.get_ping_data(g_id, "tp", u_id, default=[])
        added, removed = [], []

        for item in set(valid_targets):
            if item in user_types:
                user_types.remove(item)
                removed.append(item)
            else:
                user_types.append(item)
                added.append(item)

        await self.set_ping_data(g_id, "tp", u_id, user_types)

        msg = []
        if added:
            msg.append(f"✅ Enabled pings for: **{', '.join(added)}**")
        if removed:
            msg.append(f"❌ Disabled pings for: **{', '.join(removed)}**")
        if invalid_targets:
            msg.append(f"⚠️ Unrecognized input: **{', '.join(invalid_targets)}**")

        await ctx.reply(embed=make_embed(description="\n".join(msg)), mention_author=False)

    @commands.command(name="rp", description="View or toggle your Region Pings.")
    async def region_pings(
        self,
        ctx: commands.Context,
        *,
        target: str = commands.parameter(
            default=None,
            description="Region(s) or Gmax/Paradox/Eevos to toggle, separated by spaces or commas. Leave empty to open the menu.",
        ),
    ):
        g_id = str(ctx.guild.id)
        u_id = str(ctx.author.id)

        if not target:
            user_regions = await self.get_ping_data(g_id, "rp", u_id, default=[])
            view = RegionPingView(self, ctx.author.id, user_regions)
            embed = view.make_embed(user_regions)
            await ctx.reply(embed=embed, view=view, mention_author=False)
            return

        all_items = REGIONS + EXTRA_RP_CATEGORIES
        items_map = {i.lower(): i for i in all_items}
        targets_input = [r.strip().lower() for r in target.replace(',', ' ').split() if r.strip()]

        valid_targets = []
        invalid_targets = []

        for r_in in targets_input:
            if r_in in items_map:
                valid_targets.append(items_map[r_in])
            else:
                invalid_targets.append(r_in)

        if not valid_targets:
            await send_usage(ctx, note=f"Invalid region/category. Valid options are: {', '.join(all_items)}")
            return

        user_regions = await self.get_ping_data(g_id, "rp", u_id, default=[])
        added, removed = [], []

        for item in set(valid_targets):
            if item in user_regions:
                user_regions.remove(item)
                removed.append(item)
            else:
                user_regions.append(item)
                added.append(item)

        await self.set_ping_data(g_id, "rp", u_id, user_regions)

        msg = []
        if added:
            msg.append(f"✅ Enabled pings for: **{', '.join(added)}**")
        if removed:
            msg.append(f"❌ Disabled pings for: **{', '.join(removed)}**")
        if invalid_targets:
            msg.append(f"⚠️ Unrecognized input: **{', '.join(invalid_targets)}**")

        await ctx.reply(embed=make_embed(description="\n".join(msg)), mention_author=False)

async def setup(bot):
    await bot.add_cog(PokePings(bot))