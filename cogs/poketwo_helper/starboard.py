import io
import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands

from views.common_views import error_embed, success_embed
from views.embeds import BRAND_COLOR, handle_command_error

log = logging.getLogger(__name__)

POKETWO_ID = 716390085896962058
CATCH_PATTERN = re.compile(
    r"Congratulations <@!?(\d+)>! You caught a Level (\d+) (.+?) "
    r"\((\d+(?:\.\d+)?)%\)!?"
)
CUSTOM_EMOJI_PATTERN = re.compile(r"<a?:[^:>]+:\d+>")
SHINY_PATTERN = re.compile(r"These colors seem unusual", re.IGNORECASE)
GMAX_PATTERN = re.compile(r"Gigantamax Factor", re.IGNORECASE)
CHAIN_PATTERN = re.compile(r"Shiny streak reset\.\s*\(\*{0,2}(\d+)\*{0,2}\)", re.IGNORECASE)

# Detection kinds listed in priority order (also the order emojis/labels appear in).
DETECTION_PRIORITY = ["shiny", "gmax", "high_iv", "low_iv", "rare", "regional"]
DETECTION_STYLES = {
    "shiny": ("✨", "Shiny"),
    "gmax": ("💥", "Gigantamax"),
    "high_iv": ("📈", "High IV"),
    "low_iv": ("📉", "Low IV"),
    "rare": ("💎", "Rare"),
    "regional": ("🌍", "Regional"),
}
DETECTION_COLORS = {
    "shiny": 0xF1C40F,  # Yellow
    "gmax": 0xE74C3C,  # Red
    "high_iv": 0x1ABC9C,  # Turquoise
    "low_iv": 0x1ABC9C,  # Turquoise
}


def normalize_pokemon_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def normalize_key(name: str) -> str:
    """Same normalization dex.py uses for pokeimgs lookups (é -> e, strip symbols, lowercase)."""
    if not name:
        return ""
    nfd = unicodedata.normalize("NFD", name)
    without_accents = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    cleaned = re.sub(r"[^\w\s-]", "", without_accents)
    return " ".join(cleaned.split()).lower()


def image_id(name: str) -> str:
    """
    Canonical pokeimgs id / filename stem: lowercase, accents -> plain letters (é -> e),
    Nidoran symbols -> -m / -f. Same rule is used by rename_shinies.py and mongo_sync.py.
    """
    if not name:
        return ""
    text = unicodedata.normalize("NFC", name).replace("♂", "-m").replace("♀", "-f")
    nfd = unicodedata.normalize("NFD", text)
    stripped = "".join(c for c in nfd if unicodedata.category(c) != "Mn")  # also drops U+FE0F
    return " ".join(unicodedata.normalize("NFC", stripped).lower().split())


def image_ids(name: str) -> list:
    """Ordered, de-duplicated candidate pokeimgs ids for a Pokémon name (best match first)."""
    variants = [name, name.replace("’", "'"), name.replace("’", "").replace("'", "")]
    out = []
    for variant in variants:
        for key in (image_id(variant), variant.lower()):
            if key and key not in out:
                out.append(key)
    legacy = normalize_key(name)
    if legacy and legacy not in out and "♂" not in name and "♀" not in name:
        out.append(legacy)
    return out


def find_data_dir() -> Optional[Path]:
    """Locate data/pokes regardless of where the cog file lives."""
    here = Path(__file__).resolve()
    candidates = [parent / "data" / "pokes" for parent in here.parents]
    candidates.append(Path.cwd() / "data" / "pokes")
    for candidate in candidates:
        if (candidate / "rare.json").is_file() or (candidate / "regional.json").is_file():
            return candidate
    return None


def load_pokemon_names(data_dir: Optional[Path], filename: str) -> frozenset[str]:
    if data_dir is None or not (data_dir / filename).is_file():
        log.error("Starboard: %s not found (data dir: %s)", filename, data_dir)
        return frozenset()
    with (data_dir / filename).open(encoding="utf-8") as file:
        names = frozenset(normalize_pokemon_name(name) for name in json.load(file))
    log.info("Starboard: loaded %d names from %s", len(names), filename)
    return names


def parse_catch(content: str):
    """Parses a Poketwo catch message into a dict, or None if it isn't one."""
    match = CATCH_PATTERN.search(content)
    if not match:
        return None
    chain = CHAIN_PATTERN.search(content)
    return {
        "user_id": int(match.group(1)),
        "level": int(match.group(2)),
        "pokemon": CUSTOM_EMOJI_PATTERN.sub("", match.group(3)).strip(),
        "iv": float(match.group(4)),
        "shiny": bool(SHINY_PATTERN.search(content)),
        "gmax": bool(GMAX_PATTERN.search(content)),
        "chain": int(chain.group(1)) if chain else None,
    }


def build_title(detections: list[str]) -> str:
    """e.g. '🌍📈 Regional and High IV Catch Detected 🌍📈'."""
    emojis = "".join(DETECTION_STYLES[d][0] for d in detections)
    labels = [DETECTION_STYLES[d][1] for d in detections]
    if len(labels) == 1:
        joined = labels[0]
    else:
        joined = ", ".join(labels[:-1]) + f" and {labels[-1]}"
    return f"{emojis} {joined} Catch Detected {emojis}"


