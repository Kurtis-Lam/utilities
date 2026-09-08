import asyncio
import re
import discord
from discord.ext import commands

class PokemonExtractor(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="extract", aliases=["ex"])
    async def extract(self, ctx):
        if not ctx.message.reference:
            await ctx.send("❌ Please reply to a Pokétwo message to use this command.")
            return

        try:
            replied_message = await ctx.channel.fetch_message(ctx.message.reference.message_id)
        except discord.HTTPException:
            await ctx.send("❌ Unable to fetch the replied message.")
            return

        if not replied_message.embeds:
            await ctx.send("❌ The replied message does not contain an embed.")
            return

        embed = replied_message.embeds[0]

        # Extract content from description, field names, and field values
        content_parts = []
        if embed.description:
            content_parts.append(embed.description)

        for field in embed.fields:
            content_parts.append(field.name)   # Pokédex names live here
            content_parts.append(field.value)

        content = "\n".join(content_parts)

        # Matches Pokémon entries (e.g., "<:emoji:> Bulbasaur #1", "**Charmander #4**")
        # Ignores milestone range headers like "(#1-#809)"
        pokedex_names = re.findall(
            r'(?:<a?:[^\n:]+:\d+>\s*)?\*?\*?([A-Za-z0-9.\- \'\u0080-\uffff]+?)\*?\*?\s+#\d+(?!\d|-|\))',
            content
        )

        # Matches list IDs formatted in codeblocks (e.g., "`76307`")
        pokemon_ids = re.findall(r'`(\d+)`', content)

        if pokedex_names:
            clean_names = [name.strip() for name in pokedex_names]
            await ctx.send(", ".join(clean_names))
        elif pokemon_ids:
            await ctx.send(" ".join(pokemon_ids))
        else:
            await ctx.send("❌ No Pokémon IDs or Pokédex entries found in that embed.")


async def setup(bot):
    await bot.add_cog(PokemonExtractor(bot))