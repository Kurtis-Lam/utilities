import json
import re
import unicodedata
from pathlib import Path

POKEVARS_PATH = Path("data/pokevars.json")
POKEIMGS_DIR = Path("data/pokeimgs")


def normalize_key(name: str) -> str:
    """
    Normalizes a name matching the logic used across cogs (lowercased, accents stripped,
    special characters removed).
    """
    if not name:
        return ""
    nfd = unicodedata.normalize("NFD", name)
    without_accents = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    cleaned = re.sub(r"[^\w\s-]", "", without_accents)
    return " ".join(cleaned.split()).lower()


def compare_data():
    if not POKEVARS_PATH.exists():
        print(f"❌ Error: '{POKEVARS_PATH}' not found.")
        return

    if not POKEIMGS_DIR.exists():
        print(f"❌ Error: Directory '{POKEIMGS_DIR}' not found.")
        return

    # 1. Load pokevars.json
    with open(POKEVARS_PATH, "r", encoding="utf-8") as f:
        pokevars_data = json.load(f)

    # Handle both top-level list or dictionary {"_id": "pokevars", "data": [...]}
    if isinstance(pokevars_data, dict):
        pokevars_list = pokevars_data.get("data", [])
    elif isinstance(pokevars_data, list):
        pokevars_list = pokevars_data
    else:
        pokevars_list = []

    # Map normalized keys to original pokevars names
    pokevars_map = {normalize_key(p): p for p in pokevars_list if isinstance(p, str)}
    # Direct lowercase mapping fallback
    pokevars_lower_map = {p.strip().lower(): p for p in pokevars_list if isinstance(p, str)}

    # 2. Get local image filenames without extension
    img_files = list(POKEIMGS_DIR.glob("*.png"))
    img_names = [img.stem for img in img_files]

    img_map = {normalize_key(name): name for name in img_names}
    img_lower_map = {name.strip().lower(): name for name in img_names}

    # 3. Find missing items
    missing_images = []
    for p in pokevars_list:
        if not isinstance(p, str):
            continue
        p_norm = normalize_key(p)
        p_lower = p.strip().lower()
        if p_norm not in img_map and p_lower not in img_lower_map:
            missing_images.append(p)

    missing_in_pokevars = []
    for img_name in img_names:
        img_norm = normalize_key(img_name)
        img_lower = img_name.strip().lower()
        if img_norm not in pokevars_map and img_lower not in pokevars_lower_map:
            missing_in_pokevars.append(f"{img_name}.png")

    # 4. Display Results
    print("==================================================")
    print(f" Total in pokevars.json: {len(pokevars_list)}")
    print(f" Total in data/pokeimgs: {len(img_names)}")
    print("==================================================\n")

    print(f"❌ Missing Image Files ({len(missing_images)} Pokémon in pokevars.json without a .png):")
    if missing_images:
        for p in missing_images:
            print(f"  - {p}")
    else:
        print("  None! All Pokémon in pokevars.json have a corresponding image.")

    print("\n--------------------------------------------------\n")

    print(f"⚠️ Unlinked Images ({len(missing_in_pokevars)} .png files not listed in pokevars.json):")
    if missing_in_pokevars:
        for img in missing_in_pokevars:
            print(f"  - {img}")
    else:
        print("  None! All image files exist in pokevars.json.")


if __name__ == "__main__":
    compare_data()