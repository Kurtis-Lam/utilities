import discord
from discord.ext import commands

from cogs.poketwo_helper.lockcommon import DEFAULT_DELAY, DEFAULT_LOCKTIME, format_duration
from views.autolock_views import ALL_CATEGORIES, CATEGORY_LABELS
from views.embeds import commands_usage_embed, err_embed, handle_command_error


def _label(category: str) -> str:
    return CATEGORY_LABELS.get(category, category.capitalize())


class Settings(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_command_error(self, ctx: commands.Context, error):
        await handle_command_error(ctx, error)

    async def _get_config_cog(self, ctx: commands.Context):
        cog = self.bot.get_cog("AutoLockConfig")
        if not cog:
            await ctx.reply(embed=err_embed("AutoLockConfig is not loaded.", emoji="⚠️"), mention_author=False)
        return cog

    @staticmethod
    def _summary_field(cfg: dict) -> str:
        lock_str = "Lock: ✅" if cfg.get("enabled", False) else "Lock: ❌"
        delay_str = f"{cfg.get('delay', DEFAULT_DELAY)}s" if cfg.get("delay_enabled", True) else "Off"
        restricted = "Restrict: ✅" if cfg.get("restrict_unlockers", False) else "Restrict: ❌"
        unlock_str = (
            f"Unlock: {format_duration(cfg.get('locktime', DEFAULT_LOCKTIME))}"
            if cfg.get("locktime_enabled", False)
            else "Unlock: Off"
        )
        return f"{lock_str}\n{delay_str}\n{unlock_str}\n{restricted}"

    # --- .settings -----------------------------------------------------------
    # Just lists the two real commands (plain usage embed, no Example button).
    @commands.command(
        name="settings",
        description="Show the autolock settings commands.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def settings(self, ctx: commands.Context):
        await ctx.reply(embed=commands_usage_embed(ctx, "channelsettings", "serversettings"), mention_author=False)

    # --- .channelsettings ----------------------------------------------------
    @commands.command(
        name="channelsettings",
        aliases=["chsettings"],
        description="This channel's autolock settings.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def channel_settings(self, ctx: commands.Context):
        """This channel's autolock settings (server defaults + this channel's overrides)."""
        cog = await self._get_config_cog(ctx)
        if not cog:
            return

        embed = discord.Embed(
            title=f"⚙️ #{ctx.channel.name}",
            color=discord.Color.green(),
        )
        naming_enabled = await cog.get_naming_enabled(ctx.guild.id, ctx.channel.id)
        embed.add_field(
            name="Pokémon Naming",
            value="✅ Enabled" if naming_enabled else "❌ Disabled",
            inline=False,
        )

        for cat in ALL_CATEGORIES:
            cfg = await cog.get_category_config_channel(ctx.guild.id, ctx.channel.id, cat)
            embed.add_field(name=_label(cat), value=self._summary_field(cfg), inline=True)

        embed.set_footer(text="Edit with .set / .toggle")
        await ctx.reply(embed=embed, mention_author=False)

    # --- .serversettings -----------------------------------------------------
    @commands.command(
        name="serversettings",
        aliases=["svsettings", "ssettings"],
        description="This server's autolock settings.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def server_settings(self, ctx: commands.Context):
        """Server-wide autolock defaults (what applies where a channel has no override)."""
        cog = await self._get_config_cog(ctx)
        if not cog:
            return

        embed = discord.Embed(
            title=f"⚙️ {ctx.guild.name}",
            color=discord.Color.green(),
        )
        naming_enabled = await cog.get_naming_enabled(ctx.guild.id)
        embed.add_field(
            name="Pokémon Naming",
            value="✅ Enabled" if naming_enabled else "❌ Disabled",
            inline=False,
        )

        for cat in ALL_CATEGORIES:
            cfg = await cog.get_category_config(ctx.guild.id, cat)
            wl_count = len(cfg.get("whitelist", []))
            value = self._summary_field(cfg) + f"\nWhitelist: {wl_count}"
            embed.add_field(name=_label(cat), value=value, inline=True)

        std = await cog.get_standard(ctx.guild.id)
        embed.add_field(name="⭐ Standard", value=cog._standard_summary(std), inline=False)
        embed.set_footer(text="Edit with .c a or .set / .toggle --global")
        await ctx.reply(embed=embed, mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(Settings(bot))