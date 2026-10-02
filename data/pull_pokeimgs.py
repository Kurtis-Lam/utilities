import asyncio
import json
from pathlib import Path
from motor.motor_asyncio import AsyncIOMotorClient

CONFIG_PATH = Path("config.json")
OUTPUT_DIR = Path("data/pokeimgs")
DB_NAME = "utilities"
COLLECTION_NAME = "pokeimgs"


def load_mongo_uri(config_file: Path) -> str:
    """Reads MONGO_URI from config.json."""
    if not config_file.exists():
        raise FileNotFoundError(f"Configuration file not found at: {config_file}")

    with open(config_file, "r", encoding="utf-8") as f:
        config = json.load(f)

    mongo_uri = config.get("MONGO_URI")
    if not mongo_uri:
        raise KeyError("'MONGO_URI' key missing from config.json")

    return mongo_uri


async def download_images():
    # 1. Fetch Mongo URI from config.json
    mongo_uri = load_mongo_uri(CONFIG_PATH)

    # 2. Ensure output directory exists
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    client = AsyncIOMotorClient(mongo_uri)
    db = client[DB_NAME]
    collection = db[COLLECTION_NAME]

    saved_count = 0
    skipped_count = 0

    print("Fetching images from MongoDB...")

    # 3. Iterate through all documents in pokeimgs collection
    async for doc in collection.find({}):
        pokemon_name = doc.get("_id")
        image_bytes = doc.get("image")

        if not pokemon_name or not image_bytes:
            skipped_count += 1
            continue

        # Save image as data/pokeimgs/{pokemon_name}.png
        file_path = OUTPUT_DIR / f"{pokemon_name}.png"
        try:
            with open(file_path, "wb") as f:
                f.write(image_bytes)
            saved_count += 1
        except Exception as e:
            print(f"❌ Failed to save image for {pokemon_name}: {e}")

    client.close()
    print(f"✅ Successfully downloaded {saved_count} images to '{OUTPUT_DIR}'.")
    if skipped_count > 0:
        print(f"⚠️ Skipped {skipped_count} documents (missing _id or image data).")


if __name__ == "__main__":
    asyncio.run(download_images())