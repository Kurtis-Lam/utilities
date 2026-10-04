import asyncio
import aiohttp
import discord
import gc
import json
import os
import platform
import psutil
import sys
import time
import traceback

from discord.ext import commands
from datetime import datetime, timezone

import certifi
import motor.motor_asyncio

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
# Helpers
# ----------------------------------------------------------------------------
async def run_cmd(*args: str, timeout: int = 120):
    """Run a subprocess without blocking the event loop.
    Returns (returncode, stdout, stderr). Never raises."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # never hang asking for credentials
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=BASE_DIR,
            env=env,
        )
    except FileNotFoundError:
        return 127, "", f"`{args[0]}` is not installed or not in PATH."
    except Exception as e:
        return 1, "", str(e)

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return 124, "", f"Timed out after {timeout}s."
    except Exception as e:
        return 1, "", str(e)

    return (
        proc.returncode,
        out.decode(errors="replace").strip(),
        err.decode(errors="replace").strip(),
    )


async def safe_edit(msg, content: str):
    """Edit a message, swallowing every possible error."""
    if msg is None:
        return
    try:
        await msg.edit(content=content[:1990])
    except Exception:
        pass


async def safe_send(ctx, *args, **kwargs):
    try:
        return await ctx.send(*args, **kwargs)
    except Exception:
        return None


def get_dir_size(path: str = ".") -> int:
    """Recursively calculate directory size in bytes."""
    total = 0
    try:
        for root, _, files in os.walk(path):
            for f in files:
                filepath = os.path.join(root, f)
                try:
                    if not os.path.islink(filepath):
                        total += os.path.getsize(filepath)
                except OSError:
                    pass
    except Exception:
        pass
    return total


class Progress:
    """Tracks update progress lines and renders them into one message."""

    def __init__(self, msg):
        self.msg = msg
        self.lines = []

    def render(self) -> str:
        return "\n".join(self.lines)

    async def start(self, text: str):
        self.lines.append(f"⏳ {text}")
        await safe_edit(self.msg, self.render())

    async def ok(self, text: str = None):
        if self.lines:
            body = text or self.lines[-1][2:].strip()
            self.lines[-1] = f"✅ {body}"
        await safe_edit(self.msg, self.render())

    async def warn(self, text: str):
        if self.lines:
            self.lines[-1] = f"⚠️ {text}"
        await safe_edit(self.msg, self.render())

    async def fail(self, text: str, detail: str = ""):
        if self.lines:
            self.lines[-1] = f"❌ {text}"
        out = self.render()
        if detail:
            out += f"\n```\n{detail[:800]}\n```"
        await safe_edit(self.msg, out)


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
            "cmds": ["ai", "categories", "channels", "members", "messages", "ping", "roles", "utilities"],
            "config": ["base"],
            "poketwo_helper": ["afk", "autolock", "catches", "lockunlock", "pings", "recognizer"],
            "poketwo-management": ["set", "settings", "toggle"],
            "poketwo-utils": ["dex", "extract", "hintsolver"],
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
# Commands
# ----------------------------------------------------------------------------
@bot.command(name="stats", aliases=["botinfo", "system", "info"])
async def stats(ctx: commands.Context):
    now = datetime.now(timezone.utc)
    uptime = now - bot.start_time
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)
    days, hours = divmod(hours, 24)
    uptime_str = f"{days}d {hours}h {minutes}m {seconds}s"

    process = psutil.Process(os.getpid())
    proc_mem = process.memory_info().rss / (1024 ** 2)
    proc_cpu = process.cpu_percent(interval=None)

    sys_mem = psutil.virtual_memory()
    sys_cpu = psutil.cpu_percent(interval=None)
    cpu_cores = psutil.cpu_count(logical=True)

    bot_dir_bytes = await asyncio.to_thread(get_dir_size, ".")
    bot_dir_mb = bot_dir_bytes / (1024 ** 2)

    total_guilds = len(bot.guilds)
    total_users = sum(g.member_count or 0 for g in bot.guilds)
    total_channels = sum(len(g.channels) for g in bot.guilds)
    total_cogs = len(bot.cogs)
    total_commands = len(bot.commands)

    embed = discord.Embed(
        title=f"📊 {bot.user.name} Statistics",
        color=discord.Color.blurple(),
        timestamp=now,
    )
    if bot.user.display_avatar:
        embed.set_thumbnail(url=bot.user.display_avatar.url)

    embed.add_field(
        name="🤖 Bot Metrics",
        value=(
            f"**Latency:** `{round(bot.latency * 1000, 2)} ms`\n"
            f"**Uptime:** `{uptime_str}`\n"
            f"**Guilds:** `{total_guilds:,}`\n"
            f"**Users:** `{total_users:,}`\n"
            f"**Channels:** `{total_channels:,}`\n"
            f"**Commands:** `{total_commands}`\n"
            f"**Loaded Cogs:** `{total_cogs}`"
        ),
        inline=True,
    )
    embed.add_field(
        name="⚡ Process Hardware",
        value=(
            f"**CPU Usage:** `{proc_cpu:.1f}%`\n"
            f"**RAM Usage:** `{proc_mem:.2f} MB`\n"
            f"**Bot Directory:** `{bot_dir_mb:.2f} MB`\n"
            f"**Threads:** `{process.num_threads()}`\n"
            f"**Async Tasks:** `{len(asyncio.all_tasks())}`\n"
            f"**HTTP Session:** `{'Active' if bot.session and not bot.session.closed else 'Closed'}`"
        ),
        inline=True,
    )
    embed.add_field(
        name="🖥️ Host System Hardware",
        value=(
            f"**CPU Usage:** `{sys_cpu:.1f}%` ({cpu_cores} Cores)\n"
            f"**RAM Usage:** `{sys_mem.used / (1024**3):.2f} / {sys_mem.total / (1024**3):.2f} GB` (`{sys_mem.percent}%`)"
        ),
        inline=False,
    )
    embed.add_field(
        name="⚙️ Environment",
        value=(
            f"**Python Version:** `v{platform.python_version()}`\n"
            f"**discord.py Version:** `v{discord.__version__}`\n"
            f"**Operating System:** `{platform.system()} {platform.release()} ({platform.machine()})`"
        ),
        inline=False,
    )
    embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command(name="reload")
@commands.is_owner()
async def reload(ctx: commands.Context, cog_name: str = None):
    if cog_name:
        target_ext = None
        for category, cogs in bot.cogs_dict.items():
            if cog_name.lower() in [c.lower() for c in cogs]:
                target_ext = f"cogs.{category}.{cog_name.lower()}"
                break

        if not target_ext:
            await safe_send(ctx, f"❌ Cog `{cog_name}` not found.")
            return

        msg = await safe_send(ctx, f"🔄 Reloading `{cog_name}`...")
        try:
            await bot.reload_extension(target_ext)
            gc.collect()
            await safe_edit(msg, f"✅ Reloaded `{cog_name}`.")
        except Exception as e:
            await safe_edit(msg, f"❌ Failed to reload `{cog_name}`: `{e}`")
        return

    reloaded, failed = [], []
    msg = await safe_send(ctx, "🔄 Starting reload process...")

    for category, cogs in bot.cogs_dict.items():
        for cog in cogs:
            await safe_edit(msg, f"🔄 Reloading `{cog}`...")
            ext = f"cogs.{category}.{cog.lower()}"
            try:
                await bot.reload_extension(ext)
                reloaded.append(cog)
            except Exception as e:
                failed.append(f"`{cog}`: {e}")
            gc.collect()

    final_msg = f"🔄 **Reload Complete**\n✅ Successfully reloaded **{len(reloaded)}** cogs."
    if failed:
        final_msg += f"\n❌ **Failed ({len(failed)}):**\n" + "\n".join(failed)
    await safe_edit(msg, final_msg)


@bot.command(name="update")
@commands.is_owner()
async def update(ctx: commands.Context, mode: str = ""):
    """Pull the latest code, show progress, and restart.
    Use `update force` to restart even if nothing changed."""
    if bot.update_lock.locked():
        await safe_send(ctx, "⚠️ An update is already running.")
        return

    async with bot.update_lock:
        force = mode.lower() == "force"
        started_at = time.time()
        msg = await safe_send(ctx, "🔄 Starting update...")
        prog = Progress(msg)

        # 1. Check repo + current branch
        await prog.start("Checking git repository...")
        rc, out, err = await run_cmd("git", "rev-parse", "--abbrev-ref", "HEAD", timeout=30)
        if rc != 0:
            await prog.fail("Not a git repository (or git missing)", err or out)
            return
        branch = out.strip()
        if branch == "HEAD":
            await prog.fail("Detached HEAD, can't pull. Check out a branch first.")
            return
        await prog.ok(f"Repository OK (branch `{branch}`)")

        rc, old_commit, _ = await run_cmd("git", "rev-parse", "--short", "HEAD", timeout=30)
        old_commit = old_commit if rc == 0 else "unknown"

        # 2. Fetch
        await prog.start("Fetching from origin...")
        rc, out, err = await run_cmd("git", "fetch", "origin", timeout=120)
        if rc != 0:
            await prog.fail("Fetch failed", err or out)
            return
        await prog.ok("Fetched from origin")

        # 3. Make sure tracking is set (this is what caused the original error)
        await prog.start("Checking branch tracking...")
        rc, _, _ = await run_cmd("git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", timeout=30)
        if rc == 0:
            await prog.ok("Branch tracking OK")
        else:
            rc, out, err = await run_cmd(
                "git", "branch", f"--set-upstream-to=origin/{branch}", branch, timeout=30
            )
            if rc == 0:
                await prog.ok(f"Set tracking to `origin/{branch}`")
            else:
                await prog.warn("Couldn't set tracking, pulling explicitly instead")

        # 4. Update the working tree
        if mode.lower() == "hard":
            # Mirror GitHub exactly: discards local changes to tracked files
            await prog.start(f"Resetting to `origin/{branch}` (discarding local changes)...")
            rc, out, err = await run_cmd("git", "reset", "--hard", f"origin/{branch}", timeout=60)
            if rc != 0:
                await prog.fail("Hard reset failed", err or out)
                return
            await prog.ok(f"Reset to `origin/{branch}`")
        else:
            # Auto-stash local changes so they can never block the pull (nothing is lost)
            rc, dirty, _ = await run_cmd("git", "status", "--porcelain", "--untracked-files=no", timeout=30)
            if rc == 0 and dirty:
                await prog.start("Stashing local changes...")
                rc, out, err = await run_cmd(
                    "git", "stash", "push", "-m", f"auto-stash before update {int(time.time())}", timeout=60
                )
                if rc != 0:
                    await prog.fail("Couldn't stash local changes", err or out)
                    return
                await prog.ok("Stashed local changes (recover with `git stash pop`)")

            # Explicit remote + branch, so it works with or without tracking
            await prog.start("Pulling latest changes...")
            rc, out, err = await run_cmd("git", "pull", "--ff-only", "origin", branch, timeout=180)
            if rc != 0:
                await prog.fail(
                    "Git pull failed (if branches diverged, try `update hard`)", err or out
                )
                return

        rc, new_commit, _ = await run_cmd("git", "rev-parse", "--short", "HEAD", timeout=30)
        new_commit = new_commit if rc == 0 else "unknown"

        if old_commit == new_commit and not force:
            await prog.ok("Already up to date, no restart needed")
            return
        await prog.ok(f"Pulled `{old_commit}` → `{new_commit}`")

        # 5. Save message info to MongoDB
        await prog.start("Saving restart state to MongoDB...")
        # Pre-render what the progress text will look like after this step finishes
        preview = prog.lines[:-1] + ["✅ Saved restart state to MongoDB", "🚀 Restarting bot..."]
        saved = await bot.save_state({
            "message_id": getattr(msg, "id", None),
            "channel_id": ctx.channel.id,
            "guild_id": ctx.guild.id if ctx.guild else None,
            "old_commit": old_commit,
            "new_commit": new_commit,
            "branch": branch,
            "started_at": started_at,
            "progress": "\n".join(preview),
        })
        if saved and msg is not None:
            await prog.ok("Saved restart state to MongoDB")
        else:
            await prog.warn("Couldn't save restart state (will still restart)")

        # 6. Restart
        prog.lines.append("🚀 Restarting bot...")
        await safe_edit(msg, prog.render())

        bot.restart_requested = True
        await bot.close()  # makes bot.run() return; main() then re-execs the process


@bot.command(name="restart")
@commands.is_owner()
async def restart(ctx: commands.Context):
    msg = await safe_send(ctx, "🚀 Restarting bot...")
    saved = await bot.save_state({
        "message_id": getattr(msg, "id", None),
        "channel_id": ctx.channel.id,
        "guild_id": ctx.guild.id if ctx.guild else None,
        "old_commit": "-",
        "new_commit": "-",
        "branch": "-",
        "started_at": time.time(),
        "progress": "",
    }) if msg else False
    if not saved:
        await safe_edit(msg, "🚀 Restarting bot... (couldn't save state)")
    bot.restart_requested = True
    await bot.close()


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