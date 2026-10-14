import re
import typing

import discord
from discord import app_commands
from discord.ext import commands

from views.embeds import err_embed, handle_command_error, info_embed, send_usage
from views.loading import loading


def _clip(text: str, limit: int = 1900) -> str:
    """Trim text so two code blocks always fit inside one embed description (4096)."""
    return text if len(text) <= limit else text[: limit - 3] + "..."


class PokemonExtractor(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_command_error(self, ctx: commands.Context, error):
        await handle_command_error(ctx, error)

    @commands.hybrid_command(
        name="extract",
        aliases=["ex"],
        description="Extracts names, numbers or IDs from a Pokétwo embed (reply to it, or give its ID).",
    )
    @app_commands.describe(message="ID or link of the Pokétwo message. Not needed when replying to it with a text command.")
    @loading
    async def extract(
        self,
        ctx,
        message: typing.Optional[str] = commands.parameter(
            default=None,
            description="ID or link of the Pokétwo message. Not needed when replying to it.",
        ),
    ):
        target_id = None
        if ctx.interaction is None and ctx.message.reference:
            target_id = ctx.message.reference.message_id
        elif message:
            found = re.findall(r"\d{15,25}", message)
            target_id = int(found[-1]) if found else None

        if target_id is None:
            await send_usage(ctx, note="Reply to a Pokétwo message, or give its message ID or link.")
            return

        async def send_result(embed: discord.Embed):
            # Text commands answer on the Pokétwo message itself; slash commands answer the interaction.
            if ctx.interaction is not None:
                await ctx.reply(embed=embed, mention_author=False)
            else:
                await replied_message.reply(embed=embed, mention_author=False)

        try:
            replied_message = await ctx.channel.fetch_message(target_id)
        except discord.HTTPException:
            await ctx.reply(
                embed=err_embed("Couldn't fetch that message."),
                mention_author=False,
            )
            return

        if not replied_message.embeds:
            await ctx.reply(
                embed=err_embed("No embed in that message."),
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
            await send_result(result)
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
            await send_result(result)
        else:
            await send_result(err_embed("No Pokémon found in that embed."))


async def setup(bot):
    await bot.add_cog(PokemonExtractor(bot))