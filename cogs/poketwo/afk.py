import discord
from discord.ext import commands


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


class PingToggleButton(discord.ui.Button):
    def __init__(self, ping_key: str, label: str, is_active: bool):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.green if is_active else discord.ButtonStyle.red,
            custom_id=f"toggle_ping_{ping_key.lower()}"
        )
        self.ping_key = ping_key
        self.is_active = is_active

    async def callback(self, interaction: discord.Interaction):
        view: SetAFKView = self.view
        if interaction.user.id != view.user_id:
            return await interaction.response.send_message("This menu is not for you.", ephemeral=True)

        self.is_active = not self.is_active
        self.style = discord.ButtonStyle.green if self.is_active else discord.ButtonStyle.red

        doc_id = f"{interaction.guild_id}_{interaction.user.id}"
        await view.cog.afk_settings.update_one(
            {"_id": doc_id},
            {
                "$set": {
                    "guild_id": interaction.guild_id,
                    "user_id": interaction.user.id,
                    f"allowed_pings.{self.ping_key}": self.is_active
                }
            },
            upsert=True
        )

        embed = view.cog.make_settings_embed(view)
        await interaction.response.edit_message(embed=embed, view=view)


class SetAFKView(discord.ui.View):
    def __init__(self, cog, user_id: int, current_settings: dict):
        super().__init__(timeout=180)
        self.cog = cog
        self.user_id = user_id

        buttons = [
            ("sh", "Sh"),
            ("cl", "Cl"),
            ("rp", "Rp"),
            ("tp", "Tp")
        ]

        for key, label in buttons:
            is_active = current_settings.get(key, False)
            self.add_item(PingToggleButton(ping_key=key, label=label, is_active=is_active))


class AFK(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # Retrieves database and collection dynamically via main.py's bot.mongo_client
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

    def make_embed(self, is_afk: bool) -> discord.Embed:
        embed = discord.Embed(
            title="🌙 AFK Status",
            description="You are currently **AFK**.\nYou will not be pinged for SH/CL/TP/RP. Instead, you will appear as `userid (AFK)`." if is_afk else "You are currently **Active**.\nYou will receive standard pings.",
            color=discord.Color.dark_theme() if is_afk else discord.Color.green()
        )
        return embed

    def make_settings_embed(self, view: SetAFKView) -> discord.Embed:
        statuses = []
        for child in view.children:
            if isinstance(child, PingToggleButton):
                status_str = "🟢 Active" if child.is_active else "🔴 Ignored"
                statuses.append(f"**{child.label}**: {status_str}")

        embed = discord.Embed(
            title="⚙️ AFK Ping Exceptions",
            description=(
                "Toggle buttons below to configure ping exemptions while AFK.\n"
                "**Green** = Still receive ping when AFK.\n"
                "**Red** = Ignore ping when AFK (Default).\n\n"
                + "\n".join(statuses)
            ),
            color=discord.Color.blue()
        )
        return embed

    @commands.hybrid_command(name="afk", description="Toggle your AFK status to avoid pings.")
    async def afk(self, ctx: commands.Context):
        doc = await self.afk_collection.find_one({"_id": ctx.author.id})
        is_afk = bool(doc and doc.get("afk"))

        view = AFKView(self, ctx.author.id, is_afk)
        embed = self.make_embed(is_afk)
        await ctx.send(embed=embed, view=view)

    @commands.hybrid_command(name="setafk", description="Configure ping exceptions while AFK.")
    async def setafk(self, ctx: commands.Context):
        if not ctx.guild:
            return await ctx.send("This command can only be used in a server.")

        doc_id = f"{ctx.guild.id}_{ctx.author.id}"
        doc = await self.afk_settings.find_one({"_id": doc_id})
        current_settings = doc.get("allowed_pings", {}) if doc else {}

        view = SetAFKView(self, ctx.author.id, current_settings)
        embed = self.make_settings_embed(view)
        await ctx.send(embed=embed, view=view)

    async def format_ping_list(self, user_ids: set, guild_id: int, ping_type: str = None) -> list[str]:
        if not user_ids:
            return []

        # Check composite guild_user keys first, fallback to user_id for global AFK
        doc_ids = [f"{guild_id}_{uid}" for uid in user_ids]
        afk_docs = await self.afk_collection.find({
            "$or": [
                {"_id": {"$in": doc_ids}},
                {"_id": {"$in": list(user_ids)}}
            ]
        }).to_list(length=None)

        afk_user_ids = set()
        for doc in afk_docs:
            if doc.get("afk"):
                if isinstance(doc["_id"], int):
                    afk_user_ids.add(doc["_id"])
                else:
                    afk_user_ids.add(doc.get("user_id", int(doc["_id"].split("_")[1])))

        settings_docs = await self.afk_settings.find({"_id": {"$in": doc_ids}}).to_list(length=None)
        settings_map = {doc["user_id"]: doc.get("allowed_pings", {}) for doc in settings_docs}

        formatted = []
        for uid in user_ids:
            if uid in afk_user_ids:
                user_allowed = settings_map.get(uid, {}).get(ping_type.lower(), False) if ping_type else False
                if user_allowed:
                    formatted.append(f"<@{uid}>")
                else:
                    formatted.append(f"{uid} (AFK)")
            else:
                formatted.append(f"<@{uid}>")
        return formatted


async def setup(bot: commands.Bot):
    await bot.add_cog(AFK(bot))
