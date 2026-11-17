import os

# Prevent multi-threading memory expansion across CPU cores
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
os.environ["NUMEXPR_NUM_THREADS"] = "1"

import asyncio
import gc
import io
import json
import re
from pathlib import Path

import aiohttp

# -------------------------------------------------------------------
# Preprocessing constants (KEEP IDENTICAL to indexer.py)
# -------------------------------------------------------------------
CLIP_MEAN = (0.48145466, 0.4578275, 0.40821073)
CLIP_STD = (0.26862954, 0.26130258, 0.27577711)
TARGET_SIZE = 224
ZOOM_FACTOR = 0.85
BG_COLOR = (255, 255, 255)

COLOR_WORK_SIZE = 96
COLOR_INNER_MARGIN = 0.15
HUE_BINS = 24
GRID = 3
COLOR_DIM = HUE_BINS + GRID * GRID * 3
SAT_MIN = 40
VAL_MIN = 100

# -------------------------------------------------------------------
# Matching / re-ranking parameters (tune these)
# -------------------------------------------------------------------
SIM_CHUNK_ROWS = 4096
TOP_K_IMAGES = 40
CLASS_TOP_N = 3
CLASS_MAX_WEIGHT = 0.75
RERANK_MARGIN = 0.06
RERANK_MAX_CLASSES = 6
COLOR_WEIGHT = 0.30
HUE_DIST_WEIGHT = 0.70
GRID_DIST_WEIGHT = 0.30


def flatten_alpha(img):
    from PIL import Image

    if img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info
    ):
        rgba = img.convert("RGBA")
        background = Image.new("RGBA", rgba.size, BG_COLOR + (255,))
        background.alpha_composite(rgba)
        return background.convert("RGB")
    return img.convert("RGB")


def center_square(img):
    width, height = img.size
    size = max(1, int(min(width, height) * ZOOM_FACTOR))
    left = (width - size) // 2
    top = (height - size) // 2
    return img.crop((left, top, left + size, top + size))


def color_features(square):
    import numpy as np
    from PIL import Image

    width, _ = square.size
    margin = int(width * COLOR_INNER_MARGIN)
    inner = square.crop((margin, margin, width - margin, width - margin))
    inner = inner.resize((COLOR_WORK_SIZE, COLOR_WORK_SIZE), Image.Resampling.BOX)

    hsv = np.asarray(inner.convert("HSV"), dtype=np.float32)
    rgb = np.asarray(inner, dtype=np.float32) / 255.0

    hue = hsv[..., 0]
    sat = hsv[..., 1]
    val = hsv[..., 2]
    mask = (sat >= SAT_MIN) & (val >= VAL_MIN)

    bins = np.minimum((hue[mask] * HUE_BINS / 256.0).astype(np.int32), HUE_BINS - 1)
    hist = np.bincount(bins, minlength=HUE_BINS).astype(np.float32)
    hist /= float(COLOR_WORK_SIZE * COLOR_WORK_SIZE)
    hist = 0.25 * np.roll(hist, 1) + 0.5 * hist + 0.25 * np.roll(hist, -1)
    hist = np.sqrt(hist)

    cell = COLOR_WORK_SIZE // GRID
    grid = rgb.reshape(GRID, cell, GRID, cell, 3).mean(axis=(1, 3)).reshape(-1)

    return np.concatenate([hist, grid]).astype(np.float32)


def preprocess_image(img):
    """Returns (CHW float32 CLIP tensor, color feature vector)."""
    import numpy as np
    from PIL import Image

    rgb = flatten_alpha(img)
    square = center_square(rgb)

    resized = square.resize((TARGET_SIZE, TARGET_SIZE), Image.Resampling.BICUBIC)
    arr = np.asarray(resized, dtype=np.float32)
    arr /= 255.0
    arr -= np.array(CLIP_MEAN, dtype=np.float32)
    arr /= np.array(CLIP_STD, dtype=np.float32)
    arr = np.ascontiguousarray(arr.transpose(2, 0, 1))

    return arr, color_features(square)


def color_distance(query, rows):
    import numpy as np

    diff = np.abs(rows - query)
    hue_dist = diff[:, :HUE_BINS].sum(axis=1)
    grid_dist = diff[:, HUE_BINS:].mean(axis=1)
    return HUE_DIST_WEIGHT * hue_dist + GRID_DIST_WEIGHT * grid_dist


