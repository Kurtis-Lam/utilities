import discord
from discord.ext import commands
import typing
import json
import os
import re

CONFIG_DIR = "data"
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

class Config(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def load_config(self):
        if not os.path.exists(CONFIG_FILE):
            return {}
        with open(CONFIG_FILE, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}

    def save_config(self, data):
        if not os.path.exists(CONFIG_DIR):
            os.makedirs(CONFIG_DIR)
        with open(CONFIG_FILE, "w") as f:
            json.dump(data, f, indent=4)

    def _ensure_guild_data(self, data, guild_id):
        """Helper to ensure the guild entry exists in data structure."""
        if guild_id not in data:
            data[guild_id] = {"rare": [], "regional": [], "user": []}
        return data[guild_id]

    async def show_config(self, ctx):
        """Internal helper to render the current server configuration embed."""
        data = self.load_config()
        guild_id = str(ctx.guild.id)
        guild_config = self._ensure_guild_data(data, guild_id)

        def format_mentions(var_list, is_role=True):
            mentions = []
            for item in var_list:
                tgt_id = item["target"]
                mention = f"<@&{tgt_id}>" if is_role else f"<@{tgt_id}>"
                
                rules = []
                if item.get("restrict_categories"):
                    rules.append("only cat: " + ", ".join(f"<#{cid}>" for cid in item["restrict_categories"]))
                if item.get("restrict_channels"):
                    rules.append("only ch: " + ", ".join(f"<#{cid}>" for cid in item["restrict_channels"]))
                if item.get("exclude_categories"):
                    rules.append("excl cat: " + ", ".join(f"<#{cid}>" for cid in item["exclude_categories"]))
                if item.get("exclude_channels"):
                    rules.append("excl ch: " + ", ".join(f"<#{cid}>" for cid in item["exclude_channels"]))
                
                mention += f" ({'; '.join(rules)})" if rules else " (Global)"
                mentions.append(mention)
            return "\n".join(mentions) if mentions else "None configured"

        embed = discord.Embed(title=f"⚙️ Configuration for {ctx.guild.name}", color=discord.Color.blue())
        embed.add_field(name="Rare Roles", value=format_mentions(guild_config.get("rare", []), True), inline=False)
        embed.add_field(name="Regional Roles", value=format_mentions(guild_config.get("regional", []), True), inline=False)
        embed.add_field(name="Tracked Users", value=format_mentions(guild_config.get("user", []), False), inline=False)

        await ctx.send(embed=embed)

    @commands.hybrid_command(aliases=["c"], name="config", description="Configure server settings for roles and members")
    @commands.has_permissions(administrator=True)
    async def config(
        self, 
        ctx, 
        action: typing.Optional[str] = commands.parameter(description="Add (a) or remove (r)"), 
        variable: typing.Optional[str] = commands.parameter(description="Configuration target: rare / regional / user"), 
        target: typing.Optional[typing.Union[discord.Role, discord.Member]] = commands.parameter(description="Role / member mention"), 
        *, 
        extra: str = commands.parameter(description="Optional filter flag (--rch, --rcat, --xch, --xcat) followed by channel/category names or IDs", default=None)
    ):
        
        if action is None:
            return await self.show_config(ctx)

        if variable is None or target is None:
            return await ctx.send(
                "❌ **Syntax Error!** Use:\n"
                "`.config [add/remove] [rare/regional/user] [@mention] [--flag #channels]`"
            )

        action = action.lower()
        if action in ["a", "add"]: action = "add"
        elif action in ["r", "remove"]: action = "remove"
        else: return await ctx.send("❌ Invalid action. Use `add` (`a`) or `remove` (`r`).")
            
        variable = variable.lower()
        if variable in ["ra", "rare"]: variable = "rare"
        elif variable in ["reg", "regional"]: variable = "regional"
        elif variable in ["u", "user"]: variable = "user"
        else: return await ctx.send("❌ Invalid choice. Use `rare` (`ra`), `regional` (`reg`), or `user` (`u`).")

        data = self.load_config()
        guild_id = str(ctx.guild.id)
        
        self._ensure_guild_data(data, guild_id)

        flags_map = {
            "--restrictcategory": "restrict_categories",
            "--rcat": "restrict_categories",
            "--restrictchannel": "restrict_channels",
            "--rch": "restrict_channels",
            "--excludecategory": "exclude_categories",
            "--xcat": "exclude_categories",
            "--excludechannel": "exclude_channels",
            "--xch": "exclude_channels"
        }

        entry = {
            "target": target.id,
            "restrict_categories": [],
            "restrict_channels": [],
            "exclude_categories": [],
            "exclude_channels": []
        }

        if action == "add" and extra and extra.strip():
            parts = extra.strip().split()
            flag = parts[0].lower()

            if flag not in flags_map:
                return await ctx.send(f"❌ Unknown argument flag: `{flag}`. Available: `--rcat`, `--rch`, `--xcat`, `--xch`")

            found_flags = [p for p in parts if p.startswith("--")]
            if len(found_flags) > 1:
                return await ctx.send("❌ **Limit Exceeded:** You can only use a maximum of 1 argument flag at a time.")

            key_name = flags_map[flag]
            remaining_text = " ".join(parts[1:])
            
            extracted_ids = [int(x) for x in re.findall(r'\d+', remaining_text)]

            if not extracted_ids and remaining_text.strip():
                name_query = remaining_text.strip()
                if "category" in key_name:
                    cat = discord.utils.get(ctx.guild.categories, name=name_query)
                    if cat: extracted_ids = [cat.id]
                else:
                    ch = discord.utils.get(ctx.guild.channels, name=name_query)
                    if ch: extracted_ids = [ch.id]

            if not extracted_ids:
                return await ctx.send(f"❌ Please provide valid channel/category mentions or names following `{flag}`.")

            entry[key_name] = extracted_ids

        def get_confirmation_suffix(e):
            if e["restrict_categories"]: return f" locked to category/ies: " + ", ".join(f"<#{i}>" for i in e["restrict_categories"])
            if e["restrict_channels"]: return f" locked to channel/s: " + ", ".join(f"<#{i}>" for i in e["restrict_channels"])
            if e["exclude_categories"]: return f" excluding category/ies: " + ", ".join(f"<#{i}>" for i in e["exclude_categories"])
            if e["exclude_channels"]: return f" excluding channel/s: " + ", ".join(f"<#{i}>" for i in e["exclude_channels"])
            return " globally"

        if action == "add":
            if variable in ["rare", "regional"]:
                if len(data[guild_id][variable]) > 0:
                    return await ctx.send(
                        f"❌ A **{variable}** configuration already exists! You must remove the current one using `.config remove {variable}` before adding a new one."
                    )
                
                data[guild_id][variable] = [entry]
                self.save_config(data)
                status_suffix = get_confirmation_suffix(entry)
                await ctx.send(f"✅ Set **{variable}** to {target.mention}{status_suffix}.")
            else:
                status_suffix = get_confirmation_suffix(entry)
                if entry not in data[guild_id][variable]:
                    data[guild_id][variable].append(entry)
                    self.save_config(data)
                    await ctx.send(f"✅ Added {target.mention} to **{variable}**{status_suffix}.")
                else:
                    await ctx.send(f"⚠️ {target.mention} is already identically configured under **{variable}**.")
                
        elif action == "remove":
            existing_configs = data[guild_id][variable]
            
            if variable in ["rare", "regional"]:
                if existing_configs:
                    data[guild_id][variable] = []
                    self.save_config(data)
                    await ctx.send(f"✅ Successfully removed the configuration from **{variable}**.")
                else:
                    await ctx.send(f"⚠️ There is no configuration set for **{variable}**.")
            else:
                matching_entries = [e for e in existing_configs if e["target"] == target.id]

                if matching_entries:
                    for match in matching_entries:
                        existing_configs.remove(match)
                    self.save_config(data)
                    
                    count = len(matching_entries)
                    suffix = "configuration" if count == 1 else "configurations"
                    await ctx.send(f"✅ Removed {target.mention} from **{variable}** (wiped {count} {suffix}).")
                else:
                    await ctx.send(f"⚠️ Could not find any configurations for {target.mention} under **{variable}**.")

    @config.error
    async def config_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ You do not have the **Administrator** permission required to use this command.")
        else:
            print(f"Error in config command: {error}")
            await ctx.send(f"⚠️ An internal error occurred: `{error}`")

async def setup(bot):
    await bot.add_cog(Config(bot))