def pick_color(detections: list[str]) -> int:
    """Color of the highest-priority detection (shiny > gmax > IV > rare/regional)."""
    for kind in DETECTION_PRIORITY:
        if kind in detections and kind in DETECTION_COLORS:
            return DETECTION_COLORS[kind]
    return BRAND_COLOR


def build_status_embed(channel_id: Optional[str]) -> discord.Embed:
    if channel_id:
        description = f"Starboard Channel is currently set to <#{channel_id}>"
    else:
        description = "No channel configured"
    return discord.Embed(title="Starboard", description=description, color=BRAND_COLOR)


class ChannelSelectView(discord.ui.View):
    """Ephemeral view: pick a channel from a dropdown, then confirm."""

    def __init__(self, cog: "Starboard", parent: "StarboardView"):
        super().__init__(timeout=120)
        self.cog = cog
        self.parent = parent
        self.selected: Optional[discord.abc.GuildChannel] = None

    def build_embed(self) -> discord.Embed:
        if self.selected:
            description = (
                f"Selected: <#{self.selected.id}>\n\n"
                "Press **Confirm** to set it as the starboard channel."
            )
        else:
            description = "Choose the channel notable catches should be posted in."
        return discord.Embed(
            title="Configure Starboard", description=description, color=BRAND_COLOR
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                embed=error_embed("Only administrators can set the starboard channel."),
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        placeholder="Select a channel...",
        min_values=1,
        max_values=1,
    )
    async def channel_select(
        self, interaction: discord.Interaction, select: discord.ui.ChannelSelect
    ):
        self.selected = select.values[0]
        self.confirm.disabled = False
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, disabled=True)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.guild.get_channel(self.selected.id)
        if channel is None:
            return await interaction.response.send_message(
                embed=error_embed("I can't see that channel."), ephemeral=True
            )

        perms = channel.permissions_for(interaction.guild.me)
        missing = [
            name
            for name, ok in (
                ("View Channel", perms.view_channel),
                ("Send Messages", perms.send_messages),
                ("Embed Links", perms.embed_links),
            )
            if not ok
        ]
        if missing:
            return await interaction.response.send_message(
                embed=error_embed(
                    f"I'm missing **{', '.join(missing)}** in {channel.mention}. "
                    "Fix the permissions or pick another channel."
                ),
                ephemeral=True,
            )

        await self.cog.starboard_collection.update_one(
            {"guild_id": str(interaction.guild.id)},
            {"$set": {"channel_id": str(channel.id)}},
            upsert=True,
        )
        self.stop()
        await interaction.response.edit_message(
            embed=success_embed(f"Starboard channel set to {channel.mention}."),
            view=None,
        )
        await self.parent.refresh(interaction.guild)


