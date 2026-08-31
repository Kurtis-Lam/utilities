import re
import typing
import certifi
import discord
from discord.ext import commands
import motor.motor_asyncio  # Replaced pymongo with asynchronous motor

MONGO_URI = "mongodb+srv://KurtisLam:CsHLOnDqihiU5uYG@cluster0.7rwx3oc.mongodb.net/?appName=Cluster0"

# Fast hash table lookups for command arguments & flags
ACTION_MAP = {"a": "add", "add": "add", "r": "remove", "remove": "remove"}
VARIABLE_MAP = {
    "ra": "rare",
    "rare": "rare",
    "reg": "regional",
    "regional": "regional",
    "u": "user",
    "user": "user",
}
FLAGS_MAP = {
    "--restrictcategory": "restrict_categories",
    "--rcat": "restrict_categories",
    "--restrictchannel": "restrict_channels",
    "--rch": "restrict_channels",
    "--excludecategory": "exclude_categories",
    "--xcat": "exclude_categories",
    "--excludechannel": "exclude_channels",
    "--xch": "exclude_channels",
}


class Config(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Async Motor Client
        self.mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI, tlsCAFile=certifi.where())
        self.db = self.mongo_client["utilities"]
        self.collection = self.db["config"]

    async def cog_load(self):
        """Warms up connection to MongoDB on boot."""
        try:
            await self.mongo_client.admin.command('ping')
        except Exception as e:
            print(f"Config Cog: MongoDB warmup failed: {e}")

    async def _get_guild_data(self, guild_id: str) -> dict:
        """Fetches server configuration directly from MongoDB asynchronously."""
        # AWAITED: Non-blocking DB fetch
        doc = await self.collection.find_one({"_id": guild_id})
        if not doc:
            doc = {"_id": guild_id, "rare": [], "regional": [], "user": []}
        return doc

    async def show_config(self, ctx):
        """Internal helper to render server configuration directly from database."""
        guild_id = str(ctx.guild.id)
        # AWAITED: Async guild config lookup
        guild_config = await self._get_guild_data(guild_id)

        def format_mentions(var_list, is_role=True):
            if not var_list:
                return "None configured"

            mentions = []
            for item in var_list:
                tgt_id = item["target"]
                mention = f"<@&{tgt_id}>" if is_role else f"<@{tgt_id}>"

                rules = []
                if rcat := item.get("restrict_categories"):
                    rules.append("only cat: " + ", ".join(f"<#{cid}>" for cid in rcat))
                if rch := item.get("restrict_channels"):
                    rules.append("only ch: " + ", ".join(f"<#{cid}>" for cid in rch))
                if xcat := item.get("exclude_categories"):
                    rules.append("excl cat: " + ", ".join(f"<#{cid}>" for cid in xcat))
                if xch := item.get("exclude_channels"):
                    rules.append("excl ch: " + ", ".join(f"<#{cid}>" for cid in xch))

                rule_str = f" ({'; '.join(rules)})" if rules else " (Global)"
                mentions.append(f"{mention}{rule_str}")

            return "\n".join(mentions)

        embed = discord.Embed(
            title=f"⚙️ Configuration for {ctx.guild.name}", color=discord.Color.blue()
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

        await ctx.send(embed=embed)

    @commands.hybrid_command(
        aliases=["c"],
        name="config",
        description="Configure server settings for roles and members",
    )
    @commands.has_permissions(administrator=True)
    async def config(
        self,
        ctx,
        action: typing.Optional[str] = commands.parameter(
            description="Add (a) or remove (r)"
        ),
        variable: typing.Optional[str] = commands.parameter(
            description="Configuration target: rare / regional / user"
        ),
        target: typing.Optional[typing.Union[discord.Role, discord.Member]] = commands.parameter(
            description="Role / member mention"
        ),
        *,
        extra: str = commands.parameter(
            description="Optional filter flag (--rch, --rcat, --xch, --xcat) followed by channel/category names or IDs",
            default=None,
        ),
    ):
        if action is None:
            return await self.show_config(ctx)

        if variable is None or target is None:
            return await ctx.send(
                "❌ **Syntax Error!** Use:\n"
                "`.config [add/remove] [rare/regional/user] [@mention] [--flag #channels]`"
            )

        action_key = ACTION_MAP.get(action.lower())
        if not action_key:
            return await ctx.send(
                "❌ Invalid action. Use `add` (`a`) or `remove` (`r`)."
            )

        var_key = VARIABLE_MAP.get(variable.lower())
        if not var_key:
            return await ctx.send(
                "❌ Invalid choice. Use `rare` (`ra`), `regional` (`reg`), or `user` (`u`)."
            )

        guild_id = str(ctx.guild.id)
        # AWAITED: Async guild data retrieval
        guild_data = await self._get_guild_data(guild_id)

        entry = {
            "target": target.id,
            "restrict_categories": [],
            "restrict_channels": [],
            "exclude_categories": [],
            "exclude_channels": [],
        }

        if action_key == "add" and extra and extra.strip():
            parts = extra.strip().split()
            flag = parts[0].lower()

            key_name = FLAGS_MAP.get(flag)
            if not key_name:
                return await ctx.send(
                    f"❌ Unknown argument flag: `{flag}`. Available: `--rcat`, `--rch`, `--xcat`, `--xch`"
                )

            if sum(1 for p in parts if p.startswith("--")) > 1:
                return await ctx.send(
                    "❌ **Limit Exceeded:** You can only use a maximum of 1 argument flag at a time."
                )

            remaining_text = " ".join(parts[1:])
            extracted_ids = [int(x) for x in re.findall(r"\d+", remaining_text)]

            if not extracted_ids and remaining_text.strip():
                name_query = remaining_text.strip()
                if "category" in key_name:
                    cat = discord.utils.get(ctx.guild.categories, name=name_query)
                    if cat:
                        extracted_ids = [cat.id]
                else:
                    ch = discord.utils.get(ctx.guild.channels, name=name_query)
                    if ch:
                        extracted_ids = [ch.id]

            if not extracted_ids:
                return await ctx.send(
                    f"❌ Please provide valid channel/category mentions or names following `{flag}`."
                )

            entry[key_name] = extracted_ids

        def get_confirmation_suffix(e):
            if e["restrict_categories"]:
                return " locked to category/ies: " + ", ".join(
                    f"<#{i}>" for i in e["restrict_categories"]
                )
            if e["restrict_channels"]:
                return " locked to channel/s: " + ", ".join(
                    f"<#{i}>" for i in e["restrict_channels"]
                )
            if e["exclude_categories"]:
                return " excluding category/ies: " + ", ".join(
                    f"<#{i}>" for i in e["exclude_categories"]
                )
            if e["exclude_channels"]:
                return " excluding channel/s: " + ", ".join(
                    f"<#{i}>" for i in e["exclude_channels"]
                )
            return " globally"

        if action_key == "add":
            if var_key in ("rare", "regional"):
                if guild_data.get(var_key):
                    return await ctx.send(
                        f"❌ A **{var_key}** configuration already exists! You must remove the current one using `.config remove {var_key}` before adding a new one."
                    )

                # AWAITED: Non-blocking write
                await self.collection.update_one(
                    {"_id": guild_id},
                    {"$set": {var_key: [entry]}},
                    upsert=True,
                )
                status_suffix = get_confirmation_suffix(entry)
                await ctx.send(f"✅ Set **{var_key}** to {target.mention}{status_suffix}.")
            else:
                status_suffix = get_confirmation_suffix(entry)
                if entry not in guild_data.get(var_key, []):
                    # AWAITED: Non-blocking write
                    await self.collection.update_one(
                        {"_id": guild_id},
                        {"$push": {var_key: entry}},
                        upsert=True,
                    )
                    await ctx.send(
                        f"✅ Added {target.mention} to **{var_key}**{status_suffix}."
                    )
                else:
                    await ctx.send(
                        f"⚠️ {target.mention} is already identically configured under **{var_key}**."
                    )

        elif action_key == "remove":
            existing_configs = guild_data.get(var_key, [])

            if var_key in ("rare", "regional"):
                if existing_configs:
                    # AWAITED: Non-blocking write
                    await self.collection.update_one(
                        {"_id": guild_id},
                        {"$set": {var_key: []}},
                        upsert=True,
                    )
                    await ctx.send(
                        f"✅ Successfully removed the configuration from **{var_key}**."
                    )
                else:
                    await ctx.send(
                        f"⚠️ There is no configuration set for **{var_key}**."
                    )
            else:
                matching_entries = [
                    e for e in existing_configs if e["target"] == target.id
                ]

                if matching_entries:
                    new_user_list = [
                        e for e in existing_configs if e["target"] != target.id
                    ]
                    # AWAITED: Non-blocking write
                    await self.collection.update_one(
                        {"_id": guild_id},
                        {"$set": {var_key: new_user_list}},
                        upsert=True,
                    )

                    count = len(matching_entries)
                    suffix = "configuration" if count == 1 else "configurations"
                    await ctx.send(
                        f"✅ Removed {target.mention} from **{var_key}** (wiped {count} {suffix})."
                    )
                else:
                    await ctx.send(
                        f"⚠️ Could not find any configurations for {target.mention} under **{var_key}**."
                    )

    @config.error
    async def config_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(
                "❌ You do not have the **Administrator** permission required to use this command."
            )
        else:
            print(f"Error in config command: {error}")
            await ctx.send(f"⚠️ An internal error occurred: `{error}`")


async def setup(bot):
    await bot.add_cog(Config(bot))