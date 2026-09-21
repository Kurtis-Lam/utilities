import json
from pathlib import Path
from typing import List, Tuple, Optional
import discord
from discord.ext import commands

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
            return await interaction.response.send_message("This interactive menu is not for you.", ephemeral=True)

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
        embed = discord.Embed(title="⚡ Type Pings Configuration", color=discord.Color.blue())
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
            return await interaction.response.send_message("This interactive menu is not for you.", ephemeral=True)

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
        embed = discord.Embed(title="🌍 Region & Special Pings Configuration", color=discord.Color.blue())
        
        region_lines = [f"{'✅' if r in user_regions else '❌'} **{r}**" for r in REGIONS]
        extra_lines = [f"{'✅' if cat in user_regions else '❌'} **{cat}**" for cat in EXTRA_RP_CATEGORIES]

        embed.add_field(name="Regions", value="\n".join(region_lines), inline=True)
        embed.add_field(name="Special Categories", value="\n".join(extra_lines), inline=True)
        return embed


# --- Cog Definition ---

class PokePings(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

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

    async def parse_pokemon_list(self, raw_input: str) -> Tuple[List[str], List[str]]:
        requested_names = [p.strip().lower() for p in raw_input.split(",") if p.strip()]
        
        pipeline = [
            {"$match": {"_id": "pokevars"}},
            {"$project": {
                "matched": {
                    "$filter": {
                        "input": "$data",
                        "as": "poke",
                        "cond": {"$in": [{"$toLower": "$$poke"}, requested_names]}
                    }
                }
            }}
        ]
        
        matched = []
        cursor = self.constdata_collection.aggregate(pipeline)
        async for doc in cursor:
            matched = doc.get("matched", [])
            break

        matched_lower = [m.lower() for m in matched]
        invalid = [name for name in requested_names if name not in matched_lower]
        
        return matched, invalid

    async def _handle_role_config(self, ctx: commands.Context, role_key: str, category_name: str, role: discord.Role = None):
        g_id = str(ctx.guild.id)
        if role is None:
            current_role_id = await self.get_guild_role(g_id, role_key)
            if current_role_id:
                await ctx.reply(f"Current **{category_name}** role: <@&{current_role_id}>", mention_author=False)
            else:
                await ctx.reply(f"No role configured for **{category_name}**.", mention_author=False)
        else:
            await self.set_guild_role(g_id, role_key, str(role.id))
            await ctx.reply(f"Set **{category_name}** ping role to {role.mention}", mention_author=False)

    # --- Shiny Hunt Command ---

    @commands.command(name="sh")
    async def shiny_hunt(self, ctx: commands.Context, *, pokemon: str = None):
        g_id = str(ctx.guild.id)
        u_id = str(ctx.author.id)

        if not pokemon:
            current_sh = await self.get_ping_data(g_id, "sh", u_id)
            if current_sh:
                await ctx.reply(f"✨ Your current Shiny Hunt target is **{current_sh}**.", mention_author=False)
            else:
                await ctx.reply("You don't have a Shiny Hunt target set. Usage: `.sh <pokemon>`", mention_author=False)
            return

        matched_names, _ = await self.parse_pokemon_list(pokemon)
        if not matched_names:
            await ctx.reply("Pokémon does not exist.", mention_author=False)
            return

        matched_name = matched_names[0]
        await self.set_ping_data(g_id, "sh", u_id, matched_name)
        await ctx.reply(f"✨ Set your Shiny Hunt target to **{matched_name}** in this server!", mention_author=False)

    # --- Collection List Commands ---

    @commands.group(name="cl", invoke_without_command=True)
    async def cl_group(self, ctx: commands.Context):
        await ctx.reply("Usage: `.cl add <pokemon1, pokemon2...>`, `.cl remove <pokemon1, pokemon2...>`, `.cl clear`, or `.cl list`", mention_author=False)

    @cl_group.command(name="add", aliases=["a"])
    async def cl_add(self, ctx: commands.Context, *, pokemon: str = None):
        if not pokemon:
            await ctx.reply("Please specify at least one Pokémon name.", mention_author=False)
            return

        matched_names, invalid_names = await self.parse_pokemon_list(pokemon)
        if not matched_names:
            await ctx.reply("None of the specified Pokémon exist.", mention_author=False)
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

        await ctx.reply("\n".join(msg_parts), mention_author=False)

    @cl_group.command(name="remove", aliases=["r"])
    async def cl_remove(self, ctx: commands.Context, *, pokemon: str = None):
        if not pokemon:
            await ctx.reply("Please specify at least one Pokémon name.", mention_author=False)
            return

        g_id = str(ctx.guild.id)
        u_id = str(ctx.author.id)

        user_list = await self.get_ping_data(g_id, "cl", u_id, default=[])

        if not user_list:
            await ctx.reply("Your collection list is empty.", mention_author=False)
            return

        raw_targets = {p.strip().lower() for p in pokemon.split(",") if p.strip()}
        removed, not_found = [], []

        new_list = []
        for item in user_list:
            if item.lower() in raw_targets:
                removed.append(item)
                raw_targets.remove(item.lower())
            else:
                new_list.append(item)

        if raw_targets:
            not_found = list(raw_targets)

        if removed:
            await self.set_ping_data(g_id, "cl", u_id, new_list)

        msg_parts = []
        if removed:
            msg_parts.append(f"🗑️ Removed from collection: **{', '.join(removed)}**")
        if not_found:
            msg_parts.append(f"❌ Not found in list: **{', '.join(not_found)}**")

        await ctx.reply("\n".join(msg_parts), mention_author=False)

    @cl_group.command(name="clear", aliases=["c"])
    async def cl_clear(self, ctx: commands.Context):
        g_id = str(ctx.guild.id)
        u_id = str(ctx.author.id)

        await self.set_ping_data(g_id, "cl", u_id, [])
        await ctx.reply("🧹 Cleared your collection list!", mention_author=False)

    @cl_group.command(name="list", aliases=["l"])
    async def cl_list(self, ctx: commands.Context):
        g_id = str(ctx.guild.id)
        u_id = str(ctx.author.id)

        user_list = await self.get_ping_data(g_id, "cl", u_id, default=[])

        embed = discord.Embed(title=f"📦 {ctx.author.display_name}'s Collection List", color=discord.Color.gold())
        embed.description = "\n".join(f"• {name}" for name in user_list) if user_list else "*Your collection list is empty.*"

        await ctx.reply(embed=embed, mention_author=False)

    # --- Reserves Commands ---

    @commands.group(name="reserves", aliases=["reserve", "res", "re"], invoke_without_command=True)
    async def reserves(self, ctx: commands.Context):
        g_id = str(ctx.guild.id)
        doc = await self._get_guild_doc(g_id)
        re_data = doc.get("re", {})

        embed = discord.Embed(title=f"📋 {ctx.guild.name} Reserves List", color=discord.Color.purple())
        lines = []

        for uid, plist in re_data.items():
            if plist:
                user = ctx.guild.get_member(int(uid))
                user_str = user.mention if user else f"User ID {uid}"
                lines.append(f"• {user_str}: {', '.join(plist)}")

        embed.description = "\n".join(lines) if lines else "*No active reserves in this server.*"
        await ctx.reply(embed=embed, mention_author=False)

    @reserves.command(name="add", aliases=["a"])
    @commands.has_permissions(administrator=True)
    async def re_add(self, ctx: commands.Context, member: discord.Member, *, pokemon: str):
        matched_names, invalid_names = await self.parse_pokemon_list(pokemon)
        if not matched_names:
            await ctx.reply("None of the specified Pokémon exist.", mention_author=False)
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

        await ctx.reply("\n".join(msg_parts), mention_author=False)

    @reserves.command(name="remove", aliases=["r"])
    @commands.has_permissions(administrator=True)
    async def re_remove(self, ctx: commands.Context, member: discord.Member, *, pokemon: str):
        g_id = str(ctx.guild.id)
        u_id = str(member.id)

        user_list = await self.get_ping_data(g_id, "re", u_id, default=[])

        if not user_list:
            await ctx.reply(f"{member.mention} has no reserves.", mention_author=False)
            return

        raw_targets = {p.strip().lower() for p in pokemon.split(",") if p.strip()}
        removed, not_found = [], []

        new_list = []
        for item in user_list:
            if item.lower() in raw_targets:
                removed.append(item)
                raw_targets.remove(item.lower())
            else:
                new_list.append(item)

        if raw_targets:
            not_found = list(raw_targets)

        if removed:
            await self.set_ping_data(g_id, "re", u_id, new_list)

        msg_parts = []
        if removed:
            msg_parts.append(f"🗑️ Removed from {member.mention}'s reserves: **{', '.join(removed)}**")
        if not_found:
            msg_parts.append(f"❌ Not found in reserves: **{', '.join(not_found)}**")

        await ctx.reply("\n".join(msg_parts), mention_author=False)

    @reserves.command(name="clear", aliases=["c"])
    @commands.has_permissions(administrator=True)
    async def re_clear(self, ctx: commands.Context, member: discord.Member = None):
        g_id = str(ctx.guild.id)

        if member:
            u_id = str(member.id)
            await self.set_ping_data(g_id, "re", u_id, [])
            await ctx.reply(f"🧹 Cleared all reserves for {member.mention}!", mention_author=False)
        else:
            await self.clear_ping_category(g_id, "re")
            await ctx.reply("🧹 Cleared **ALL** reserves for this server!", mention_author=False)

    @re_add.error
    @re_remove.error
    @re_clear.error
    async def reserves_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("❌ You must have **Administrator** permissions to manage reserves.", mention_author=False)
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.reply("❌ Missing arguments. Usage: `.re add @user <pokemon1, pokemon2...>` or `.re clear [@user]`", mention_author=False)
        elif isinstance(error, commands.MemberNotFound):
            await ctx.reply("❌ Could not find that user.", mention_author=False)

    # --- Type & Region Commands ---

    @commands.command(name="tp")
    async def type_pings(self, ctx: commands.Context, *, target: str = None):
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
            await ctx.reply(f"❌ Invalid type(s). Valid types are: {', '.join(TYPES)}", mention_author=False)
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

        await ctx.reply("\n".join(msg), mention_author=False)

    @commands.command(name="rp")
    async def region_pings(self, ctx: commands.Context, *, target: str = None):
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
            await ctx.reply(f"❌ Invalid region/category. Valid options are: {', '.join(all_items)}", mention_author=False)
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

        await ctx.reply("\n".join(msg), mention_author=False)


async def setup(bot):
    await bot.add_cog(PokePings(bot))