class StarboardView(discord.ui.View):
    def __init__(self, cog: "Starboard", ctx: commands.Context):
        super().__init__(timeout=180)
        self.cog = cog
        self.ctx = ctx
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                embed=error_embed("This menu isn't yours. Run `.starboard` yourself."),
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Configure", style=discord.ButtonStyle.primary, emoji="⚙️")
    async def configure(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                embed=error_embed("Only administrators can set the starboard channel."),
                ephemeral=True,
            )
        view = ChannelSelectView(self.cog, self)
        await interaction.response.send_message(
            embed=view.build_embed(), view=view, ephemeral=True
        )

    async def refresh(self, guild: discord.Guild):
        if self.message is None:
            return
        config = await self.cog.starboard_collection.find_one({"guild_id": str(guild.id)})
        embed = build_status_embed(config.get("channel_id") if config else None)
        try:
            await self.message.edit(embed=embed, view=self)
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class Starboard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.rare: frozenset[str] = frozenset()
        self.regional: frozenset[str] = frozenset()

    @property
    def starboard_collection(self):
        return self.bot.mongo_client["utilities"]["starboard_channels"]

    @property
    def img_collection(self):
        return self.bot.mongo_client["utilities"]["pokeimgs"]

    async def cog_load(self):
        data_dir = find_data_dir()
        self.rare = load_pokemon_names(data_dir, "rare.json")
        self.regional = load_pokemon_names(data_dir, "regional.json")

    async def cog_command_error(self, ctx, error):
        await handle_command_error(ctx, error)

    async def _get_channel(self, guild_id: int) -> Optional[discord.abc.Messageable]:
        config = await self.starboard_collection.find_one({"guild_id": str(guild_id)})
        if not config or not config.get("channel_id"):
            return None
        channel_id = int(config["channel_id"])
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException as error:
                log.warning("Starboard: can't fetch channel %s: %s", channel_id, error)
                return None
        return channel

    async def _get_shiny_image(self, names: list[str]) -> Optional[discord.File]:
        """Loads the shiny sprite from pokeimgs, stored under ids like "shiny pikachu"."""
        keys = []
        for name in names:
            for key in image_ids(name):
                if f"shiny {key}" not in keys:
                    keys.append(f"shiny {key}")
        try:
            for key in keys:
                doc = await self.img_collection.find_one({"_id": key})
                if doc and "image" in doc:
                    filename = "shiny_" + re.sub(r"\W+", "_", normalize_key(names[-1])) + ".png"
                    return discord.File(fp=io.BytesIO(bytes(doc["image"])), filename=filename)
        except Exception:
            log.exception("Starboard: shiny image lookup failed for %s", names)
        log.warning("Starboard: no shiny image for %s", names)
        return None

    async def _get_pokemon_image(
        self, pokemon: str, shiny: bool = False
    ) -> Optional[discord.File]:
        """Fetches the Pokémon's image (shiny file if shiny, else pokeimgs like dex.py)."""
        names = [pokemon]
        dex = self.bot.get_cog("Dex")  # optional: resolves alt names via the Dex cog
        alt_index = getattr(dex, "alt_index", None) or {}
        for key in (pokemon.casefold(), normalize_key(pokemon)):
            target = alt_index.get(key)
            if target and target not in names:
                names.append(target)

        if shiny:
            shiny_file = await self._get_shiny_image(names)
            if shiny_file:
                return shiny_file
            # fall through to the normal image if no shiny image exists

        lookup_keys = []
        for name in names:
            for key in image_ids(name):
                if key not in lookup_keys:
                    lookup_keys.append(key)

        try:
            for key in lookup_keys:
                doc = await self.img_collection.find_one({"_id": key})
                if doc and "image" in doc:
                    filename = re.sub(r"\W+", "_", normalize_key(names[-1])) + ".png"
                    return discord.File(fp=io.BytesIO(doc["image"]), filename=filename)
        except Exception:
            log.exception("Starboard: image lookup failed for %s", pokemon)
        return None

    def detect(self, catch: dict) -> list[str]:
        """Returns the detection kinds that apply, in priority order."""
        name = normalize_pokemon_name(catch["pokemon"])
        iv = catch["iv"]
        found = set()
        if catch["shiny"]:
            found.add("shiny")
        if catch["gmax"]:
            found.add("gmax")
        if iv > 90:
            found.add("high_iv")
        elif iv < 10:
            found.add("low_iv")
        if name in self.rare:
            found.add("rare")
        if name in self.regional:
            found.add("regional")
        return [kind for kind in DETECTION_PRIORITY if kind in found]

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.id != POKETWO_ID:
            return
        catch = parse_catch(message.content)
        if not catch:
            return

        detections = self.detect(catch)
        if not detections:
            return
        pokemon = catch["pokemon"]

        try:
            channel = await self._get_channel(message.guild.id)
            if channel is None:
                return

            lines = [
                f"**User:** <@{catch['user_id']}>",
                f"**Pokémon:** {pokemon}",
                f"**Level:** {catch['level']}",
                f"**IV:** {catch['iv']:g}%",
            ]
            if catch["chain"] is not None:
                lines.append(f"**Chain:** {catch['chain']}")
            embed = discord.Embed(
                title=build_title(detections),
                description=(
                    "\n".join(lines)
                    + f"\n\nCaught <t:{int(message.created_at.timestamp())}:f>"
                ),
                color=pick_color(detections),
            )

            file = await self._get_pokemon_image(pokemon, shiny=catch["shiny"])
            if file:
                embed.set_thumbnail(url=f"attachment://{file.filename}")
            elif message.embeds:
                catch_embed = message.embeds[0]
                image_url = catch_embed.thumbnail.url or catch_embed.image.url
                if image_url:
                    embed.set_thumbnail(url=image_url)

            jump_view = discord.ui.View(timeout=None)
            jump_view.add_item(
                discord.ui.Button(label="Jump to Message", url=message.jump_url)
            )
            send_kwargs = {"embed": embed, "view": jump_view}
            if file:
                send_kwargs["file"] = file
            await channel.send(**send_kwargs)
        except Exception:
            log.exception("Starboard: failed to post catch of %s", pokemon)

    @commands.hybrid_group(
        name="starboard",
        invoke_without_command=True,
        description="View or configure the notable-catch starboard channel.",
    )
    @commands.guild_only()
    async def starboard(self, ctx: commands.Context):
        config = await self.starboard_collection.find_one({"guild_id": str(ctx.guild.id)})
        embed = build_status_embed(config.get("channel_id") if config else None)
        view = StarboardView(self, ctx)
        view.message = await ctx.send(embed=embed, view=view)

    @starboard.command(
        name="reset",
        description="Remove the configured notable-catch starboard channel.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def starboard_reset(self, ctx: commands.Context):
        result = await self.starboard_collection.delete_one({"guild_id": str(ctx.guild.id)})
        message = (
            "Starboard channel configuration removed."
            if result.deleted_count
            else "No starboard channel is configured."
        )
        await ctx.send(embed=success_embed(message))


async def setup(bot):
    await bot.add_cog(Starboard(bot))