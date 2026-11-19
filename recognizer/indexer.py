import json
import os
import random
import zipfile
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image
from tqdm.auto import tqdm

# -------------------------------------------------------------------
# 1. Path Configuration
# -------------------------------------------------------------------
BASE_DIR = os.path.abspath(".")
LOCAL_DATA_DIR = os.path.join(BASE_DIR, "recognizer/traindata")
LOCAL_ZIP_FILE = os.path.join(BASE_DIR, "data.zip")

MODEL_DIR = os.path.join(BASE_DIR, "models")
OUTPUT_VECTORS = os.path.join(MODEL_DIR, "pokemon_db_vectors.npy")
OUTPUT_NAMES = os.path.join(MODEL_DIR, "pokemon_db_names.json")
OUTPUT_TEMPLATES = os.path.join(MODEL_DIR, "pokemon_db_templates.npy")
OUTPUT_TEMPLATE_NAMES = os.path.join(MODEL_DIR, "pokemon_db_template_names.json")

# Small, object-only references for known near-identical classes. These are
# transparent PNGs, so indexing them needs only the existing Pillow/NumPy stack.
CALIBRATION_TEMPLATES = {
    "meloetta": "meloetta.png",
    "white flower flabb": "white_flower_flabb.png",
    "palafin": "palafin.png",
    "finizen": "finizen.png",
}
CALIBRATION_TEMPLATE_DIR = os.path.join(BASE_DIR, "recognizer", "calibration_templates")

# The exact quantized ONNX model used by the bot is required for indexing.
# This script deliberately has no PyTorch / Transformers fallback.
ONNX_MODEL_PATH = os.path.join(BASE_DIR, "models", "clip_vision_quantized.onnx")

CPU_COUNT = os.cpu_count() or 4

# -------------------------------------------------------------------
# 2. Shared preprocessing (KEEP IDENTICAL to recognize.py)
# -------------------------------------------------------------------
CLIP_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
CLIP_STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
TARGET_SIZE = 224
ZOOM_FACTOR = 0.85
BG_COLOR = (255, 255, 255)

# -------------------------------------------------------------------
# 3. Class template parameters
#
# Every class gets one or more small RGBA templates (artwork + mask). The bot
# aligns a query with the templates of the classes CLIP cannot separate and
# compares them pixel by pixel.
#   - transparent artwork (or artwork on a flat background): the images
#     themselves are the templates (up to TPL_MAX_ALPHA per class)
#   - opaque spawn screenshots: the per-pixel median of up to TPL_SAMPLES images
#     is the template and the pixels that stay constant across the images
#     (the Pokémon, not the changing scenery) become its mask
# -------------------------------------------------------------------
TPL_SIZE = 64
TPL_MAX_ALPHA = 3
TPL_SAMPLES = 16
TPL_MIN_MEDIAN = 5
STAB_LO = 6.0
STAB_HI = 22.0
MIN_STABLE_AREA = 0.04
BORDER_TOL = 40
FLOOD_TOL = 60


def flatten_alpha(img):
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


def preprocess_image(img):
    """CHW float32 CLIP tensor."""
    square = center_square(flatten_alpha(img))

    resized = square.resize((TARGET_SIZE, TARGET_SIZE), Image.Resampling.BICUBIC)
    arr = np.asarray(resized, dtype=np.float32)
    arr /= 255.0
    arr -= CLIP_MEAN
    arr /= CLIP_STD
    return np.ascontiguousarray(arr.transpose(2, 0, 1))


# -------------------------------------------------------------------
# 4. Template building
# -------------------------------------------------------------------
def box3(x):
    height, width = x.shape[:2]
    pad = np.pad(x, ((1, 1), (1, 1)) + ((0, 0),) * (x.ndim - 2), mode="edge")
    out = np.zeros_like(x, dtype=np.float32)
    for dy in range(3):
        for dx in range(3):
            out += pad[dy : dy + height, dx : dx + width]
    return out / 9.0


