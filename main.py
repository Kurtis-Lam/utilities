import asyncio
import aiohttp
import discord
import gc
import json
import os
import sys
import time
import traceback

from discord.ext import commands
from datetime import datetime, timezone

import certifi
import motor.motor_asyncio

from cogs.owner_cmds.utils import safe_send

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
try:
    os.chdir(BASE_DIR)  # make relative paths (config.json, cogs/) always work
except OSError:
    pass

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
config = {}
config_path = os.path.join(BASE_DIR, "config.json")
if os.path.exists(config_path):
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
            if not isinstance(config, dict):
                raise ValueError("config.json must contain a JSON object")
    except Exception as e:
        print(f"❌ Error: could not read 'config.json': {e}")
        sys.exit(1)
else:
    print("❌ Error: 'config.json' file not found.")
    sys.exit(1)

TOKEN = config.get("TOKEN") or os.environ.get("TOKEN")
MONGO_URI = config.get("MONGO_URI") or os.environ.get("MONGO_URI")
PREFIX = config.get("PREFIX", ".")  # defaults to '.' if not specified

if not TOKEN:
    print("❌ Error: 'TOKEN' missing from config.json.")
    sys.exit(1)

OWNERS = {
    1250429544486273038, 1281560553130692618, 1528374615720591381,
    1432984051341459527, 1432983193681920014,
}

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.guilds = True
INTENTS.members = True

STATE_DB = "utilities"
STATE_COLLECTION = "bot_state"
STATE_ID = "pending_update"

# ----------------------------------------------------------------------------
# Bot
# ----------------------------------------------------------------------------
class Utilities(commands.Bot):
    def __init__(self, prefix: str = "!"):
        self.prefix_str = prefix
        super().__init__(
            command_prefix=self.get_prefix_with_space,
            owner_ids=OWNERS,
            intents=INTENTS,
            case_insensitive=True,
            chunk_guilds_at_startup=False,
            member_cache_flags=discord.MemberCacheFlags.none(),
        )
        self.start_time = datetime.now(timezone.utc)
        self.active_predictions = {}
        self.session = None
        self.restart_requested = False
        self.update_lock = asyncio.Lock()
        self._ready_once = False

        # Shared MongoDB client for all cogs
        self.mongo_client = None
        if MONGO_URI:
            try:
                self.mongo_client = motor.motor_asyncio.AsyncIOMotorClient(
                    MONGO_URI,
                    tlsCAFile=certifi.where(),
                    maxPoolSize=2,
                    minPoolSize=0,
                    serverSelectionTimeoutMS=5000,
                )
            except Exception as e:
                print(f"⚠️ MongoDB client could not be created: {e}")
        else:
            print("⚠️ MONGO_URI missing, MongoDB features are disabled.")

        self.cogs_dict = {
            "owner_cmds": ["reload", "pull", "stats", "restart"],
            "cmds": ["ai", "categories", "channels", "members", "messages", "ping", "roles", "utilities"],
            "config": ["base"],
            "poketwo_helper": ["afk", "autolock", "catches", "lockunlock", "pings", "recognizer", "starboard"],
            "poketwo_management": ["set", "settings", "toggle"],
            "poketwo_utils": ["catchtime", "dex", "extract", "hintsolver"],
        }

    async def get_prefix_with_space(self, bot, message):
        try:
            return commands.when_mentioned_or(self.prefix_str, f"{self.prefix_str} ")(bot, message)
        except Exception:
            return self.prefix_str

    # ---- MongoDB restart-state helpers (never raise) ----
    @property
    def state_col(self):
        if self.mongo_client is None:
            return None
        return self.mongo_client[STATE_DB][STATE_COLLECTION]

    async def save_state(self, data: dict) -> bool:
        col = self.state_col
        if col is None:
            return False
        try:
            await asyncio.wait_for(
                col.update_one({"_id": STATE_ID}, {"$set": data}, upsert=True), timeout=10
            )
            return True
        except Exception as e:
            print(f"⚠️ Could not save update state: {e}")
            return False

    async def load_state(self):
        col = self.state_col
        if col is None:
            return None
        try:
            return await asyncio.wait_for(col.find_one({"_id": STATE_ID}), timeout=10)
        except Exception as e:
            print(f"⚠️ Could not load update state: {e}")
            return None

    async def clear_state(self):
        col = self.state_col
        if col is None:
            return
        try:
            await asyncio.wait_for(col.delete_one({"_id": STATE_ID}), timeout=10)
        except Exception as e:
            print(f"⚠️ Could not delete update state: {e}")

    async def finish_pending_update(self):
        """After a restart: edit the saved message to 'completed', then delete the DB entry."""
        state = await self.load_state()
        if not state:
            return
        try:
            channel_id = state.get("channel_id")
            message_id = state.get("message_id")
            channel = self.get_channel(channel_id) if channel_id else None
            if channel is None and channel_id:
                try:
                    channel = await self.fetch_channel(channel_id)
                except Exception:
                    channel = None

            if channel is not None and message_id:
                took = ""
                started = state.get("started_at")
                if isinstance(started, (int, float)):
                    took = f" in `{time.time() - started:.1f}s`"
                old, new = state.get("old_commit", "?"), state.get("new_commit", "?")
                progress = state.get("progress", "")
                text = (
                    f"{progress}\n\n" if progress else ""
                ) + (
                    f"🎉 **Update completed**{took}\n"
                    f"`{old}` → `{new}` on `{state.get('branch', '?')}`\n"
                    f"Logged in as **{self.user}** ({round(self.latency * 1000)} ms)"
                )
                # Mark the restart step as done in the old progress text
                text = text.replace("🚀 Restarting bot...", "✅ Restarted bot")
                try:
                    msg = channel.get_partial_message(message_id)
                    await msg.edit(content=text[:1990])
                except Exception as e:
                    print(f"⚠️ Could not edit update message: {e}")
        except Exception:
            traceback.print_exc()
        finally:
            await self.clear_state()  # always delete, even if editing failed

    async def setup_hook(self):
        try:
            connector = aiohttp.TCPConnector(limit=5, enable_cleanup_closed=True)
            self.session = aiohttp.ClientSession(connector=connector)
        except Exception as e:
            print(f"⚠️ Could not create HTTP session: {e}")

        print("\n📦 Loading cogs...")
        for category, cogs in self.cogs_dict.items():
            print(f"📂 [{category}]")
            for i, cog in enumerate(cogs):
                branch = "└──" if i == len(cogs) - 1 else "├──"
                try:
                    await self.load_extension(f"cogs.{category}.{cog.lower()}")
                    print(f"  {branch} ✅ {cog}")
                except Exception as e:
                    print(f"  {branch} ❌ {cog} (Error: {e})")
            print()

        gc.collect()

    async def close(self):
        try:
            if self.session and not self.session.closed:
                await self.session.close()
        except Exception:
            pass
        try:
            if self.mongo_client:
                self.mongo_client.close()
        except Exception:
            pass
        try:
            await super().close()
        except Exception:
            pass


