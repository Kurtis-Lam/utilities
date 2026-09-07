import asyncio
import json
import socket
import gc
from pathlib import Path
from collections import OrderedDict

import aiohttp
import discord
from discord.ext import commands

from .recognize import extract_pokemon_from_text, format_name

class Recognize(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.poketwo_id = 716390085896962058
        self.correct_log_channel_id = 1533816816701407382
        self.wrong_log_channel_id = 1534081811511246910
        
        self.max_predictions_cache = 20  # Reduced cache size to conserve RAM
        self.pending_verifications = OrderedDict()
        
        self.session: aiohttp.ClientSession | None = None
        self.recognizer = None
        self.is_model_loaded = False
        
        self.category_files = {
            "rare": "pokes/rare.json",
            "regional": "pokes/regional.json",
            "gmax": "pokes/gmax.json",
            "paradox": "pokes/paradox.json",
            "eevos": "pokes/eevos.json",
        }
        self.category_pokes = {}
        self._background_tasks = set()

    @property
    def pings_cog(self):
        return self.bot.get_cog("PokePings")

    @property
    def autolock_cog(self):
        return self.bot.get_cog("AutoLock")

    @property
    def afk_cog(self):
        return self.bot.get_cog("AFK")

    def _load_category_pokes(self):
        for key, filepath in self.category_files.items():
            path = Path(filepath)
            if path.exists():
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        self.category_pokes[key] = frozenset(p.strip().lower() for p in data if isinstance(p, str))
                except Exception as e:
                    print(f"[Recognizer] Failed to load {filepath}: {e}")
                    self.category_pokes[key] = frozenset()
            else:
                self.category_pokes[key] = frozenset()

    async def _ensure_model_loaded(self):
        """Lazy loads heavy weights into RAM only on first usage."""
        if not self.is_model_loaded:
            from .recognize import PokemonRecognizer
            if self.recognizer is None:
                self.recognizer = PokemonRecognizer()
                
            await asyncio.to_thread(self.recognizer.load_resources)
            self.is_model_loaded = True
            gc.collect()  # Keep GC sweep only for initial heavy allocation

    async def cog_load(self):
        self._load_category_pokes()
        
        connector = aiohttp.TCPConnector(
            family=socket.AF_INET,
            ttl_dns_cache=300,
            limit=2,
            enable_cleanup_closed=True
        )
        timeout = aiohttp.ClientTimeout(total=5, connect=2)
        self.session = aiohttp.ClientSession(connector=connector, timeout=timeout)

    async def cog_unload(self):
        if self.session and not self.session.closed:
            await self.session.close()
        self.recognizer = None
        self.is_model_loaded = False
        gc.collect()

    async def _get_ping_info(self, guild_id: int, pokemon_name: str):
        if not self.pings_cog:
            return "", [], set()

        try:
            g_data = await self.pings_cog._get_guild_doc(str(guild_id))
        except Exception:
            return "", [], set()

        if not g_data:
            return "", [], set()

        pok_lower = pokemon_name.strip().lower()
        pokeinfo_map = getattr(self.recognizer, 'pokeinfo', {}) if self.recognizer else {}
        
        # Fast direct dict lookup replacing linear key search loops
        info = pokeinfo_map.get(pokemon_name.strip()) or pokeinfo_map.get(pok_lower) or {}

        raw_types = info.get("types", [])
        if isinstance(raw_types, list):
            types = {t.lower() for t in raw_types}
        elif isinstance(raw_types, str):
            types = {t.strip().lower() for t in raw_types.split("\n") if t.strip()}
        else:
            types = set()

        region = (info.get("region") or "").lower()
        if not region:
            if "hisuian" in pok_lower or "hisui" in pok_lower:
                region = "hisui"
            elif "galarian" in pok_lower or "galar" in pok_lower:
                region = "galar"
            elif "alolan" in pok_lower or "alola" in pok_lower:
                region = "alola"
            elif "paldean" in pok_lower or "paldea" in pok_lower:
                region = "paldea"

        # Streamlined set comprehensions for faster extraction
        sh_uids = {int(uid) for uid, target in g_data.get("sh", {}).items() if target and target.lower() == pok_lower}
        cl_uids = {int(uid) for uid, cl_list in g_data.get("cl", {}).items() if isinstance(cl_list, list) and any(c.lower() == pok_lower for c in cl_list)}
        tp_uids = {int(uid) for uid, tp_list in g_data.get("tp", {}).items() if isinstance(tp_list, list) and any(t.lower() in types for t in tp_list)}

        is_gmax = pok_lower in self.category_pokes.get("gmax", frozenset())
        is_paradox = pok_lower in self.category_pokes.get("paradox", frozenset())
        is_eevos = pok_lower in self.category_pokes.get("eevos", frozenset())

        rp_uids = set()
        for uid, rp_list in g_data.get("rp", {}).items():
            if isinstance(rp_list, list):
                if any((region and r.lower() == region) or 
                       (is_gmax and r.lower() == "gmax") or 
                       (is_paradox and r.lower() == "paradox") or 
                       (is_eevos and r.lower() == "eevos") for r in rp_list):
                    rp_uids.add(int(uid))

        if self.afk_cog:
            sh_pings = await self.afk_cog.format_ping_list(sh_uids)
            cl_pings = await self.afk_cog.format_ping_list(cl_uids)
            tp_pings = await self.afk_cog.format_ping_list(tp_uids)
            rp_pings = await self.afk_cog.format_ping_list(rp_uids)
        else:
            sh_pings = [f"<@{uid}>" for uid in sh_uids]
            cl_pings = [f"<@{uid}>" for uid in cl_uids]
            tp_pings = [f"<@{uid}>" for uid in tp_uids]
            rp_pings = [f"<@{uid}>" for uid in rp_uids]

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

        roles = g_data.get("roles", {})
        cat_labels = (
            ("rare", "Rare Ping"),
            ("regional", "Regional Ping"),
            ("gmax", "Gmax Ping"),
            ("paradox", "Paradox Ping"),
            ("eevos", "Eevos Ping"),
        )
        for cat_key, label in cat_labels:
            if pok_lower in self.category_pokes.get(cat_key, frozenset()):
                activated_categories.append(cat_key)
                role_id = roles.get(cat_key)
                lines.append(f"{label}: <@&{role_id}>" if role_id else f"{label}: no role configured")

        pinged_user_ids = sh_uids | cl_uids | tp_uids | rp_uids
        return "\n".join(lines), activated_categories, pinged_user_ids

    async def _send_log_embed(self, is_correct: bool, predicted: str, actual: str, confidence: float, image_url: str, jump_url: str):
        channel_id = self.correct_log_channel_id if is_correct else self.wrong_log_channel_id
        channel = self.bot.get_channel(channel_id)
        if not channel:
            return  

        color = discord.Color.green() if is_correct else discord.Color.red()
        embed = discord.Embed(title="✅ Prediction Correct" if is_correct else "❌ Prediction Incorrect", color=color)
        embed.add_field(name="Predicted", value=f"**{format_name(predicted)}**", inline=True)
        embed.add_field(name="Actual Pokémon", value=f"**{format_name(actual)}**", inline=True)
        embed.add_field(name="Confidence", value=f"**{confidence:.2%}**", inline=True)
        embed.add_field(name="Detection Message", value=f"[Jump to Message]({jump_url})", inline=False)
        embed.set_thumbnail(url=image_url)

        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass

    @commands.command(name="rec", aliases=["recognize"])
    async def manual_recognize(self, ctx: commands.Context):
        image_url = None

        # 1. Check direct attachments on the command message
        if ctx.message.attachments:
            image_url = ctx.message.attachments[0].url

        # 2. Check replied-to (referenced) message
        elif ctx.message.reference and ctx.message.reference.message_id:
            try:
                ref_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
                if ref_msg.attachments:
                    image_url = ref_msg.attachments[0].url
                elif ref_msg.embeds:
                    for embed in ref_msg.embeds:
                        if embed.image and embed.image.url:
                            image_url = embed.image.url
                            break
                        if embed.thumbnail and embed.thumbnail.url:
                            image_url = embed.thumbnail.url
                            break
            except (discord.NotFound, discord.HTTPException):
                pass

        if not image_url:
            await ctx.send("❌ Please attach an image or reply to a message containing a Pokémon image.")
            return

        await self._ensure_model_loaded()
        async with ctx.typing():
            try:
                pokemon_name, confidence = await self.recognizer.identify_from_url(self.session, image_url)
                pings, _, _ = await self._get_ping_info(ctx.guild.id if ctx.guild else 0, pokemon_name)
                
                formatted_name = format_name(pokemon_name)

                embed = discord.Embed(
                    title=f"🔍 Recognition Result",
                    color=discord.Color.blue()
                )
                embed.add_field(name="Pokémon", value=f"**{formatted_name}**", inline=True)
                embed.add_field(name="Confidence", value=f"**{confidence:.2%}**", inline=True)
                
                if pings:
                    embed.add_field(name="Matched Pings", value=pings, inline=False)

                embed.set_thumbnail(url=image_url)

                # allowed_mentions=discord.AllowedMentions.none() suppresses all notifications
                await ctx.send(
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none()
                )
            except Exception as e:
                await ctx.send(f"❌ Recognition failed: `{e}`")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.id != self.poketwo_id:
            return

        channel_id = message.channel.id
        content = message.content.lower()

        # Fast-path verification check (Avoids unnecessary embed/string processing on non-catch messages)
        if channel_id in self.pending_verifications:
            if "you caught a level" in content or "fled" in content:
                text_to_check = content
                if message.embeds:
                    text_to_check += " " + " ".join(f"{e.title or ''} {e.description or ''}" for e in message.embeds).lower()

                actual_pokemon = extract_pokemon_from_text(text_to_check)
                if actual_pokemon:
                    pending_data = self.pending_verifications.pop(channel_id, None)
                    if pending_data:
                        predicted_name = pending_data["predicted"]
                        is_correct = predicted_name.lower() == actual_pokemon
                        
                        pokevars = getattr(self.recognizer, 'pokevars', set()) if self.recognizer else set()
                        if is_correct or actual_pokemon in pokevars:
                            task = asyncio.create_task(
                                self._send_log_embed(
                                    is_correct=is_correct,
                                    predicted=predicted_name,
                                    actual=actual_pokemon,
                                    confidence=pending_data["confidence"],
                                    image_url=pending_data["image_url"],
                                    jump_url=pending_data["jump_url"]
                                )
                            )
                            self._background_tasks.add(task)
                            task.add_done_callback(self._background_tasks.discard)
                return

        # Early exit for non-spawns before running expensive operations
        if "appeared" not in content or "pok" not in content:
            return

        image_url = None
        if message.embeds:
            for embed in message.embeds:
                if embed.image and embed.image.url:
                    image_url = embed.image.url
                    break
                if embed.thumbnail and embed.thumbnail.url:
                    image_url = embed.thumbnail.url
                    break
        if not image_url and message.attachments:
            image_url = message.attachments[0].url

        if not image_url:
            return

        await self._ensure_model_loaded()

        try:
            pokemon_name, confidence = await self.recognizer.identify_from_url(self.session, image_url)
        except Exception as e:
            print(f"[ONNX Inference Error] {e}")
            return

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

        self.pending_verifications[channel_id] = {
            "predicted": pokemon_name,
            "confidence": confidence,
            "image_url": image_url,
            "jump_url": detection_msg.jump_url
        }
        if len(self.pending_verifications) > self.max_predictions_cache:
            self.pending_verifications.popitem(last=False)

        if self.autolock_cog and activated_categories:
            task1 = asyncio.create_task(
                self.autolock_cog.process_autolock(
                    channel=message.channel,
                    activated_categories=activated_categories,
                    pinged_user_ids=pinged_uids,
                )
            )
            self._background_tasks.add(task1)
            task1.add_done_callback(self._background_tasks.discard)

async def setup(bot):
    await bot.add_cog(Recognize(bot))