def derive_alpha(rgb):
    """Alpha for artwork sitting on a flat background (flood fill from the border).
    Returns None when the border is not a uniform colour (i.e. a real scene)."""
    height, width, _ = rgb.shape
    ring = np.zeros((height, width), dtype=bool)
    ring[:2, :] = True
    ring[-2:, :] = True
    ring[:, :2] = True
    ring[:, -2:] = True

    px = rgb.astype(np.int16)
    border_colour = np.median(px[ring], axis=0)
    dist = np.abs(px - border_colour).sum(axis=-1)
    if float((dist[ring] < BORDER_TOL).mean()) < 0.9:
        return None

    candidate = dist < FLOOD_TOL
    background = candidate & ring
    while True:
        grown = background.copy()
        grown[1:, :] |= background[:-1, :]
        grown[:-1, :] |= background[1:, :]
        grown[:, 1:] |= background[:, :-1]
        grown[:, :-1] |= background[:, 1:]
        grown &= candidate
        if int(grown.sum()) == int(background.sum()):
            break
        background = grown

    foreground = ~background
    eroded = foreground.copy()
    eroded[1:, :] &= foreground[:-1, :]
    eroded[:-1, :] &= foreground[1:, :]
    eroded[:, 1:] &= foreground[:, :-1]
    eroded[:, :-1] &= foreground[:, 1:]

    share = float(eroded.mean())
    if not 0.05 <= share <= 0.95:
        return None
    return np.round(box3(eroded.astype(np.float32)) * 255.0).astype(np.uint8)