bot = Utilities(prefix=PREFIX)


# ----------------------------------------------------------------------------
# Events
# ----------------------------------------------------------------------------
@bot.event
async def on_ready():
    print(f"We have logged in as {bot.user}")
    if bot._ready_once:  # on_ready can fire multiple times on reconnects
        return
    bot._ready_once = True
    await bot.finish_pending_update()


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    try:
        if after.author.bot or after.guild is None or before.content == after.content:
            return
        await bot.process_commands(after)
    except Exception:
        traceback.print_exc()


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    # Let commands/cogs with their own handlers deal with it
    try:
        if ctx.command and ctx.command.has_error_handler():
            return
        if ctx.cog and ctx.cog.has_error_handler():
            return
    except Exception:
        pass

    error = getattr(error, "original", error)

    if isinstance(error, (commands.CommandNotFound, commands.NotOwner)):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await safe_send(ctx, f"❌ Missing argument: `{error.param.name}`")
    elif isinstance(error, (commands.BadArgument, commands.BadUnionArgument)):
        await safe_send(ctx, f"❌ Invalid argument: {error}")
    elif isinstance(error, commands.MissingPermissions):
        await safe_send(ctx, "❌ You don't have permission to do that.")
    elif isinstance(error, commands.BotMissingPermissions):
        await safe_send(ctx, "❌ I don't have the permissions needed for that.")
    elif isinstance(error, commands.CommandOnCooldown):
        await safe_send(ctx, f"⏳ Try again in `{error.retry_after:.1f}s`.")
    elif isinstance(error, commands.CheckFailure):
        return
    else:
        print(f"Unhandled error in command '{ctx.command}':")
        traceback.print_exception(type(error), error, error.__traceback__)
        await safe_send(ctx, "❌ Something went wrong running that command.")


@bot.event
async def on_error(event_method: str, *args, **kwargs):
    print(f"Unhandled error in event '{event_method}':")
    traceback.print_exc()


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------
def main():
    try:
        bot.run(TOKEN)
    except discord.LoginFailure:
        print("❌ Invalid bot token. Check TOKEN in config.json.")
        sys.exit(1)
    except discord.PrivilegedIntentsRequired:
        print("❌ Enable the Message Content / Server Members intents in the Discord developer portal.")
        sys.exit(1)
    except discord.HTTPException as e:
        if e.status == 429:
            print("The Discord servers denied the connection for making too many requests")
        else:
            print(f"❌ HTTP error while starting: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("Shutting down.")
        sys.exit(0)
    except Exception:
        traceback.print_exc()
        sys.exit(1)

    # bot.run() returned cleanly -> restart if requested
    if bot.restart_requested:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            print(f"❌ Failed to restart process: {e}")
            sys.exit(1)


if __name__ == "__main__":
    main()