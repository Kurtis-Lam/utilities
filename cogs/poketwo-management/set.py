import discord
from discord.ext import commands


class Set(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # --- group command ---------------------------------------------------------
    @commands.group(name="set", invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def set_group(self, ctx: commands.Context):
        help_msg = (
            "⚙️ **Set Commands Help & Examples**\n\n"
            "**⏱️ Lock Delay**\n"
            "Set how many seconds to wait before auto-locking a category. "
            "Applies to the current channel by default — add `--global` to apply it to the whole server.\n"
            "• **One lock:** `.set lockdelay 15 sh`\n"
            "• **Multiple locks:** `.set lockdelay 15 sh cl tp`\n"
            "• **All locks at once:** `.set lockdelay 15 all`\n"
            "• **Whole server:** `.set lockdelay 15 sh --global`\n\n"
            "**🏷️ Category Ping Roles**\n"
            "Assign or check the ping role for specific categories. Mention a role to set it, or leave it blank to view current settings.\n"
            "• `.set rarerole @Rare Ping` *(Alias: `.set rarole`)*\n"
            "• `.set regionalrole @Regional Ping` *(Alias: `.set regrole`)*\n"
            "• `.set gigantamaxrole @GMax Ping` *(Alias: `.set gmaxrole`)*\n"
            "• `.set paradoxrole @Paradox Ping` *(Alias: `.set pararole`)*\n"
            "• `.set eeveelutionsrole @Eevee Ping` *(Alias: `.set eevosrole`)*"
        )
        await ctx.send(help_msg)

    # --- .set lockdelay <seconds> <lock...> [--global] ------------------------------------
    @set_group.command(name="lockdelay", aliases=["ld", "delay", "lock-delay"])
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

        if seconds is None or not locks:
            usage = (
                "⚠️ **Usage:** `.set lockdelay <seconds> <lock>` [--global]\n\n"
                "**Examples:**\n"
                "• `.set lockdelay 15 sh` *(Sets Shiny Hunt delay to 15s for current channel)*\n"
                "• `.set lockdelay 15 sh --global` *(Sets Shiny Hunt delay to 15s for whole server)*\n"
                "• `.set lockdelay 15 sh cl tp` *(Sets Shiny, Collection, and Type Ping delays)*\n"
                "• `.set lockdelay 15 all` *(Sets all delays to 15s)*"
            )
            return await ctx.send(usage)

        try:
            delay = int(seconds.lower().rstrip("s"))
        except ValueError:
            return await ctx.send(f"⚠️ `{seconds}` isn't a valid number of seconds.")

        if not (1 <= delay <= 600):
            return await ctx.send(
                "⚠️ Delay must be between **1** and **600** seconds. To lock instantly, turn the delay off with `.toggle lockdelay <lock>`."
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

            line = f"⏱️ **{cog.display_name(cat)}** delay set to **{delay}s** for {scope}."
            if not global_flag:
                # Only show delay status for channel-specific setting
                cfg_doc = await cog.get_guild_config(ctx.guild.id)
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
                    allowed_mentions=discord.AllowedMentions.none()
                )
            return await ctx.send(f"No role configured for **{label}**.", allowed_mentions=discord.AllowedMentions.none())

        if old_id and str(old_id) == str(role.id):
            return await ctx.send(f"**{label}** role is already {role.mention}.", allowed_mentions=discord.AllowedMentions.none())

        await pings.set_guild_role(g_id, key, str(role.id))

        if old_id:
            await ctx.send(
                f"✅ **{label}** role replaced: <@&{old_id}> → {role.mention}",
                allowed_mentions=discord.AllowedMentions.none()
            )
        else:
            await ctx.send(f"✅ **{label}** role set to {role.mention}", allowed_mentions=discord.AllowedMentions.none())

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
    await bot.add_cog(Set(bot))