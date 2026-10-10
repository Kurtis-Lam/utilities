import json
import re
import unicodedata
from pathlib import Path
from typing import List, Tuple, Optional
import discord
from discord.ext import commands

from views.common_views import EMBED_COLOR, confirm, error_embed, info_embed, make_embed, success_embed, warning_embed
from views.embeds import handle_command_error, send_usage
from views.pings_views import (
    EXTRA_RP_CATEGORIES,
    REGIONS,
    RegionPingView,
    ShinyHuntView,
    TYPES,
    TypePingView,
    create_paginator_view,
)


# --- Alt-name helpers ---------------------------------------------------------

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


# --- Cog Definition ---

class PokePings(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._alt_index = {}  # normalized alt name -> canonical Pokédex name
        self._pokevars_cache = None

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

    async def _get_pokevars(self) -> list[str]:
        """Lazily fetches and caches the pokevars list from constdata."""
        if self._pokevars_cache is not None:
            return self._pokevars_cache
        try:
            doc = await self.constdata_collection.find_one({"_id": "pokevars"})
            if doc and isinstance(doc.get("data"), list):
                self._pokevars_cache = doc["data"]
                return self._pokevars_cache
        except Exception as e:
            print(f"PokePings: failed to fetch pokevars: {e}")
        return []

    async def find_sh_variants(self, pokemon: str, *, exact: bool = False) -> list[str]:
        """Return distinct pokevars names containing the query, or exactly matching it."""
        query = pokemon.strip()
        if not query:
            return []

        # Resolve alt names first (e.g., 'ghos' -> 'gastly', 'flabebe' -> 'flabébé')
        resolved = await self._resolve_alt_lower(query)
        search_term = resolved if resolved else query

        norm_search = _strip_accents(unicodedata.normalize("NFKC", search_term)).casefold()

        pokevars = await self._get_pokevars()

        unique, seen = [], set()
        for name in pokevars:
            if not isinstance(name, str):
                continue
            norm_name = _strip_accents(unicodedata.normalize("NFKC", name)).casefold()

            if exact:
                is_match = (norm_name == norm_search)
            else:
                is_match = (norm_search in norm_name)

            if is_match:
                key = name.casefold()
                if key not in seen:
                    unique.append(name)
                    seen.add(key)
        return unique

    @staticmethod
    def _sh_confirmation_embeds(variants: list[str], title: str) -> list[discord.Embed]:
        """Split long variant lists across embeds without hitting Discord limits."""
        chunks = []
        current = []
        current_length = 0
        for name in variants:
            line = f"• {name}"
            if current and current_length + len(line) + 1 > 3500:
                chunks.append("\n".join(current))
                current = []
                current_length = 0
            current.append(line)
            current_length += len(line) + 1
        if current:
            chunks.append("\n".join(current))
        return [
            make_embed(
                title=title if index == 0 else f"{title} (continued)",
                description=chunk,
            )
            for index, chunk in enumerate(chunks)
        ]

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

        candidates: List[List[str]] = []
        for original in requested:
            cands = [original.lower(), _strip_accents(original.lower())]
            for key in name_keys(original):
                target = alt_index.get(key)
                if target:
                    t_low = target.lower()
                    if t_low not in cands:
                        cands.append(t_low)
                    t_strip = _strip_accents(t_low)
                    if t_strip not in cands:
                        cands.append(t_strip)
            candidates.append(cands)

        pokevars = await self._get_pokevars()

        lookup = {}
        for m in pokevars:
            if isinstance(m, str):
                m_low = m.lower()
                lookup[m_low] = m
                lookup[_strip_accents(m_low)] = m

        matched, invalid, seen = [], [], set()
        for original, cands in zip(requested, candidates):
            hit = None
            for c in cands:
                if c in lookup:
                    hit = lookup[c]
                    break
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

    async def _confirm_clear(self, ctx: commands.Context, ping_type: str, label: str, what: str) -> None:
        """Ask before wiping the author's whole list, then edit that same message to say
        what happened (cancelled / timed out are edited automatically)."""
        g_id = str(ctx.guild.id)
        u_id = str(ctx.author.id)

        current = await self.get_ping_data(g_id, ping_type, u_id, default=[])
        if not current:
            await ctx.reply(embed=warning_embed(f"Your {label} is already empty."), mention_author=False)
            return

        count = len(current) if isinstance(current, list) else 1
        confirmed = await confirm(
            ctx,
            f"Clear your whole {label}? This removes **{count}** {what} and turns all of its pings off.",
            title="⚠️ Confirm",
            cancel_text="❌ Cancelled",
            timeout_text="⏱️ Timed out",
        )
        if not confirmed:
            return

        await self.clear_ping_category(g_id, ping_type, u_id)
        await confirmed.show(make_embed(description=f"🧹 Cleared your {label} (**{count}** {what} removed)."))

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
                embed = info_embed(f"**{category_name}** role: <@&{current_role_id}>")
            else:
                embed = warning_embed(f"No **{category_name}** role set.")
            await ctx.reply(embed=embed, mention_author=False)
        else:
            await self.set_guild_role(g_id, role_key, str(role.id))
            await ctx.reply(embed=success_embed(f"**{category_name}** role set to {role.mention}"), mention_author=False)

    # --- Shiny Hunt Command ---

    @commands.command(name="sh", description="View or set your Shiny Hunt target.")
    async def shiny_hunt(
        self,
        ctx: commands.Context,
        *,
        pokemon: str = commands.parameter(
            default=None,
            description="Pokémon, or 'clear' / 'reset'.",
        ),
    ):
        g_id = str(ctx.guild.id)
        u_id = str(ctx.author.id)
        p = ctx.clean_prefix

        if not pokemon:
            current_sh = await self.get_ping_data(g_id, "sh", u_id)
            desc_lines = []
            if current_sh:
                current_targets = current_sh if isinstance(current_sh, list) else [current_sh]
                desc_lines.append(
                    f"✨ Current targets: **{', '.join(current_targets)}**\n"
                )
            desc_lines.extend([
                f"• `{p}sh <pokemon>` — Choose matching variants",
                f"• `{p}sh all <pokemon>` — Subscribe to all matching variants",
                f"• `{p}sh only <pokemon>` — Subscribe to the exact Pokémon only",
                f"• `{p}sh clear` | `{p}sh reset` — Remove all your targets"
            ])
            embed = discord.Embed(
                title="✨ Shiny Hunt",
                description="\n".join(desc_lines),
                color=EMBED_COLOR
            )
            await ctx.reply(embed=embed, mention_author=False)
            return

        if pokemon.strip().lower() in ("clear", "reset"):
            await self._confirm_clear(ctx, "sh", "Shiny Hunt list", "target(s)")
            return

        mode = "select"
        query = pokemon.strip()
        parts = query.split(maxsplit=1)
        if parts and parts[0].casefold() in {"all", "only"}:
            mode = parts[0].casefold()
            query = parts[1].strip() if len(parts) > 1 else ""

        variants = await self.find_sh_variants(query, exact=(mode == "only"))
        if not variants:
            await ctx.reply(
                embed=error_embed(f"No Pokémon found matching `{query}`."),
                mention_author=False,
            )
            return

        if mode == "all":
            await self.set_ping_data(g_id, "sh", u_id, variants)
            embeds = self._sh_confirmation_embeds(variants, "✨ Shiny Hunt Pings Added")
            await ctx.reply(embeds=embeds[:10], mention_author=False)
            for start in range(10, len(embeds), 10):
                await ctx.reply(embeds=embeds[start:start + 10], mention_author=False)
            return

        if mode == "only" or len(variants) == 1:
            await self.set_ping_data(g_id, "sh", u_id, variants)
            description = (
                f"✨ Only **{variants[0]}** was added to your Shiny Hunt pings."
                if mode == "only"
                else f"✨ Target set to **{variants[0]}**."
            )
            await ctx.reply(embed=make_embed(description=description), mention_author=False)
            return

        view = ShinyHuntView(self, ctx.author.id, variants)
        message = await ctx.reply(embed=view.make_embed(), view=view, mention_author=False)
        view.message = message

    # --- Collection List Commands ---

    @commands.group(name="cl", invoke_without_command=True, description="Manage your collection list pings.")
    async def cl_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            passed = getattr(ctx, "subcommand_passed", None)
            if passed:
                await send_usage(ctx, note=f"Unknown subcommand `{passed}`.")
            else:
                p = ctx.clean_prefix
                embed = discord.Embed(
                    title="📦 Collection List",
                    description=(
                        f"• `{p}cl a <pokemon>` — Add\n"
                        f"• `{p}cl r <pokemon>` — Remove\n"
                        f"• `{p}cl clear` | `{p}cl reset` — Remove everything\n"
                        f"• `{p}cl list` — View"
                    ),
                    color=EMBED_COLOR
                )
                await ctx.reply(embed=embed, mention_author=False)

    @cl_group.command(name="add", aliases=["a"], description="Add Pokémon to your collection list.")
    async def cl_add(
        self,
        ctx: commands.Context,
        *,
        pokemon: str = commands.parameter(description="Pokémon name(s), comma separated."),
    ):
        matched_names, invalid_names = await self.parse_pokemon_list(pokemon)
        if not matched_names:
            await ctx.reply(embed=error_embed("No such Pokémon."), mention_author=False)
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
        pokemon: str = commands.parameter(description="Pokémon name(s), comma separated."),
    ):
        raw_targets = {p.strip().lower() for p in pokemon.split(",") if p.strip()}
        if not raw_targets:
            return await send_usage(ctx, note="Give a Pokémon name.")

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

    @cl_group.command(name="clear", aliases=["c", "reset"], description="Clear your whole collection list.")
    async def cl_clear(self, ctx: commands.Context):
        await self._confirm_clear(ctx, "cl", "collection list", "Pokémon")

    @cl_group.command(name="list", aliases=["l"], description="Show your collection list.")
    async def cl_list(self, ctx: commands.Context):
        g_id = str(ctx.guild.id)
        u_id = str(ctx.author.id)

        user_list = await self.get_ping_data(g_id, "cl", u_id, default=[])

        if not user_list:
            embed = discord.Embed(
                title=f"📦 {ctx.author.display_name}'s Collection List",
                description="*Empty.*",
                color=EMBED_COLOR
            )
            await ctx.reply(embed=embed, mention_author=False)
            return

        page_size = 20
        chunks = [user_list[i:i + page_size] for i in range(0, len(user_list), page_size)]
        pages = []
        total_pages = len(chunks)

        for i, chunk in enumerate(chunks):
            embed = discord.Embed(
                title=f"📦 {ctx.author.display_name}'s Collection List",
                description="\n".join(f"• {name}" for name in chunk),
                color=EMBED_COLOR
            )
            embed.set_footer(text=f"Page {i + 1}/{total_pages}")
            pages.append(embed)

        if total_pages == 1:
            await ctx.reply(embed=pages[0], mention_author=False)
        else:
            view = create_paginator_view(pages, ctx.author.id)
            await ctx.reply(embed=pages[0], view=view, mention_author=False)

    # --- Reserves Commands ---

    @commands.group(
        name="reserves",
        aliases=["reserve", "res", "re"],
        invoke_without_command=True,
        description="Show your reserves or view all server reserves.",
    )
    async def reserves(self, ctx: commands.Context, *, scope: Optional[str] = None):
        if ctx.invoked_subcommand is not None:
            return

        if scope:
            s = scope.strip().lower()
            if s == "all":
                await ctx.invoke(self.re_all)
                return
            elif s in ("list", "l"):
                await ctx.invoke(self.re_list)
                return

        p = ctx.clean_prefix
        embed = discord.Embed(
            title="📋 Reserves",
            description=(
                f"• `{p}res a <member> <pokemon>` — Add (admin)\n"
                f"• `{p}res r <member> <pokemon>` — Remove (admin)\n"
                f"• `{p}res all` — All (admin)\n"
                f"• `{p}res list` — Yours"
            ),
            color=EMBED_COLOR
        )
        await ctx.reply(embed=embed, mention_author=False)

    @reserves.command(name="list", aliases=["l"], description="Show your reserves.")
    async def re_list(self, ctx: commands.Context):
        g_id = str(ctx.guild.id)
        u_id = str(ctx.author.id)
        user_list = await self.get_ping_data(g_id, "re", u_id, default=[])

        if not user_list:
            embed = discord.Embed(
                title=f"📋 {ctx.author.display_name}'s Reserves",
                description="*No reserves.*",
                color=EMBED_COLOR
            )
            await ctx.reply(embed=embed, mention_author=False)
            return

        page_size = 20
        chunks = [user_list[i:i + page_size] for i in range(0, len(user_list), page_size)]
        pages = []
        total_pages = len(chunks)

        for i, chunk in enumerate(chunks):
            embed = discord.Embed(
                title=f"📋 {ctx.author.display_name}'s Reserves",
                description="\n".join(f"• {name}" for name in chunk),
                color=EMBED_COLOR
            )
            embed.set_footer(text=f"Page {i + 1}/{total_pages}")
            pages.append(embed)

        if total_pages == 1:
            await ctx.reply(embed=pages[0], mention_author=False)
        else:
            view = create_paginator_view(pages, ctx.author.id)
            await ctx.reply(embed=pages[0], view=view, mention_author=False)

    @reserves.command(name="all", description="View all user reserves in this server (admin only).")
    async def re_all(self, ctx: commands.Context):
        if not ctx.author.guild_permissions.administrator:
            await ctx.reply(
                embed=warning_embed("Admins only."),
                mention_author=False
            )
            return

        g_id = str(ctx.guild.id)
        doc = await self._get_guild_doc(g_id)
        re_data = doc.get("re", {})

        lines = []
        for uid, plist in re_data.items():
            if plist:
                lines.append(f"• <@{uid}>: {', '.join(plist)}")

        if not lines:
            embed = discord.Embed(
                title=f"📋 {ctx.guild.name} Reserves List",
                description="*No reserves.*",
                color=EMBED_COLOR
            )
            await ctx.reply(embed=embed, mention_author=False)
            return

        page_size = 20
        chunks = [lines[i:i + page_size] for i in range(0, len(lines), page_size)]
        pages = []
        total_pages = len(chunks)

        for i, chunk in enumerate(chunks):
            embed = discord.Embed(
                title=f"📋 {ctx.guild.name} Reserves List",
                description="\n".join(chunk),
                color=EMBED_COLOR
            )
            embed.set_footer(text=f"Page {i + 1}/{total_pages}")
            pages.append(embed)

        if total_pages == 1:
            await ctx.reply(embed=pages[0], mention_author=False)
        else:
            view = create_paginator_view(pages, ctx.author.id)
            await ctx.reply(embed=pages[0], view=view, mention_author=False)

    @reserves.command(name="add", aliases=["a"], description="Reserve Pokémon for a member (admin only).")
    @commands.has_permissions(administrator=True)
    async def re_add(
        self,
        ctx: commands.Context,
        member: discord.Member = commands.parameter(description="Member (mention or ID)."),
        *,
        pokemon: str = commands.parameter(description="Pokémon name(s), comma separated."),
    ):
        matched_names, invalid_names = await self.parse_pokemon_list(pokemon)
        if not matched_names:
            await ctx.reply(embed=error_embed("No such Pokémon."), mention_author=False)
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
        member: discord.Member = commands.parameter(description="Member (mention or ID)."),
        *,
        pokemon: str = commands.parameter(description="Pokémon name(s), comma separated."),
    ):
        g_id = str(ctx.guild.id)
        u_id = str(member.id)

        user_list = await self.get_ping_data(g_id, "re", u_id, default=[])

        if not user_list:
            await ctx.reply(embed=warning_embed(f"{member.mention} has no reserves."), mention_author=False)
            return

        raw_targets = {p.strip().lower() for p in pokemon.split(",") if p.strip()}
        if not raw_targets:
            return await send_usage(ctx, note="Give a Pokémon name.")

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
            description="Member. Empty = everyone.",
        ),
    ):
        g_id = str(ctx.guild.id)

        if member:
            u_id = str(member.id)
            await self.set_ping_data(g_id, "re", u_id, [])
            await ctx.reply(embed=make_embed(description=f"🧹 Cleared {member.mention}'s reserves."), mention_author=False)
        else:
            await self.clear_ping_category(g_id, "re")
            await ctx.reply(embed=make_embed(description="🧹 Cleared all reserves."), mention_author=False)

    # --- Type & Region Commands ---

    @commands.command(name="tp", description="View or toggle your Type Pings.")
    async def type_pings(
        self,
        ctx: commands.Context,
        *,
        target: str = commands.parameter(
            default=None,
            description="Type(s) to toggle, 'clear' / 'reset' to turn all off. Empty = menu.",
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

        if target.strip().lower() in ("clear", "reset"):
            await self._confirm_clear(ctx, "tp", "type pings", "type(s)")
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
            await send_usage(ctx, note=f"Invalid. Valid: {', '.join(TYPES)}")
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
            description="Region(s) or Gmax/Paradox/Eevos, 'clear' / 'reset' to turn all off. Empty = menu.",
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

        if target.strip().lower() in ("clear", "reset"):
            await self._confirm_clear(ctx, "rp", "region pings", "region(s)")
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
            await send_usage(ctx, note=f"Invalid. Valid: {', '.join(all_items)}")
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