import io

import discord
from discord.ext import commands

TYPE_EMOJIS = {
    "normal": "🔘",
    "fire": "🔥",
    "water": "💧",
    "electric": "⚡",
    "grass": "🌿",
    "ice": "❄️",
    "fighting": "🥊",
    "poison": "☠️",
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


class Dex(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self.pokedex = {}

    # Retrieves MongoDB instance dynamically from main.py's bot.mongo_client
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
        except Exception as e:
            print(f"❌ Failed to load Pokédex from MongoDB: {e}")
            self.pokedex = {}

    @commands.command(name="dex", aliases=["pokedex"])
    async def dex_cmd(self, ctx: commands.Context, *, query: str):
        query_clean = query.strip().lower().lstrip("#")

        matched_name = None
        data = None

        for name, info in self.pokedex.items():
            if name.lower() == query_clean:
                matched_name = name
                data = info
                break

            dex_num = str(info.get("pokedex_number", "")).lstrip("#")
            if dex_num == query_clean:
                matched_name = name
                data = info
                break

        if not data:
            await ctx.reply("❌ Pokémon not found in Pokédex.")
            return

        dex_num = data.get("pokedex_number", "???").lstrip("#")
        embed_title = f"#{dex_num} — {matched_name}"

        description = data.get("description", "")
        evolution = data.get("evolution", "")
        if evolution:
            description += f"\n\n**Evolution**\n{evolution}"

        embed = discord.Embed(
            title=embed_title,
            description=description,
            color=0xdbbe00
        )

        img_doc = await self.img_collection.find_one({"_id": matched_name.lower()})
        file = None

        if img_doc and "image" in img_doc:
            image_bytes = img_doc["image"]
            filename = f"{matched_name.lower()}.png"

            image_stream = io.BytesIO(image_bytes)
            file = discord.File(fp=image_stream, filename=filename)

            embed.set_thumbnail(url=f"attachment://{filename}")

        # 1. Types
        raw_types = data.get("types", [])
        if isinstance(raw_types, list):
            types_formatted = "\n".join(
                f"{TYPE_EMOJIS.get(t.lower(), '')} {t}".strip()
                for t in raw_types
            )
        else:
            types_formatted = str(raw_types)
        embed.add_field(
            name="Types", value=types_formatted or "N/A", inline=True
        )

        # 2. Region
        embed.add_field(
            name="Region", value=data.get("region", "N/A"), inline=True
        )

        # 3. Catchable
        catchable = data.get("catchable")
        catchable_str = (
            "Yes"
            if catchable is True
            else ("No" if catchable is False else str(catchable))
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