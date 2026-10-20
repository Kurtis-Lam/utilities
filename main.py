import asyncio
import aiohttp
import discord
import gc
import json
import os
import sys
import time
import traceback

from discord import app_commands
from discord.ext import commands
from discord.ext.commands.hybrid import HybridAppCommand
from datetime import datetime, timezone

import certifi
import motor.motor_asyncio

from cogs.owner.utils import safe_send

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

TOKEN = config.get("TOKEN")
MONGO_URI = config.get("MONGO_URI")
PREFIX = config.get("PREFIX", ".")

if not TOKEN:
    print("❌ Error: 'TOKEN' missing from config.json.")
    sys.exit(1)

try:
    POKETWO_ID = int(config["POKETWO_ID"])
except (KeyError, TypeError, ValueError):
    print("❌ Error: 'POKETWO_ID' missing or invalid in config.json (must be a user ID number).")
    sys.exit(1)

try:
    OWNERS = {int(owner_id) for owner_id in config.get("OWNER_IDS", [])}
except (TypeError, ValueError):
    print("❌ Error: 'OWNER_IDS' in config.json must be a list of user ID numbers.")
    sys.exit(1)
if not OWNERS:
    print("⚠️ Warning: 'OWNER_IDS' is empty in config.json, nobody can use owner commands.")

COGS = config.get("COGS")
if not isinstance(COGS, dict) or not all(
    isinstance(cogs, list) and all(isinstance(c, str) for c in cogs) for cogs in COGS.values()
):
    print("❌ Error: 'COGS' in config.json must be an object like {\"cmds\": [\"ping\", ...], ...}.")
    sys.exit(1)

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.guilds = True
INTENTS.members = True

STATE_DB = "utilities"
STATE_COLLECTION = "bot_state"
STATE_ID = "pending_update"


# ----------------------------------------------------------------------------
# Error messages delete themselves (and the command that caused them)
# ----------------------------------------------------------------------------
# Every error the bot shows (❌ / 🚫 / ⛔ messages, red ⚠️ embeds) disappears after
# ERROR_DELETE_DELAY seconds, wherever it was sent from:
#   * UtilitiesContext      - replies of prefix / hybrid commands (also removes the user's command message),
#   * gateway listeners     - any message the bot posts or edits (listeners, buttons, background tasks),
#   * interaction patches   - ephemeral replies and follow-ups, which never reach the gateway.
# Messages that carry buttons or menus are never treated as errors.
ERROR_DELETE_DELAY = 5
ERROR_COLOR_VALUE = discord.Color.red().value
ERROR_PREFIXES = ("❌", "🚫", "⛔")
_INTERACTIVE_COMPONENT_TYPES = {2, 3, 5, 6, 7, 8}
_scheduled_deletes: set[int] = set()


def _strip_heading(text: str) -> str:
    return text.lstrip().lstrip("#").lstrip()


def _embed_is_error(embed) -> bool:
    text = _strip_heading(embed.title or embed.description or "")
    if text.startswith(ERROR_PREFIXES):
        return True
    return text.startswith("⚠️") and embed.color is not None and embed.color.value == ERROR_COLOR_VALUE


def _looks_like_error(content, embeds) -> bool:
    """True for plain text / embeds that were written as an error message (❌ ..., 🚫 ...)."""
    if isinstance(content, str) and _strip_heading(content).startswith(ERROR_PREFIXES):
        return True
    return any(_embed_is_error(embed) for embed in embeds)


def _view_is_error(view) -> bool:
    """A button-less Components V2 message whose first text is an error."""
    walk = getattr(view, "walk_children", None)
    if walk is None:
        return False
    first = None
    for child in walk():
        if isinstance(child, (discord.ui.Button, discord.ui.Select)):
            return False
        if first is None and isinstance(child, discord.ui.TextDisplay):
            first = child.content
    return isinstance(first, str) and _strip_heading(first).startswith(ERROR_PREFIXES)


def _kwargs_are_error(content, kwargs: dict) -> bool:
    embeds = [e for e in (kwargs.get("embeds") or []) if isinstance(e, discord.Embed)]
    if isinstance(kwargs.get("embed"), discord.Embed):
        embeds.append(kwargs["embed"])
    if _looks_like_error(content, embeds):
        return True
    return _view_is_error(kwargs.get("view"))


