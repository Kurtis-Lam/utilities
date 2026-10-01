import re
import discord
from discord.ext import commands

from views.embeds import err_embed, handle_command_error, info_embed, send_usage


def _clip(text: str, limit: int = 1900) -> str:
    """Trim text so two code blocks always fit inside one embed description (4096)."""
    return text if len(text) <= limit else text[: limit - 3] + "..."


class PokemonExtractor(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_command_error(self, ctx: commands.Context, error):
        await handle_command_error(ctx, error)

    @commands.command(
        name="extract",
        aliases=["ex"],
        description=(
            "Reply to a Pokétwo embed (Pokédex / pokémon list) with this command to "
            "extract the Pokémon names, dex numbers or IDs."
        ),
    )
    async def extract(self, ctx):
        if not ctx.message.reference:
            await send_usage(ctx, note="Please reply to a Pokétwo message to use this command.")
            return

        try:
            replied_message = await ctx.channel.fetch_message(ctx.message.reference.message_id)
        except discord.HTTPException:
            await ctx.reply(
                embed=err_embed("Fetch Failed", "Unable to fetch the replied message."),
                mention_author=False,
            )
            return

        if not replied_message.embeds:
            await ctx.reply(
                embed=err_embed("No Embed Found", "The replied message does not contain an embed."),
                mention_author=False,
            )
            return

        embed = replied_message.embeds[0]

        # Extract content from description, field names, and field values
        content_parts = []
        if embed.description:
            content_parts.append(embed.description)

        for field in embed.fields:
            content_parts.append(field.name)
            content_parts.append(field.value)

        content = "\n".join(content_parts)

        # Captures Group 1: Pokemon Name, Group 2: Dex Number (e.g., standard Pokédex entries)
        pokedex_matches = re.findall(
            r'(?:<a?:[^\n:]+:\d+>\s*)?\*?\*?([A-Za-z0-9.\- \'\u0080-\uffff]+?)\*?\*?\s+#(\d+)(?!\d|-|\))',
            content
        )

        # Captures Group 1: List ID, Group 2: Raw bolded content inside **...**
        list_matches = re.findall(r'`(\d+)`[^\n]*?\*\*(.*?)\*\*', content)

        if pokedex_matches:
            names = [match[0].strip() for match in pokedex_matches]
            dex_numbers = [match[1] for match in pokedex_matches]

            names_str = _clip(", ".join(names))
            numbers_str = _clip(", ".join([f"#{num}" for num in dex_numbers]))

            result = info_embed(
                "Pokédex Extract",
                f"**Names:**\n```\n{names_str}\n```\n**Dex Numbers:**\n```\n{numbers_str}\n```",
                emoji="📋",
            )
            result.set_footer(text=f"{len(names)} Pokémon extracted")
            await replied_message.reply(embed=result, mention_author=False)
        elif list_matches:
            pokemon_ids = []
            names = []

            for pokemon_id, raw_name in list_matches:
                # 1. Strip Discord custom emojis (<:emoji:123>) and shortcodes (:_:, :male:, :female:)
                clean = re.sub(r'<a?:[^\n:]+:\d+>|:[a-zA-Z0-9_]+:', '', raw_name)

                # 2. Strip "Level XX", "Lvl XX", shiny stars (✨), and leading whitespace
                clean = re.sub(r'^(?:✨|\s|Level\s+\d+|Lvl\s+\d+)+', '', clean, flags=re.IGNORECASE)

                # 3. Strip trailing gender symbols (♂/♀) and trailing whitespace
                clean = re.sub(r'[\s♂♀]+$', '', clean)

                clean_name = clean.strip()

                if clean_name:
                    pokemon_ids.append(pokemon_id)
                    names.append(clean_name)

            ids_str = _clip(" ".join(pokemon_ids))
            names_str = _clip(", ".join(names))

            result = info_embed(
                "Pokémon List Extract",
                f"**Pokémon IDs:**\n```\n{ids_str}\n```\n**Pokémon Names:**\n```\n{names_str}\n```",
                emoji="📋",
            )
            result.set_footer(text=f"{len(names)} Pokémon extracted")
            await replied_message.reply(embed=result, mention_author=False)
        else:
            await replied_message.reply(
                embed=err_embed(
                    "Nothing Found",
                    "No Pokémon IDs or Pokédex entries found in that embed.",
                ),
                mention_author=False,
            )


async def setup(bot):
    await bot.add_cog(PokemonExtractor(bot))