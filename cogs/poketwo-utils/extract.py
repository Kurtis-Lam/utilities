import re
import discord
from discord.ext import commands

class PokemonExtractor(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="extract", aliases=["ex"])
    async def extract(self, ctx):
        if not ctx.message.reference:
            await ctx.reply("❌ Please reply to a Pokétwo message to use this command.", mention_author=False)
            return

        try:
            replied_message = await ctx.channel.fetch_message(ctx.message.reference.message_id)
        except discord.HTTPException:
            await ctx.reply("❌ Unable to fetch the replied message.", mention_author=False)
            return

        if not replied_message.embeds:
            await ctx.reply("❌ The replied message does not contain an embed.", mention_author=False)
            return

        embed = replied_message.embeds[0]

        # Extract content from description and fields
        content_parts = []
        if embed.description:
            content_parts.append(embed.description)

        for field in embed.fields:
            content_parts.append(field.name)
            content_parts.append(field.value)

        content = "\n".join(content_parts)

        # Captures Group 1: Name, Group 2: Dex Number (e.g., standard Pokédex entries)
        pokedex_matches = re.findall(
            r'(?:<a?:[^\n:]+:\d+>\s*)?\*?\*?([A-Za-z0-9.\- \'\u0080-\uffff]+?)\*?\*?\s+#(\d+)(?!\d|-|\))',
            content
        )

        # Captures Group 1: List ID, Group 2: Raw bolded string (e.g., "Level 36 Zubat ♀")
        list_matches = re.findall(r'`(\d+)`[^\n]*?\*\*(.*?)\*\*', content)

        if pokedex_matches:
            names = [match[0].strip() for match in pokedex_matches]
            dex_numbers = [match[1] for match in pokedex_matches]

            names_str = ", ".join(names)
            numbers_str = ", ".join([f"#{num}" for num in dex_numbers])

            await replied_message.reply(
                f"Names:\n```\n{names_str}\n```\nDex Numbers:\n```\n{numbers_str}\n```",
                mention_author=False
            )
        elif list_matches:
            pokemon_ids = []
            names = []

            for pokemon_id, raw_name in list_matches:
                # Strip leading "Level 36", "✨", and spaces
                clean_name = re.sub(r'^(?:✨|Level\s+\d+|\s)+', '', raw_name, flags=re.IGNORECASE)
                # Strip trailing gender symbols ♂/♀ and trailing spaces
                clean_name = re.sub(r'[\s♂♀]+$', '', clean_name)

                pokemon_ids.append(pokemon_id)
                names.append(clean_name.strip())

            ids_str = " ".join(pokemon_ids)
            names_str = ", ".join(names)

            await replied_message.reply(
                f"Pokemon IDs:\n```\n{ids_str}\n```\nPokemon Names:\n```\n{names_str}\n```",
                mention_author=False
            )
        else:
            await replied_message.reply("❌ No Pokémon IDs or Pokédex entries found in that embed.", mention_author=False)

async def setup(bot):
    await bot.add_cog(PokemonExtractor(bot))