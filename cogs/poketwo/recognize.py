import asyncio
import gc
import io
import json
import re
from pathlib import Path

import aiohttp
import certifi
import numpy as np
import onnxruntime as ort
from PIL import Image
from pymongo import MongoClient

MONGO_URI = "mongodb+srv://KurtisLam:CsHLOnDqihiU5uYG@cluster0.7rwx3oc.mongodb.net/?appName=Cluster0"


def resolve_file_path(filename: str) -> Path:
    """Searches for a file in the cog/module folder first, then falls back to current working directory."""
    cog_dir_path = Path(__file__).resolve().parent / filename
    root_dir_path = Path.cwd() / filename

    if cog_dir_path.exists():
        return cog_dir_path
    if root_dir_path.exists():
        return root_dir_path

    raise FileNotFoundError(
        f"Could not locate '{filename}'. Checked:\n"
        f"  - {cog_dir_path}\n"
        f"  - {root_dir_path}"
    )


def format_name(name: str) -> str:
    """Capitalizes names correctly while preserving apostrophes, hyphens, colons, and percentages."""
    if not name:
        return ""

    words = name.split(" ")
    formatted_words = []

    for word in words:
        hyphen_parts = word.split("-")
        formatted_hyphen_parts = []

        for part in hyphen_parts:
            colon_parts = part.split(":")
            formatted_colon_parts = []

            for c_part in colon_parts:
                if "'" in c_part:
                    apo_parts = c_part.split("'")
                    formatted_colon_parts.append(
                        apo_parts[0].capitalize() + "'" + apo_parts[1].lower()
                    )
                else:
                    formatted_colon_parts.append(c_part.capitalize())

            formatted_hyphen_parts.append(":".join(formatted_colon_parts))

        formatted_words.append("-".join(formatted_hyphen_parts))

    formatted_str = " ".join(formatted_words)
    formatted_str = re.sub(r"\b(Jangmo|Hakamo|Kommo)-O\b", r"\1-o", formatted_str)
    return formatted_str


def extract_pokemon_from_text(text: str) -> str | None:
    """Extracts the Pokémon name from caught/fled messages."""
    cleaned_text = re.sub(r"<a?:[a-zA-Z0-9_]+:\d+>", "", text)
    cleaned_text = re.sub(r"[♂♀♂️♀️✨]", "", cleaned_text)

    caught_match = re.search(
        r"you caught a level \d+\s+([^\(!]+?)\s*(?:\(|\!)",
        cleaned_text,
        re.IGNORECASE,
    )
    if caught_match:
        return caught_match.group(1).strip().lower()

    fled_match = re.search(
        r"wild\s+([^\!\.\n]+?)\s+fled", cleaned_text, re.IGNORECASE
    )
    if fled_match:
        return fled_match.group(1).strip().lower()

    return None


