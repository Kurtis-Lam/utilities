import discord
from discord.ext import commands

from .set import ALL_CATEGORIES, _label


class Settings(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _get_config_cog(self, ctx: commands.Context):
        cog = self.bot.get_cog("AutoLockConfig")
        if not cog:
            await ctx.send("⚠️ Internal error: `AutoLockConfig` cog is not loaded.")
        return cog

    @commands.command(name="serversettings", aliases=["ssettings"])
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def server_settings(self, ctx: commands.Context):
        """Displays the server-wide settings for all autolock categories."""
        cog = await self._get_config_cog(ctx)
        if not cog:
            return

        embed = discord.Embed(
            title=f"⚙️ Server Settings — {ctx.guild.name}",
            color=discord.Color.blue(),
        )

        for cat in ALL_CATEGORIES:
            cfg = await cog.get_category_config(ctx.guild.id, cat)

            enabled = "✅ Enabled" if cfg.get("enabled", False) else "❌ Disabled"
            
            delay_enabled = cfg.get("delay_enabled", True)
            delay_val = cfg.get("delay", 10)
            delay_str = f"{delay_val}s" if delay_enabled else "Off (Instant)"

            restricted = "Yes" if cfg.get("restrict_unlockers", False) else "No"

            field_value = (
                f"**Status:** {enabled}\n"
                f"**Delay:** {delay_str}\n"
                f"**Restrict Unlockers:** {restricted}"
            )

            embed.add_field(
                name=_label(cat),
                value=field_value,
                inline=True,
            )

        embed.set_footer(text="Use .set or .toggle commands to modify server-wide settings.")
        await ctx.send(embed=embed)

    @commands.command(name="channelsettings", aliases=["chsettings"])
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def channel_settings(self, ctx: commands.Context):
        """Displays settings for all autolock categories specific to the current channel."""
        cog = await self._get_config_cog(ctx)
        if not cog:
            return

        embed = discord.Embed(
            title=f"⚙️ Channel Settings — #{ctx.channel.name}",
            color=discord.Color.green(),
        )

        for cat in ALL_CATEGORIES:
            cfg = await cog.get_category_config_channel(ctx.guild.id, ctx.channel.id, cat)

            enabled = "✅ Enabled" if cfg.get("enabled", False) else "❌ Disabled"

            delay_enabled = cfg.get("delay_enabled", True)
            delay_val = cfg.get("delay", 10)
            delay_str = f"{delay_val}s" if delay_enabled else "Off (Instant)"

            restricted = "Yes" if cfg.get("restrict_unlockers", False) else "No"

            field_value = (
                f"**Status:** {enabled}\n"
                f"**Delay:** {delay_str}\n"
                f"**Restrict Unlockers:** {restricted}"
            )

            embed.add_field(
                name=_label(cat),
                value=field_value,
                inline=True,
            )

        embed.set_footer(text="Use .set or .toggle commands to modify channel settings.")
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Settings(bot))