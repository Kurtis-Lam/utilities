import asyncio
import json
import re
import socket
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands

from .recognize import PokemonRecognizer, extract_pokemon_from_text, format_name

class Recognize(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.poketwo_id = 716390085896962058
        self.correct_log_channel_id = 1533816816701407382
        self.wrong_log_channel_id = 1534081811511246910
        self.last_predictions = {}
        self.session: aiohttp.ClientSession | None = None
        self.recognizer = PokemonRecognizer()
        self.category_files = {
            "rare": "pokes/rare.json",
            "regional": "pokes/regional.json",
            "gmax": "pokes/gmax.json",
            "paradox": "pokes/paradox.json",
            "eevos": "pokes/eevos.json",
        }
        self.category_pokes = {}

    def _load_category_pokes(self):
        for key, filepath in self.category_files.items():
            path = Path(filepath)
            if path.exists():
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        self.category_pokes[key] = {p.strip().lower() for p in data}
                except Exception as e:
                    print(f"[Recognizer] Failed to load {filepath}: {e}")
                    self.category_pokes[key] = set()
            else:
                self.category_pokes[key] = set()

    async def cog_load(self):
        await asyncio.to_thread(self.recognizer.load_resources)
        self._load_category_pokes()
        connector = aiohttp.TCPConnector(
            family=socket.AF_INET, ttl_dns_cache=300
        )
        self.session = aiohttp.ClientSession(connector=connector)

    async def cog_unload(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def _get_ping_info(self, guild_id: int, pokemon_name: str):
        """Returns tuple of (formatted_ping_string, activated_categories, pinged_user_ids)."""
        pings_cog = self.bot.get_cog("PokePings")
        if not pings_cog:
            return "", [], set()

        try:
            g_data = await pings_cog._get_guild_doc(str(guild_id))
        except Exception as e:
            print(f"[Ping Lookup Error] {e}")
            return "", [], set()

        if not g_data:
            return "", [], set()

        pok_lower = pokemon_name.strip().lower()

        info = (
            self.recognizer.pokeinfo.get(pokemon_name.strip())
            or self.recognizer.pokeinfo.get(pok_lower)
            or next(
                (v for k, v in self.recognizer.pokeinfo.items() if k.lower() == pok_lower),
                {}
            )
        )

        raw_types = info.get("types", [])
        types = [t.lower() for t in raw_types] if isinstance(raw_types, list) else [t.strip().lower() for t in raw_types.split("\n") if t.strip()]
        region = (info.get("region") or "").lower()

        sh_pings, cl_pings, tp_pings, rp_pings = [], [], [], []
        sh_uids, cl_uids, tp_uids, rp_uids = set(), set(), set(), set()

        for uid, target in g_data.get("sh", {}).items():
            if target and target.lower() == pok_lower:
                sh_pings.append(f"<@{uid}>")
                sh_uids.add(int(uid))

        for uid, cl_list in g_data.get("cl", {}).items():
            if isinstance(cl_list, list) and any(c.lower() == pok_lower for c in cl_list):
                cl_pings.append(f"<@{uid}>")
                cl_uids.add(int(uid))

        for uid, tp_list in g_data.get("tp", {}).items():
            if isinstance(tp_list, list):
                tp_lower = [t.lower() for t in tp_list]
                if any(t in tp_lower for t in types):
                    tp_pings.append(f"<@{uid}>")
                    tp_uids.add(int(uid))

        for uid, rp_list in g_data.get("rp", {}).items():
            if isinstance(rp_list, list):
                rp_lower = [r.lower() for r in rp_list]
                is_match = False

                if region and region in rp_lower:
                    is_match = True
                if "gmax" in rp_lower and pok_lower in self.category_pokes.get("gmax", set()):
                    is_match = True
                if "paradox" in rp_lower and pok_lower in self.category_pokes.get("paradox", set()):
                    is_match = True
                if "eevos" in rp_lower and pok_lower in self.category_pokes.get("eevos", set()):
                    is_match = True

                if is_match:
                    rp_pings.append(f"<@{uid}>")
                    rp_uids.add(int(uid))

        lines = []
        activated_categories = []

        if sh_pings:
            activated_categories.append("sh")
            lines.append(f"Shiny Hunt Pings: {' '.join(sh_pings)}")
        if cl_pings:
            activated_categories.append("cl")
            lines.append(f"Collection Pings: {' '.join(cl_pings)}")
        if tp_pings:
            activated_categories.append("tp")
            lines.append(f"Type Pings: {' '.join(tp_pings)}")
        if rp_pings:
            activated_categories.append("rp")
            lines.append(f"Region Pings: {' '.join(rp_pings)}")

        # Role Category Pings Check
        roles = g_data.get("roles", {})
        cat_labels = {
            "rare": "Rare Ping",
            "regional": "Regional Ping",
            "gmax": "Gmax Ping",
            "paradox": "Paradox Ping",
            "eevos": "Eevos Ping",
        }
        for cat_key, label in cat_labels.items():
            if pok_lower in self.category_pokes.get(cat_key, set()):
                activated_categories.append(cat_key)
                role_id = roles.get(cat_key)
                if role_id:
                    lines.append(f"{label}: <@&{role_id}>")
                else:
                    lines.append(f"{label}: no role configured")

        pinged_user_ids = sh_uids | cl_uids | tp_uids | rp_uids
        return "\n".join(lines), activated_categories, pinged_user_ids

    async def _send_log_embed(
        self,
        is_correct: bool,
        predicted: str,
        actual: str,
        confidence: float,
        image_url: str,
        jump_url: str,
    ):
        channel_id = (
            self.correct_log_channel_id
            if is_correct
            else self.wrong_log_channel_id
        )
        channel = self.bot.get_channel(channel_id)
        if not channel:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException as e:
                print(f"[Log Error] Could not fetch log channel {channel_id}: {e}")
                return

        color = discord.Color.green() if is_correct else discord.Color.red()
        status_title = "✅ Prediction Correct" if is_correct else "❌ Prediction Incorrect"

        embed = discord.Embed(title=status_title, color=color)
        embed.add_field(name="Predicted", value=f"**{format_name(predicted)}**", inline=True)
        embed.add_field(name="Actual Pokémon", value=f"**{format_name(actual)}**", inline=True)
        embed.add_field(name="Confidence", value=f"**{confidence:.2%}**", inline=True)
        embed.add_field(name="Detection Message", value=f"[Jump to Message]({jump_url})", inline=False)
        embed.set_thumbnail(url=image_url)

        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            print(f"[Log Error] Failed to send embed to {channel_id}: {e}")

    async def _verify_prediction(
        self,
        channel: discord.abc.Messageable,
        predicted_name: str,
        confidence: float,
        image_url: str,
        jump_url: str,
    ):
        def check(m: discord.Message) -> bool:
            if m.channel.id != channel.id or m.author.id != self.poketwo_id:
                return False

            full_text = m.content
            if m.embeds:
                for emb in m.embeds:
                    if emb.title:
                        full_text += " " + emb.title
                    if emb.description:
                        full_text += " " + emb.description

            full_text_lower = full_text.lower()
            return "you caught a level" in full_text_lower or "fled" in full_text_lower

        try:
            msg = await self.bot.wait_for("message", check=check, timeout=300.0)

            full_text = msg.content
            if msg.embeds:
                for emb in msg.embeds:
                    if emb.title:
                        full_text += " " + emb.title
                    if emb.description:
                        full_text += " " + emb.description

            actual_pokemon = extract_pokemon_from_text(full_text)
            target_pokemon_name = actual_pokemon or predicted_name

            if not actual_pokemon:
                return

            is_correct = predicted_name.lower() == actual_pokemon

            if not is_correct and actual_pokemon not in self.recognizer.pokevars:
                return

            await self._send_log_embed(
                is_correct=is_correct,
                predicted=predicted_name,
                actual=actual_pokemon,
                confidence=confidence,
                image_url=image_url,
                jump_url=jump_url,
            )

        except asyncio.TimeoutError:
            pass
        except Exception as e:
            print(f"[Verification Error] {e}")

    @commands.command(name="recognize", aliases=["rec"])
    async def recognize_cmd(self, ctx: commands.Context):
        image_url = None

        if ctx.message.reference and ctx.message.reference.message_id:
            try:
                ref_msg = ctx.message.reference.cached_message
                if not ref_msg:
                    ref_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)

                if ref_msg.attachments:
                    image_url = ref_msg.attachments[0].url
                elif ref_msg.embeds:
                    for emb in ref_msg.embeds:
                        if emb.image and emb.image.url:
                            image_url = emb.image.url
                            break
                        elif emb.thumbnail and emb.thumbnail.url:
                            image_url = emb.thumbnail.url
                            break
            except discord.HTTPException:
                pass

        if not image_url and ctx.message.attachments:
            image_url = ctx.message.attachments[0].url

        if not image_url:
            await ctx.reply("❌ Please reply to a message containing a Pokémon image or attach an image.")
            return

        try:
            pokemon_name, confidence = await self.recognizer.identify_from_url(
                self.session, image_url
            )
            await ctx.reply(f"**{format_name(pokemon_name)}**: {confidence:.3%}")
        except Exception as e:
            await ctx.reply(f"❌ Error identifying Pokémon: {e}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.id != self.poketwo_id:
            return

        text_to_check = message.content.lower()
        if message.embeds:
            for embed in message.embeds:
                if embed.title:
                    text_to_check += " " + embed.title.lower()
                if embed.description:
                    text_to_check += " " + embed.description.lower()

        is_spawn = bool(
            re.search(r"a\s+(new\s+)?wild\s+pok[eé]mon\s+(has\s+)?appeared", text_to_check)
        )
        if not is_spawn:
            return

        image_url = None
        if message.embeds:
            for embed in message.embeds:
                if embed.image and embed.image.url:
                    image_url = embed.image.url
                    break
                elif embed.thumbnail and embed.thumbnail.url:
                    image_url = embed.thumbnail.url
                    break

        if not image_url and message.attachments:
            image_url = message.attachments[0].url

        if not image_url:
            return

        try:
            pokemon_name, confidence = await self.recognizer.identify_from_url(
                self.session, image_url
            )
        except Exception as e:
            print(f"[ONNX Inference Error] {e}")
            return

        self.last_predictions[message.channel.id] = pokemon_name

        pings, activated_categories, pinged_uids = await self._get_ping_info(
            message.guild.id if message.guild else 0, pokemon_name
        )

        out_text = f"{format_name(pokemon_name)}: {confidence:.3%}"
        if pings:
            out_text += f"\n{pings}"

        try:
            detection_msg = await message.reply(out_text)
        except discord.Forbidden:
            return

        # Trigger autolock directly with recognized categories
        autolock_cog = self.bot.get_cog("AutoLock")
        if autolock_cog and activated_categories:
            self.bot.loop.create_task(
                autolock_cog.process_autolock(
                    channel=message.channel,
                    activated_categories=activated_categories,
                    pinged_user_ids=pinged_uids,
                )
            )

        self.bot.loop.create_task(
            self._verify_prediction(
                channel=message.channel,
                predicted_name=pokemon_name,
                confidence=confidence,
                image_url=image_url,
                jump_url=detection_msg.jump_url,
            )
        )


async def setup(bot):
    await bot.add_cog(Recognize(bot))