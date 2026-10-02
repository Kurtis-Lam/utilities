import asyncio
import json
from pathlib import Path
from motor.motor_asyncio import AsyncIOMotorClient

CONFIG_PATH = Path("config.json")
POKEDEX_JSON_PATH = Path("data/pokedex.json")
DB_NAME = "utilities"
COLLECTION_NAME = "constdata"
DOC_ID = "pokedex"


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


async def push_pokedex():
    """Reads data/pokedex.json and updates the 'pokedex' document in MongoDB."""
    mongo_uri = load_mongo_uri(CONFIG_PATH)

    if not POKEDEX_JSON_PATH.exists():
        print(f"❌ Error: Local file '{POKEDEX_JSON_PATH}' does not exist.")
        return

    with open(POKEDEX_JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    client = AsyncIOMotorClient(mongo_uri)
    db = client[DB_NAME]
    collection = db[COLLECTION_NAME]

    try:
        print(f"🚀 Pushing Pokédex data ({len(data)} entries) to MongoDB...")
        # Replace or insert document with _id: "pokedex"
        await collection.replace_one(
            {"_id": DOC_ID},
            {"_id": DOC_ID, "data": data},
            upsert=True
        )
        print(f"✅ Successfully updated '{DB_NAME}.{COLLECTION_NAME}' (id: '{DOC_ID}').")
    except Exception as e:
        print(f"❌ Failed to push Pokédex to MongoDB: {e}")
    finally:
        client.close()


async def pull_pokedex():
    """Downloads the 'pokedex' document from MongoDB and saves it to data/pokedex.json."""
    mongo_uri = load_mongo_uri(CONFIG_PATH)

    client = AsyncIOMotorClient(mongo_uri)
    db = client[DB_NAME]
    collection = db[COLLECTION_NAME]

    try:
        print(f"📥 Pulling Pokédex data from MongoDB ('{DB_NAME}.{COLLECTION_NAME}')...")
        doc = await collection.find_one({"_id": DOC_ID})

        if not doc or "data" not in doc:
            print(f"⚠️ Document with _id '{DOC_ID}' not found or empty.")
            return

        pokedex_data = doc["data"]

        # Ensure directory exists
        POKEDEX_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)

        with open(POKEDEX_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(pokedex_data, f, indent=4, ensure_ascii=False)

        print(f"✅ Successfully pulled {len(pokedex_data)} entries to '{POKEDEX_JSON_PATH}'.")
    except Exception as e:
        print(f"❌ Failed to pull Pokédex from MongoDB: {e}")
    finally:
        client.close()


if __name__ == "__main__":
    import sys

    # Quick command-line interface to push or pull
    if len(sys.argv) > 1 and sys.argv[1].lower() == "pull":
        asyncio.run(pull_pokedex())
    else:
        # Default behavior: push local json to MongoDB
        asyncio.run(push_pokedex())