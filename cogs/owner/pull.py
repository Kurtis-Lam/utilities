import asyncio
import re
import time
import uuid

import firebase_admin
from discord import app_commands
from discord.ext import commands
from firebase_admin import credentials, db

from cogs.owner.utils import OwnerCog, Progress, run_cmd, safe_edit, safe_send

# --- GRINDER REMOTE UPDATE (Firebase signal) ---
FIREBASE_DB_URL = "https://versatile-kl-default-rtdb.firebaseio.com/"
GRINDER_UPDATE_PATH = "grinder_update"   # request + status live here (kept out of `grinder` on purpose)
GRINDER_ACK_TIMEOUT = 20                 # seconds to wait for the JS engine to pick the signal up
GRINDER_TOTAL_TIMEOUT = 600              # hard cap for the whole update
GRINDER_RESTART_TIMEOUT = 120            # seconds to wait for "back online" after the restart began
GRINDER_POLL_SECS = 1.5

_REMOTE_RE = re.compile(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$")


def describe_source(remote_url: str, branch: str) -> str:
    """'https://github.com/Kurtis-Lam/my-discord-bot.git' + 'main' -> 'Kurtis-Lam/my-discord-bot/main'.
    Works for https and ssh remotes and never leaks credentials embedded in the URL."""
    m = _REMOTE_RE.search((remote_url or "").strip())
    return f"{m.group(1)}/{m.group(2)}/{branch}" if m else f"origin/{branch}"


def _grinder_update_ref():
    if not firebase_admin._apps:
        firebase_admin.initialize_app(
            credentials.Certificate("firebase_key.json"), {"databaseURL": FIREBASE_DB_URL}
        )
    return db.reference(GRINDER_UPDATE_PATH)


def _as_list(data) -> list:
    if data is None:
        return []
    if isinstance(data, list):
        return [x for x in data if x is not None]
    if isinstance(data, dict):
        keys = sorted(data.keys(), key=lambda k: int(k) if str(k).isdigit() else k)
        return [data[k] for k in keys if data[k] is not None]
    return []


class Pull(OwnerCog):
    def __init__(self, bot: commands.Bot):
        super().__init__(bot)

    @commands.hybrid_command(
        name="pull",
        description="Pulls the latest code from git and restarts (owner only). Add `grinder` to update the JS engine.",
    )
    @app_commands.describe(mode="Options: `grinder`/`g` (update the JS grinder), `force` (restart even if nothing changed), `hard` (reset to origin).")
    @commands.is_owner()
    async def pull(self, ctx: commands.Context, *, mode: str = commands.parameter(default="", description="`grinder`/`g` = update the JS grinder instead of the bot. `force` restarts even when nothing changed, `hard` resets to origin.")):
        """Pull the latest code, show progress, and restart.
        `.pull`                  update + restart this bot
        `.pull grinder` / `g`    tell the JS grinder to pull from GitHub and restart itself
        Add `force` to restart even if nothing changed, or `hard` to reset to origin."""
        bot = self.bot

        tokens = mode.lower().split()
        force = "force" in tokens
        hard = "hard" in tokens
        grinder = any(t in ("grinder", "g") for t in tokens)

        if bot.update_lock.locked():
            await safe_send(ctx, "⚠️ An update is already running.")
            return

        async with bot.update_lock:
            if grinder:
                await self._pull_grinder(ctx, force=force, hard=hard)
                return

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

            # Where are we pulling from? e.g. Kurtis-Lam/my-discord-bot/main
            rc, remote_url, _ = await run_cmd("git", "remote", "get-url", "origin", timeout=30)
            source = describe_source(remote_url if rc == 0 else "", branch)

            # 2. Fetch
            await prog.start(f"Pulling from `{source}`...")
            rc, out, err = await run_cmd("git", "fetch", "origin", timeout=120)
            if rc != 0:
                await prog.fail(f"Fetch from `{source}` failed", err or out)
                return
            await prog.ok(f"Fetched from `{source}`")

            # 3. Make sure tracking is set and check commits behind
            await prog.start("Checking branch tracking & status...")
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

            # Check how many commits behind local is compared to upstream
            rc, behind_str, _ = await run_cmd("git", "rev-list", "--count", f"HEAD..origin/{branch}", timeout=30)
            commits_behind = int(behind_str.strip()) if rc == 0 and behind_str.strip().isdigit() else 0

            # Get the latest commit message from remote
            rc, commit_msg, _ = await run_cmd("git", "log", "-1", "--format=%s", f"origin/{branch}", timeout=30)
            latest_commit_msg = commit_msg.strip() if rc == 0 else "No commit message found"

            # 4. Update the working tree
            if hard:
                # Mirror GitHub exactly: discards local changes to tracked files
                await prog.start(f"Resetting to `{source}` ({commits_behind} commit(s) behind)...")
                rc, out, err = await run_cmd("git", "reset", "--hard", f"origin/{branch}", timeout=60)
                if rc != 0:
                    await prog.fail("Hard reset failed", err or out)
                    return
                await prog.ok(f"Reset to `{source}`")
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
                await prog.start(f"Pulling {commits_behind} commit(s) from `{source}`...")
                rc, out, err = await run_cmd("git", "pull", "--ff-only", "origin", branch, timeout=180)
                if rc != 0:
                    await prog.fail(
                        "Git pull failed (if branches diverged, try `.pull hard`)", err or out
                    )
                    return

            rc, new_commit, _ = await run_cmd("git", "rev-parse", "--short", "HEAD", timeout=30)
            new_commit = new_commit if rc == 0 else "unknown"

            if old_commit == new_commit and not force:
                await prog.ok(f"Already up to date with `{source}`, no restart needed")
                return
            await prog.ok(f"Pulled `{old_commit}` → `{new_commit}` from `{source}` ({commits_behind} commit(s) behind)\n📝 Latest commit: *{latest_commit_msg}*")

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
                "commits_behind": commits_behind,
                "latest_commit_msg": latest_commit_msg,
                "branch": branch,
                "source": source,
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

    # ------------------------------------------------------------------
    # .pull grinder  ->  signal the JS engine through Firebase
    # ------------------------------------------------------------------
    async def _pull_grinder(self, ctx: commands.Context, *, force: bool, hard: bool):
        """Writes an update request to Firebase. The JS grinder picks it up, runs the git pull on
        its own host (bot-hosting.net), restarts itself and reports every step back through
        `grinder_update/status`; those steps are mirrored here exactly like a normal `.pull`."""
        msg = await safe_send(ctx, "🔄 Starting grinder update...")
        prog = Progress(msg)

        # 1. Send the signal
        await prog.start("Sending update signal to Firebase...")
        req_id = uuid.uuid4().hex[:12]
        try:
            root = await asyncio.to_thread(_grinder_update_ref)
            await asyncio.to_thread(root.child("status").delete)   # forget any old run
            await asyncio.to_thread(root.child("request").set, {
                "id": req_id,
                "force": force,
                "hard": hard,
                "requestedBy": str(ctx.author),
                "requestedAt": int(time.time() * 1000),
            })
        except Exception as e:
            await prog.fail("Couldn't send the signal to Firebase", f"{type(e).__name__}: {e}")
            return
        await prog.ok("Signal sent to Firebase")

        # 2. Wait for the JS engine, then mirror its progress
        await prog.start("Waiting for the grinder engine to pick it up...")
        t0 = time.time()
        acked = False
        applied = 0
        restart_seen_at = None

        while True:
            await asyncio.sleep(GRINDER_POLL_SECS)
            now = time.time()

            try:
                status = await asyncio.to_thread(root.child("status").get)
            except Exception:
                status = None  # transient network hiccup, try again on the next tick

            mine = isinstance(status, dict) and status.get("id") == req_id

            if mine:
                if not acked:
                    acked = True
                    await prog.ok("Grinder engine picked up the request")

                events = _as_list(status.get("events"))
                for ev in events[applied:]:
                    applied += 1
                    if not isinstance(ev, dict):
                        continue
                    kind = ev.get("kind")
                    text = str(ev.get("text") or "")
                    if kind == "start":
                        await prog.start(text)
                    elif kind == "ok":
                        await prog.ok(text)
                    elif kind == "warn":
                        await prog.warn(text)
                    elif kind == "fail":
                        await prog.fail(text, ev.get("detail") or None)
                        return

                state = status.get("state")
                if state == "done":
                    return
                if state == "failed":
                    await prog.fail("Grinder update failed")
                    return
                if state == "restarting" and restart_seen_at is None:
                    restart_seen_at = now

            if not acked and now - t0 > GRINDER_ACK_TIMEOUT:
                try:  # don't let a late engine suddenly run a stale request
                    await asyncio.to_thread(root.child("request").delete)
                except Exception:
                    pass
                await prog.fail(
                    f"Grinder engine didn't respond within {GRINDER_ACK_TIMEOUT}s",
                    "Is the JS process running on bot-hosting.net, and does it include `grinder_updater.js`?",
                )
                return

            if restart_seen_at is not None and now - restart_seen_at > GRINDER_RESTART_TIMEOUT:
                await prog.warn(
                    "Grinder hasn't reported back online yet — check the bot-hosting.net console"
                )
                return

            if now - t0 > GRINDER_TOTAL_TIMEOUT:
                await prog.fail("Timed out waiting for the grinder update to finish")
                return


async def setup(bot: commands.Bot):
    await bot.add_cog(Pull(bot))