class PokemonRecognizer:
    def __init__(self):
        self.ort_session = None
        self.input_name = None
        self.db_vectors = None
        self.db_names = None
        self.converts_sorted = []
        self.pokevars = set()
        self.pokeinfo = {}
        self.mongo_client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
        self.db = self.mongo_client["utilities"]
        self.constdata = self.db["constdata"]

    def _preprocess_image(
        self, img: Image.Image, target_size: int = 224
    ) -> np.ndarray:
        """Preprocesses image for CLIP Vision Model using NumPy, keeping parity with indexer."""
        img = img.convert("RGB")

        # 1. Apply exact parity cropping logic
        width, height = img.size
        zoom_factor = 0.85  # Must match the indexer's zoom factor
        new_size = int(min(width, height) * zoom_factor)
        left = (width - new_size) // 2
        top = (height - new_size) // 2
        right = left + new_size
        bottom = top + new_size
        
        # 2. Crop to strict square, then resize (prevents stretching)
        img = img.crop((left, top, right, bottom))
        img = img.resize((target_size, target_size), Image.Resampling.BICUBIC)

        arr = np.array(img, dtype=np.float32) / 255.0

        mean = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
        std = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)

        arr = (arr - mean) / std
        arr = arr.transpose(2, 0, 1)  # HWC -> CHW
        return np.expand_dims(arr, axis=0)  # (1, 3, 224, 224)

    def load_resources(self):
        """Loads lightweight ONNX model & vector db from disk, and constant datasets from MongoDB Atlas."""

        # 1. Load ONNX Vision Model with strict memory limits
        try:
            model_path = resolve_file_path("clip_vision_quantized.onnx")
            opts = ort.SessionOptions()
            opts.graph_optimization_level = (
                ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
            )
            opts.intra_op_num_threads = 1
            opts.inter_op_num_threads = 1

            # Disable RAM Arena allocations to prevent OOM
            opts.add_session_config_entry("session.enable_cpu_mem_arena", "0")
            opts.add_session_config_entry(
                "session.use_device_allocator_for_initializers", "0"
            )

            self.ort_session = ort.InferenceSession(
                str(model_path),
                sess_options=opts,
                providers=["CPUExecutionProvider"],
            )
            self.input_name = self.ort_session.get_inputs()[0].name
        except Exception as e:
            print(
                f"[Recognize] Critical Warning: Could not load clip_vision_quantized.onnx ({e})"
            )

        # 2. Load NumPy Vector Database
        try:
            db_path = resolve_file_path("pokemon_db.npz")
            with np.load(str(db_path)) as db:
                raw_vectors = np.array(db["vectors"], dtype=np.float32)
                self.db_names = np.array(db["names"])

            norms = np.linalg.norm(raw_vectors, axis=-1, keepdims=True)
            norms[norms == 0] = 1.0
            self.db_vectors = raw_vectors / norms

            del raw_vectors, norms
        except Exception as e:
            print(
                f"[Recognize] Critical Warning: Could not load pokemon_db.npz ({e})"
            )

        # 3. Load constant JSON data from MongoDB Atlas
        try:
            converts_doc = self.constdata.find_one({"_id": "converts"})
            converts = converts_doc.get("data", {}) if converts_doc else {}
            self.converts_sorted = sorted(
                converts.items(), key=lambda x: len(x[0]), reverse=True
            )
        except Exception as e:
            print(
                f"[Recognize] Warning: Could not load converts from MongoDB ({e})"
            )
            self.converts_sorted = []

        try:
            pokevars_doc = self.constdata.find_one({"_id": "pokevars"})
            pokevars_list = pokevars_doc.get("data", []) if pokevars_doc else []
            self.pokevars = {name.strip().lower() for name in pokevars_list}
        except Exception as e:
            print(
                f"[Recognize] Warning: Could not load pokevars from MongoDB ({e})"
            )
            self.pokevars = set()

        try:
            pokedex_doc = self.constdata.find_one({"_id": "pokedex"})
            pokeinfo_raw = pokedex_doc.get("data", {}) if pokedex_doc else {}
            self.pokeinfo = {
                k.strip().lower(): v for k, v in pokeinfo_raw.items()
            }
        except Exception as e:
            print(
                f"[Recognize] Warning: Could not load pokedex from MongoDB ({e})"
            )
            self.pokeinfo = {}

        # Force garbage collection to free startup allocations
        gc.collect()

    def apply_conversions(self, name: str) -> str:
        if not self.converts_sorted:
            return name

        current_name = name
        for key, val in self.converts_sorted:
            if key.lower() in current_name.lower():
                pattern = re.compile(re.escape(key), re.IGNORECASE)
                current_name = pattern.sub(val, current_name)

        return current_name

    def predict_from_image_bytes(self, image_bytes: bytes) -> tuple[str, float]:
        if self.ort_session is None or self.db_vectors is None:
            raise RuntimeError(
                "ONNX Session or Database not loaded properly."
            )

        with Image.open(io.BytesIO(image_bytes)) as img:
            input_tensor = self._preprocess_image(img, target_size=224)

        outputs = self.ort_session.run(None, {self.input_name: input_tensor})
        target_vector = outputs[0]

        norm = np.linalg.norm(target_vector, axis=-1, keepdims=True)
        if norm != 0:
            target_vector = target_vector / norm

        similarities = np.dot(target_vector, self.db_vectors.T).squeeze()
        best_match_idx = int(np.argmax(similarities))
        confidence = float(similarities[best_match_idx])

        raw_name = str(self.db_names[best_match_idx])
        converted_name = self.apply_conversions(raw_name)

        return converted_name, confidence

    async def identify_from_url(
        self, session: aiohttp.ClientSession, url: str
    ) -> tuple[str, float]:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise ValueError(
                    f"Failed to download image. HTTP {resp.status}"
                )
            image_bytes = await resp.read()

        return await asyncio.to_thread(
            self.predict_from_image_bytes, image_bytes
        )