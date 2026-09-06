import discord
from discord.ext import commands
import motor.motor_asyncio
import certifi

MONGO_URI = "mongodb+srv://KurtisLam:CsHLOnDqihiU5uYG@cluster0.7rwx3oc.mongodb.net/?appName=Cluster0"

class AFKButton(discord.ui.Button):
    def __init__(self, is_afk: bool):
        super().__init__(
            label="Remove AFK" if is_afk else "Set AFK",
            style=discord.ButtonStyle.red if is_afk else discord.ButtonStyle.green,
            custom_id="toggle_afk"
        )
        self.is_afk = is_afk

    async def callback(self, interaction: discord.Interaction):
        view: AFKView = self.view
        if interaction.user.id != view.user_id:
            return await interaction.response.send_message("This button is not for you.", ephemeral=True)

        new_afk_status = not self.is_afk
        
        if new_afk_status:
            await view.cog.afk_collection.update_one(
                {"_id": interaction.user.id},
                {"$set": {"afk": True}},
                upsert=True
            )
        else:
            await view.cog.afk_collection.delete_one({"_id": interaction.user.id})
        
        embed = view.cog.make_embed(new_afk_status)
        self.is_afk = new_afk_status
        self.label = "Remove AFK" if new_afk_status else "Set AFK"
        self.style = discord.ButtonStyle.red if new_afk_status else discord.ButtonStyle.green
        
        await interaction.response.edit_message(embed=embed, view=view)

class AFKView(discord.ui.View):
    def __init__(self, cog, user_id: int, is_afk: bool):
        super().__init__(timeout=180)
        self.cog = cog
        self.user_id = user_id
        self.add_item(AFKButton(is_afk))

class AFK(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI, tlsCAFile=certifi.where())
        self.db = self.mongo_client["utilities"]
        self.afk_collection = self.db["afk"]

    def make_embed(self, is_afk: bool) -> discord.Embed:
        embed = discord.Embed(
            title="🌙 AFK Status",
            description="You are currently **AFK**.\nYou will not be pinged for SH/CL/TP/RP. Instead, you will appear as `userid (AFK)`." if is_afk else "You are currently **Active**.\nYou will receive standard pings.",
            color=discord.Color.dark_theme() if is_afk else discord.Color.green()
        )
        return embed

    @commands.hybrid_command(name="afk", description="Toggle your AFK status to avoid pings.")
    async def afk(self, ctx: commands.Context):
        doc = await self.afk_collection.find_one({"_id": ctx.author.id})
        is_afk = bool(doc and doc.get("afk"))
        
        view = AFKView(self, ctx.author.id, is_afk)
        embed = self.make_embed(is_afk)
        await ctx.send(embed=embed, view=view)
        
    async def format_ping_list(self, user_ids: set) -> list[str]:
        if not user_ids:
            return []
        
        afk_docs = await self.afk_collection.find({"_id": {"$in": list(user_ids)}}).to_list(length=None)
        afk_users = {doc["_id"] for doc in afk_docs}
        
        formatted = []
        for uid in user_ids:
            if uid in afk_users:
                formatted.append(f"{uid} (AFK)")
            else:
                formatted.append(f"<@{uid}>")
        return formatted

async def setup(bot: commands.Bot):
    await bot.add_cog(AFK(bot))