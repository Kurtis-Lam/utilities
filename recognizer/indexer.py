import json
import os
import zipfile

import numpy as np
import torch
from PIL import Image
from torch.utils.data import DataLoader, Dataset
from tqdm.auto import tqdm

# -------------------------------------------------------------------
# 1. Path Configuration
# -------------------------------------------------------------------
BASE_DIR = os.path.abspath(".")
LOCAL_DATA_DIR = os.path.join(BASE_DIR, "recognizer/traindata")
LOCAL_ZIP_FILE = os.path.join(BASE_DIR, "data.zip")

OUTPUT_VECTORS = os.path.join(BASE_DIR, "pokemon_db_vectors.npy")
OUTPUT_NAMES = os.path.join(BASE_DIR, "pokemon_db_names.json")
OUTPUT_COLORS = os.path.join(BASE_DIR, "pokemon_db_colors.npy")

# If the exact ONNX model used by the bot is present here, the database is built
# with it so database and query embeddings carry the same quantization noise.
ONNX_MODEL_PATH = os.path.join(BASE_DIR, "models", "clip_vision_quantized.onnx")
HF_MODEL_NAME = "openai/clip-vit-base-patch32"

DEVICE = "cpu"
DTYPE = torch.float32
CPU_COUNT = os.cpu_count() or 4

# -------------------------------------------------------------------
# 2. Shared preprocessing (KEEP IDENTICAL to recognize.py)
# -------------------------------------------------------------------
CLIP_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
CLIP_STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
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


def color_features(square):
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
    rgb = flatten_alpha(img)
    square = center_square(rgb)

    resized = square.resize((TARGET_SIZE, TARGET_SIZE), Image.Resampling.BICUBIC)
    arr = np.asarray(resized, dtype=np.float32)
    arr /= 255.0
    arr -= CLIP_MEAN
    arr /= CLIP_STD
    arr = np.ascontiguousarray(arr.transpose(2, 0, 1))

    return arr, color_features(square)


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


# -------------------------------------------------------------------
# 3. Dataset Definitions
# -------------------------------------------------------------------
class PokemonDataset(Dataset):
    def __init__(self, data_dir: str):
        self.samples = []
        valid_extensions = (".png", ".jpg", ".jpeg", ".webp")

        if os.path.exists(data_dir):
            for root, _, files in os.walk(data_dir):
                for file_name in sorted(files):
                    if file_name.lower().endswith(valid_extensions):
                        full_path = os.path.join(root, file_name)
                        pokemon_name = os.path.basename(root)
                        if pokemon_name == os.path.basename(data_dir):
                            pokemon_name = "unknown"
                        self.samples.append((full_path, pokemon_name))

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        path, name = self.samples[idx]
        try:
            with Image.open(path) as img:
                pixels, colors = preprocess_image(img)
            return pixels, colors, name
        except Exception:
            return None


def collate_fn(batch):
    batch = [item for item in batch if item is not None]
    if not batch:
        return None
    pixels, colors, names = zip(*batch)
    return np.stack(pixels), np.stack(colors), list(names)


# -------------------------------------------------------------------
# 4. Embedding backends
# -------------------------------------------------------------------
def build_embedder():
    if os.path.exists(ONNX_MODEL_PATH):
        import onnxruntime as ort

        print(f"Using ONNX model for indexing: '{ONNX_MODEL_PATH}'")
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = CPU_COUNT
        session = ort.InferenceSession(
            ONNX_MODEL_PATH, sess_options=opts, providers=["CPUExecutionProvider"]
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

    from transformers import CLIPModel

    print("ONNX model not found, loading HuggingFace CLIP model...")
    model = CLIPModel.from_pretrained(HF_MODEL_NAME).to(DEVICE, dtype=DTYPE).eval()
    torch.set_num_threads(CPU_COUNT)

    def embed(pixels: np.ndarray) -> np.ndarray:
        with torch.inference_mode():
            tensor = torch.from_numpy(pixels).to(DEVICE, dtype=DTYPE)
            outputs = model.get_image_features(pixel_values=tensor)

            if hasattr(outputs, "image_embeds"):
                features = outputs.image_embeds
            elif hasattr(outputs, "pooler_output"):
                features = outputs.pooler_output
            elif isinstance(outputs, torch.Tensor):
                features = outputs
            else:
                features = outputs[0]

            return features.cpu().to(torch.float32).numpy()

    return embed


# -------------------------------------------------------------------
# 5. Main Execution Block
# -------------------------------------------------------------------
def main():
    prepare_dataset()

    dataset = PokemonDataset(LOCAL_DATA_DIR)

    if len(dataset) == 0:
        print(f"Error: No valid images found in '{LOCAL_DATA_DIR}'.")
        return

    print(f"Found {len(dataset)} valid images.")

    embed = build_embedder()

    batch_size = 32
    num_workers = min(CPU_COUNT, 8)

    dataloader = DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=False,
        num_workers=num_workers,
        pin_memory=False,
        persistent_workers=(num_workers > 0),
        collate_fn=collate_fn,
    )

    all_vectors = []
    all_colors = []
    all_names = []

    print(f"Extracting features (Batch Size: {batch_size}, Workers: {num_workers})...")

    for batch in tqdm(dataloader, desc="Processing Batches"):
        if batch is None:
            continue

        pixels, colors, names = batch
        features = np.asarray(embed(pixels), dtype=np.float32)

        norms = np.linalg.norm(features, axis=-1, keepdims=True)
        norms[norms == 0] = 1.0
        features = features / norms

        all_vectors.append(features)
        all_colors.append(colors)
        all_names.extend(names)

    if all_vectors:
        vectors = np.concatenate(all_vectors, axis=0).astype(np.float16)
        colors = np.concatenate(all_colors, axis=0).astype(np.float16)

        np.save(OUTPUT_VECTORS, vectors)
        np.save(OUTPUT_COLORS, colors)
        with open(OUTPUT_NAMES, "w", encoding="utf-8") as f:
            json.dump(all_names, f)

        print(f"\nDone! Successfully processed {len(all_names)} embeddings.")
        print(f" -> {OUTPUT_VECTORS}")
        print(f" -> {OUTPUT_COLORS}")
        print(f" -> {OUTPUT_NAMES}")


if __name__ == "__main__":
    main()