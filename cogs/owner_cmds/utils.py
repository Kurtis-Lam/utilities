import asyncio
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


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