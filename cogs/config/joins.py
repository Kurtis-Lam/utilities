import discord
from discord.ext import commands
import motor.motor_asyncio 
import certifi

from views.joinsview import WelcomeConfigView, get_welcome_embed
from .base import config_group

MONGO_URI = "mongodb+srv://KurtisLam:CsHLOnDqihiU5uYG@cluster0.7rwx3oc.mongodb.net/?appName=Cluster0"

# Attached to config_group at module level without 'self'
@config_group.command(
    name="joins", 
    aliases=["j"], 
    description="Configure server welcome, greet messages, and autoroles"
)
@commands.has_permissions(manage_guild=True)
async def joinsconfig(ctx: commands.Context):
    cog = ctx.bot.get_cog("Joins")
    if not cog:
        return await ctx.send("Joins module is currently unavailable.")
        
    config = await cog.get_guild_config(ctx.guild.id)
    embed = get_welcome_embed(config)
    view = WelcomeConfigView(cog.collection, config)
    await ctx.send(embed=embed, view=view)

class Joins(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI, tlsCAFile=certifi.where())
        self.db = self.mongo_client["utilities"]
        self.collection = self.db["joins_config"] 

    async def cog_load(self):
        try:
            await self.mongo_client.admin.command('ping')
        except Exception as e:
            print(f"Joins Cog: MongoDB warmup failed: {e}")

    async def get_guild_config(self, guild_id: int) -> dict:
        config = await self.collection.find_one({"_id": guild_id})
        if not config:
            config = {
                "_id": guild_id,
                "welcome_channel": None,
                "welcome_message": "Welcome {mention} to {server}!",
                "use_embed": True,
                "greet_channels": [],
                "autoroles": []
            }
        return config

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        config = await self.get_guild_config(member.guild.id)

        # 1. Handle Welcome Message
        welcome_channel_id = config.get("welcome_channel")
        if welcome_channel_id:
            channel = member.guild.get_channel(welcome_channel_id)
            if channel:
                raw_message = config.get("welcome_message", "Welcome {mention}!")
                formatted_message = raw_message.format(
                    mention=member.mention,
                    username=member.name,
                    display_name=member.display_name,
                    server=member.guild.name,
                    membercount=member.guild.member_count
                )

                if config.get("use_embed"):
                    embed = discord.Embed(
                        description=formatted_message, 
                        color=discord.Color.green()
                    )
                    embed.set_author(name=f"{member.name} joined!", icon_url=member.display_avatar.url if member.display_avatar else None)
                    await channel.send(embed=embed)
                else:
                    await channel.send(formatted_message)

        # 2. Handle Greet / Ghost Pings
        greet_channels = config.get("greet_channels", [])
        for channel_id in greet_channels:
            greet_channel = member.guild.get_channel(channel_id)
            if greet_channel:
                try:
                    msg = await greet_channel.send(member.mention)
                    await msg.delete()
                except discord.Forbidden:
                    pass

        # 3. Handle Autoroles
        autorole_ids = config.get("autoroles", [])
        if autorole_ids:
            roles_to_add = []
            for role_id in autorole_ids:
                role = member.guild.get_role(role_id)
                if role:
                    roles_to_add.append(role)
            if roles_to_add:
                try:
                    await member.add_roles(*roles_to_add, reason="Automatic role assignment on join")
                except (discord.Forbidden, discord.HTTPException):
                    pass

async def setup(bot: commands.Bot):
    await bot.add_cog(Joins(bot))