def _components_are_error(components) -> bool:
    """Same check for the raw component payload of a message (list of dicts)."""
    texts: list[str] = []
    interactive = False

    def walk(items):
        nonlocal interactive
        for comp in items or []:
            if not isinstance(comp, dict):
                continue
            if comp.get("type") in _INTERACTIVE_COMPONENT_TYPES:
                interactive = True
            if comp.get("type") == 10 and isinstance(comp.get("content"), str):
                texts.append(comp["content"])
            walk(comp.get("components"))
            if isinstance(comp.get("accessory"), dict):
                walk([comp["accessory"]])

    walk(components)
    if interactive or not texts:
        return False
    return _strip_heading(texts[0]).startswith(ERROR_PREFIXES)


def _has_interactive(components) -> bool:
    for comp in components or []:
        if isinstance(comp, dict):
            if comp.get("type") in _INTERACTIVE_COMPONENT_TYPES:
                return True
            if _has_interactive(comp.get("components")):
                return True
    return False


def _payload_is_error(data: dict) -> bool:
    """Error check for a raw message payload (gateway create / update data)."""
    components = data.get("components")
    if _has_interactive(components):
        return False
    if _components_are_error(components):
        return True
    try:
        embeds = [discord.Embed.from_dict(e) for e in data.get("embeds") or []]
    except Exception:
        embeds = []
    return _looks_like_error(data.get("content"), embeds)


async def _delete_after_delay(delete_calls) -> None:
    await asyncio.sleep(ERROR_DELETE_DELAY)
    for call in delete_calls:
        try:
            await call()
        except Exception:
            pass


def _schedule(delete_calls, message_id: int | None = None) -> None:
    if message_id is not None:
        if message_id in _scheduled_deletes:
            return
        _scheduled_deletes.add(message_id)
    task = asyncio.create_task(_delete_after_delay(delete_calls))
    if message_id is not None:
        task.add_done_callback(lambda _t, mid=message_id: _scheduled_deletes.discard(mid))


class UtilitiesContext(commands.Context):
    """Context that removes error answers after ERROR_DELETE_DELAY seconds.

    When a command fails (missing permissions, missing arguments, bad input, cooldown, ...)
    the invoking message of the author and the error answer of the bot are both deleted.
    An answer counts as an error when the command raised (``error_mode``) or when the reply
    itself is an error message (starts with ❌ / 🚫 / ⛔, or a red ⚠️ embed).
    """

    error_mode: bool = False

    def _is_error_reply(self, content, kwargs: dict) -> bool:
        if self.error_mode:
            return True
        return _kwargs_are_error(content, kwargs)

    def cleanup_if_error(self, sent, content=None, kwargs: dict | None = None) -> None:
        """Schedules the deletion of ``sent`` (and the author's command) if it is an error reply."""
        if sent is None or not self._is_error_reply(content, kwargs or {}):
            return
        calls = [sent.delete]
        if self.interaction is None and not getattr(self, "_author_cleanup_scheduled", False):
            self._author_cleanup_scheduled = True
            calls.append(self.message.delete)
        _schedule(calls, getattr(sent, "id", None))

    async def send(self, content=None, **kwargs):
        sent = await super().send(content, **kwargs)
        self.cleanup_if_error(sent, content, kwargs)
        return sent

    async def reply(self, content=None, **kwargs):
        sent = await super().reply(content, **kwargs)
        self.cleanup_if_error(sent, content, kwargs)
        return sent


def _install_interaction_cleanup() -> None:
    """Error answers to buttons, menus, modals and slash commands (usually ephemeral) delete themselves too."""

    def schedule_original(interaction) -> None:
        _schedule([interaction.delete_original_response])

    original_send_message = discord.InteractionResponse.send_message
    original_edit_message = discord.InteractionResponse.edit_message
    original_edit_original = discord.Interaction.edit_original_response
    original_followup_send = discord.Webhook.send

    async def send_message(self, content=None, **kwargs):
        result = await original_send_message(self, content, **kwargs)
        try:
            if kwargs.get("delete_after") is None and _kwargs_are_error(content, kwargs):
                schedule_original(self._parent)
        except Exception:
            pass
        return result

    async def edit_message(self, **kwargs):
        result = await original_edit_message(self, **kwargs)
        try:
            if _kwargs_are_error(kwargs.get("content"), kwargs):
                schedule_original(self._parent)
        except Exception:
            pass
        return result

    async def edit_original_response(self, **kwargs):
        result = await original_edit_original(self, **kwargs)
        try:
            if _kwargs_are_error(kwargs.get("content"), kwargs):
                schedule_original(self)
        except Exception:
            pass
        return result

    async def followup_send(self, content=discord.utils.MISSING, **kwargs):
        result = await original_followup_send(self, content, **kwargs)
        try:
            if (
                isinstance(result, discord.WebhookMessage)
                and kwargs.get("delete_after") is None
                and _kwargs_are_error(None if content is discord.utils.MISSING else content, kwargs)
            ):
                _schedule([result.delete])
        except Exception:
            pass
        return result

    discord.InteractionResponse.send_message = send_message
    discord.InteractionResponse.edit_message = edit_message
    discord.Interaction.edit_original_response = edit_original_response
    discord.Webhook.send = followup_send


