import asyncio
import gc
import io
import os
import re
import aiohttp
import discord
from discord.ext import commands
import numpy as np
import onnxruntime as ort
from PIL import Image

TARGET_USER_ID = 716390085896962058
LOG_CHANNEL_ID = 1534081811511246910
DB_PATH = "pokemon_db.npz"
MODEL_PATH = "vision_model_quantized.onnx"

# Direct URL for Xenova's quantized vision model
MODEL_URL = "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/onnx/vision_model_quantized.onnx"


class PokemonRecognizeCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Track pending predictions: channel_id -> {"predicted": str, "confidence": float, "url": str}
        self.pending_predictions = {}
        self.session: aiohttp.ClientSession | None = None
        self.ort_session: ort.InferenceSession | None = None

        # 1. Load Pokémon Vector Database
        if not os.path.exists(DB_PATH):
            raise FileNotFoundError(
                f"Database file '{DB_PATH}' not found in root directory!"
            )

        print("[Pokétwo Cog] Loading Pokémon database...")
        # Use mmap_mode="r" to avoid reading the whole file into RAM upfront
        db = np.load(DB_PATH, mmap_mode="r")
        self.db_vectors = np.array(db["vectors"], dtype=np.float32)  # Shape: (N, 512)
        self.db_names = db["names"]  # Shape: (N,)
        del db
        gc.collect()

    async def cog_load(self):
        """Async initialization downloading model to disk to enable memory-mapping."""
        self.session = aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0"}
        )

        # Download model to disk in 1MB chunks to keep RAM usage minimal
        if not os.path.exists(MODEL_PATH):
            print("[Pokétwo Cog] Model not found locally. Downloading to disk...")
            async with self.session.get(MODEL_URL) as resp:
                if resp.status != 200:
                    raise RuntimeError(
                        f"Failed to fetch model from HF: HTTP {resp.status}"
                    )
                with open(MODEL_PATH, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1024 * 1024):
                        f.write(chunk)
            print("[Pokétwo Cog] Model download complete.")

        print("[Pokétwo Cog] Initializing memory-mapped ONNX Runtime Session...")
        opts = ort.SessionOptions()
        # Aggressive RAM Saver Settings
        opts.enable_cpu_mem_arena = False
        opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1

        # Load session using file path so ONNX memory-maps it without duplicating RAM
        self.ort_session = ort.InferenceSession(
            MODEL_PATH,
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )

        gc.collect()
        print("[Pokétwo Cog] Recognized Cog loaded and ready!")

    async def cog_unload(self):
        """Cleanup network session upon cog unloading."""
        if self.session and not self.session.closed:
            await self.session.close()

    def _preprocess_image(self, image: Image.Image) -> np.ndarray:
        """Pure NumPy CLIP image preprocessing."""
        image = image.resize((224, 224), Image.BICUBIC)
        img_np = np.array(image, dtype=np.float32) / 255.0

        # OpenAI CLIP normalization
        mean = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
        std = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)

        img_np = (img_np - mean) / std
        img_np = np.transpose(img_np, (2, 0, 1))  # (C, H, W)
        img_np = np.expand_dims(img_np, axis=0)  # (1, C, H, W)

        return img_np

    def _predict_pokemon(self, image_bytes: bytes) -> tuple[str, float]:
        """Pure NumPy / ONNX prediction worker with instant garbage collection."""
        if not self.ort_session:
            raise RuntimeError("ONNX Session is not initialized yet.")

        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        pixel_values = self._preprocess_image(image)

        input_name = self.ort_session.get_inputs()[0].name
        outputs = self.ort_session.run(None, {input_name: pixel_values})

        # Extract pooled image vector
        query_vec = outputs[1] if len(outputs) > 1 else outputs[0]

        if len(query_vec.shape) == 3:
            query_vec = query_vec[:, 0, :]

        # L2 Normalization
        query_vec = query_vec / np.linalg.norm(
            query_vec, axis=-1, keepdims=True
        )

        # Cosine similarity
        similarities = np.dot(self.db_vectors, query_vec.T).squeeze()

        best_idx = np.argmax(similarities)
        best_name = str(self.db_names[best_idx])
        best_score = float(similarities[best_idx])

        # Force clear local variables & run GC to protect against RAM caps
        del image, pixel_values, outputs, query_vec, similarities
        gc.collect()

        return best_name, best_score

    async def _log_misprediction(
        self,
        predicted: str,
        actual: str,
        confidence: float,
        image_url: str,
    ):
        """Sends misprediction details to the designated log channel."""
        log_channel = self.bot.get_channel(LOG_CHANNEL_ID)
        if not log_channel:
            try:
                log_channel = await self.bot.fetch_channel(LOG_CHANNEL_ID)
            except Exception as e:
                print(f"[Pokétwo Cog] Failed to fetch log channel: {e}")
                return

        embed = discord.Embed(
            title="❌ Misprediction Detected", color=discord.Color.red()
        )
        embed.add_field(name="Predicted", value=predicted.title(), inline=True)
        embed.add_field(name="Actual", value=actual.title(), inline=True)
        embed.add_field(
            name="Confidence", value=f"{confidence:.2f}%", inline=True
        )
        if image_url:
            embed.set_thumbnail(url=image_url)

        await log_channel.send(embed=embed)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.id != TARGET_USER_ID:
            return

        channel_id = message.channel.id

        # 1. Handle Catch Result (Congratulations message)
        if (
            "Congratulations" in message.content
            and "You caught a" in message.content
        ):
            # Matches: "You caught a Level 8 Vileplume (65.59%)!" or "You caught a Vileplume!"
            match = re.search(
                r"You caught a (?:Level \d+ )?([A-Za-z0-9\-\.\'\s]+?)(?:\s*\([\d\.\%]+\))?!",
                message.content,
            )
            if match:
                actual_name = match.group(1).strip()
                if channel_id in self.pending_predictions:
                    data = self.pending_predictions.pop(channel_id)
                    if data["predicted"].lower() != actual_name.lower():
                        await self._log_misprediction(
                            predicted=data["predicted"],
                            actual=actual_name,
                            confidence=data["confidence"],
                            image_url=data["url"],
                        )
            return

        # 2. Check for Embeds (Spawns or Fled events)
        if message.embeds:
            embed = message.embeds[0]
            if not embed.title:
                return

            title_lower = embed.title.lower()

            # Handle Fled Event (e.g., "Wild Machop fled. A new wild pokémon has appeared!")
            if "fled" in title_lower:
                match = re.search(
                    r"Wild\s+([A-Za-z0-9\-\.\'\s]+?)\s+fled",
                    embed.title,
                    re.IGNORECASE,
                )
                if match:
                    actual_name = match.group(1).strip()
                    if channel_id in self.pending_predictions:
                        data = self.pending_predictions.pop(channel_id)
                        if data["predicted"].lower() != actual_name.lower():
                            await self._log_misprediction(
                                predicted=data["predicted"],
                                actual=actual_name,
                                confidence=data["confidence"],
                                image_url=data["url"],
                            )

            # Handle New Wild Pokémon Spawn
            if title_lower.startswith("a wild") or title_lower.startswith(
                "wild"
            ):
                image_url = None
                if embed.image and embed.image.url:
                    image_url = embed.image.url
                elif embed.thumbnail and embed.thumbnail.url:
                    image_url = embed.thumbnail.url

                if not image_url or not self.session:
                    return

                try:
                    async with self.session.get(image_url) as response:
                        if response.status != 200:
                            return
                        image_bytes = await response.read()

                    pokemon_name, confidence = await asyncio.to_thread(
                        self._predict_pokemon, image_bytes
                    )

                    accuracy_pct = confidence * 100
                    formatted_name = str(pokemon_name).title()

                    # Save prediction details for validation
                    self.pending_predictions[channel_id] = {
                        "predicted": str(pokemon_name),
                        "confidence": accuracy_pct,
                        "url": image_url,
                    }

                    await message.channel.send(
                        f"Detected: {formatted_name} ({accuracy_pct:.2f}%)"
                    )

                except Exception as e:
                    print(f"[Pokétwo Cog] Error processing image: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(PokemonRecognizeCog(bot))