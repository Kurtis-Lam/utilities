import os
import zipfile
import numpy as np
import torch
from PIL import Image
from torch.utils.data import DataLoader, Dataset
from tqdm.auto import tqdm
from transformers import CLIPModel, CLIPProcessor

# -------------------------------------------------------------------
# 1. Path Configuration
# -------------------------------------------------------------------
BASE_DIR = os.path.abspath(".")
LOCAL_DATA_DIR = os.path.join(BASE_DIR, "data")
LOCAL_ZIP_FILE = os.path.join(BASE_DIR, "data.zip")
OUTPUT_FILE = os.path.join(BASE_DIR, "pokemon_db.npz")

DEVICE = "cpu"
DTYPE = torch.float32
CPU_COUNT = os.cpu_count() or 4


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
# 2. Dataset Definitions
# -------------------------------------------------------------------
class PokemonDataset(Dataset):
    def __init__(self, data_dir: str):
        self.samples = []
        valid_extensions = (
            ".png",
            ".jpg",
            ".jpeg",
            ".webp"
        )

        if os.path.exists(data_dir):
            for root, _, files in os.walk(data_dir):
                for file_name in files:
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
            image = Image.open(path).convert("RGB")
            return image, name
        except Exception:
            return None, name


def collate_fn(batch):
    batch = [item for item in batch if item[0] is not None]
    if not batch:
        return [], []
    images, names = zip(*batch)
    return list(images), list(names)


# -------------------------------------------------------------------
# 3. Main Execution Block
# -------------------------------------------------------------------
def main():
    prepare_dataset()

    model_name = "openai/clip-vit-base-patch32"
    print("Loading HuggingFace CLIP model and processor...")

    model = CLIPModel.from_pretrained(model_name).to(DEVICE, dtype=DTYPE).eval()
    processor = CLIPProcessor.from_pretrained(model_name)

    dataset = PokemonDataset(LOCAL_DATA_DIR)

    if len(dataset) == 0:
        print(f"Error: No valid images found in '{LOCAL_DATA_DIR}'.")
        return

    print(f"Found {len(dataset)} valid images.")

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
    all_names = []

    torch.set_num_threads(CPU_COUNT)

    print(f"Extracting features (Batch Size: {batch_size}, Workers: {num_workers})...")

    with torch.inference_mode():
        for images, names in tqdm(dataloader, desc="Processing Batches"):
            if not images:
                continue

            inputs = processor(images=images, return_tensors="pt").to(DEVICE)

            outputs = model.get_image_features(**inputs)

            if hasattr(outputs, "image_embeds"):
                features = outputs.image_embeds
            elif hasattr(outputs, "pooler_output"):
                features = outputs.pooler_output
            elif isinstance(outputs, torch.Tensor):
                features = outputs
            else:
                features = outputs[0]

            features = features / features.norm(p=2, dim=-1, keepdim=True)

            all_vectors.append(features.cpu().to(torch.float32))
            all_names.extend(names)

    if all_vectors:
        vectors = torch.cat(all_vectors, dim=0).numpy()
        names = np.array(all_names)

        np.savez_compressed(OUTPUT_FILE, vectors=vectors, names=names)
        print(f"\nDone! Successfully saved {len(names)} embeddings to:")
        print(f" -> {OUTPUT_FILE}")


if __name__ == "__main__":
    main()