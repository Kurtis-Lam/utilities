import discord
from discord.ext import commands

from cogs.owner_cmds.utils import OwnerCog, run_cmd, safe_send
from views.common_views import EMBED_COLOR

MAX_OTHER_REFS = 10


def _counts(raw: str) -> tuple[int, int] | None:
    """'2\t1' (from `rev-list --left-right --count HEAD...X`) -> (ahead, behind)."""
    parts = raw.split()
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        return int(parts[0]), int(parts[1])
    return None


def _describe(ahead: int, behind: int) -> str:
    if ahead == 0 and behind == 0:
        return "up to date"
    bits = []
    if ahead:
        bits.append(f"{ahead} ahead")
    if behind:
        bits.append(f"{behind} behind")
    return ", ".join(bits)


class GitStats(OwnerCog):
    def __init__(self, bot: commands.Bot):
        super().__init__(bot)

    @commands.command(name="gitstats")
    @commands.is_owner()
    async def gitstats(self, ctx: commands.Context):
        """Show the current git branch/commit and how far ahead/behind it is."""
        async with ctx.typing():
            rc, branch, err = await run_cmd("git", "rev-parse", "--abbrev-ref", "HEAD", timeout=30)
            if rc != 0:
                await safe_send(ctx, f"❌ Not a git repository (or git missing).\n```\n{(err or branch)[:500]}\n```")
                return

            # Refresh remote info so ahead/behind is accurate (failure is non-fatal).
            frc, _, ferr = await run_cmd("git", "fetch", "--all", timeout=60)

            detached = branch == "HEAD"
            rc, info, _ = await run_cmd(
                "git", "log", "-1", "--format=%h%x1f%s%x1f%an%x1f%ct", timeout=30
            )
            short, subject, author, stamp = (info.split("\x1f") + ["", "", "", ""])[:4] if rc == 0 else ("unknown", "", "", "")

            rc, dirty, _ = await run_cmd("git", "status", "--porcelain", timeout=30)
            changes = len([line for line in dirty.splitlines() if line.strip()]) if rc == 0 else 0

            # Upstream (tracking) branch
            upstream = None
            upstream_line = "No upstream branch set."
            rc, up_name, _ = await run_cmd(
                "git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", timeout=30
            )
            if rc == 0 and up_name:
                upstream = up_name
                rc, raw, _ = await run_cmd(
                    "git", "rev-list", "--left-right", "--count", "HEAD...@{u}", timeout=30
                )
                counts = _counts(raw) if rc == 0 else None
                upstream_line = (
                    f"`{upstream}`: **{_describe(*counts)}**" if counts else f"`{upstream}`: unknown"
                )

            # Every other local / remote branch
            rc, remotes_raw, _ = await run_cmd("git", "remote", timeout=30)
            remotes = set(remotes_raw.split()) if rc == 0 else set()
            rc, refs_raw, _ = await run_cmd(
                "git", "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes", timeout=30
            )
            refs = refs_raw.splitlines() if rc == 0 else []

            differing: list[str] = []
            in_sync = 0
            skip = {branch, upstream}
            for ref in refs:
                ref = ref.strip()
                if not ref or ref in skip or ref in remotes or ref.endswith("/HEAD"):
                    continue
                rc, raw, _ = await run_cmd(
                    "git", "rev-list", "--left-right", "--count", f"HEAD...{ref}", timeout=30
                )
                counts = _counts(raw) if rc == 0 else None
                if counts is None:
                    continue
                if counts == (0, 0):
                    in_sync += 1
                else:
                    differing.append(f"`{ref}`: {_describe(*counts)}")

        embed = discord.Embed(title="🌿 Git Stats", color=EMBED_COLOR)
        embed.add_field(
            name="Branch",
            value=f"`{short}` (detached HEAD)" if detached else f"`{branch}`",
            inline=True,
        )
        commit_text = f"`{short}`"
        if subject:
            commit_text += f" {subject}"
        embed.add_field(name="Commit", value=commit_text[:1024], inline=True)
        if author and stamp.isdigit():
            embed.add_field(name="Author", value=f"{author}\n<t:{stamp}:R>", inline=True)
        embed.add_field(
            name="Working tree",
            value="Clean" if changes == 0 else f"{changes} uncommitted change(s)",
            inline=True,
        )
        embed.add_field(name="Upstream", value=upstream_line[:1024], inline=False)

        if differing:
            shown = differing[:MAX_OTHER_REFS]
            text = "\n".join(shown)
            if len(differing) > len(shown):
                text += f"\n… and {len(differing) - len(shown)} more"
            if in_sync:
                text += f"\n{in_sync} other ref(s) up to date"
            embed.add_field(name="Compared to other branches", value=text[:1024], inline=False)
        elif in_sync:
            embed.add_field(
                name="Compared to other branches", value=f"{in_sync} other ref(s) up to date", inline=False
            )

        if frc != 0:
            embed.set_footer(text=f"⚠️ Fetch failed, remote info may be stale: {ferr[:100]}")
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(GitStats(bot))