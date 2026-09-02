import asyncio
import certifi
import discord
from discord.ext import commands
import motor.motor_asyncio

from views.spawnsconfigview import SpawnsConfigView

MONGO_URI = "mongodb+srv://KurtisLam:CsHLOnDqihiU5uYG@cluster0.7rwx3oc.mongodb.net/?appName=Cluster0"
POKETWO_ID = 716390085896962058
SENSOR_IDS = {874910942490677270, 854233015475109888, 1250429544486273038}


async def get_poketwo_target(guild: discord.Guild):
    """Retrieve Poketwo Member object via cache or fetch."""
    return guild.get_member(POKETWO_ID) or await guild.fetch_member(POKETWO_ID)


class UnlockView(discord.ui.View):
    def __init__(self, cog=None):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Unlock", style=discord.ButtonStyle.green, emoji="🔓")
    async def unlock(self, interaction: discord.Interaction, button: discord.ui.Button):
        target = await get_poketwo_target(interaction.guild)
        await interaction.channel.set_permissions(target, view_channel=True, send_messages=True)

        button.disabled = True
        button.label = "Unlocked"
        button.style = discord.ButtonStyle.secondary

        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            f"🔓 **{interaction.channel.mention}** was unlocked by {interaction.user.mention}."
        )

        if self.cog:
            self.cog.active_locks.pop(interaction.channel.id, None)


