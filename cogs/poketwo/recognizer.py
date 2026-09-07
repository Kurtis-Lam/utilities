import asyncio
import json
import socket
import gc
from pathlib import Path
from collections import OrderedDict

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
        
        # Reduced max predictions cache size to limit memory overhead
        self.max_predictions_cache = 50 
        self.pending_verifications = OrderedDict()
        
        self.session: aiohttp.ClientSession | None = None
        self.recognizer = PokemonRecognizer()
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
        """Dynamic getter for PokePings cog."""
        return self.bot.get_cog("PokePings")

    @property
    def autolock_cog(self):
        """Dynamic getter for AutoLock cog."""
        return self.bot.get_cog("AutoLock")

    @property
    def afk_cog(self):
        """Dynamic getter for AFK cog."""
        return self.bot.get_cog("AFK")

    def _load_category_pokes(self):
        for key, filepath in self.category_files.items():
            path = Path(filepath)
            if path.exists():
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        self.category_pokes[key] = {p.strip().lower() for p in data if isinstance(p, str)}
                except Exception as e:
                    print(f"[Recognizer] Failed to load {filepath}: {e}")
                    self.category_pokes[key] = set()
            else:
                self.category_pokes[key] = set()

    async def _ensure_model_loaded(self):
        """Lazy loads heavy ONNX weights into RAM only when the first spawn arrives."""
        if not self.is_model_loaded:
            await asyncio.to_thread(self.recognizer.load_resources)
            self.is_model_loaded = True
            gc.collect()

    async def cog_load(self):
        self._load_category_pokes()
        
        # Tight connector settings to prevent socket & buffer RAM spikes
        connector = aiohttp.TCPConnector(
            family=socket.AF_INET,
            ttl_dns_cache=300,
            limit=3,
            enable_cleanup_closed=True
        )
        timeout = aiohttp.ClientTimeout(total=6, connect=3)
        self.session = aiohttp.ClientSession(connector=connector, timeout=timeout)

    async def cog_unload(self):
        if self.session and not self.session.closed:
            await self.session.close()

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

        info = (
            self.recognizer.pokeinfo.get(pokemon_name.strip())
            or self.recognizer.pokeinfo.get(pok_lower)
            or {}
        )
        
        if not info:
            for k, v in self.recognizer.pokeinfo.items():
                if k.lower() == pok_lower:
                    info = v
                    break

        raw_types = info.get("types", [])
        if isinstance(raw_types, list):
            types = {t.lower() for t in raw_types}
        else:
            types = {t.strip().lower() for t in raw_types.split("\n") if t.strip()}

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

        sh_uids, cl_uids, tp_uids, rp_uids = set(), set(), set(), set()

        for uid, target in g_data.get("sh", {}).items():
            if target and target.lower() == pok_lower:
                sh_uids.add(int(uid))

        for uid, cl_list in g_data.get("cl", {}).items():
            if isinstance(cl_list, list) and any(c.lower() == pok_lower for c in cl_list):
                cl_uids.add(int(uid))

        for uid, tp_list in g_data.get("tp", {}).items():
            if isinstance(tp_list, list):
                if any(t.lower() in types for t in tp_list):
                    tp_uids.add(int(uid))

        is_gmax = pok_lower in self.category_pokes.get("gmax", set())
        is_paradox = pok_lower in self.category_pokes.get("paradox", set())
        is_eevos = pok_lower in self.category_pokes.get("eevos", set())

        for uid, rp_list in g_data.get("rp", {}).items():
            if isinstance(rp_list, list):
                if any((region and r.lower() == region) or 
                       (is_gmax and r.lower() == "gmax") or 
                       (is_paradox and r.lower() == "paradox") or 
                       (is_eevos and r.lower() == "eevos") for r in rp_list):
                    rp_uids.add(int(uid))

        # Format ping lists through AFK cog if available to suppress direct mentions
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
            if pok_lower in self.category_pokes.get(cat_key, set()):
                activated_categories.append(cat_key)
                role_id = roles.get(cat_key)
                if role_id:
                    lines.append(f"{label}: <@&{role_id}>")
                else:
                    lines.append(f"{label}: no role configured")

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
        finally:
            del embed

    @commands.command(name="rec", aliases=["recognize"])
    async def manual_recognize(self, ctx: commands.Context):
        """Manually trigger Pokémon recognition on an image or replied message."""
        image_url = None
        
        if ctx.message.attachments:
            image_url = ctx.message.attachments[0].url
        elif ctx.message.reference and ctx.message.reference.message_id:
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

        if not image_url:
            await ctx.send("❌ Please attach an image or reply to a message containing a Pokémon image.")
            return

        await self._ensure_model_loaded()
        async with ctx.typing():
            try:
                pokemon_name, confidence = await self.recognizer.identify_from_url(self.session, image_url)
                pings, _, _ = await self._get_ping_info(ctx.guild.id if ctx.guild else 0, pokemon_name)
                
                out_text = f"**{format_name(pokemon_name)}**: {confidence:.2%}"
                if pings:
                    out_text += f"\n{pings}"
                await ctx.send(out_text)
            except Exception as e:
                await ctx.send(f"❌ Recognition failed: `{e}`")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.id != self.poketwo_id:
            return

        content = message.content.lower()
        is_spawn = "appeared" in content and "pok" in content
        is_catch_or_flee = "you caught a level" in content or "fled" in content

        text_to_check = content
        if message.embeds:
            text_to_check += " " + " ".join(f"{e.title or ''} {e.description or ''}" for e in message.embeds).lower()
        
        # Event-driven verification
        if is_catch_or_flee:
            if message.channel.id in self.pending_verifications:
                actual_pokemon = extract_pokemon_from_text(text_to_check)
                if actual_pokemon:
                    pending_data = self.pending_verifications.pop(message.channel.id, None)
                    if pending_data:
                        predicted_name = pending_data["predicted"]
                        is_correct = predicted_name.lower() == actual_pokemon
                        
                        if is_correct or actual_pokemon in self.recognizer.pokevars:
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

        # Spawn detection
        if not ("appeared" in text_to_check and "pok" in text_to_check):
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

        # Ensure model is initialized on demand without blocking application startup
        await self._ensure_model_loaded()

        try:
            pokemon_name, confidence = await self.recognizer.identify_from_url(self.session, image_url)
        except Exception as e:
            print(f"[ONNX Inference Error] {e}")
            return
            
        gc.collect()

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

        self.pending_verifications[message.channel.id] = {
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