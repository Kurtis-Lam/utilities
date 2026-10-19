"""Guess The Number (administrators start it, everyone plays).

* ``gtn <lower> <upper> <prize>``  start a game in this channel; the secret number is DMed to you
* ``gtn hint``                     narrow the public bounds around the secret number
                                   (15 second cooldown, each hint shrinks the range by 13%)
* ``gtn end``                      cancel the running game in this channel

Anyone can guess by simply typing a number in the channel. The first correct guess wins: the bot
reacts with a ticket and congratulates the winner, saying after how many guesses it was found.
"""
import random
import re
import time
import typing
from dataclasses import dataclass, field

import discord
from discord.ext import commands

from views.embeds import BRAND_COLOR, err_embed, handle_command_error, ok_embed, send_usage

TICKET_EMOJI = "🎫"
MAX_PRIZE_LENGTH = 200
MAX_ABS_NUMBER = 10 ** 12
HINT_COOLDOWN = 15
HINT_SHRINK = 0.13
_GUESS = re.compile(r"^-?\d{1,13}$")
_NO_PINGS = discord.AllowedMentions.none()


@dataclass
class GtnGame:
    secret: int
    lower: int
    upper: int
    prize: str
    host_id: int
    low: int = 0
    high: int = 0
    guesses: int = 0
    hints: int = 0
    last_hint: float = 0.0
    started_at: float = field(default_factory=time.time)

    def __post_init__(self):
        self.low = self.lower
        self.high = self.upper


def _bounds_text(game: GtnGame) -> str:
    return f"**{game.low:,}** and **{game.high:,}**"


