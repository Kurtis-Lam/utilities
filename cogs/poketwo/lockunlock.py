import discord
from discord.ext import commands
import motor.motor_asyncio
import certifi

MONGO_URI = "mongodb+srv://KurtisLam:CsHLOnDqihiU5uYG@cluster0.7rwx3oc.mongodb.net/?appName=Cluster0"

class LockUnlock(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.target_user_id = 716390085896962058
        self.mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI, tlsCAFile=certifi.where())
        self.db = self.mongo_client["utilities"]
        self.locks_collection = self.db["locked_channels"]

    @commands.hybrid_command(aliases=["u"], name="unlock", description="Unlocks the current channel.")
    async def unlock(self, ctx):
        # Database Permission Check
        lock_doc = await self.locks_collection.find_one({"_id": ctx.channel.id})
        if lock_doc:
            allowed = lock_doc.get("allowed_users")
            if allowed is not None and ctx.author.id not in allowed and not ctx.author.guild_permissions.administrator:
                return await ctx.reply("⚠️ Only the user(s) who triggered this lock can unlock this channel.")
            await self.locks_collection.delete_one({"_id": ctx.channel.id})

        permissions = discord.PermissionOverwrite(
            read_messages=True,
            send_messages=True
        )
        channel = ctx.channel 
        user = await self.bot.fetch_user(self.target_user_id)

        await channel.set_permissions(user, overwrite=permissions)
        
        await ctx.reply(f"🔓 **{channel.mention}** was unlocked by {ctx.author.mention}")

    @commands.hybrid_command(aliases=["l"], name="lock", description="Locks the current channel.")
    async def lock(self, ctx):
        permissions = discord.PermissionOverwrite(
            read_messages=False,
            send_messages=False
        )
        channel = ctx.channel 
        user = await self.bot.fetch_user(self.target_user_id)

        await channel.set_permissions(user, overwrite=permissions)
        
        # Save lock to MongoDB persistently
        await self.locks_collection.update_one(
            {"_id": ctx.channel.id},
            {"$set": {"allowed_users": [ctx.author.id], "guild_id": ctx.guild.id}},
            upsert=True
        )
        
        embed = discord.Embed(
            description=f"🔒 Channel locked by {ctx.author.mention}",
            color=discord.Color.red()
        )
        await ctx.reply(embed=embed)

    @commands.has_permissions(administrator=True)
    @commands.command(aliases=["uac"], description="Unlocks all channels in the server.")
    async def unlockallchannels(self, ctx):
        permissions = discord.PermissionOverwrite(
            read_messages=True,
            send_messages=True
        )
        user = await self.bot.fetch_user(self.target_user_id)
        
        msg = await ctx.send("Unlocking all channels...\nThis might take some time...")
        for channel in ctx.guild.channels:
            await channel.set_permissions(user, overwrite=permissions)
            
        await self.locks_collection.delete_many({"guild_id": ctx.guild.id})
            
        embed = discord.Embed(
            description=f"🔓 **All channels** unlocked by {ctx.author.mention}",
            color=discord.Color.green()
        )
        
        await msg.delete()
        await ctx.reply(embed=embed)

    @commands.hybrid_command(name="stats", description="Shows all locked and unlocked channels.")
    async def stats(self, ctx):
        user = ctx.guild.get_member(self.target_user_id) or await self.bot.fetch_user(self.target_user_id)

        locked = []
        unlocked = []

        # Check permissions for text channels in the guild
        for channel in ctx.guild.text_channels:
            overwrite = channel.overwrites_for(user)
            if overwrite.send_messages is False:
                locked.append(channel)
            else:
                unlocked.append(channel)

        embed = discord.Embed(
            title="📊 Channel Status Overview",
            color=discord.Color.blue()
        )

        def add_channel_fields(title, channel_list):
            if not channel_list:
                embed.add_field(name=f"{title} (0)", value="None", inline=False)
                return

            chunks = []
            current_chunk = ""

            for channel in channel_list:
                mention = f"{channel.mention} "
                # Ensure value stays well below the 1024-character field limit
                if len(current_chunk) + len(mention) > 1000:
                    chunks.append(current_chunk.strip())
                    current_chunk = mention
                else:
                    current_chunk += mention

            if current_chunk:
                chunks.append(current_chunk.strip())

            for i, chunk in enumerate(chunks, start=1):
                field_name = f"{title} ({len(channel_list)})" if len(chunks) == 1 else f"{title} ({len(channel_list)}) - Part {i}"
                embed.add_field(name=field_name, value=chunk, inline=False)

        # Add locked and unlocked fields dynamically
        add_channel_fields("🔒 Locked Channels", locked)
        add_channel_fields("🔓 Unlocked Channels", unlocked)

        await ctx.reply(embed=embed)

async def setup(bot):
    await bot.add_cog(LockUnlock(bot))