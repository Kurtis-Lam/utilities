from collections import defaultdict
import discord
from discord.ext import commands

TARGET_USER_ID = 716390085896962058

class HintSolver(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Pre-indexes candidates as (original_name, lower_name) by string length
        self.pokemons_by_len = defaultdict(list)

    @property
    def collection(self):
        # Uses the centralized Mongo client on self.bot to save RAM
        return self.bot.mongo_client["utilities"]["constdata"]

    async def cog_load(self):
        """Loads Pokémon data into RAM asynchronously on cog load."""
        await self.load_pokemon_data()

    async def load_pokemon_data(self):
        """Loads pokevars from MongoDB and stores pre-lowercased pairs."""
        try:
            doc = await self.collection.find_one({"_id": "pokevars"})
            pokemons = doc.get("data", []) if doc else []

            self.pokemons_by_len.clear()
            for name in pokemons:
                # Store (original_name, name_lower) tuple to avoid runtime .lower() calls
                self.pokemons_by_len[len(name)].append((name, name.lower()))
        except Exception as e:
            print(f"[HintSolver] Failed to load pokevars from MongoDB: {e}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.id != TARGET_USER_ID:
            return

        content = message.content
        if not content and message.embeds:
            content = "\n".join(e.description for e in message.embeds if e.description)

        if not content:
            return

        # Fast string indexing
        content_lower = content.lower()
        target_str = "the pokémon is "
        idx = content_lower.find(target_str)

        if idx == -1:
            target_str = "the pokemon is "
            idx = content_lower.find(target_str)
            if idx == -1:
                return

        # Slice raw hint
        start = idx + len(target_str)
        end = content.find("\n", start)
        raw_hint = content[start:end] if end != -1 else content[start:]

        # Fast sanitization
        raw_hint = (
            raw_hint.replace("\\", "")
            .replace("*", "")
            .replace("`", "")
            .strip()
            .rstrip(".")
        )

        if not raw_hint:
            return

        candidates = self.pokemons_by_len.get(len(raw_hint))
        if not candidates:
            return

        raw_hint_lower = raw_hint.lower()
        hint_len = len(raw_hint_lower)

        # Index-based positional character match against pre-lowercased strings
        matches = [
            orig_name for orig_name, lower_name in candidates
            if all(
                raw_hint_lower[i] == "_" or raw_hint_lower[i] == lower_name[i]
                for i in range(hint_len)
            )
        ]

        if matches:
            response = f"Possible Pokémon: {', '.join(matches)}"
            if len(response) > 2000:
                response = response[:1990] + "..."

            await message.reply(response)


async def setup(bot: commands.Bot):
    await bot.add_cog(HintSolver(bot))