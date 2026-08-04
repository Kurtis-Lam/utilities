import asyncio
import re
import discord
from discord.ext import commands

class PokemonDetector(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.target_user_id = 874910942490677270

    @commands.Cog.listener()
    async def on_message(self, message):
        # Ignore messages from other users
        if message.author.id != self.target_user_id:
            return

        # Check if the message begins with "##"
        if message.content.startswith("##"):
            # Clean up the content by removing the "##" prefix
            content = message.content[2:].strip()
            
            # Extract the Pokémon name (everything before the first custom emoji or special character)
            pokemon_name = content.split("<:")[0].split("【")[0].strip()

            # Remove anything inside parentheses (and the parentheses themselves), e.g., "Nidoran (M)" -> "Nidoran"
            pokemon_name = re.sub(r'\s*\([^)]*\)', '', pokemon_name).strip()

            if pokemon_name:
                await message.channel.send(f"Detected: {pokemon_name} (100.00%)")

async def setup(bot):
    await bot.add_cog(PokemonDetector(bot))
