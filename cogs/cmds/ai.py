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

PROJECT_ROOT = os.path.dirname(CONFIG_PATH)

BOT_PREFIX = config.get("PREFIX", "!")
AI_CHATBOT_CHANNELID = int(config["AI_CHATBOT_CHANNELID"])
OWNER_IDS = set(config.get("OWNER_IDS", []))

# --- API keys ----------------------------------------------------------
_raw_keys = config.get("AI_CHATBOT_KEYS")
if not _raw_keys:
    _legacy_key = config.get("AI_CHATBOT")
    if not _legacy_key:
        raise ValueError(
            "config.json needs either 'AI_CHATBOT_KEYS' (a list of OpenRouter API keys) "
            "or the legacy 'AI_CHATBOT' (a single key string)."
        )
    _raw_keys = [_legacy_key]
if isinstance(_raw_keys, str):
    _raw_keys = [_raw_keys]

OPENROUTER_API_KEYS = [k for k in _raw_keys if k]
if not OPENROUTER_API_KEYS:
    raise ValueError("No usable OpenRouter API keys found in config.json.")

MAX_TOOL_ITERATIONS = 30       # limit for multi-step file edits
TOOL_CALL_MAX_TOKENS = 500    # budget for reasoning/tools
CHAT_MAX_TOKENS = 500
REQUEST_TIMEOUT_SECONDS = 180  # timeout for OpenRouter responses

