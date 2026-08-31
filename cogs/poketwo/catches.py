import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import certifi
import discord
from discord.ext import commands
import motor.motor_asyncio
# We still import these from pymongo as Motor relies on them for operations
from pymongo import ASCENDING, DESCENDING, UpdateOne 

POKETWO_ID = 716390085896962058
HKT = timezone(timedelta(hours=8))

MONGO_URI = "mongodb+srv://KurtisLam:CsHLOnDqihiU5uYG@cluster0.7rwx3oc.mongodb.net/?appName=Cluster0"

TIMEFRAME_CONFIG = {
    "daily": {
        "key": "daily",
        "label": "Daily",
        "color": 0x3498DB,  # Sapphire Blue
        "icon": "⚡",
    },
    "weekly": {
        "key": "weekly",
        "label": "Weekly",
        "color": 0x9B59B6,  # Amethyst Purple
        "icon": "📅",
    },
    "monthly": {
        "key": "monthly",
        "label": "Monthly",
        "color": 0x2ECC71,  # Emerald Green
        "icon": "🗓️",
    },
    "alltime": {
        "key": "alltime",
        "label": "All-Time",
        "color": 0xF1C40F,  # Gold
        "icon": "👑",
    },
}

TIMEFRAME_MAP = {
    "d": "daily",
    "daily": "daily",
    "w": "weekly",
    "weekly": "weekly",
    "m": "monthly",
    "monthly": "monthly",
    "all": "alltime",
    "alltime": "alltime",
    "total": "alltime",
    "a": "alltime",
}

PODIUM_EMOJIS = {1: "🥇", 2: "🥈", 3: "🥉"}


def get_hkt_period_keys():
    """Generates period identifiers based on current Hong Kong Time (UTC+8)."""
    now = datetime.now(HKT)
    daily_key = now.strftime("%Y-%m-%d")

    days_since_sunday = (now.weekday() + 1) % 7
    last_sunday = now - timedelta(days=days_since_sunday)
    weekly_key = last_sunday.strftime("%Y-%m-%d")

    monthly_key = now.strftime("%Y-%m")
    return daily_key, weekly_key, monthly_key


