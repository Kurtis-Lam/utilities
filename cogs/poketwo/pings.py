import json
from pathlib import Path
from typing import List, Tuple, Optional
import certifi
import discord
from discord.ext import commands
import motor.motor_asyncio  # Replaced pymongo with asynchronous motor

MONGO_URI = "mongodb+srv://KurtisLam:CsHLOnDqihiU5uYG@cluster0.7rwx3oc.mongodb.net/?appName=Cluster0"

TYPES = [
    "Normal", "Fire", "Water", "Grass", "Electric", "Ice",
    "Fighting", "Poison", "Ground", "Flying", "Psychic", "Bug",
    "Rock", "Ghost", "Dragon", "Steel", "Dark", "Fairy"
]

REGIONS = [
    "Kanto", "Johto", "Hoenn", "Sinnoh", "Unova",
    "Kalos", "Alola", "Galar", "Hisui", "Paldea"
]


def resolve_file_path(filename: str) -> Path:
    cog_dir_path = Path(__file__).resolve().parent / filename
    root_dir_path = Path.cwd() / filename
    if cog_dir_path.exists():
        return cog_dir_path
    if root_dir_path.exists():
        return root_dir_path
    return cog_dir_path


POKEVARS_FILE = resolve_file_path("data/pokevars.json")


# --- UI Components for Type Pings ---

class TypePingButton(discord.ui.Button):
    def __init__(self, type_name: str, is_active: bool):
        super().__init__(
            label=type_name,
            style=discord.ButtonStyle.green if is_active else discord.ButtonStyle.red,
            custom_id=f"tp_{type_name}"
        )
        self.type_name = type_name

    async def callback(self, interaction: discord.Interaction):
        view: TypePingView = self.view
        if interaction.user.id != view.user_id:
            return await interaction.response.send_message("This interactive menu is not for you.", ephemeral=True)

        g_id = str(interaction.guild_id)
        u_id = str(interaction.user.id)

        # AWAITED: Async fetch
        user_types = await view.cog.get_ping_data(g_id, "tp", u_id, default=[])

        if self.type_name in user_types:
            user_types.remove(self.type_name)
            self.style = discord.ButtonStyle.red
        else:
            user_types.append(self.type_name)
            self.style = discord.ButtonStyle.green

        # AWAITED: Async write
        await view.cog.set_ping_data(g_id, "tp", u_id, user_types)
        embed = view.make_embed(user_types)
        await interaction.response.edit_message(embed=embed, view=view)


class TypePingView(discord.ui.View):
    def __init__(self, cog: "PokePings", user_id: int, user_types: list):
        super().__init__(timeout=180)
        self.cog = cog
        self.user_id = user_id
        for t in TYPES:
            self.add_item(TypePingButton(t, t in user_types))

    def make_embed(self, user_types: list) -> discord.Embed:
        embed = discord.Embed(title="⚡ Type Pings Configuration", color=discord.Color.blue())
        embed.description = "\n".join(
            f"{'✅' if t in user_types else '❌'} **{t}**" for t in TYPES
        )
        return embed


# --- UI Components for Region Pings ---

class RegionPingButton(discord.ui.Button):
    def __init__(self, region_name: str, is_active: bool):
        super().__init__(
            label=region_name,
            style=discord.ButtonStyle.green if is_active else discord.ButtonStyle.red,
            custom_id=f"rp_{region_name}"
        )
        self.region_name = region_name

    async def callback(self, interaction: discord.Interaction):
        view: RegionPingView = self.view
        if interaction.user.id != view.user_id:
            return await interaction.response.send_message("This interactive menu is not for you.", ephemeral=True)

        g_id = str(interaction.guild_id)
        u_id = str(interaction.user.id)

        # AWAITED: Async fetch
        user_regions = await view.cog.get_ping_data(g_id, "rp", u_id, default=[])

        if self.region_name in user_regions:
            user_regions.remove(self.region_name)
            self.style = discord.ButtonStyle.red
        else:
            user_regions.append(self.region_name)
            self.style = discord.ButtonStyle.green

        # AWAITED: Async write
        await view.cog.set_ping_data(g_id, "rp", u_id, user_regions)
        embed = view.make_embed(user_regions)
        await interaction.response.edit_message(embed=embed, view=view)


class RegionPingView(discord.ui.View):
    def __init__(self, cog: "PokePings", user_id: int, user_regions: list):
        super().__init__(timeout=180)
        self.cog = cog
        self.user_id = user_id
        for r in REGIONS:
            self.add_item(RegionPingButton(r, r in user_regions))

    def make_embed(self, user_regions: list) -> discord.Embed:
        embed = discord.Embed(title="🌍 Region Pings Configuration", color=discord.Color.blue())
        embed.description = "\n".join(
            f"{'✅' if r in user_regions else '❌'} **{r}**" for r in REGIONS
        )
        return embed


# --- Cog Definition ---