SUBTASK_MAX_ITERATIONS = 6
SUBTASK_MAX_TOKENS = 500


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

        self.api_keys = OPENROUTER_API_KEYS
        self.primary_key = self.api_keys[0]
        self.worker_keys = self.api_keys[1:] if len(self.api_keys) > 1 else self.api_keys
        self._worker_key_cycle_idx = 0

        self.current_model = "openrouter/free"
        self.history = defaultdict(list)
        self.max_history = 10
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

    # -- API key pool & rotation ---------------------------------------------

    def _next_worker_key(self) -> str:
        """Round-robins across configured worker keys for parallel subtasks."""
        pool = self.worker_keys or [self.primary_key]
        key = pool[self._worker_key_cycle_idx % len(pool)]
        self._worker_key_cycle_idx += 1
        return key

    # -- low-level OpenRouter call --------------------------------------------

    async def _call_openrouter(
        self, key: str, messages: list, tools: list | None, max_tokens: int
    ) -> dict:
        """Sends one chat-completions request using a specific key."""
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://your-site-or-repo.com",
            "X-Title": "Utilities Discord Bot Assistant",
        }
        payload = {
            "model": self.current_model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools

        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self.openrouter_url, headers=headers, json=payload) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        raise RuntimeError(f"OpenRouter error ({resp.status}): {error_text}")
                    return await resp.json()
        except asyncio.TimeoutError as e:
            raise RuntimeError(
                f"OpenRouter didn't respond within {REQUEST_TIMEOUT_SECONDS}s "
                "(the model may be overloaded or slow right now — try again)."
            ) from e

    async def _call_openrouter_with_rotation(
        self, messages: list, tools: list | None, max_tokens: int
    ) -> dict:
        """Sends chat request. If a 429 Rate Limit error occurs, automatically rotates
        through all configured API keys until a working key is found."""
        start_key = self.primary_key
        start_idx = self.api_keys.index(start_key) if start_key in self.api_keys else 0
        n_keys = len(self.api_keys)

        last_error = None
        for offset in range(n_keys):
            key_idx = (start_idx + offset) % n_keys
            key = self.api_keys[key_idx]

            try:
                data = await self._call_openrouter(key, messages, tools, max_tokens)
                self.primary_key = key
                return data
            except RuntimeError as e:
                last_error = e
                if "429" in str(e) and n_keys > 1:
                    print(
                        f"[Key Rotation] Key #{key_idx + 1} hit 429 rate limit. "
                        f"Rotating to key #{((key_idx + 1) % n_keys) + 1}..."
                    )
                    continue
                raise e

        raise last_error

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
            worker_note = f"{len(self.api_keys)} total API keys loaded"
            capabilities.append(
                "DEVELOPER / OWNER MODE ENABLED:\n"
                "The user messaging you is the owner/developer of this bot. "
                "You have access to tools (`list_files`, `read_file`, `write_file`, `delete_file`, "
                "`load_extension`, `unload_extension`, `reload_extension`, `restart_bot`, "
                "`delegate_subtasks`) which let you inspect, modify, and reload/restart your own "
                f"code when requested. ({worker_note}.)\n\n"
                "Guidelines for self-editing tasks:\n"
                "- All paths are relative to the project root (where config.json lives).\n"
                "- MANDATORY: After completing all file edits, deletions, or extension reloads, "
                "you MUST send the user a plain text summary detailing what changes you made."
            )

        return "\n".join(capabilities)

    def get_owner_tools(self) -> list[dict]:
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
                    "description": "Deletes a file from the bot project directory.",
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
                    "description": "Unloads a currently loaded cog/extension.",
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
                    "description": "Restarts the entire bot process.",
                    "parameters": {"type": "object", "properties": {}}
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "delegate_subtasks",
                    "description": "Runs one or more independent, READ-ONLY investigation subtasks in parallel.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "tasks": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "string"},
                                        "instructions": {"type": "string"}
                                    },
                                    "required": ["id", "instructions"]
                                }
                            }
                        },
                        "required": ["tasks"]
                    }
                }
            }
        ]

    def get_worker_tools(self) -> list[dict]:
        read_only_names = {"list_files", "read_file"}
        return [t for t in self.get_owner_tools() if t["function"]["name"] in read_only_names]

    # -- tool execution -------------------------------------------------------

    async def execute_tool_call(self, tool_call: dict) -> str:
        fn_name = tool_call["function"]["name"]
        try:
            args = json.loads(tool_call["function"].get("arguments", "{}"))
        except json.JSONDecodeError as e:
            return f"Error: Invalid JSON arguments passed ({e})."

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
            async def _delayed_restart():
                await asyncio.sleep(2)
                os.execv(sys.executable, [sys.executable] + sys.argv)

            asyncio.create_task(_delayed_restart())
            return "Restart scheduled in ~2 seconds."

        elif fn_name == "delegate_subtasks":
            tasks = args.get("tasks", [])
            if not isinstance(tasks, list) or not tasks:
                return "Error: 'tasks' must be a non-empty list."

            valid_tasks = [t for t in tasks if isinstance(t, dict) and t.get("instructions")]
            if not valid_tasks:
                return "Error: no valid tasks provided."

            coros = [
                self._run_subtask(
                    str(t.get("id", f"task{i}")), t["instructions"], self._next_worker_key()
                )
                for i, t in enumerate(valid_tasks)
            ]
            results = await asyncio.gather(*coros)

            combined = "\n\n".join(
                f"### Subtask '{t.get('id', f'task{i}')}' result:\n{res}"
                for i, (t, res) in enumerate(zip(valid_tasks, results))
            )
            return combined

        return f"Unknown tool function: {fn_name}"

    async def _run_subtask(self, task_id: str, instructions: str, key: str) -> str:
        sub_messages = [
            {
                "role": "system",
                "content": (
                    "You are a sub-agent helping a primary assistant investigate part of a task. "
                    "You may only use list_files and read_file. Investigate and reply with a plain-text summary."
                ),
            },
            {"role": "user", "content": instructions},
        ]

        for _ in range(SUBTASK_MAX_ITERATIONS):
            try:
                data = await self._call_openrouter_with_rotation(
                    sub_messages, self.get_worker_tools(), SUBTASK_MAX_TOKENS
                )
            except RuntimeError as e:
                return f"[subtask failed: {e}]"

            if "choices" not in data or not data["choices"]:
                return "[subtask failed: empty response from OpenRouter]"

            choice_message = data["choices"][0]["message"]
            tool_calls = choice_message.get("tool_calls")

            if tool_calls:
                sub_messages.append(choice_message)
                for tool_call in tool_calls:
                    result = await self.execute_tool_call(tool_call)
                    sub_messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": result,
                    })
                continue

            content = (choice_message.get("content") or choice_message.get("reasoning") or "").strip()
            return content or "[subtask returned no text]"

        return f"[subtask hit iteration limit ({SUBTASK_MAX_ITERATIONS})]"

    # -- commands -------------------------------------------------------

    @commands.command(name="reset", help="Clears the AI chatbot memory for this channel.")
    async def reset_memory(self, ctx: commands.Context):
        if ctx.channel.id != AI_CHATBOT_CHANNELID:
            await ctx.send("The AI Chatbot is not active in this channel.")
            return

        if ctx.channel.id in self.history:
            self.history[ctx.channel.id].clear()
            await ctx.send("🧹 Conversation memory has been cleared!")
        else:
            await ctx.send("Memory is already empty.")

    @commands.command(name="ai-info", help="Displays model status, daily usage limits, and account balances for configured keys.")
    @commands.has_permissions(administrator=True)
    async def ai_info(self, ctx: commands.Context):
        """Fetches OpenRouter key metadata, free daily limits, balance, and usage for all keys."""
        timeout = aiohttp.ClientTimeout(total=15)

        embed = discord.Embed(title="🤖 AI Key Pool & Usage Status", color=discord.Color.blue())
        embed.add_field(name="Active Model", value=f"`{self.current_model}`", inline=False)
        embed.add_field(
            name="Configured Key Pool",
            value=f"`{len(self.api_keys)}` total API key(s) loaded",
            inline=False,
        )

        async with aiohttp.ClientSession(timeout=timeout) as session:
            for idx, key in enumerate(self.api_keys):
                is_active = " 🟢 (Primary)" if key == self.primary_key else ""
                role_label = f"Key #{idx + 1}{is_active}"
                headers = {"Authorization": f"Bearer {key}"}

                try:
                    async with session.get(self.key_info_url, headers=headers) as resp:
                        if resp.status != 200:
                            embed.add_field(
                                name=role_label, value=f"❌ Failed to fetch info (HTTP {resp.status})", inline=False
                            )
                            continue

                        res = await resp.json()
                        data = res.get("data", {})

                        label = data.get("label", "Unnamed Key")
                        usage_usd = data.get("usage", 0.0)
                        is_free_tier = data.get("is_free_tier", True)
                        rate_limit = data.get("rate_limit", {})

                        if rate_limit and rate_limit.get("requests") and rate_limit.get("interval"):
                            daily_free_usage = f"{rate_limit.get('requests')} req / {rate_limit.get('interval')}"
                        else:
                            daily_free_usage = "50 req/day" if is_free_tier else "1,000 req/day"

                        fields = [
                            f"**Label:** `{label}`",
                            f"**Total Spent:** `${usage_usd:.4f}`",
                            f"**Daily Free Usage:** `{daily_free_usage}`",
                        ]

                        embed.add_field(
                            name=role_label,
                            value="\n".join(fields),
                            inline=False,
                        )
                except Exception as e:
                    embed.add_field(name=role_label, value=f"❌ Network Error: `{e}`", inline=False)

        await ctx.send(embed=embed)

    @ai_info.error
    async def ai_info_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ You must have Administrator permissions to use this command.")

    # -- main listener -------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.channel.id != AI_CHATBOT_CHANNELID:
            return

        if message.content.startswith(BOT_PREFIX):
            return

        channel_id = message.channel.id
        lock = self._channel_locks[channel_id]

        async with lock:
            try:
                await self._handle_ai_message(message)
            except Exception:
                tb = traceback.format_exc()
                print(f"Unhandled exception in AIChat.on_message:\n{tb}")
                try:
                    await message.reply(
                        "❌ Something went wrong handling that message. Check console logs."
                    )
                except Exception:
                    pass

    async def _handle_ai_message(self, message: discord.Message):
        channel_id = message.channel.id

        async with message.channel.typing():
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
            self.history[channel_id] = self.history[channel_id][-self.max_history:]

            current_payload_messages = [{"role": "system", "content": system_instruction}] + self.history[channel_id]

            reply_sent = False

            for iteration in range(MAX_TOOL_ITERATIONS):
                tools_for_call = self.get_owner_tools() if is_owner else None
                max_tokens_for_call = TOOL_CALL_MAX_TOKENS if is_owner else CHAT_MAX_TOKENS

                try:
                    data = await self._call_openrouter_with_rotation(
                        current_payload_messages, tools_for_call, max_tokens_for_call
                    )
                except RuntimeError as e:
                    print(f"OpenRouter request failed: {e}")
                    await message.reply(f"❌ {e}")
                    reply_sent = True
                    break

                if "choices" not in data or not data["choices"]:
                    print(f"OpenRouter returned no choices: {data}")
                    await message.reply("Sorry, I got an empty response from the AI backend.")
                    reply_sent = True
                    break

                choice = data["choices"][0]
                choice_message = choice["message"]
                finish_reason = choice.get("finish_reason")

                if finish_reason == "length":
                    if choice_message.get("tool_calls"):
                        await message.reply(
                            "⚠️ The AI's response was cut off before it finished. Try again with a narrower request."
                        )
                    else:
                        await message.reply(
                            "⚠️ The AI ran out of tokens before it could act on your request."
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
                    continue

                reply_text = (
                    choice_message.get("content")
                    or choice_message.get("reasoning")
                    or choice_message.get("thinking")
                    or ""
                )

                if not reply_text.strip() and current_payload_messages and current_payload_messages[-1].get("role") == "tool":
                    current_payload_messages.append({
                        "role": "user",
                        "content": "All tool actions are completed. Please provide a brief plain text summary of what changes were made."
                    })
                    try:
                        summary_data = await self._call_openrouter_with_rotation(
                            current_payload_messages, None, CHAT_MAX_TOKENS
                        )
                        if "choices" in summary_data and summary_data["choices"]:
                            summary_msg = summary_data["choices"][0]["message"]
                            reply_text = (
                                summary_msg.get("content")
                                or summary_msg.get("reasoning")
                                or summary_msg.get("thinking")
                                or ""
                            )
                    except Exception as e:
                        print(f"Failed to retrieve fallback summary: {e}")

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
                print(f"AIChat: hit MAX_TOOL_ITERATIONS ({MAX_TOOL_ITERATIONS}) in channel {channel_id}.")
                await message.reply("⚠️ Task hit tool call iteration limits.")
                reply_sent = True

            if not reply_sent:
                await message.reply("❌ Something unexpected happened and no response was generated.")


async def setup(bot):
    await bot.add_cog(AIChat(bot))