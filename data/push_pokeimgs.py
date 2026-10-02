import asyncio
import json
from pathlib import Path
from motor.motor_asyncio import AsyncIOMotorClient

CONFIG_PATH = Path("config.json")
POKEIMGS_DIR = Path("data/pokeimgs")
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


async def replace_pokeimgs():
    # 1. Load MONGO_URI
    mongo_uri = load_mongo_uri(CONFIG_PATH)

    if not POKEIMGS_DIR.exists():
        print(f"❌ Error: Directory '{POKEIMGS_DIR}' does not exist.")
        return

    # 2. Gather image files
    img_files = list(POKEIMGS_DIR.glob("*.png"))
    if not img_files:
        print(f"⚠️ No .png files found in '{POKEIMGS_DIR}'. Aborting.")
        return

    client = AsyncIOMotorClient(mongo_uri)
    db = client[DB_NAME]
    collection = db[COLLECTION_NAME]

    try:
        # 3. Clear existing contents in the collection
        print(f"🗑️ Deleting all existing documents in '{DB_NAME}.{COLLECTION_NAME}'...")
        delete_result = await collection.delete_many({})
        print(f"✅ Removed {delete_result.deleted_count} existing document(s).")

        # 4. Prepare batch documents
        documents = []
        for file_path in img_files:
            pokemon_name = file_path.stem.strip().lower()
            with open(file_path, "rb") as f:
                image_bytes = f.read()

            documents.append({
                "_id": pokemon_name,
                "image": image_bytes
            })

        # 5. Insert new images into MongoDB
        if documents:
            print(f"🚀 Uploading {len(documents)} images to MongoDB...")
            insert_result = await collection.insert_many(documents)
            print(f"✅ Successfully inserted {len(insert_result.inserted_ids)} images into '{COLLECTION_NAME}'.")

    except Exception as e:
        print(f"❌ Failed to update MongoDB: {e}")

    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(replace_pokeimgs())