class GuessTheNumber(commands.Cog):
    """Admin-started number guessing game."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.games: dict[int, GtnGame] = {}

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        await handle_command_error(ctx, error)

    # ------------------------------------------------------------------
    # Command
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="gtn",
        usage="<lower> <upper> <prize> | hint | end",
        description="Starts a guess-the-number game, or gives a hint (administrators only).",
        extras={
            "args": {
                "lower": "The lowest possible number, or `hint` / `end`.",
                "upper": "The highest possible number.",
                "prize": "What the winner gets.",
            }
        },
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def gtn(
        self,
        ctx: commands.Context,
        lower: str,
        upper: typing.Optional[int] = None,
        *,
        prize: typing.Optional[str] = None,
    ):
        action = lower.strip().lower()
        if action == "hint":
            return await self._hint(ctx)
        if action in ("end", "cancel", "stop"):
            return await self._end(ctx)

        if upper is None or not prize:
            return await send_usage(ctx)

        try:
            low = int(lower)
        except ValueError:
            return await ctx.reply(embed=err_embed("The lower bound must be a whole number."), mention_author=False)

        if low >= upper:
            return await ctx.reply(embed=err_embed("The lower bound must be smaller than the upper bound."), mention_author=False)
        if upper - low < 2:
            return await ctx.reply(embed=err_embed("Leave a gap of at least 3 possible numbers."), mention_author=False)
        if abs(low) > MAX_ABS_NUMBER or abs(upper) > MAX_ABS_NUMBER:
            return await ctx.reply(embed=err_embed(f"Numbers can't go beyond ±{MAX_ABS_NUMBER:,}."), mention_author=False)

        prize = prize.strip()
        if len(prize) > MAX_PRIZE_LENGTH:
            return await ctx.reply(embed=err_embed(f"The prize can be at most {MAX_PRIZE_LENGTH} characters."), mention_author=False)

        if ctx.channel.id in self.games:
            return await ctx.reply(
                embed=err_embed(f"A game is already running here. Use `{ctx.clean_prefix}gtn end` to cancel it."),
                mention_author=False,
            )

        perms = ctx.channel.permissions_for(ctx.guild.me)
        if not (perms.send_messages and perms.embed_links and perms.add_reactions):
            return await ctx.reply(
                embed=err_embed("I need Send Messages, Embed Links and Add Reactions here."), mention_author=False
            )

        secret = random.randint(low, upper)
        game = GtnGame(secret=secret, lower=low, upper=upper, prize=prize, host_id=ctx.author.id)

        dm = ok_embed(
            "Your secret number",
            f"The number is **{secret:,}**.\n"
            f"Range: {low:,} to {upper:,}\nPrize: **{prize}**\nChannel: {ctx.channel.mention}",
            emoji="🔢",
        )
        try:
            await ctx.author.send(embed=dm)
        except discord.HTTPException:
            return await ctx.reply(
                embed=err_embed("I couldn't DM you the secret number. Open your DMs and try again."),
                mention_author=False,
            )

        self.games[ctx.channel.id] = game

        embed = discord.Embed(
            title="🔢 Guess The Number",
            description=(
                f"I'm thinking of a number between **{low:,}** and **{upper:,}**.\n"
                "Type your guess in this channel!\n\n"
                f"**Prize:** {prize}\n"
                f"**Hosted by:** {ctx.author.mention}"
            ),
            color=BRAND_COLOR,
        )
        await ctx.send(embed=embed, allowed_mentions=_NO_PINGS)

    async def _hint(self, ctx: commands.Context):
        game = self.games.get(ctx.channel.id)
        if game is None:
            return await ctx.reply(embed=err_embed("There's no game running in this channel."), mention_author=False)

        remaining = HINT_COOLDOWN - (time.time() - game.last_hint)
        if remaining > 0:
            return await ctx.reply(
                embed=err_embed(f"Hints have a cooldown. Try again in {int(remaining) + 1}s."),
                mention_author=False,
            )

        span = game.high - game.low
        if span <= 2:
            return await ctx.reply(embed=err_embed("The range can't be narrowed any further."), mention_author=False)

        width = min(span - 1, max(2, int(span * (1 - HINT_SHRINK))))
        window_low = max(game.low, game.secret - width)
        window_high = min(game.secret, game.high - width)
        new_low = random.randint(window_low, window_high)
        game.low, game.high = new_low, new_low + width
        game.hints += 1
        game.last_hint = time.time()

        embed = discord.Embed(
            title="💡 Hint",
            description=f"The number is between {_bounds_text(game)}.",
            color=BRAND_COLOR,
        )
        await ctx.send(embed=embed)

    async def _end(self, ctx: commands.Context):
        game = self.games.pop(ctx.channel.id, None)
        if game is None:
            return await ctx.reply(embed=err_embed("There's no game running in this channel."), mention_author=False)
        await ctx.reply(
            embed=ok_embed("Game Cancelled", f"The number was **{game.secret:,}**.", emoji="🛑"),
            mention_author=False,
        )

    # ------------------------------------------------------------------
    # Guessing
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        game = self.games.get(message.channel.id)
        if game is None or message.author.id == game.host_id:
            return

        content = message.content.strip().replace(",", "")
        if not _GUESS.match(content):
            return

        guess = int(content)
        game.guesses += 1
        if guess != game.secret:
            return

        # Remove first so two people typing the answer at the same moment can't both win.
        if self.games.pop(message.channel.id, None) is not game:
            return

        try:
            await message.add_reaction(TICKET_EMOJI)
        except discord.HTTPException:
            pass

        embed = discord.Embed(
            title="🎉 We have a winner!",
            description=(
                f"Congratulations {message.author.mention}! You guessed the number "
                f"**{game.secret:,}** after **{game.guesses:,}** guess{'es' if game.guesses != 1 else ''}.\n\n"
                f"**Prize:** {game.prize}"
            ),
            color=discord.Color.green(),
        )
        try:
            await message.channel.send(
                content=message.author.mention,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(users=[message.author]),
            )
        except discord.HTTPException as exc:
            print(f"GTN Cog: could not announce the winner in {message.channel.id}: {exc}")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        self.games.pop(channel.id, None)


async def setup(bot: commands.Bot):
    await bot.add_cog(GuessTheNumber(bot))