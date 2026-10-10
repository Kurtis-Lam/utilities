from discord.ext import commands

from views.afk_views import AFKView
from views.common_views import error_embed
from views.embeds import handle_command_error


class AFK(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        await handle_command_error(ctx, error)

    @property
    def mongo_client(self):
        return self.bot.mongo_client

    @property
    def db(self):
        return self.mongo_client["utilities"]

    @property
    def afk_collection(self):
        return self.db["afk"]

    @property
    def afk_settings(self):
        return self.db["afk_settings"]

    @commands.hybrid_command(name="afk", aliases=["setafk"], description="Toggle AFK status and configure ping settings.")
    async def afk(self, ctx: commands.Context):
        if not ctx.guild:
            return await ctx.send(embed=error_embed("Server only."))

        doc = await self.afk_collection.find_one({"_id": ctx.author.id})
        is_afk = bool(doc and doc.get("afk"))

        doc_id = f"{ctx.guild.id}_{ctx.author.id}"
        settings_doc = await self.afk_settings.find_one({"_id": doc_id})
        current_settings = settings_doc.get("allowed_pings", {}) if settings_doc else {}

        view = AFKView(self, ctx.author.id, is_afk, current_settings)
        # Components V2: send only the view (no content/embed)
        await ctx.send(view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(AFK(bot))