class SpawnsConfig(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_locks = {}
        self.pending_locks = set()
        self.mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI, tlsCAFile=certifi.where())
        self.db = self.mongo_client["utilities"]
        self.collection = self.db["config"]

    async def cog_load(self):
        try:
            await self.mongo_client.admin.command('ping')
        except Exception as e:
            print(f"SpawnsConfig Cog: MongoDB warmup failed: {e}")

    async def _get_guild_data(self, guild_id: str) -> dict:
        doc = await self.collection.find_one({"_id": guild_id})
        if not doc:
            doc = {"_id": guild_id, "rare": [], "regional": [], "user": []}
        return doc

    async def build_config_embed(self, guild: discord.Guild) -> discord.Embed:
        guild_config = await self._get_guild_data(str(guild.id))

        def format_mentions(var_list, is_role=True):
            if not var_list:
                return "None configured"

            mentions = []
            for item in var_list:
                tgt_id = item.get("target")

                rules = []
                if rcat := item.get("restrict_categories"):
                    cat_names = [guild.get_channel(cid).name if guild.get_channel(cid) else f"Cat {cid}" for cid in rcat]
                    rules.append("only cat: " + ", ".join(cat_names))
                if rch := item.get("restrict_channels"):
                    rules.append("only ch: " + ", ".join(f"<#{cid}>" for cid in rch))
                if xcat := item.get("exclude_categories"):
                    cat_names = [guild.get_channel(cid).name if guild.get_channel(cid) else f"Cat {cid}" for cid in xcat]
                    rules.append("excl cat: " + ", ".join(cat_names))
                if xch := item.get("exclude_channels"):
                    rules.append("excl ch: " + ", ".join(f"<#{cid}>" for cid in xch))

                rule_str = f" ({'; '.join(rules)})" if rules else " (Global)"

                if tgt_id:
                    mention = f"<@&{tgt_id}>" if is_role else f"<@{tgt_id}>"
                    mentions.append(f"{mention}{rule_str}")
                else:
                    mentions.append(f"{'; '.join(rules)}" if rules else "Global")

            return "\n".join(mentions)

        embed = discord.Embed(
            title=f"⚙️ Spawns Configuration — {guild.name}", color=discord.Color.blue()
        )
        embed.add_field(
            name="Rare Roles",
            value=format_mentions(guild_config.get("rare", []), True),
            inline=False,
        )
        embed.add_field(
            name="Regional Roles",
            value=format_mentions(guild_config.get("regional", []), True),
            inline=False,
        )
        embed.add_field(
            name="Tracked Users",
            value=format_mentions(guild_config.get("user", []), False),
            inline=False,
        )
        return embed

    async def _check_target_triggered(self, message: discord.Message, guild_config: dict) -> bool:
        channel_id = message.channel.id
        category_id = message.channel.category_id
        content = message.content

        for key in ("rare", "regional", "user"):
            for entry in guild_config.get(key, []):
                target_id = entry.get("target")

                if (r_ch := entry.get("restrict_channels")) and channel_id not in r_ch:
                    continue
                if (r_cat := entry.get("restrict_categories")) and category_id not in r_cat:
                    continue
                if (x_ch := entry.get("exclude_channels")) and channel_id in x_ch:
                    continue
                if (x_cat := entry.get("exclude_categories")) and category_id in x_cat:
                    continue

                if target_id:
                    if self._is_target_mentioned(content, target_id):
                        return True
                else:
                    return True
        return False

    @commands.hybrid_command(
        name="spawnsconfig",
        aliases=["sc"],
        description="Display and edit server autolock configurations for rares, regionals, and users.",
    )
    @commands.has_permissions(administrator=True)
    async def spawnsconfig(self, ctx: commands.Context):
        embed = await self.build_config_embed(ctx.guild)
        view = SpawnsConfigView(self, author_id=ctx.author.id)
        await ctx.send(embed=embed, view=view)

    @spawnsconfig.error
    async def spawnsconfig_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ You need **Administrator** permissions to use this command.")
        else:
            await ctx.send(f"⚠️ Internal error: `{error}`")

    def _is_target_mentioned(self, content: str, target_id: int) -> bool:
        tid = str(target_id)
        return f"<@{tid}>" in content or f"<@&{tid}>" in content or f"<@!{tid}>" in content

    async def _check_target_triggered(self, message: discord.Message, guild_config: dict) -> bool:
        channel_id = message.channel.id
        category_id = message.channel.category_id
        content = message.content

        for key in ("rare", "regional", "user"):
            for entry in guild_config.get(key, []):
                target_id = entry.get("target")
                if not target_id:
                    continue

                if (r_ch := entry.get("restrict_channels")) and channel_id not in r_ch:
                    continue
                if (r_cat := entry.get("restrict_categories")) and category_id not in r_cat:
                    continue
                if (x_ch := entry.get("exclude_channels")) and channel_id in x_ch:
                    continue
                if (x_cat := entry.get("exclude_categories")) and category_id in x_cat:
                    continue

                if self._is_target_mentioned(content, target_id):
                    return True
        return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild:
            return

        content = message.content.strip()

        if content and content.split(maxsplit=1)[0].lower() in (".u", ".unlock"):
            if message.channel.id in self.active_locks:
                lock_msg = self.active_locks.pop(message.channel.id, None)
                if lock_msg:
                    try:
                        view = UnlockView(cog=self)
                        view.children[0].disabled = True
                        view.children[0].label = "Unlocked"
                        view.children[0].style = discord.ButtonStyle.secondary
                        await lock_msg.edit(view=view)
                    except discord.HTTPException:
                        pass
            return

        if message.author.id not in SENSOR_IDS or message.channel.id in self.pending_locks:
            return

        guild_config = await self._get_guild_data(str(message.guild.id))
        if not guild_config or not await self._check_target_triggered(message, guild_config):
            return

        self.pending_locks.add(message.channel.id)
        status_msg = await message.channel.send("⏳ **Auto-Lock Triggered:** Locking in 15 seconds...")

        def poketwo_check(m: discord.Message) -> bool:
            return (
                m.author.id == POKETWO_ID
                and m.channel.id == message.channel.id
                and m.content.startswith("Congratulations")
            )

        try:
            await self.bot.wait_for("message", check=poketwo_check, timeout=15.0)
            try:
                await status_msg.delete()
            except discord.HTTPException:
                pass
        except asyncio.TimeoutError:
            try:
                await status_msg.delete()
            except discord.HTTPException:
                pass

            target = await get_poketwo_target(message.guild)
            await message.channel.set_permissions(target, view_channel=False, send_messages=False)

            lock_embed = discord.Embed(
                title="🔒 Channel Locked",
                description="Use `.u` or the button to unlock!",
                color=discord.Color.red(),
            )
            lock_msg = await message.channel.send(embed=lock_embed, view=UnlockView(cog=self))
            self.active_locks[message.channel.id] = lock_msg
        finally:
            self.pending_locks.remove(message.channel.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(SpawnsConfig(bot))