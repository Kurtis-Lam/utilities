import time

from discord.ext import commands

from cogs.owner_cmds.utils import Progress, run_cmd, safe_edit, safe_send


class Pull(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="pull")
    @commands.is_owner()
    async def pull(self, ctx: commands.Context, mode: str = ""):
        """Pull the latest code, show progress, and restart.
        Use `update force` to restart even if nothing changed."""
        bot = self.bot

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
            if mode.lower() == "hard":
                # Mirror GitHub exactly: discards local changes to tracked files
                await prog.start(f"Resetting to `origin/{branch}` ({commits_behind} commit(s) behind)...")
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
                await prog.start(f"Pulling {commits_behind} commit(s) behind...")
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
            await prog.ok(f"Pulled `{old_commit}` → `{new_commit}` ({commits_behind} commit(s) behind)\n📝 Latest commit: *{latest_commit_msg}*")

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


async def setup(bot: commands.Bot):
    await bot.add_cog(Pull(bot))