def get_next_reset_unix(tf_key: str) -> Optional[int]:
    """Calculates the Discord timestamp for the next timeframe reset in HKT."""
    now = datetime.now(HKT)
    if tf_key == "daily":
        next_reset = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    elif tf_key == "weekly":
        days_until_sunday = (6 - now.weekday()) % 7
        if days_until_sunday == 0:
            days_until_sunday = 7
        next_reset = (now + timedelta(days=days_until_sunday)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    elif tf_key == "monthly":
        if now.month == 12:
            next_reset = datetime(now.year + 1, 1, 1, tzinfo=HKT)
        else:
            next_reset = datetime(now.year, now.month + 1, 1, tzinfo=HKT)
    else:
        return None
    return int(next_reset.timestamp())


class Catches(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Async Motor Client
        self.mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI, tlsCAFile=certifi.where())
        self.db = self.mongo_client["utilities"]
        self.collection = self.db["catches"]
        self.links_collection = self.db["guild_links"]

        self.guild_cache: dict[str, str] = {}
        
    async def cog_load(self):
        """Warms up connection and asynchronously builds indexes."""
        try:
            await self.mongo_client.admin.command('ping')
            
            # AWAITED: Motor requires index creation to be awaited
            await self.collection.create_index(
                [
                    ("user_id", ASCENDING),
                    ("group_id", ASCENDING),
                    ("timeframe", ASCENDING),
                    ("key", ASCENDING),
                ],
                unique=True,
            )
            await self.collection.create_index(
                [
                    ("group_id", ASCENDING),
                    ("timeframe", ASCENDING),
                    ("key", ASCENDING),
                    ("count", DESCENDING),
                ]
            )
            await self.links_collection.create_index([("guild_id", ASCENDING)], unique=True)
            
        except Exception as e:
            print(f"Catches Cog: MongoDB warmup failed: {e}")

    # AWAITED: Made this method async because it queries the DB
    async def get_group_id(self, guild_id: str) -> str:
        guild_id_str = str(guild_id)
        if guild_id_str in self.guild_cache:
            return self.guild_cache[guild_id_str]

        # AWAITED: Non-blocking DB fetch
        doc = await self.links_collection.find_one({"guild_id": guild_id_str})
        group_id = doc["group_id"] if doc else guild_id_str
        self.guild_cache[guild_id_str] = group_id
        return group_id

    def parse_timeframe(self, tf: Optional[str]):
        if not tf:
            return TIMEFRAME_CONFIG["alltime"]
        key = TIMEFRAME_MAP.get(tf.lower())
        if not key:
            return None
        return TIMEFRAME_CONFIG[key]

    async def _send_and_clean(self, ctx, content: str):
        if ctx.message:
            try:
                await ctx.message.delete()
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                pass
        await ctx.send(content, delete_after=1.0)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if (
            not message.guild
            or message.author.id != POKETWO_ID
            or not message.content.startswith("Congratulations ")
            or not message.mentions
        ):
            return

        caught_user = message.mentions[0]
        user_id_str = str(caught_user.id)
        
        # AWAITED: Async fetch
        group_id = await self.get_group_id(str(message.guild.id))

        await message.channel.send(
            f"**{caught_user.name}** caught a Pokémon!"
        )

        daily_k, weekly_k, monthly_k = get_hkt_period_keys()

        operations = [
            UpdateOne(
                {
                    "user_id": user_id_str,
                    "group_id": group_id,
                    "timeframe": "alltime",
                    "key": "all",
                },
                {"$inc": {"count": 1}},
                upsert=True,
            ),
            UpdateOne(
                {
                    "user_id": user_id_str,
                    "group_id": group_id,
                    "timeframe": "daily",
                    "key": daily_k,
                },
                {"$inc": {"count": 1}},
                upsert=True,
            ),
            UpdateOne(
                {
                    "user_id": user_id_str,
                    "group_id": group_id,
                    "timeframe": "weekly",
                    "key": weekly_k,
                },
                {"$inc": {"count": 1}},
                upsert=True,
            ),
            UpdateOne(
                {
                    "user_id": user_id_str,
                    "group_id": group_id,
                    "timeframe": "monthly",
                    "key": monthly_k,
                },
                {"$inc": {"count": 1}},
                upsert=True,
            ),
        ]

        # AWAITED: Non-blocking bulk write
        await self.collection.bulk_write(operations)

    @commands.hybrid_command(
        name="catcheslink",
        description="Link two guilds together permanently so their catch data merges.",
    )
    @commands.has_permissions(administrator=True)
    async def catches_link(self, ctx, guild_id_1: str, guild_id_2: str):
        if guild_id_1 == guild_id_2:
            return await self._send_and_clean(
                ctx, "❌ You cannot link a guild to itself!"
            )

        # AWAITED: Async calls to the DB/cache
        group1 = await self.get_group_id(guild_id_1)
        group2 = await self.get_group_id(guild_id_2)

        if group1 == group2:
            return await self._send_and_clean(
                ctx, "ℹ️ These guilds are already linked!"
            )

        target_group = group1
        old_group = group2

        # AWAITED: Async DB updates
        await self.links_collection.update_many(
            {"group_id": old_group}, {"$set": {"group_id": target_group}}
        )

        await self.links_collection.update_one(
            {"guild_id": str(guild_id_1)},
            {"$set": {"group_id": target_group}},
            upsert=True,
        )
        await self.links_collection.update_one(
            {"guild_id": str(guild_id_2)},
            {"$set": {"group_id": target_group}},
            upsert=True,
        )

        for g_id, g_group in list(self.guild_cache.items()):
            if g_group == old_group:
                self.guild_cache[g_id] = target_group
        self.guild_cache[str(guild_id_1)] = target_group
        self.guild_cache[str(guild_id_2)] = target_group

        # AWAITED: Motor cursors require `.to_list()`
        old_docs = await self.collection.find({"group_id": old_group}).to_list(length=None)
        
        if old_docs:
            merge_operations = []
            for doc in old_docs:
                merge_operations.append(
                    UpdateOne(
                        {
                            "user_id": doc["user_id"],
                            "group_id": target_group,
                            "timeframe": doc["timeframe"],
                            "key": doc["key"],
                        },
                        {"$inc": {"count": doc["count"]}},
                        upsert=True,
                    )
                )
            if merge_operations:
                # AWAITED
                await self.collection.bulk_write(merge_operations)

            # AWAITED
            await self.collection.delete_many({"group_id": old_group})

        await self._send_and_clean(
            ctx,
            f"✅ Linked Guild `{guild_id_1}` & `{guild_id_2}`! All catch records merged.",
        )

    @commands.hybrid_command(
        name="catches", description="View your personalized Pokétwo catch profile card."
    )
    async def view_catches(
        self,
        ctx,
        timeframe: Optional[str] = commands.parameter(
            default=None, description="Timeframe (daily/weekly/monthly/alltime)"
        ),
    ):
        if not ctx.guild:
            return await ctx.send("❌ This command can only be used in a server!")

        tf_config = self.parse_timeframe(timeframe)
        if not tf_config:
            return await ctx.send(
                "❌ Invalid timeframe! Use `d`/`daily`, `w`/`weekly`, `m`/`monthly`, or `all`."
            )

        daily_k, weekly_k, monthly_k = get_hkt_period_keys()
        keys_map = {
            "daily": daily_k,
            "weekly": weekly_k,
            "monthly": monthly_k,
            "alltime": "all",
        }

        # AWAITED
        group_id = await self.get_group_id(str(ctx.guild.id))
        user_id_str = str(ctx.author.id)

        # AWAITED: .to_list() formatting
        user_docs = await self.collection.find({"user_id": user_id_str, "group_id": group_id}).to_list(length=None)
        
        counts = {"daily": 0, "weekly": 0, "monthly": 0, "alltime": 0}

        for doc in user_docs:
            tf = doc.get("timeframe")
            if tf in keys_map and doc.get("key") == keys_map[tf]:
                counts[tf] = doc.get("count", 0)

        active_tf_key = tf_config["key"]
        selected_count = counts[active_tf_key]

        embed = discord.Embed(
            title=f"{tf_config['icon']}  {ctx.author.name}'s Catch Stats",
            color=tf_config["color"],
        )
        embed.set_thumbnail(url=ctx.author.display_avatar.url)

        embed.add_field(
            name=f"Current Selection ({tf_config['label']})",
            value=f"```pico\n{selected_count:,} Pokémon Caught\n```",
            inline=False,
        )

        overview_text = (
            f"⚡ **Daily:** `{counts['daily']:,}`\n"
            f"📅 **Weekly:** `{counts['weekly']:,}`\n"
            f"🗓️ **Monthly:** `{counts['monthly']:,}`\n"
            f"👑 **All-Time:** `{counts['alltime']:,}`"
        )
        embed.add_field(name="📊 Period Breakdown", value=overview_text, inline=True)

        reset_unix = get_next_reset_unix(active_tf_key)
        if reset_unix:
            embed.add_field(
                name="⏳ Resets In",
                value=f"<t:{reset_unix}:R>",
                inline=True,
            )

        embed.set_footer(
            text=f"Server ID: {ctx.guild.id} • Pokétwo Tracker",
            icon_url=ctx.guild.icon.url if ctx.guild.icon else None,
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="catchleaderboard",
        aliases=["clb"],
        description="View the server's top Pokétwo catch leaders.",
    )
    async def catch_leaderboard(
        self,
        ctx,
        timeframe: Optional[str] = commands.parameter(
            default=None, description="Timeframe (daily/weekly/monthly/alltime)"
        ),
    ):
        if not ctx.guild:
            return await ctx.send("❌ This command can only be used in a server!")

        tf_config = self.parse_timeframe(timeframe)
        if not tf_config:
            return await ctx.send(
                "❌ Invalid timeframe! Use `d`/`daily`, `w`/`weekly`, `m`/`monthly`, or `all`."
            )

        daily_k, weekly_k, monthly_k = get_hkt_period_keys()
        keys_map = {
            "daily": daily_k,
            "weekly": weekly_k,
            "monthly": monthly_k,
            "alltime": "all",
        }
        active_key = tf_config["key"]
        search_key = keys_map[active_key]

        # AWAITED
        group_id = await self.get_group_id(str(ctx.guild.id))

        # AWAITED: Motor limits and sorts on the cursor, then awaits compilation to list
        top_catches = await self.collection.find(
            {"group_id": group_id, "timeframe": active_key, "key": search_key}
        ).sort("count", DESCENDING).limit(10).to_list(length=10)

        author_id_str = str(ctx.author.id)
        
        # AWAITED
        author_doc = await self.collection.find_one(
            {
                "user_id": author_id_str,
                "group_id": group_id,
                "timeframe": active_key,
                "key": search_key,
            }
        )
        author_count = author_doc["count"] if author_doc else 0

        if author_count > 0:
            # AWAITED
            higher_count = await self.collection.count_documents(
                {
                    "group_id": group_id,
                    "timeframe": active_key,
                    "key": search_key,
                    "count": {"$gt": author_count},
                }
            )
            user_rank_str = f"#{higher_count + 1}"
        else:
            user_rank_str = "Unranked"

        embed = discord.Embed(
            title=f"{tf_config['icon']}  {tf_config['label']} Leaderboard",
            color=tf_config["color"],
        )

        reset_unix = get_next_reset_unix(active_key)
        reset_str = f"⏱️ Period resets <t:{reset_unix}:R>\n\n" if reset_unix else ""

        if not top_catches:
            embed.description = f"{reset_str}*No catch data available for this timeframe yet.*"
        else:
            async def resolve_username(user_id: int) -> str:
                user = self.bot.get_user(user_id)
                if user:
                    return user.name
                try:
                    user = await self.bot.fetch_user(user_id)
                    return user.name
                except discord.NotFound:
                    return f"User({user_id})"

            user_names = await asyncio.gather(
                *(resolve_username(int(d["user_id"])) for d in top_catches)
            )

            lines = []
            for idx, (data, name) in enumerate(zip(top_catches, user_names), start=1):
                badge = PODIUM_EMOJIS.get(idx, f"`#{idx:<2}`")
                count_str = f"{data['count']:,}"

                if idx <= 3:
                    lines.append(f"{badge} **{name}** — **{count_str}** catches")
                else:
                    lines.append(f"{badge} {name} — `{count_str}` catches")

            embed.description = reset_str + "\n".join(lines)

        embed.add_field(
            name="━━━━ Your Position ━━━━",
            value=f"👤 **Rank:** `{user_rank_str}` ; 🎯 **Catches:** `{author_count:,}`",
            inline=False,
        )

        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Catches(bot))