_install_interaction_cleanup()

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
            allowed_mentions=discord.AllowedMentions(replied_user=False),
            help_command=None,
            chunk_guilds_at_startup=False,
            member_cache_flags=discord.MemberCacheFlags.none(),
        )
        self.config = config
        self.poketwo_id = POKETWO_ID
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

        # Which cogs to load is defined in config.json ("COGS")
        self.cogs_dict = COGS

    async def get_context(self, origin, *, cls=UtilitiesContext):
        return await super().get_context(origin, cls=cls)

    async def invoke(self, ctx):
        """Same as Bot.invoke, but marks the context as failed before any error handler answers."""
        if ctx.command is None:
            return await super().invoke(ctx)
        self.dispatch("command", ctx)
        try:
            if await self.can_run(ctx, call_once=True):
                await ctx.command.invoke(ctx)
            else:
                raise commands.CheckFailure("The global check once functions failed.")
        except commands.CommandError as exc:
            if isinstance(ctx, UtilitiesContext):
                ctx.error_mode = True
            await ctx.command.dispatch_error(ctx, exc)
        else:
            self.dispatch("command_completion", ctx)

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

        # Register every hybrid command as a slash command with Discord.
        if self.config.get("SYNC_SLASH_COMMANDS", True):
            try:
                count = await self.sync_slash_commands()
                print(f"🔁 Synced {count} slash command(s).")
            except Exception as e:
                print(f"⚠️ Could not sync slash commands: {e}")

        self.tree.on_error = self.on_app_command_error

    async def sync_slash_commands(self) -> int:
        """Pushes the app command tree to Discord (global). Returns how many top-level commands exist."""
        synced = await self.tree.sync()
        return len(synced)

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        """Errors raised before a hybrid command runs (checks...) end up here.

        Failed checks (e.g. a non-owner using an owner-only command) stay completely silent."""
        error = getattr(error, "original", error)
        if isinstance(error, (app_commands.CheckFailure, commands.CheckFailure, commands.NotOwner)):
            return
        if isinstance(error, app_commands.CommandNotFound):
            return
        if isinstance(interaction.command, HybridAppCommand):
            return  # hybrid commands report errors through on_command_error below
        print(f"Unhandled error in slash command '{getattr(interaction.command, 'qualified_name', '?')}':")
        traceback.print_exception(type(error), error, error.__traceback__)
        try:
            if interaction.response.is_done():
                await interaction.followup.send("❌ Something went wrong running that command.", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Something went wrong running that command.", ephemeral=True)
        except discord.HTTPException:
            pass

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


def _is_command_text(text: str) -> bool:
    text = (text or "").lstrip()
    starts = [bot.prefix_str]
    if bot.user is not None:
        starts += [f"<@{bot.user.id}>", f"<@!{bot.user.id}>"]
    return bool(text) and text.startswith(tuple(starts))


@bot.listen("on_message")
async def delete_own_error_messages(message: discord.Message):
    """Any error the bot posts (even outside a command context) goes away after a few seconds,
    together with the prefix command it answered."""
    try:
        if bot.user is None or message.author.id != bot.user.id:
            return
        data = {
            "content": message.content,
            "embeds": [e.to_dict() for e in message.embeds],
            "components": [c.to_dict() for c in message.components],
        }
        if not _payload_is_error(data):
            return
        calls = [message.delete]
        reference = message.reference.resolved if message.reference else None
        if isinstance(reference, discord.Message) and not reference.author.bot and _is_command_text(reference.content):
            calls.append(reference.delete)
        _schedule(calls, message.id)
    except Exception:
        traceback.print_exc()


@bot.listen("on_raw_message_edit")
async def delete_own_edited_error_messages(payload: discord.RawMessageUpdateEvent):
    """Errors that appear by editing a bot message (button results, status messages) are removed too."""
    try:
        data = payload.data
        author_id = (data.get("author") or {}).get("id")
        if author_id is None and payload.cached_message is not None:
            author_id = payload.cached_message.author.id
        if bot.user is None or author_id is None or int(author_id) != bot.user.id:
            return
        if not _payload_is_error(data):
            return
        partial = bot.get_partial_messageable(payload.channel_id).get_partial_message(payload.message_id)
        _schedule([partial.delete], payload.message_id)
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