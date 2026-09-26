import asyncio
import json
import os
import sys
import traceback
from collections import defaultdict

import aiohttp
import discord
from discord.ext import commands

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

_CANDIDATE_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "config.json"
)

if os.path.exists(_CANDIDATE_CONFIG_PATH):
    CONFIG_PATH = os.path.abspath(_CANDIDATE_CONFIG_PATH)
else:
    CONFIG_PATH = os.path.abspath("config.json")

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = json.load(f)

# PROJECT_ROOT is derived from where config.json actually lives, NOT from
# os.getcwd(). This matters because bot-hosting.net (and some process
# managers) can launch the process from a different working directory than
# the one you'd get running it from VSC. Anchoring to config.json's location
# means self-edit tools resolve the same project root no matter how/where
# the bot was started.
PROJECT_ROOT = os.path.dirname(CONFIG_PATH)

BOT_PREFIX = config.get("PREFIX", "!")
AI_CHANNEL_ID = int(config["AI_CHATBOT_CHANNELID"])
OPENROUTER_API_KEY = config["AI_CHATBOT"]
OWNER_IDS = set(config.get("OWNER_IDS", []))

MAX_TOOL_ITERATIONS = 15     # was 5 — too low for multi-step file edits
TOOL_CALL_MAX_TOKENS = 4096  # was 1500 — full source files need more room
CHAT_MAX_TOKENS = 1500


def split_message(text: str, limit: int = 2000) -> list[str]:
    """Splits a long message string into chunks under Discord's character limit."""
    if len(text) <= limit:
        return [text]

    chunks = []
    while len(text) > limit:
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = text.rfind(" ", 0, limit)
        if split_at == -1:
            split_at = limit

        chunks.append(text[:split_at].strip())
        text = text[split_at:].lstrip()

    if text:
        chunks.append(text)

    return chunks