class PokePings(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Async Motor Client
        self.mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI, tlsCAFile=certifi.where())
        self.db = self.mongo_client["utilities"]
        self.collection = self.db["pings"]
        self.pokevars = {}

    async def cog_load(self):
        """Warms up connection and asynchronously loads local pokevars data on boot."""
        try:
            await self.mongo_client.admin.command('ping')
            self.pokevars = self._load_pokevars()
        except Exception as e:
            print(f"PokePings Cog: MongoDB warmup failed: {e}")

    async def _get_guild_doc(self, guild_id: str) -> dict:
        # AWAITED: Non-blocking DB find
        doc = await self.collection.find_one({"_id": guild_id})
        return doc if doc else {"_id": guild_id, "sh": {}, "cl": {}, "re": {}, "tp": {}, "rp": {}}

    async def get_ping_data(self, guild_id: str, ping_type: str, user_id: str, default=None):
        doc = await self._get_guild_doc(guild_id)
        return doc.get(ping_type, {}).get(user_id, default)

    async def set_ping_data(self, guild_id: str, ping_type: str, user_id: str, value):
        # AWAITED: Non-blocking DB update
        await self.collection.update_one(
            {"_id": guild_id},
            {"$set": {f"{ping_type}.{user_id}": value}},
            upsert=True
        )

    async def clear_ping_category(self, guild_id: str, ping_type: str, user_id: Optional[str] = None):
        if user_id:
            # AWAITED
            await self.collection.update_one(
                {"_id": guild_id},
                {"$unset": {f"{ping_type}.{user_id}": ""}}
            )
        else:
            # AWAITED
            await self.collection.update_one(
                {"_id": guild_id},
                {"$set": {ping_type: {}}},
                upsert=True
            )

    def _load_pokevars(self) -> dict:
        """Loads pokevars dictionary once into memory on startup."""
        if POKEVARS_FILE.exists():
            try:
                with open(POKEVARS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return {name.strip().lower(): name.strip() for name in data}
            except Exception:
                return {}
        return {}

    def parse_pokemon_list(self, raw_input: str) -> Tuple[List[str], List[str]]:
        """Splits input and matches Pokémon names directly against memory lookup."""
        names = [p.strip().lower() for p in raw_input.split(",") if p.strip()]
        matched = []
        invalid = []
        for name in names:
            m = self.pokevars.get(name)
            if m:
                if m not in matched:
                    matched.append(m)
            else:
                invalid.append(name)
        return matched, invalid

    # --- Shiny Hunt Command ---

    @commands.command(name="sh")
    async def shiny_hunt(self, ctx: commands.Context, *, pokemon: str = None):
        g_id = str(ctx.guild.id)
        u_id = str(ctx.author.id)

        if not pokemon:
            # AWAITED
            current_sh = await self.get_ping_data(g_id, "sh", u_id)
            if current_sh:
                await ctx.send(f"✨ Your current Shiny Hunt target is **{current_sh}**.")
            else:
                await ctx.send("You don't have a Shiny Hunt target set. Usage: `.sh <pokemon>`")
            return

        matched_name = self.pokevars.get(pokemon.strip().lower())
        if not matched_name:
            await ctx.send("Pokémon does not exist.")
            return

        # AWAITED
        await self.set_ping_data(g_id, "sh", u_id, matched_name)
        await ctx.send(f"✨ Set your Shiny Hunt target to **{matched_name}** in this server!")

    # --- Collection List Commands ---

    @commands.group(name="cl", invoke_without_command=True)
    async def collection(self, ctx: commands.Context):
        await ctx.send("Usage: `.cl add <pokemon1, pokemon2...>`, `.cl remove <pokemon1, pokemon2...>`, `.cl clear`, or `.cl list`")

    @collection.command(name="add", aliases=["a"])
    async def cl_add(self, ctx: commands.Context, *, pokemon: str = None):
        if not pokemon:
            await ctx.send("Please specify at least one Pokémon name.")
            return

        matched_names, invalid_names = self.parse_pokemon_list(pokemon)
        if not matched_names:
            await ctx.send("None of the specified Pokémon exist.")
            return

        g_id = str(ctx.guild.id)
        u_id = str(ctx.author.id)

        # AWAITED
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

        # AWAITED
        await self.set_ping_data(g_id, "cl", u_id, user_list)

        msg_parts = []
        if added:
            msg_parts.append(f"📦 Added to collection: **{', '.join(added)}**")
        if already_in:
            msg_parts.append(f"⚠️ Already in collection: **{', '.join(already_in)}**")
        if invalid_names:
            msg_parts.append(f"❌ Invalid Pokémon: **{', '.join(invalid_names)}**")

        await ctx.send("\n".join(msg_parts))

    @collection.command(name="remove", aliases=["r"])
    async def cl_remove(self, ctx: commands.Context, *, pokemon: str = None):
        if not pokemon:
            await ctx.send("Please specify at least one Pokémon name.")
            return

        g_id = str(ctx.guild.id)
        u_id = str(ctx.author.id)

        # AWAITED
        user_list = await self.get_ping_data(g_id, "cl", u_id, default=[])

        if not user_list:
            await ctx.send("Your collection list is empty.")
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
            # AWAITED
            await self.set_ping_data(g_id, "cl", u_id, new_list)

        msg_parts = []
        if removed:
            msg_parts.append(f"🗑️ Removed from collection: **{', '.join(removed)}**")
        if not_found:
            msg_parts.append(f"❌ Not found in list: **{', '.join(not_found)}**")

        await ctx.send("\n".join(msg_parts))

    @collection.command(name="clear", aliases=["c"])
    async def cl_clear(self, ctx: commands.Context):
        g_id = str(ctx.guild.id)
        u_id = str(ctx.author.id)

        # AWAITED
        await self.set_ping_data(g_id, "cl", u_id, [])
        await ctx.send("🧹 Cleared your collection list!")

    @collection.command(name="list", aliases=["l"])
    async def cl_list(self, ctx: commands.Context):
        g_id = str(ctx.guild.id)
        u_id = str(ctx.author.id)

        # AWAITED
        user_list = await self.get_ping_data(g_id, "cl", u_id, default=[])

        embed = discord.Embed(title=f"📦 {ctx.author.display_name}'s Collection List", color=discord.Color.gold())
        embed.description = "\n".join(f"• {name}" for name in user_list) if user_list else "*Your collection list is empty.*"

        await ctx.send(embed=embed)

    # --- Reserves Commands ---

    @commands.group(name="reserves", aliases=["reserve", "re"], invoke_without_command=True)
    async def reserves(self, ctx: commands.Context):
        g_id = str(ctx.guild.id)
        # AWAITED
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
        await ctx.send(embed=embed)

    @reserves.command(name="add", aliases=["a"])
    @commands.has_permissions(administrator=True)
    async def re_add(self, ctx: commands.Context, member: discord.Member, *, pokemon: str):
        matched_names, invalid_names = self.parse_pokemon_list(pokemon)
        if not matched_names:
            await ctx.send("None of the specified Pokémon exist.")
            return

        g_id = str(ctx.guild.id)
        u_id = str(member.id)

        # AWAITED
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

        # AWAITED
        await self.set_ping_data(g_id, "re", u_id, user_list)

        msg_parts = []
        if added:
            msg_parts.append(f"📌 Added to {member.mention}'s reserves: **{', '.join(added)}**")
        if already_in:
            msg_parts.append(f"⚠️ Already in reserves: **{', '.join(already_in)}**")
        if invalid_names:
            msg_parts.append(f"❌ Invalid Pokémon: **{', '.join(invalid_names)}**")

        await ctx.send("\n".join(msg_parts))

    @reserves.command(name="remove", aliases=["r"])
    @commands.has_permissions(administrator=True)
    async def re_remove(self, ctx: commands.Context, member: discord.Member, *, pokemon: str):
        g_id = str(ctx.guild.id)
        u_id = str(member.id)

        # AWAITED
        user_list = await self.get_ping_data(g_id, "re", u_id, default=[])

        if not user_list:
            await ctx.send(f"{member.mention} has no reserves.")
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
            # AWAITED
            await self.set_ping_data(g_id, "re", u_id, new_list)

        msg_parts = []
        if removed:
            msg_parts.append(f"🗑️ Removed from {member.mention}'s reserves: **{', '.join(removed)}**")
        if not_found:
            msg_parts.append(f"❌ Not found in reserves: **{', '.join(not_found)}**")

        await ctx.send("\n".join(msg_parts))

    @reserves.command(name="clear", aliases=["c"])
    @commands.has_permissions(administrator=True)
    async def re_clear(self, ctx: commands.Context, member: discord.Member = None):
        g_id = str(ctx.guild.id)

        if member:
            u_id = str(member.id)
            # AWAITED
            await self.set_ping_data(g_id, "re", u_id, [])
            await ctx.send(f"🧹 Cleared all reserves for {member.mention}!")
        else:
            # AWAITED
            await self.clear_ping_category(g_id, "re")
            await ctx.send("🧹 Cleared **ALL** reserves for this server!")

    @re_add.error
    @re_remove.error
    @re_clear.error
    async def reserves_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ You must have **Administrator** permissions to manage reserves.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("❌ Missing arguments. Usage: `.re add @user <pokemon1, pokemon2...>` or `.re clear [@user]`")
        elif isinstance(error, commands.MemberNotFound):
            await ctx.send("❌ Could not find that user.")

    # --- Type & Region Commands ---

    @commands.command(name="tp")
    async def type_pings(self, ctx: commands.Context):
        g_id = str(ctx.guild.id)
        u_id = str(ctx.author.id)

        # AWAITED
        user_types = await self.get_ping_data(g_id, "tp", u_id, default=[])
        view = TypePingView(self, ctx.author.id, user_types)
        embed = view.make_embed(user_types)
        await ctx.send(embed=embed, view=view)

    @commands.command(name="rp")
    async def region_pings(self, ctx: commands.Context):
        g_id = str(ctx.guild.id)
        u_id = str(ctx.author.id)

        # AWAITED
        user_regions = await self.get_ping_data(g_id, "rp", u_id, default=[])
        view = RegionPingView(self, ctx.author.id, user_regions)
        embed = view.make_embed(user_regions)
        await ctx.send(embed=embed, view=view)


async def setup(bot):
    await bot.add_cog(PokePings(bot))