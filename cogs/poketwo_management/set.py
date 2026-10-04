import discord
from discord.ext import commands

from views.embeds import (
    err_embed,
    handle_command_error,
    info_embed,
    ok_embed,
    send_usage,
    warn_embed,
)


class Set(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_command_error(self, ctx: commands.Context, error):
        # Missing/invalid arguments -> "how to use" embed, other errors -> embeds
        await handle_command_error(ctx, error)

    # --- group command ---------------------------------------------------------
    @commands.group(
        name="set",
        invoke_without_command=True,
        description="Configure lock delays and category ping roles.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_group(self, ctx: commands.Context):
        await send_usage(ctx)

    # --- .set lockdelay <seconds> <lock...> [--global] ------------------------------------
    @set_group.command(
        name="lockdelay",
        aliases=["ld", "delay", "lock-delay"],
        usage="<seconds> <lock...> [--global]",
        description="Set a lock's delay in seconds.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_lockdelay(self, ctx: commands.Context, seconds: str = None, *locks_and_flags: str):
        # Parse arguments to check for --global flag
        locks = []
        global_flag = False

        for arg in locks_and_flags:
            if arg.lower() == "--global":
                global_flag = True
            else:
                locks.append(arg)

        if seconds is None:
            return await send_usage(ctx, title="Missing arg: `seconds`")
        if not locks:
            return await send_usage(ctx, title="Missing arg: `lock`")

        try:
            delay = int(seconds.lower().rstrip("s"))
        except ValueError:
            return await ctx.send(embed=err_embed(f"Invalid seconds: {seconds}"))

        if not (1 <= delay <= 600):
            return await ctx.send(embed=err_embed("Delay must be 1-600s."))

        cog = self.bot.get_cog("AutoLockConfig")
        if not cog:
            return await ctx.send(embed=err_embed("AutoLockConfig is not loaded.", emoji="⚠️"))

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
            return await ctx.send(embed=err_embed(f"Unknown lock(s): {', '.join(unknown)}"))

        lines = []
        for cat in cats:
            if global_flag:
                # Apply to whole server (guild level)
                await cog.set_delay(ctx.guild.id, cat, delay)
                scope = "whole server"
            else:
                # Apply to current channel only
                await cog.set_delay_channel(ctx.guild.id, ctx.channel.id, cat, delay)
                scope = f"{ctx.channel.mention}"

            line = f"⏱️ **{cog.display_name(cat)}** → **{delay}s** ({scope})"
            if not global_flag:
                cfg_doc = await cog.get_guild_config(ctx.guild.id)
                if not (cfg_doc.get(cat) or {}).get("delay_enabled", True):
                    line += " — delay is off"
            lines.append(line)

        embed = ok_embed("Delay Updated", "\n".join(lines)[:4096])
        if unknown:
            embed.add_field(
                name="⚠️ Unknown Lock(s)",
                value=", ".join(f"`{u}`" for u in unknown)[:1024],
                inline=False,
            )
        await ctx.send(embed=embed)

    # --- role setters ----------------------------------------------------------
    # Roles live in the PokePings data (utilities.pings -> roles.<key>), which is
    # what the Recognizer reads when it pings, so we write through PokePings.

    async def _set_role(self, ctx: commands.Context, key: str, label: str, role: discord.Role | None):
        pings = self.bot.get_cog("PokePings")
        if not pings:
            return await ctx.send(embed=err_embed("PokePings is not loaded.", emoji="⚠️"))

        g_id = str(ctx.guild.id)
        old_id = await pings.get_guild_role(g_id, key)

        if role is None:
            if old_id:
                return await ctx.send(embed=info_embed(f"{label} Role", f"<@&{old_id}>", emoji="🏷️"))
            return await ctx.send(embed=warn_embed(f"{label} Role", "Not set."))

        if old_id and str(old_id) == str(role.id):
            return await ctx.send(embed=info_embed("No Change", f"Already {role.mention}."))

        await pings.set_guild_role(g_id, key, str(role.id))

        if old_id:
            await ctx.send(embed=ok_embed(
                f"{label} Role Replaced", f"<@&{old_id}> → {role.mention}"))
        else:
            await ctx.send(embed=ok_embed(f"{label} Role Set", role.mention))

    @set_group.command(
        name="rarerole", aliases=["rarole"], usage="[@role]",
        description="Set or view the Rare ping role.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_rarerole(self, ctx: commands.Context, role: discord.Role = None):
        await self._set_role(ctx, "rare", "Rare", role)

    @set_group.command(
        name="regionalrole", aliases=["regrole"], usage="[@role]",
        description="Set or view the Regional ping role.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_regionalrole(self, ctx: commands.Context, role: discord.Role = None):
        await self._set_role(ctx, "regional", "Regional", role)

    @set_group.command(
        name="gigantamaxrole", aliases=["gmaxrole"], usage="[@role]",
        description="Set or view the Gigantamax ping role.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_gigantamaxrole(self, ctx: commands.Context, role: discord.Role = None):
        await self._set_role(ctx, "gmax", "Gigantamax", role)

    @set_group.command(
        name="paradoxrole", aliases=["pararole"], usage="[@role]",
        description="Set or view the Paradox ping role.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_paradoxrole(self, ctx: commands.Context, role: discord.Role = None):
        await self._set_role(ctx, "paradox", "Paradox", role)

    @set_group.command(
        name="eeveelutionsrole", aliases=["eevosroles", "eevosrole"], usage="[@role]",
        description="Set or view the Eeveelutions ping role.",
    )
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_eeveelutionsrole(self, ctx: commands.Context, role: discord.Role = None):
        await self._set_role(ctx, "eevos", "Eeveelutions", role)


async def setup(bot: commands.Bot):
    await bot.add_cog(Set(bot))