class AIChat(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.openrouter_url = "https://openrouter.ai/api/v1/chat/completions"
        self.key_info_url = "https://openrouter.ai/api/v1/key"
        self.api_key = OPENROUTER_API_KEY
        self.current_model = "deepseek/deepseek-v4-flash-vision-exp"
        # Memory store: channel_id -> list of message dicts
        self.history = defaultdict(list)
        self.max_history = 10  # Store up to 10 back-and-forth messages
        # Prevents two messages in the same channel from interleaving their
        # tool-call loops and corrupting shared history state.
        self._channel_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    # -- path safety -------------------------------------------------------

    def _resolve_path(self, relative_path: str) -> str:
        """Resolves a user/model-supplied relative path against PROJECT_ROOT."""
        return os.path.abspath(os.path.join(PROJECT_ROOT, relative_path))

    def _is_safe_path(self, target_path: str) -> bool:
        """Security check to ensure code modifications stay inside the project directory."""
        abs_target = self._resolve_path(target_path)
        root = os.path.abspath(PROJECT_ROOT)
        return abs_target == root or abs_target.startswith(root + os.sep)

    # -- prompt / tool definitions ------------------------------------------

    def generate_bot_capabilities_prompt(self, is_owner: bool) -> str:
        """Inspects all cogs and commands dynamically to build system context."""
        capabilities = ["Here is a summary of available server commands you can explain to users:\n"]

        for cog_name, cog in self.bot.cogs.items():
            capabilities.append(f"### Cog: {cog_name}")
            for cmd in cog.get_commands():
                if cmd.hidden:
                    continue

                desc = cmd.help or cmd.brief or "No description provided."
                signature = f"{BOT_PREFIX}{cmd.name} {cmd.signature}".strip()
                capabilities.append(f"- `{signature}`: {desc}")
            capabilities.append("")

        if is_owner:
            capabilities.append(
                "DEVELOPER / OWNER MODE ENABLED:\n"
                "The user messaging you is the owner/developer of this bot. "
                "You have access to tools (`list_files`, `read_file`, `write_file`, `delete_file`, "
                "`load_extension`, `unload_extension`, `reload_extension`, `restart_bot`) which let you "
                "inspect, modify, and reload/restart your own code when requested.\n\n"
                "Guidelines for self-editing tasks:\n"
                "- All paths are relative to the project root (where config.json lives), regardless of "
                "the process's current working directory.\n"
                "- Extension/cog names use dotted module paths derived from the file path relative to "
                "project root, e.g. a file at 'cogs/afk.py' is extension 'cogs.afk'.\n"
                "- To merge or rename a file: read the source file(s) with `read_file`, `write_file` the "
                "new merged content to the target file, `delete_file` any file(s) that are no longer "
                "needed, then `unload_extension` any removed module and `reload_extension` (or "
                "`load_extension` if it wasn't previously loaded) the resulting module so the change "
                "takes effect immediately.\n"
                "- Only use `restart_bot` if a full process restart is actually necessary (e.g. changes "
                "to files that aren't cogs, like config loading or the main entrypoint) — prefer "
                "reload_extension/unload_extension/load_extension for cog changes since they apply "
                "instantly without dropping the connection.\n"
                "- After finishing file edits and reload/restart, always send the user a short plain "
                "text summary of what you did."
            )

        return "\n".join(capabilities)

    def get_owner_tools(self) -> list[dict]:
        """Tool definitions passed to OpenRouter for code self-editing."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "list_files",
                    "description": "Lists files and subdirectories in the bot codebase, relative to the project root.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Relative directory path (default is '.')."}
                        }
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Reads the content of a file in the bot project directory.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string", "description": "Relative file path from project root."}
                        },
                        "required": ["file_path"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "Creates or overwrites a source code file in the bot project with the given content.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string", "description": "Relative file path from project root."},
                            "content": {"type": "string", "description": "Full new code/content for the file."}
                        },
                        "required": ["file_path", "content"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "delete_file",
                    "description": "Deletes a file from the bot project directory (e.g. an old file being merged away).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string", "description": "Relative file path from project root."}
                        },
                        "required": ["file_path"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "load_extension",
                    "description": "Loads a cog/extension that is not currently loaded.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "extension_name": {"type": "string", "description": "Dotted module path (e.g. 'cogs.afk')."}
                        },
                        "required": ["extension_name"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "unload_extension",
                    "description": "Unloads a currently loaded cog/extension (e.g. before deleting its file).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "extension_name": {"type": "string", "description": "Dotted module path (e.g. 'cogs.setafk')."}
                        },
                        "required": ["extension_name"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "reload_extension",
                    "description": "Reloads a bot cog/extension dynamically so changes take effect immediately.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "extension_name": {"type": "string", "description": "Dotted module path (e.g. 'cogs.afk')."}
                        },
                        "required": ["extension_name"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "restart_bot",
                    "description": (
                        "Restarts the entire bot process. Use only when a change can't be applied via "
                        "load/unload/reload_extension (e.g. changes to config loading or the entrypoint "
                        "script itself). Works whether hosted on bot-hosting.net or run locally, since it "
                        "re-execs the same Python process."
                    ),
                    "parameters": {"type": "object", "properties": {}}
                }
            }
        ]

    # -- tool execution -------------------------------------------------------

    async def execute_tool_call(self, tool_call: dict) -> str:
        """Executes the tool call requested by OpenRouter and returns the result string."""
        fn_name = tool_call["function"]["name"]
        try:
            args = json.loads(tool_call["function"].get("arguments", "{}"))
        except json.JSONDecodeError as e:
            return f"Error: Invalid JSON arguments passed ({e}). Re-send this tool call with valid JSON."

        if fn_name == "list_files":
            rel_path = args.get("path", ".")
            if not self._is_safe_path(rel_path):
                return "Error: Permission denied (path outside project directory)."
            try:
                full_path = self._resolve_path(rel_path)
                files = os.listdir(full_path)
                return json.dumps(files, indent=2)
            except Exception as e:
                return f"Error listing directory: {e}"

        elif fn_name == "read_file":
            rel_path = args.get("file_path", "")
            if not self._is_safe_path(rel_path):
                return "Error: Permission denied (path outside project directory)."
            try:
                full_path = self._resolve_path(rel_path)
                with open(full_path, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception as e:
                return f"Error reading file '{rel_path}': {e}"

        elif fn_name == "write_file":
            rel_path = args.get("file_path", "")
            content = args.get("content", "")
            if not self._is_safe_path(rel_path):
                return "Error: Permission denied (path outside project directory)."
            try:
                full_path = self._resolve_path(rel_path)
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                with open(full_path, "w", encoding="utf-8") as f:
                    f.write(content)
                return f"Successfully wrote file: {rel_path}"
            except Exception as e:
                return f"Error writing file '{rel_path}': {e}"

        elif fn_name == "delete_file":
            rel_path = args.get("file_path", "")
            if not self._is_safe_path(rel_path):
                return "Error: Permission denied (path outside project directory)."
            try:
                full_path = self._resolve_path(rel_path)
                os.remove(full_path)
                return f"Successfully deleted file: {rel_path}"
            except Exception as e:
                return f"Error deleting file '{rel_path}': {e}"

        elif fn_name == "load_extension":
            ext = args.get("extension_name", "")
            try:
                await self.bot.load_extension(ext)
                return f"Successfully loaded extension '{ext}'!"
            except Exception as e:
                return f"Error loading extension '{ext}': {e}"

        elif fn_name == "unload_extension":
            ext = args.get("extension_name", "")
            try:
                await self.bot.unload_extension(ext)
                return f"Successfully unloaded extension '{ext}'!"
            except Exception as e:
                return f"Error unloading extension '{ext}': {e}"

        elif fn_name == "reload_extension":
            ext = args.get("extension_name", "")
            try:
                await self.bot.reload_extension(ext)
                return f"Successfully reloaded extension '{ext}'!"
            except Exception as e:
                return f"Error reloading extension '{ext}': {e}"

        elif fn_name == "restart_bot":
            # Re-exec the same interpreter/args in-place. This works regardless
            # of host (bot-hosting.net, systemd, a plain VSC-launched process)
            # because it doesn't depend on any host-specific process manager
            # API — it just replaces the current process image. We delay it
            # slightly so the tool result / final reply can be sent first.
            async def _delayed_restart():
                await asyncio.sleep(2)
                os.execv(sys.executable, [sys.executable] + sys.argv)

            asyncio.create_task(_delayed_restart())
            return "Restart scheduled in ~2 seconds. The process will relaunch itself."

        return f"Unknown tool function: {fn_name}"

    # -- commands -------------------------------------------------------

    @commands.command(name="reset", help="Clears the AI chatbot memory for this channel.")
    async def reset_memory(self, ctx: commands.Context):
        """Resets stored conversation history for the channel."""
        if ctx.channel.id != AI_CHANNEL_ID:
            await ctx.send("The AI Chatbot is not active in this channel.")
            return

        if ctx.channel.id in self.history:
            self.history[ctx.channel.id].clear()
            await ctx.send("🧹 Conversation memory has been cleared!")
        else:
            await ctx.send("Memory is already empty.")

    @commands.command(name="ai-info", help="Displays current active AI model and API key usage details.")
    async def ai_info(self, ctx: commands.Context):
        """Fetches AI model information and spending/usage stats from OpenRouter."""
        headers = {"Authorization": f"Bearer {self.api_key}"}
        timeout = aiohttp.ClientTimeout(total=15)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(self.key_info_url, headers=headers) as resp:
                    if resp.status == 200:
                        res = await resp.json()
                        data = res.get("data", {})

                        label = data.get("label", "Unnamed Key")
                        usage_usd = data.get("usage", 0.0)
                        limit = data.get("limit")
                        limit_remaining = data.get("limit_remaining")
                        limit_reset = data.get("limit_reset")
                        is_free_tier = data.get("is_free_tier", False)

                        limit_str = f"${limit:.4f}" if limit is not None else "Unlimited"
                        remaining_str = f"${limit_remaining:.4f}" if limit_remaining is not None else "Unlimited"
                        reset_str = limit_reset.title() if limit_reset else "No automatic reset"

                        embed = discord.Embed(
                            title="🤖 AI Chatbot Information & Usage",
                            color=discord.Color.blue()
                        )
                        embed.add_field(name="Active Model", value=f"`{self.current_model}`", inline=False)
                        embed.add_field(name="Key Label", value=f"`{label}`", inline=False)
                        embed.add_field(name="Total Usage", value=f"`${usage_usd:.4f}` USD", inline=True)
                        embed.add_field(name="Credit Limit", value=f"`{limit_str}`", inline=True)
                        embed.add_field(name="Remaining Budget", value=f"`{remaining_str}`", inline=True)
                        embed.add_field(name="Limit Reset Policy", value=f"`{reset_str}`", inline=True)
                        embed.add_field(name="Free Tier Account", value=f"`{is_free_tier}`", inline=True)

                        await ctx.send(embed=embed)
                    else:
                        error_text = await resp.text()
                        print(f"OpenRouter Usage Error ({resp.status}): {error_text}")
                        await ctx.send(f"❌ Failed to retrieve API usage details (Status {resp.status}).")

        except Exception as e:
            print(f"Usage Exception: {e}")
            await ctx.send("❌ Encountered a network error while fetching AI details.")

    # -- main listener -------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore messages from bots or outside the configured AI channel
        if message.author.bot or message.channel.id != AI_CHANNEL_ID:
            return

        # Ignore messages starting with the command prefix so commands process normally
        if message.content.startswith(BOT_PREFIX):
            return

        channel_id = message.channel.id
        lock = self._channel_locks[channel_id]

        async with lock:
            try:
                await self._handle_ai_message(message)
            except Exception:
                # Make sure a bug in this handler is never silent again: it
                # gets printed AND the user gets told something went wrong.
                tb = traceback.format_exc()
                print(f"Unhandled exception in AIChat.on_message:\n{tb}")
                try:
                    await message.reply(
                        "❌ Something went wrong handling that message. Check the console/logs for details."
                    )
                except Exception:
                    pass

    async def _handle_ai_message(self, message: discord.Message):
        channel_id = message.channel.id

        async with message.channel.typing():
            # Check if sender is bot owner
            is_owner = (await self.bot.is_owner(message.author)) or (message.author.id in OWNER_IDS)

            capabilities_info = self.generate_bot_capabilities_prompt(is_owner)

            system_instruction = (
                "You are Utilities, a dedicated Discord bot for server management and Pokétwo assistance. "
                "Your main capabilities include:\n"
                "- Server moderation and utility operations.\n"
                "- Pokétwo assistance: automatically identifying and naming spawned Pokémon.\n"
                "- Supporting shiny hunting, Pokémon collection tracking, and rare/regional spawn pings.\n\n"
                "Answer user questions accurately, maintain a friendly and helpful tone, and guide users "
                "on how to navigate server features and bot commands.\n\n"
                f"{capabilities_info}"
            )

            # Build user content payload (Handling Text + Images)
            image_urls = [
                att.url for att in message.attachments
                if (att.content_type and att.content_type.startswith("image/"))
                or att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))
            ]

            if image_urls:
                user_content = []
                text_prompt = message.content.strip() if message.content else "Please look at the attached image(s)."
                user_content.append({"type": "text", "text": text_prompt})

                for url in image_urls:
                    user_content.append({"type": "image_url", "image_url": {"url": url}})

                user_message_dict = {"role": "user", "content": user_content}
            else:
                user_message_dict = {"role": "user", "content": message.content}

            self.history[channel_id].append(user_message_dict)

            # Keep recent history within limits
            self.history[channel_id] = self.history[channel_id][-self.max_history:]

            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://your-site-or-repo.com",
                "X-Title": "Utilities Discord Bot Assistant"
            }

            timeout = aiohttp.ClientTimeout(total=90)

            current_payload_messages = [{"role": "system", "content": system_instruction}] + self.history[channel_id]

            reply_sent = False

            for iteration in range(MAX_TOOL_ITERATIONS):
                payload = {
                    "model": self.current_model,
                    "messages": current_payload_messages,
                    "max_tokens": TOOL_CALL_MAX_TOKENS if is_owner else CHAT_MAX_TOKENS,
                }

                if is_owner:
                    payload["tools"] = self.get_owner_tools()

                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(self.openrouter_url, headers=headers, json=payload) as resp:
                        if resp.status != 200:
                            error_text = await resp.text()
                            print(f"OpenRouter Error ({resp.status}): {error_text}")
                            await message.reply("Sorry, I had trouble generating a response.")
                            reply_sent = True
                            break

                        data = await resp.json()

                        if "choices" not in data or not data["choices"]:
                            print(f"OpenRouter returned no choices: {data}")
                            await message.reply("Sorry, I got an empty response from the AI backend.")
                            reply_sent = True
                            break

                        choice = data["choices"][0]
                        choice_message = choice["message"]
                        finish_reason = choice.get("finish_reason")

                        # If the model got cut off mid tool-call (arguments
                        # truncated), tell the user instead of silently
                        # continuing on broken JSON.
                        if finish_reason == "length" and choice_message.get("tool_calls"):
                            await message.reply(
                                "⚠️ The AI's response was cut off before it finished (likely writing a "
                                f"large file). Try again with a narrower request, or raise "
                                f"TOOL_CALL_MAX_TOKENS (currently {TOOL_CALL_MAX_TOKENS})."
                            )
                            reply_sent = True
                            break

                        tool_calls = choice_message.get("tool_calls")
                        if tool_calls and is_owner:
                            current_payload_messages.append(choice_message)
                            for tool_call in tool_calls:
                                tool_result = await self.execute_tool_call(tool_call)
                                current_payload_messages.append({
                                    "role": "tool",
                                    "tool_call_id": tool_call["id"],
                                    "content": tool_result
                                })
                            # Loop back to let model process tool outputs and provide final response
                            continue

                        # Standard text response (Safe handling for null/None content)
                        reply_text = choice_message.get("content") or ""
                        if reply_text.strip():
                            self.history[channel_id].append({"role": "assistant", "content": reply_text})

                            chunks = split_message(reply_text)
                            for i, chunk in enumerate(chunks):
                                if i == 0:
                                    await message.reply(chunk)
                                else:
                                    await message.channel.send(chunk)
                        else:
                            await message.reply("⚠️ Action completed, but no text summary was generated by the AI.")

                        reply_sent = True
                        break
            else:
                # The for-loop exhausted MAX_TOOL_ITERATIONS without ever
                # breaking (i.e. the model kept calling tools). Previously
                # this fell through silently with no reply and no error.
                print(
                    f"AIChat: hit MAX_TOOL_ITERATIONS ({MAX_TOOL_ITERATIONS}) in channel {channel_id} "
                    "without a final reply."
                )
                await message.reply(
                    "⚠️ That task needed more tool calls than I'm allowed to make in one go "
                    f"(limit: {MAX_TOOL_ITERATIONS}). It may be partially done — check the console/logs, "
                    "or try breaking the request into smaller steps."
                )
                reply_sent = True

            if not reply_sent:
                # Defensive fallback — should not normally be reachable.
                await message.reply("❌ Something unexpected happened and no response was generated.")


async def setup(bot):
    await bot.add_cog(AIChat(bot))