def make_thumb(img):
    """TPL_SIZE x TPL_SIZE x 4 uint8 (RGB + alpha) of the whole image and its kind:
    'alpha' (real or derived transparency) or 'opaque' (a scene)."""
    size = TPL_SIZE
    has_alpha_mode = img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info
    )

    if has_alpha_mode:
        rgba = img.convert("RGBA")
        alpha = np.asarray(rgba.getchannel("A"))
        if int(alpha.min()) < 250 and float((alpha < 250).mean()) > 0.02:
            width, height = rgba.size
            side = max(width, height)
            canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            canvas.paste(rgba, ((side - width) // 2, (side - height) // 2))
            method = Image.Resampling.BOX if side > size else Image.Resampling.BILINEAR
            return np.array(canvas.resize((size, size), method)), "alpha"
        rgb = rgba.convert("RGB")
    else:
        rgb = img.convert("RGB")

    width, height = rgb.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    square = rgb.crop((left, top, left + side, top + side))
    method = Image.Resampling.BOX if side > size else Image.Resampling.BILINEAR
    small = np.asarray(square.resize((size, size), method))

    derived = derive_alpha(small)
    if derived is not None:
        return np.dstack([small, derived]), "alpha"
    return np.dstack([small, np.full((size, size), 255, dtype=np.uint8)]), "opaque"


def median_template(thumbs):
    """Median of opaque screenshots; pixels that stay constant form the mask."""
    stack = np.stack(thumbs).astype(np.float32)
    median = np.median(stack, axis=0)
    spread = np.median(np.abs(stack - median), axis=0).mean(axis=-1)

    alpha = np.clip((STAB_HI - spread) / (STAB_HI - STAB_LO), 0.0, 1.0)
    alpha = box3(alpha)
    if float((alpha > 0.5).mean()) < MIN_STABLE_AREA:
        return None

    out = np.dstack([np.clip(median, 0, 255), alpha * 255.0])
    return np.round(out).astype(np.uint8)


class TemplateBuilder:
    def __init__(self):
        self.order = []
        self.alpha = {}
        self.opaque = {}
        self.seen = {}
        self.rng = random.Random(0)

    def add(self, name, thumb, kind):
        if name not in self.seen:
            self.seen[name] = 0
            self.order.append(name)

        if kind == "alpha":
            kept = self.alpha.setdefault(name, [])
            if len(kept) < TPL_MAX_ALPHA:
                kept.append(thumb)
            return

        rgb = np.ascontiguousarray(thumb[..., :3])
        kept = self.opaque.setdefault(name, [])
        self.seen[name] += 1
        if len(kept) < TPL_SAMPLES:
            kept.append(rgb)
        else:
            slot = self.rng.randint(0, self.seen[name] - 1)
            if slot < TPL_SAMPLES:
                kept[slot] = rgb

    def build(self):
        templates, names = [], []
        stats = {"artwork": 0, "median": 0, "plain": 0, "skipped": 0}

        for name in self.order:
            artwork = self.alpha.get(name)
            if artwork:
                templates.extend(artwork)
                names.extend([name] * len(artwork))
                stats["artwork"] += 1
                continue

            scenes = self.opaque.get(name, [])
            if len(scenes) >= TPL_MIN_MEDIAN:
                template = median_template(scenes)
                if template is None:
                    stats["skipped"] += 1
                    continue
                templates.append(template)
                names.append(name)
                stats["median"] += 1
            elif scenes:
                kept = scenes[:TPL_MAX_ALPHA]
                solid = np.full((TPL_SIZE, TPL_SIZE, 1), 255, dtype=np.uint8)
                templates.extend(np.concatenate([rgb, solid], axis=-1) for rgb in kept)
                names.extend([name] * len(kept))
                stats["plain"] += 1

        if not templates:
            return np.zeros((0, TPL_SIZE, TPL_SIZE, 4), dtype=np.uint8), names, stats
        return np.stack(templates).astype(np.uint8), names, stats


# -------------------------------------------------------------------
# 5. Dataset handling
# -------------------------------------------------------------------
def prepare_dataset():
    """Handles dataset extraction for local setup."""
    if os.path.exists(LOCAL_DATA_DIR) and len(os.listdir(LOCAL_DATA_DIR)) > 0:
        print(f"Dataset ready at path: '{LOCAL_DATA_DIR}'")
        return

    if os.path.exists(LOCAL_ZIP_FILE):
        print(f"Extracting '{LOCAL_ZIP_FILE}'...")
        with zipfile.ZipFile(LOCAL_ZIP_FILE, "r") as zip_ref:
            zip_ref.extractall(BASE_DIR)
        print("Extraction complete!")
    else:
        raise FileNotFoundError(
            f"Could not find valid image directory at '{LOCAL_DATA_DIR}' or zip file at '{LOCAL_ZIP_FILE}'."
        )


def list_samples(data_dir):
    samples = []
    valid_extensions = (".png", ".jpg", ".jpeg", ".webp")

    if os.path.exists(data_dir):
        for root, _, files in os.walk(data_dir):
            for file_name in sorted(files):
                if file_name.lower().endswith(valid_extensions):
                    full_path = os.path.join(root, file_name)
                    pokemon_name = os.path.basename(root)
                    if pokemon_name == os.path.basename(data_dir):
                        pokemon_name = "unknown"
                    samples.append((full_path, pokemon_name))
    return samples


def process_file(path):
    try:
        with Image.open(path) as img:
            img.load()
            pixels = preprocess_image(img)
            thumb, kind = make_thumb(img)
        return pixels, thumb, kind
    except Exception:
        return None


def iter_batches(samples, batch_size, workers):
    """Yields (batch, results); the next batch is decoded while the current one is embedded."""
    batches = [samples[i : i + batch_size] for i in range(0, len(samples), batch_size)]
    if not batches:
        return

    with ThreadPoolExecutor(max_workers=workers) as pool:

        def submit(batch):
            return [pool.submit(process_file, path) for path, _ in batch]

        pending = submit(batches[0])
        for i, batch in enumerate(batches):
            futures = pending
            pending = submit(batches[i + 1]) if i + 1 < len(batches) else None
            yield batch, [f.result() for f in futures]


# -------------------------------------------------------------------
# 6. Embedding backends
# -------------------------------------------------------------------
def build_embedder():
    """Build the image embedder from the bundled quantized ONNX model only."""
    if not os.path.exists(ONNX_MODEL_PATH):
        raise FileNotFoundError(
            f"Required ONNX model not found at '{ONNX_MODEL_PATH}'. "
            "PyTorch/Transformers fallback is intentionally disabled."
        )

    import onnxruntime as ort

    print(f"Using ONNX model for indexing: '{ONNX_MODEL_PATH}'")
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = CPU_COUNT
    opts.inter_op_num_threads = 1
    opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    session = ort.InferenceSession(
        ONNX_MODEL_PATH,
        sess_options=opts,
        providers=["CPUExecutionProvider"],
    )
    input_name = session.get_inputs()[0].name
    state = {"batched": True}

    def embed(pixels: np.ndarray) -> np.ndarray:
        if state["batched"]:
            try:
                return session.run(None, {input_name: pixels})[0]
            except Exception:
                state["batched"] = False
        rows = [
            session.run(None, {input_name: pixels[i : i + 1]})[0]
            for i in range(pixels.shape[0])
        ]
        return np.concatenate(rows, axis=0)

    return embed


def add_calibration_templates(builder):
    """Add vetted transparent references without requiring OpenCV at runtime."""
    for pokemon_name, filename in CALIBRATION_TEMPLATES.items():
        path = os.path.join(CALIBRATION_TEMPLATE_DIR, filename)
        if not os.path.isfile(path):
            continue
        try:
            with Image.open(path) as img:
                img.load()
                thumb, kind = make_thumb(img)
            if kind != "alpha":
                print(f"[Indexer] Skipping non-transparent calibration template: {path}")
                continue
            builder.add(pokemon_name, thumb, kind)
        except Exception as exc:
            print(f"[Indexer] Could not load calibration template {path}: {exc}")


# -------------------------------------------------------------------
# 7. Main Execution Block
# -------------------------------------------------------------------
def main():
    os.makedirs(MODEL_DIR, exist_ok=True)
    prepare_dataset()

    samples = list_samples(LOCAL_DATA_DIR)

    # The shipped ZIP intentionally contains only a few correction examples,
    # not the full original training corpus. Never overwrite a large database
    # with a tiny partial dataset by accident.
    if os.path.exists(OUTPUT_NAMES):
        try:
            with open(OUTPUT_NAMES, "r", encoding="utf-8") as f:
                existing_count = len(json.load(f))
            minimum_safe_count = max(256, int(existing_count * 0.5))
            if len(samples) < minimum_safe_count:
                raise RuntimeError(
                    "Refusing to rebuild the Pokémon database: found "
                    f"{len(samples)} training images but the existing database "
                    f"contains {existing_count} rows. Restore the full training "
                    "dataset before running indexer.py."
                )
        except json.JSONDecodeError:
            print("[Indexer] Existing names file is unreadable; skipping rebuild-size guard.")

    if len(samples) == 0:
        print(f"Error: No valid images found in '{LOCAL_DATA_DIR}'.")
        return

    print(f"Found {len(samples)} valid images.")

    embed = build_embedder()
    builder = TemplateBuilder()
    add_calibration_templates(builder)

    batch_size = 32
    workers = min(CPU_COUNT, 8)
    total_batches = (len(samples) + batch_size - 1) // batch_size

    all_vectors = []
    all_names = []

    print(f"Extracting features (Batch Size: {batch_size}, Workers: {workers})...")

    for batch, results in tqdm(
        iter_batches(samples, batch_size, workers),
        total=total_batches,
        desc="Processing Batches",
    ):
        good = [(item, res) for item, res in zip(batch, results) if res is not None]
        if not good:
            continue

        pixels = np.stack([res[0] for _, res in good])
        features = np.asarray(embed(pixels), dtype=np.float32)

        norms = np.linalg.norm(features, axis=-1, keepdims=True)
        norms[norms == 0] = 1.0
        all_vectors.append(features / norms)

        for (_, name), (_, thumb, kind) in good:
            all_names.append(name)
            builder.add(name, thumb, kind)

    if not all_vectors:
        print("Error: no image could be processed.")
        return

    vectors = np.concatenate(all_vectors, axis=0).astype(np.float32)

    # Apply the same NumPy-only form calibration used by the shipped database.
    # Keep the helper offline/indexer-only; the bot runtime does not import it.
    try:
        from .calibrate_form_weights import calibrate_vectors
    except ImportError:
        from calibrate_form_weights import calibrate_vectors
    vectors, calibration_record = calibrate_vectors(vectors, all_names)
    vectors = vectors.astype(np.float16)
    templates, template_names, stats = builder.build()

    np.save(OUTPUT_VECTORS, vectors)
    with open(os.path.join(MODEL_DIR, "pokemon_db_calibration.json"), "w", encoding="utf-8") as f:
        json.dump(calibration_record, f, indent=2)
    with open(OUTPUT_NAMES, "w", encoding="utf-8") as f:
        json.dump(all_names, f)

    np.save(OUTPUT_TEMPLATES, templates)
    with open(OUTPUT_TEMPLATE_NAMES, "w", encoding="utf-8") as f:
        json.dump(template_names, f)

    print(f"\nDone! Successfully processed {len(all_names)} embeddings.")
    print(f" -> {OUTPUT_VECTORS}")
    print(f" -> {OUTPUT_NAMES}")
    print(
        f"Templates: {len(template_names)} for {len(set(template_names))} classes "
        f"(artwork: {stats['artwork']}, median: {stats['median']}, "
        f"single screenshots: {stats['plain']}, skipped: {stats['skipped']})"
    )
    print(f" -> {OUTPUT_TEMPLATES}")
    print(f" -> {OUTPUT_TEMPLATE_NAMES}")
    if stats["skipped"]:
        print(
            f"Note: {stats['skipped']} classes have no stable subject across their images "
            "and are verified by CLIP only."
        )


if __name__ == "__main__":
    main()