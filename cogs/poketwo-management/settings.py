import discord
from discord.ext import commands

from views.autolockview import ALL_CATEGORIES, CATEGORY_LABELS
from views.embeds import err_embed, handle_command_error


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
            await ctx.send(embed=err_embed("AutoLockConfig is not loaded.", emoji="⚠️"))
        return cog

    # Server-wide settings are already covered by `.c a` (AutoLockConfig's own
    # interactive menu), so this cog only handles the one thing that menu
    # doesn't show at a glance: this specific channel's effective settings.
    @commands.command(
        name="channelsettings",
        aliases=["chsettings"],
        description="This channel's autolock settings.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def channel_settings(self, ctx: commands.Context):
        """This channel's autolock settings."""
        cog = await self._get_config_cog(ctx)
        if not cog:
            return

        embed = discord.Embed(
            title=f"⚙️ #{ctx.channel.name}",
            color=discord.Color.green(),
        )

        for cat in ALL_CATEGORIES:
            cfg = await cog.get_category_config_channel(ctx.guild.id, ctx.channel.id, cat)

            enabled = "✅ Enabled" if cfg.get("enabled", False) else "❌ Disabled"

            delay_enabled = cfg.get("delay_enabled", True)
            delay_val = cfg.get("delay", 15)
            delay_str = f"{delay_val}s" if delay_enabled else "Off"

            restricted = "✅ Yes" if cfg.get("restrict_unlockers", False) else "❌ No"

            field_value = f"{enabled}\n⏱️ {delay_str}\n🔐 Restrict: {restricted}"

            embed.add_field(
                name=_label(cat),
                value=field_value,
                inline=True,
            )

        embed.set_footer(text="Edit with .set / .toggle")
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Settings(bot))