def resolve_file_path(filename: str) -> Path:
    """Searches for a file in the cog folder first, then falls back to current working directory."""
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
        self.db_colors = None
        self.converts_sorted = []
        self.pokevars = set()
        self.pokeinfo = {}

    def load_resources(self):
        """Loads lightweight ONNX model & memory-mapped vector db from disk."""
        import numpy as np
        import onnxruntime as ort

        # 1. Load ONNX Vision Model with low-memory settings
        try:
            model_path = resolve_file_path("models/clip_vision_quantized.onnx")
            opts = ort.SessionOptions()
            opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            opts.graph_optimization_level = (
                ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
            )
            opts.intra_op_num_threads = 1
            opts.inter_op_num_threads = 1
            opts.enable_cpu_mem_arena = False
            opts.enable_mem_pattern = False

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
            print(f"[Recognize] Warning: Could not load models/clip_vision_quantized.onnx ({e})")

        # 2. Memory-map vectors directly from disk without consuming RAM
        try:
            vectors_path = resolve_file_path("models/pokemon_db_vectors.npy")
            names_path = resolve_file_path("models/pokemon_db_names.json")

            self.db_vectors = np.load(str(vectors_path), mmap_mode="r")

            with open(names_path, "r", encoding="utf-8") as f:
                self.db_names = json.load(f)
        except Exception as e:
            print(f"[Recognize] Warning: Could not memory-map vector db ({e})")

        # 2b. Optional color feature db used to separate near-identical species
        self.db_colors = None
        try:
            colors_path = resolve_file_path("models/pokemon_db_colors.npy")
            colors = np.load(str(colors_path), mmap_mode="r")
            if (
                self.db_vectors is not None
                and colors.ndim == 2
                and colors.shape[0] == self.db_vectors.shape[0]
                and colors.shape[1] == COLOR_DIM
            ):
                self.db_colors = colors
            else:
                print(
                    "[Recognize] Warning: models/pokemon_db_colors.npy does not match "
                    "the vector db, color re-ranking disabled. Re-run indexer.py."
                )
        except Exception:
            print(
                "[Recognize] Note: models/pokemon_db_colors.npy not found, "
                "color re-ranking disabled. Re-run indexer.py to enable it."
            )

        # 3. Load constant local datasets
        try:
            converts_path = resolve_file_path("data/pokes/converts.json")
            with open(converts_path, "r", encoding="utf-8") as f:
                converts = json.load(f)
                self.converts_sorted = sorted(
                    converts.items(), key=lambda x: len(x[0]), reverse=True
                )
        except Exception:
            self.converts_sorted = []

        try:
            pokevars_path = resolve_file_path("data/pokevars.json")
            with open(pokevars_path, "r", encoding="utf-8") as f:
                pokevars_list = json.load(f)
                self.pokevars = {name.strip().lower() for name in pokevars_list}
        except Exception:
            self.pokevars = set()

        try:
            pokeinfo_path = resolve_file_path("pokeinfo.json")
            with open(pokeinfo_path, "r", encoding="utf-8") as f:
                pokeinfo_raw = json.load(f)
                self.pokeinfo = {
                    k.strip().lower(): {
                        "types": v.get("types", []) if isinstance(v, dict) else [],
                        "region": v.get("region", "") if isinstance(v, dict) else ""
                    }
                    for k, v in pokeinfo_raw.items()
                }
        except Exception:
            self.pokeinfo = {}

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

    def _similarities(self, target):
        """Cosine similarities in float32, computed in chunks over the mmap'd db."""
        import numpy as np

        total = self.db_vectors.shape[0]
        sims = np.empty(total, dtype=np.float32)

        for start in range(0, total, SIM_CHUNK_ROWS):
            end = min(start + SIM_CHUNK_ROWS, total)
            chunk = np.asarray(self.db_vectors[start:end], dtype=np.float32)
            sims[start:end] = chunk @ target

        return sims

    def _rank(self, sims, query_color) -> tuple[str, float]:
        """Class-level scoring over the top images, then color re-ranking of close contenders."""
        import numpy as np

        total = sims.shape[0]
        k = min(TOP_K_IMAGES, total)

        if k < total:
            top = np.argpartition(-sims, k - 1)[:k]
        else:
            top = np.arange(total)
        top = top[np.argsort(-sims[top])]

        groups = {}
        for idx in top:
            groups.setdefault(self.db_names[int(idx)], []).append(int(idx))

        scored = []
        for name, idxs in groups.items():
            vals = sims[idxs[:CLASS_TOP_N]]
            score = CLASS_MAX_WEIGHT * float(vals[0]) + (1.0 - CLASS_MAX_WEIGHT) * float(vals.mean())
            scored.append((score, name, idxs))
        scored.sort(key=lambda item: -item[0])

        winner = scored[0]

        if self.db_colors is not None and query_color is not None and len(scored) > 1:
            best_score = scored[0][0]
            contenders = [
                item
                for item in scored[:RERANK_MAX_CLASSES]
                if best_score - item[0] <= RERANK_MARGIN
            ]

            if len(contenders) > 1:
                query = query_color.astype(np.float32)
                adjusted = []
                for score, name, idxs in contenders:
                    rows = np.asarray(
                        self.db_colors[idxs[:CLASS_TOP_N]], dtype=np.float32
                    )
                    distance = float(color_distance(query, rows).mean())
                    adjusted.append((score - COLOR_WEIGHT * distance, name, idxs))
                adjusted.sort(key=lambda item: -item[0])
                winner = adjusted[0]

        _, name, idxs = winner
        return str(name), float(sims[idxs[0]])

    def predict_from_image_bytes(self, image_bytes: bytes) -> tuple[str, float]:
        import numpy as np
        from PIL import Image

        if self.ort_session is None or self.db_vectors is None:
            raise RuntimeError("ONNX Session or Database not loaded properly.")

        with Image.open(io.BytesIO(image_bytes)) as img:
            pixels, query_color = preprocess_image(img)

        input_tensor = pixels[None, ...]
        outputs = self.ort_session.run(None, {self.input_name: input_tensor})

        target_vector = np.asarray(outputs[0], dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(target_vector))
        if norm != 0:
            target_vector = target_vector / norm

        similarities = self._similarities(target_vector)
        raw_name, confidence = self._rank(similarities, query_color)
        converted_name = self.apply_conversions(raw_name)

        del input_tensor, pixels, outputs, target_vector, similarities
        gc.collect()

        return converted_name, confidence

    async def identify_from_url(
        self, session: aiohttp.ClientSession, url: str
    ) -> tuple[str, float]:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise ValueError(f"Failed to download image. HTTP {resp.status}")
            image_bytes = await resp.read()

        return await asyncio.to_thread(
            self.predict_from_image_bytes, image_bytes
        )