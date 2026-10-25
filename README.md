# 🛠️ Utilities

**Utilities** is an all-in-one Discord bot for server management, community engagement and [Pokétwo](https://poketwo.net) assistance. It combines moderation, ticketing, verification, giveaways and an AI chatbot with a full Pokétwo toolkit: automatic Pokémon recognition, spawn pings, autolocks, catch timers, a starboard and catch leaderboards.

Every command works both as a **prefix command** (default prefix `.`) and as a **slash command**, and nearly every configuration screen is an interactive button/select-menu panel.

---

## ✨ Features

### 🧩 Core behaviour
- **Hybrid commands** – every command works as `.command` and as `/command`.
- **Case-insensitive commands**, plus the bot mention works as a prefix (`@Utilities help`).
- **Edit to re-run** – editing a command message re-runs the command.
- **Self-cleaning errors** – error replies (❌ / 🚫 / ⛔ and red ⚠️ embeds), including the command that caused them, delete themselves after 5 seconds. This also covers ephemeral replies from buttons, menus and modals.
- **Auto-generated help** – `.help` reads the loaded cogs, so it never goes out of date. It separates user, admin and Pokétwo commands, supports `.help <command>`, `.help <cog>` and `.help <group> <sub>`, and hides owner-only commands from everyone else.
- **Modern UI** – Discord Components V2 layouts, paginators, confirmation dialogs and loading indicators.
- **MongoDB persistence** – one shared async client (Motor) for every cog; giveaways, tickets, reminders and timers survive restarts.
- **Modular cogs** – which cogs load is controlled from `config.json`, so features can be switched on or off without touching the code.
- **Live updates** – the owner can pull the latest code from git and restart from Discord; after the restart the bot edits the progress message to "Update completed".

### 🔮 Pokétwo toolkit

| Feature | Description |
| --- | --- |
| **Pokémon recognition** | Identifies spawned Pokémon from the image with a quantized CLIP vision model running on ONNX Runtime. `.rec` identifies a Pokémon from an image on demand. Correct/wrong predictions can be logged to review channels. |
| **Hint solver** | Solves Pokétwo hint messages from a Pokémon name list stored in MongoDB. |
| **Spawn pings** | Pings the right people the moment a Pokémon spawns (see below). |
| **Autolock** | Locks a channel when a pinged Pokémon spawns, so only the right person can catch it. |
| **Autolock DMs** | Optional DMs when a channel is autolocked and a configurable time before it auto-unlocks. |
| **Catch timers** | Per-category timers that announce when a locked Pokémon can be caught by everyone, and detect steals. |
| **Starboard** | Posts notable catches (✨ Shiny, 💥 Gigantamax, 📈 High IV, 📉 Low IV, 💎 Rare, 🌍 Regional) to a channel of your choice. |
| **Catch statistics** | Per-user catch stats and a daily / weekly / monthly / all-time server leaderboard. Two servers can be linked so their catches merge. |
| **AFK mode** | Members can go AFK and choose which ping types they still want to receive. |
| **Dex** | Pokédex lookup by name or number, with a shiny view. |
| **Utilities** | Extract names/IDs from Pokétwo embeds and read the creation time hidden inside a MongoDB ObjectId. |

**Ping types** – everything below is looked up automatically on every spawn:

- **Shiny Hunt (`sh`)** – each member sets their target Pokémon.
- **Collection List (`cl`)** – add/remove/clear/list Pokémon you collect.
- **Reserves (`reserves`)** – admins reserve Pokémon for specific members.
- **Type Pings (`tp`)** – ping for chosen Pokémon types.
- **Region Pings (`rp`)** – ping for chosen regions, plus Gigantamax, Paradox and Eeveelutions.
- **Role pings** – Rare, Regional, Gigantamax, Paradox and Eeveelutions roles configured per server.

**Autolock controls** – every lock type (Reserves, Shiny Hunt, Collection, Type, Region, Rare, Regional, Gigantamax, Paradox, Eeveelutions) can be tuned separately:

- Lock **delay** (or lock instantly) and **lock time** (automatic unlock after a set time).
- **Whitelist** of channels or categories (or the whole server) where the lock may trigger.
- **Restrict unlockers** – only the pinged user(s) may unlock. Priority is reserves > shiny hunt > collection > the rest.
- A server-wide **standard** profile that can be copied onto any lock.
- **Pokémon naming** can be toggled per channel or per server.
- Manual `.lock` / `.unlock`, `.unlockallchannels` and `.lockstats` overview of every locked channel.

### 🛡️ Moderation
- `kick`, `ban`, `mute` / `unmute` (timed, uses a configurable mute role), `nick`.
- **Warning system** – `warn`, `warns`, removable by ID. History lookups with `kicked`, `banned` and `mutelogs`.
- **Automod trap channel** – anyone who is not an administrator and talks in the trap channel is kicked or banned instantly. Includes a warning message with a live kick/ban counter and a punishment log.
- **Message tools** – `purge` (all messages or one user), `pin` / `unpin` / `pins`, `forward`, `echo`, `timediff` (time between two messages).
- **Snipe** – shows the last deleted messages in a channel (up to 10), including bulk deletions.
- **Sticky messages** – `stick`, `unstick` and `sticks` to keep a message at the bottom of a channel.
- **Member info** – `whois` (alias `userinfo`), `membercount`, and per-user `messages` counts.

### 🏗️ Server management
- **Channels** – create, delete, rename, move, lock, unlock, hide and unhide text channels, plus `nuke` (clone and replace a channel).
- **Categories** – create, delete, rename, lock, unlock, hide and unhide categories with permission sync.
- **Roles** – `giverole` (optionally time-limited), `removerole`, `createrole` (colour, permissions, initial members), `deleterole` and `setperms`.
- Step-by-step prompts when channel/category commands are run without arguments, and names with spaces are supported.

### 🎉 Community
- **✅ Verification** – image-CAPTCHA verification panel with unverified/verified roles, configurable CAPTCHA length, attempt limit, expiry and fully customisable panel text.
- **🎫 Tickets** – publishable support panels with multiple categories, claim system, close confirmation, reopen/delete, per-category staff pings, optional auto-delete after closing and full **Discord-style HTML transcripts** (avatars, embeds, attachments, reactions, dark/light theme) saved when a ticket is deleted.
- **🎁 Giveaways** – `giveaway create | end | list | participants | reroll`. Giveaways are persistent, expired ones are ended automatically (even after downtime) and the Enter button keeps working after restarts.
- **📨 Invite tracker** – shows who invited whom (total, still here, left, rejoins) and detects vanity URLs and invites that hit their max uses.
- **👋 Joins** – welcome messages (with "Invited by ..."), greet pings and autoroles, all configured from one panel.
- **🎲 Guess the Number** – admins start a game, everyone guesses by typing a number, and `gtn hint` narrows the range.

### 🤖 AI chatbot
- Powered by **OpenRouter**, with support for several API keys (rotated automatically), per-account quota tracking and a configurable list of fallback models.
- Admins choose which channels the chatbot is active in (`ai config`, `ai enable`, `ai disable`, `ai add`, `ai remove`).
- Per-user, per-channel **memory** of recent messages (`ai memory`, `ai reset`).
- Understands **image attachments**.
- Knows the bot's own commands and can guide members through its features.
- **Owner mode** – the owner can give the bot file-management and extension-management tools (list/read/write/delete files, load/unload/reload cogs, restart). Every mutating action needs explicit owner approval through a button prompt.
- `ai info` shows model status and daily usage for the owner.

### ⏰ Utilities
- `remind` / `reminders` – personal reminders, listed and removable by ID.
- `timer` – start a timer and get pinged when it ends.
- `roll` – random number (1–100 by default, or a custom range).
- `ping` – bot latency.
- `invites` – your invite statistics.

### 👑 Owner tools
- `pull` – pull the latest code from git and restart (`force`, `hard` options), with live progress.
- `reload`, `sync` (slash commands), `restart`.
- `stats` – bot, process and host system statistics. `gitstats` – current branch and commit.
- `grinder` – configuration manager for an external grinder engine (backed by Firebase).

---

## 📋 Command overview

<details>
<summary><b>Click to expand the full command list</b></summary>

| Category | Commands |
| --- | --- |
| **General** | `help`, `ping` |
| **Utilities** | `remind` (`rm`, `remindme`), `reminders`, `timer`, `roll`, `ai` (`config`, `enable`, `disable`, `add`, `remove`, `reset`, `memory`, `info`) |
| **Moderation** | `kick`, `ban`, `mute`, `unmute`, `muterole`, `nick`, `warn`, `warns`, `kicked`, `banned`, `mutelogs`, `whois` (`userinfo`, `ui`), `membercount` (`mc`), `automod` (`am`: `set`, `action`, `warn`, `disable`) |
| **Messages** | `messages` (`msgcount`), `timediff` (`td`), `forward` (`fwd`), `snipe`, `purge`, `pin`, `unpin`, `pins`, `echo`, `stick`, `unstick`, `sticks` |
| **Server** | `createchannel`, `deletechannel`, `renamechannel`, `movechannel`, `lockchannel`, `unlockchannel`, `hidechannel`, `unhidechannel`, `nuke`, `createcategory`, `deletecategory`, `renamecategory`, `lockcategory`, `unlockcategory`, `hidecategory`, `unhidecategory`, `giverole`, `removerole`, `createrole`, `deleterole`, `setperms` |
| **Community** | `verify`, `tickets`, `giveaway` (`gw`), `invites` (`inv`), `joins`, `gtn` |
| **Pokétwo – pings** | `sh`, `cl` (`add`, `remove`, `clear`, `list`), `reserves` (`list`, `all`, `add`, `remove`, `clear`), `tp`, `rp`, `afk` |
| **Pokétwo – locks** | `lock`, `unlock`, `unlockallchannels`, `lockstats`, `autolockdm` |
| **Pokétwo – config** | `config autolock`, `config timer`, `set` (`lockdelay`, `locktime`, `whitelist`, `restrictunlockers`, `use-standard`, `shtimer`, `cltimer`, `rptimer`, `tptimer`, `rarerole`, `regionalrole`, `gigantamaxrole`, `paradoxrole`, `eeveelutionsrole`), `toggle` (`naming`, `lockdelay`, `locktime`, `restrictunlockers`, `timer`, `shtimer`, `cltimer`, `rptimer`, `tptimer`), `settings`, `channelsettings`, `serversettings`, `starboard` |
| **Pokétwo – tools** | `rec`, `dex`, `extract`, `catchtime`, `catches`, `catchleaderboard`, `catcheslink` |
| **Owner** | `pull`, `reload`, `sync`, `restart`, `stats`, `gitstats`, `grinder` |

Use `.help <command>` in Discord for arguments, aliases and examples.

</details>

---

## 🚀 Getting started

### Requirements
- Python **3.12** (the project is developed and tested on 3.12)
- A [MongoDB](https://www.mongodb.com/) database (Atlas works fine)
- A Discord bot token with the **Message Content** and **Server Members** privileged intents enabled
- Optional: one or more [OpenRouter](https://openrouter.ai) API keys for the AI chatbot
- Optional: a Firebase service account (only for the owner `grinder` / `pull grinder` features)

### Installation

```bash
git clone <your-repository-url>
cd <your-repository-folder>
pip install -r requirements.txt
```

Dependencies: `discord.py`, `aiohttp`, `numpy`, `firebase-admin`, `pymongo`, `motor`, `certifi`, `psutil`, `onnxruntime`, `Pillow`.

### Configuration

Create a `config.json` next to `main.py`:

```json
{
  "TOKEN": "your-bot-token",
  "MONGO_URI": "mongodb+srv://...",
  "PREFIX": ".",
  "POKETWO_ID": 716390085896962058,
  "OWNER_IDS": [123456789012345678],
  "SYNC_SLASH_COMMANDS": true,
  "AI_CHATBOT_KEYS": ["sk-or-..."],
  "AI_CHATBOT_FALLBACK_MODELS": ["openrouter/free"],
  "COGS": {
    "general": ["help", "ping"],
    "utilities": ["utilities", "ai"],
    "moderation": ["members", "messages", "automod"],
    "server": ["channels", "categories", "roles"],
    "community": ["verify", "tickets", "giveaway", "invites", "joins"],
    "games": ["gtn"],
    "poketwo_config": ["baseconfigs", "autolock", "timer"],
    "poketwo_management": ["set", "toggle", "settings"],
    "poketwo_helper": ["afk", "autolock", "autolockdm", "catches", "lockunlock", "pings", "recognizer", "starboard"],
    "poketwo_utils": ["catchtime", "dex", "extract", "hintsolver"],
    "owner": ["utils", "gitstats", "grinder", "pull", "reload", "restart", "stats"]
  }
}
```

| Key | Required | Purpose |
| --- | --- | --- |
| `TOKEN` | ✅ | Discord bot token |
| `POKETWO_ID` | ✅ | User ID of the Pokétwo bot |
| `OWNER_IDS` | ✅ | List of user IDs allowed to use owner commands |
| `COGS` | ✅ | Folder → list of cogs to load (`cogs/<folder>/<cog>.py`) |
| `MONGO_URI` | ⚠️ | MongoDB connection string (most features need it) |
| `PREFIX` | – | Command prefix, defaults to `.` |
| `SYNC_SLASH_COMMANDS` | – | Sync slash commands on startup, defaults to `true` |
| `AI_CHATBOT_KEYS` | for AI | List of OpenRouter API keys (the legacy `AI_CHATBOT` single key also works) |
| `AI_CHATBOT_KEY_ACCOUNTS` | – | Account label per key, so keys from one account share quota tracking |
| `AI_CHATBOT_FALLBACK_MODELS` | – | Models to try when the main one fails |

> Remove from `COGS` any cog you do not want. Note that the AI cog refuses to load without an OpenRouter key, and `grinder` needs `firebase_key.json`.

### Extra files used by the Pokétwo features

The recognition features load data at runtime. Provide them in the project root (or next to the cog):

- `models/clip_vision_quantized.onnx` – recognition model
- `data/pokevars.json`, `data/alt.json` – Pokémon names and alternate names
- `data/pokes/{rare,regional,gmax,paradox,eevos,converts}.json` – category lists

### Run

```bash
python main.py
```

### Invite permissions
Give the bot the permissions the features you use need, typically: Manage Channels, Manage Roles, Manage Messages, Manage Nicknames, Kick/Ban Members, Manage Server (for invite tracking), Create Invite, and View Channels / Send Messages / Embed Links / Attach Files.

---

## 🗂️ Project structure

```
main.py                  Bot entry point, config loading, error auto-cleanup, cog loader
cogs/
├── general/             help, ping
├── utilities/           reminders, timers, roll, AI chatbot
├── moderation/          members, messages, automod
├── server/              channels, categories, roles
├── community/           verify, tickets, giveaway, invites, joins
├── games/               guess the number
├── poketwo_helper/      recognizer, autolock, pings, catches, starboard, AFK
├── poketwo_config/      autolock / timer configuration menus
├── poketwo_management/  set, toggle, settings commands
├── poketwo_utils/       dex, hint solver, extract, catchtime
└── owner/               pull, reload, restart, stats, gitstats, grinder
views/                   Buttons, menus, embeds and Components V2 layouts for every cog
```

---

## 📜 License

This application's source code is shared under the PolyForm Noncommercial License 1.0.0.

* Personal & Educational Use: Free to use, modify, and explore. You must give credit to the original author.
* Commercial Use: If you intend to use this code to earn revenue, build a commercial product, or use it within a business, you must purchase a commercial license.

For commercial licensing terms and pricing, please contact me at: `kurtislam100@gmail.com`

## 🙌 Acknowledgements

Built with ❤️ by Kurtis.