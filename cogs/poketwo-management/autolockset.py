import discord
from discord.ext import commands

MIN_DELAY = 1
MAX_DELAY = 600

NO_PINGS = discord.AllowedMentions.none()

USAGE = (
    "⚠️ **Usage**\n"
    "`.set lockdelay <seconds> <lock> [<lock> ...]` — e.g. `.set lockdelay 15 sh cl` (or `all`)\n"
    "`.set rarerole @role` (`rarole`)\n"
    "`.set regionalrole @role` (`regrole`)\n"
    "`.set gigantamaxrole @role` (`gmaxrole`)\n"
    "`.set paradoxrole @role` (`pararole`)\n"
    "`.set eeveelutionsrole @role` (`eevosroles`)"
)


class AutoLockSet(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.group(name="set", invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_group(self, ctx: commands.Context):
        await ctx.send(USAGE)

    # --- .set lockdelay <seconds> <lock...> ------------------------------------

    @set_group.command(name="lockdelay", aliases=["ld", "delay", "lock-delay"])
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_lockdelay(self, ctx: commands.Context, seconds: str = None, *locks: str):
        if seconds is None or not locks:
            return await ctx.send(
                "⚠️ Usage: `.set lockdelay <seconds> <lock> [<lock> ...]` — e.g. `.set lockdelay 15 sh cl` "
                "(use `all` for every lock)"
            )

        try:
            delay = int(seconds.lower().rstrip("s"))
        except ValueError:
            return await ctx.send(f"⚠️ `{seconds}` isn't a valid number of seconds.")

        if not (MIN_DELAY <= delay <= MAX_DELAY):
            return await ctx.send(
                f"⚠️ Delay must be between **{MIN_DELAY}** and **{MAX_DELAY}** seconds. "
                "To lock instantly, turn the delay off with `.toggle lockdelay <lock>`."
            )

        cog = self.bot.get_cog("AutoLockConfig")
        if not cog:
            return await ctx.send("⚠️ Internal error: `AutoLockConfig` cog is not loaded.")

        if any(l.lower() == "all" for l in locks):
            cats = list(cog.all_categories())
            unknown = []
        else:
            cats, unknown = [], []
            for tok in locks:
                cat = cog.resolve_category(tok)
                if cat is None:
                    unknown.append(tok)
                elif cat not in cats:
                    cats.append(cat)

        if not cats:
            return await ctx.send(f"⚠️ Unknown lock(s): {', '.join(f'`{u}`' for u in unknown)}")

        cfg_doc = await cog.get_guild_config(ctx.guild.id)
        lines = []
        for cat in cats:
            await cog.set_delay(ctx.guild.id, cat, delay)
            line = f"⏱️ **{cog.display_name(cat)}** delay set to **{delay}s**."
            if not (cfg_doc.get(cat) or {}).get("delay_enabled", True):
                line += f" (Delay is currently **off**, so it still locks immediately. Turn it on with `.toggle lockdelay {cat}`.)"
            lines.append(line)

        if unknown:
            lines.append(f"⚠️ Unknown lock(s): {', '.join(f'`{u}`' for u in unknown)}")

        await ctx.send("\n".join(lines))

    # --- role setters ----------------------------------------------------------
    # Roles live in the PokePings data (utilities.pings -> roles.<key>), which is
    # what the Recognizer reads when it pings, so we write through PokePings.

    async def _set_role(self, ctx: commands.Context, key: str, label: str, role: discord.Role | None):
        pings = self.bot.get_cog("PokePings")
        if not pings:
            return await ctx.send("⚠️ Internal error: `PokePings` cog is not loaded.")

        g_id = str(ctx.guild.id)
        old_id = await pings.get_guild_role(g_id, key)

        if role is None:
            if old_id:
                return await ctx.send(
                    f"Current **{label}** role: <@&{old_id}>\n-# Set a new one with the command + a role mention.",
                    allowed_mentions=NO_PINGS,
                )
            return await ctx.send(f"No role configured for **{label}**.", allowed_mentions=NO_PINGS)

        if old_id and str(old_id) == str(role.id):
            return await ctx.send(f"**{label}** role is already {role.mention}.", allowed_mentions=NO_PINGS)

        await pings.set_guild_role(g_id, key, str(role.id))

        if old_id:
            await ctx.send(
                f"✅ **{label}** role replaced: <@&{old_id}> → {role.mention}",
                allowed_mentions=NO_PINGS,
            )
        else:
            await ctx.send(f"✅ **{label}** role set to {role.mention}", allowed_mentions=NO_PINGS)

    @set_group.command(name="rarerole", aliases=["rarole"])
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_rarerole(self, ctx: commands.Context, role: discord.Role = None):
        await self._set_role(ctx, "rare", "Rare", role)

    @set_group.command(name="regionalrole", aliases=["regrole"])
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_regionalrole(self, ctx: commands.Context, role: discord.Role = None):
        await self._set_role(ctx, "regional", "Regional", role)

    @set_group.command(name="gigantamaxrole", aliases=["gmaxrole"])
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_gigantamaxrole(self, ctx: commands.Context, role: discord.Role = None):
        await self._set_role(ctx, "gmax", "Gigantamax", role)

    @set_group.command(name="paradoxrole", aliases=["pararole"])
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_paradoxrole(self, ctx: commands.Context, role: discord.Role = None):
        await self._set_role(ctx, "paradox", "Paradox", role)

    @set_group.command(name="eeveelutionsrole", aliases=["eevosroles", "eevosrole"])
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_eeveelutionsrole(self, ctx: commands.Context, role: discord.Role = None):
        await self._set_role(ctx, "eevos", "Eeveelutions", role)


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoLockSet(bot))