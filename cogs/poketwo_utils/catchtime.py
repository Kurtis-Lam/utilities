import datetime
import re
from discord.ext import commands

from cogs.owner_cmds.utils import safe_send

# Regex pattern for a 24-character hexadecimal MongoDB ObjectId
OBJECT_ID_REGEX = re.compile(r"^[0-9a-fA-F]{24}$")


class CatchTime(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="catchtime", aliases=["ct"])
    async def catchtime(self, ctx: commands.Context, object_id: str = ""):
        """Extract the creation timestamp from a MongoDB ObjectId.
        Usage: .ct xxxxxxx
        """
        clean_id = object_id.strip()

        if not clean_id:
            await safe_send(ctx, "⚠️ Please provide a valid 24-character ID. Usage: `.ct <id>`")
            return

        if not OBJECT_ID_REGEX.match(clean_id):
            await safe_send(
                ctx,
                "❌ Invalid ID format. Must be a 24-character hexadecimal string (e.g. `6ac446fba81ca859e3a5cf03`).",
            )
            return

        try:
            # Extract the first 8 hex characters (4 bytes) representing the creation timestamp
            timestamp_seconds = int(clean_id[:8], 16)

            # Discord timestamps:
            # <t:SECONDS:F> -> Full date and time (e.g., October 6, 2026 4:13 PM)
            # <t:SECONDS:R> -> Relative time (e.g., 2 hours ago)
            full_ts = f"<t:{timestamp_seconds}:F>"
            relative_ts = f"<t:{timestamp_seconds}:R>"

            embed_msg = (
                f"`{clean_id}` was caught:\n"
                f"> {full_ts} ({relative_ts})"
            )

            await safe_send(ctx, embed_msg)

        except Exception as e:
            await safe_send(ctx, f"❌ Failed to parse timestamp from ID: `{e}`")


async def setup(bot: commands.Bot):
    await bot.add_cog(CatchTime(bot))