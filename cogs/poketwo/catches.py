import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import re
from typing import Optional

import discord
from discord.ext import commands

# Constants
POKETWO_ID = 716390085896962058
HKT = timezone(timedelta(hours=8))  # Hong Kong Time (UTC+8)
CATCHES_FILE = os.path.join("data", "catches.json")


def get_hkt_period_keys():
    """Generates period identifiers based on current Hong Kong Time (UTC+8)."""
    now = datetime.now(HKT)

    # Daily: Resets every day at 00:00 HKT
    daily_key = now.strftime("%Y-%m-%d")

    # Weekly: Resets every SUNDAY at 00:00 HKT
    # In Python, weekday() is 0 for Mon, ..., 5 for Sat, 6 for Sun.
    days_since_sunday = (now.weekday() + 1) % 7
    last_sunday = now - timedelta(days=days_since_sunday)
    weekly_key = last_sunday.strftime("%Y-%m-%d")

    # Monthly: Resets on the 1st day of the month at 00:00 HKT
    monthly_key = now.strftime("%Y-%m")

    return daily_key, weekly_key, monthly_key


def load_catches_data():
    """Loads catch data and resets daily/weekly/monthly scores if HKT 00:00 has passed."""
    daily_k, weekly_k, monthly_k = get_hkt_period_keys()
    data = {}

    if os.path.exists(CATCHES_FILE):
        try:
            with open(CATCHES_FILE, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, AttributeError):
            data = {}

    if not isinstance(data, dict):
        data = {}

    # Ensure top-level sections exist
    if "alltime" not in data or not isinstance(data["alltime"], dict):
        data["alltime"] = {}

    # Reset daily scores if HKT date has changed
    if "daily" not in data or data["daily"].get("key") != daily_k:
        data["daily"] = {"key": daily_k, "scores": {}}

    # Reset weekly scores if Sunday 00:00 HKT has passed
    if "weekly" not in data or data["weekly"].get("key") != weekly_k:
        data["weekly"] = {"key": weekly_k, "scores": {}}

    # Reset monthly scores if 1st of month 00:00 HKT has passed
    if "monthly" not in data or data["monthly"].get("key") != monthly_k:
        data["monthly"] = {"key": monthly_k, "scores": {}}

    return data


def save_catches_data(data):
    """Saves catch data to the JSON file, ensuring the directory exists."""
    os.makedirs(os.path.dirname(CATCHES_FILE), exist_ok=True)
    with open(CATCHES_FILE, "w") as f:
        json.dump(data, f, indent=4)


def parse_timeframe(tf: Optional[str]):
    """Parses user input into standard timeframe keys and labels."""
    if not tf:
        return "alltime", "All-Time"
    tf = tf.lower()
    if tf in ["d", "daily"]:
        return "daily", "Daily"
    elif tf in ["w", "weekly"]:
        return "weekly", "Weekly"
    elif tf in ["m", "monthly"]:
        return "monthly", "Monthly"
    elif tf in ["all", "alltime", "total", "a"]:
        return "alltime", "All-Time"
    return None, None


class Catches(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # --- EVENT LISTENERS ---
    @commands.Cog.listener()
    async def on_message(self, message):
        # Ignore non-Poketwo messages
        if message.author.id != POKETWO_ID:
            return

        # Handle Congratulations (Increments Catch Counters)
        if message.content.startswith("Congratulations "):
            if not message.mentions:
                return

            caught_user = message.mentions[0]
            await message.channel.send(
                f"> **{caught_user.display_name}** has caught a pokemon!"
            )

            data = load_catches_data()
            user_id_str = str(caught_user.id)

            # Increment All-Time catches
            data["alltime"][user_id_str] = data["alltime"].get(user_id_str, 0) + 1

            # Increment Daily, Weekly, and Monthly catches
            for period in ["daily", "weekly", "monthly"]:
                scores = data[period]["scores"]
                scores[user_id_str] = scores.get(user_id_str, 0) + 1

            save_catches_data(data)

    # --- COMMANDS ---
    @commands.hybrid_command(
        name="catches",
        description="View your own catch count.",
    )
    async def view_catches(
        self,
        ctx,
        timeframe: Optional[str] = commands.parameter(
            default=None,
            description="Timeframe to view (d/daily, w/weekly, m/monthly, or alltime).",
        ),
    ):
        tf_key, tf_label = parse_timeframe(timeframe)
        if not tf_key:
            await ctx.send(
                "❌ Invalid timeframe! Use `d`/`daily`, `w`/`weekly`, `m`/`monthly`, or `alltime`."
            )
            return

        data = load_catches_data()
        user_id_str = str(ctx.author.id)

        if tf_key == "alltime":
            count = data["alltime"].get(user_id_str, 0)
        else:
            count = data[tf_key]["scores"].get(user_id_str, 0)

        await ctx.send(
            f"📊 **{ctx.author.display_name}** has caught **{count}** Pokémon ({tf_label})."
        )

    @commands.hybrid_command(
        name="catchleaderboard",
        aliases=["clb"],
        description="View the top 10 catch leaders.",
    )
    async def catch_leaderboard(
        self,
        ctx,
        timeframe: Optional[str] = commands.parameter(
            default=None,
            description="Timeframe to view (d/daily, w/weekly, m/monthly, or alltime).",
        ),
    ):
        tf_key, tf_label = parse_timeframe(timeframe)
        if not tf_key:
            await ctx.send(
                "❌ Invalid timeframe! Use `d`/`daily`, `w`/`weekly`, `m`/`monthly`, or `alltime`."
            )
            return

        data = load_catches_data()

        if tf_key == "alltime":
            scores = data["alltime"]
        else:
            scores = data[tf_key]["scores"]

        if not scores:
            await ctx.send(f"The {tf_label.lower()} leaderboard is currently empty.")
            return

        sorted_catches = sorted(
            scores.items(), key=lambda item: item[1], reverse=True
        )[:10]

        leaderboard_text = ""
        for index, (user_id, count) in enumerate(sorted_catches, start=1):
            user = self.bot.get_user(int(user_id))
            user_name = user.display_name if user else f"User ID: {user_id}"
            leaderboard_text += f"{index}. **{user_name}** — {count} catches\n"

        embed = discord.Embed(
            title=f"🏆 Pokétwo Catch Leaderboard ({tf_label})",
            description=leaderboard_text,
            color=discord.Color.gold(),
        )
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Catches(bot))
