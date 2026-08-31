from collections import defaultdict
import certifi
import discord
from discord.ext import commands
import motor.motor_asyncio  # Replaced pymongo with asynchronous motor

MONGO_URI = "mongodb+srv://KurtisLam:CsHLOnDqihiU5uYG@cluster0.7rwx3oc.mongodb.net/?appName=Cluster0"
TARGET_USER_ID = 716390085896962058

class HintSolver(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Async Motor Client
        self.mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI, tlsCAFile=certifi.where())
        self.db = self.mongo_client["utilities"]
        self.collection = self.db["constdata"]

        # Group Pokémon by string length for O(1) candidate lookup
        self.pokemons_by_len = defaultdict(list)
        # Removed synchronous load_pokemon_data() from here

    async def cog_load(self):
        """Warms up connection and asynchronously loads Pokémon data into memory on boot."""
        try:
            await self.mongo_client.admin.command('ping')
            await self.load_pokemon_data()
        except Exception as e:
            print(f"[HintSolver] MongoDB warmup or data loading failed: {e}")

    async def load_pokemon_data(self):
        """Loads Pokémon list from MongoDB into RAM asynchronously and indexes them by name length."""
        try:
            # AWAITED: Non-blocking fetch
            doc = await self.collection.find_one({"_id": "pokevars"})
            pokemons = doc.get("data", []) if doc else []

            self.pokemons_by_len.clear()
            for name in pokemons:
                self.pokemons_by_len[len(name)].append(name)
        except Exception as e:
            print(f"[HintSolver] Failed to load pokevars from MongoDB: {e}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.id != TARGET_USER_ID:
            return

        # Fast extraction without unnecessary string concatenation
        content = message.content
        if not content and message.embeds:
            content = "\n".join(e.description for e in message.embeds if e.description)

        if not content:
            return

        # Fast string lookup instead of re.search
        content_lower = content.lower()
        target_str = "the pokémon is "
        idx = content_lower.find(target_str)

        if idx == -1:
            target_str = "the pokemon is "
            idx = content_lower.find(target_str)
            if idx == -1:
                return

        # Extract raw hint line
        start = idx + len(target_str)
        end = content.find("\n", start)
        raw_hint = content[start:end] if end != -1 else content[start:]

        # Clean string formatting using built-in string methods (faster than re.sub)
        raw_hint = (
            raw_hint.replace("\\", "")
            .replace("*", "")
            .replace("`", "")
            .strip()
            .rstrip(".")
        )

        if not raw_hint:
            return

        # Look up ONLY candidates that match the exact character length
        candidates = self.pokemons_by_len.get(len(raw_hint))
        if not candidates:
            return

        # Compare characters directly in Python (bypasses Regex engine completely)
        raw_hint_lower = raw_hint.lower()
        matches = [
            name for name in candidates
            if all(h == "_" or h == c for h, c in zip(raw_hint_lower, name.lower()))
        ]

        if matches:
            response = f"Possible Pokémon: {', '.join(matches)}"
            if len(response) > 2000:
                response = response[:1990] + "..."

            await message.reply(response)


async def setup(bot: commands.Bot):
    await bot.add_cog(HintSolver(bot))