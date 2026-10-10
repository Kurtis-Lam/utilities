import gc

from discord.ext import commands

from cogs.owner_cmds.utils import OwnerCog, safe_edit, safe_send


class Reload(OwnerCog):
    def __init__(self, bot: commands.Bot):
        super().__init__(bot)

    @commands.command(name="reload")
    @commands.is_owner()
    async def reload(self, ctx: commands.Context, cog_name: str = None):
        bot = self.bot

        if cog_name:
            target_ext = None
            for category, cogs in bot.cogs_dict.items():
                if cog_name.lower() in [c.lower() for c in cogs]:
                    target_ext = f"cogs.{category}.{cog_name.lower()}"
                    break

            if not target_ext:
                await safe_send(ctx, f"❌ Cog `{cog_name}` not found.")
                return

            msg = await safe_send(ctx, f"🔄 Reloading `{cog_name}`...")
            try:
                await bot.reload_extension(target_ext)
                gc.collect()
                await safe_edit(msg, f"✅ Reloaded `{cog_name}`.")
            except Exception as e:
                await safe_edit(msg, f"❌ Failed to reload `{cog_name}`: `{e}`")
            return

        reloaded, failed = [], []
        msg = await safe_send(ctx, "🔄 Starting reload process...")

        for category, cogs in bot.cogs_dict.items():
            for cog in cogs:
                await safe_edit(msg, f"🔄 Reloading `{cog}`...")
                ext = f"cogs.{category}.{cog.lower()}"
                try:
                    await bot.reload_extension(ext)
                    reloaded.append(cog)
                except Exception as e:
                    failed.append(f"`{cog}`: {e}")
                gc.collect()

        final_msg = f"🔄 **Reload Complete**\n✅ Successfully reloaded **{len(reloaded)}** cogs."
        if failed:
            final_msg += f"\n❌ **Failed ({len(failed)}):**\n" + "\n".join(failed)
        await safe_edit(msg, final_msg)


async def setup(bot: commands.Bot):
    await bot.add_cog(Reload(bot))