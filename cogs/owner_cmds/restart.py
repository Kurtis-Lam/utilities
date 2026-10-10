import time

from discord.ext import commands

from cogs.owner_cmds.utils import OwnerCog, safe_edit, safe_send


class Restart(OwnerCog):
    def __init__(self, bot: commands.Bot):
        super().__init__(bot)

    @commands.command(name="restart")
    @commands.is_owner()
    async def restart(self, ctx: commands.Context):
        bot = self.bot
        msg = await safe_send(ctx, "🚀 Restarting bot...")
        saved = await bot.save_state({
            "message_id": getattr(msg, "id", None),
            "channel_id": ctx.channel.id,
            "guild_id": ctx.guild.id if ctx.guild else None,
            "old_commit": "-",
            "new_commit": "-",
            "branch": "-",
            "started_at": time.time(),
            "progress": "",
        }) if msg else False
        if not saved:
            await safe_edit(msg, "🚀 Restarting bot... (couldn't save state)")
        bot.restart_requested = True
        await bot.close()


async def setup(bot: commands.Bot):
    await bot.add_cog(Restart(bot))