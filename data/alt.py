import asyncio
import json
from pathlib import Path
from motor.motor_asyncio import AsyncIOMotorClient

CONFIG_PATH = Path("config.json")
ALT_JSON_PATH = Path("data/alt.json")
DB_NAME = "utilities"
COLLECTION_NAME = "constdata"
DOC_ID = "alt"


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


async def push_alt():
    """Reads data/alt.json and updates the 'alt' document in MongoDB."""
    mongo_uri = load_mongo_uri(CONFIG_PATH)

    if not ALT_JSON_PATH.exists():
        print(f"❌ Error: Local file '{ALT_JSON_PATH}' does not exist.")
        return

    with open(ALT_JSON_PATH, "r", encoding="utf-8") as f:
        content = json.load(f)

    # Handle dictionary directly or dict containing a "data" wrapper
    if isinstance(content, dict) and "data" in content and isinstance(content["data"], dict) and "_id" in content:
        alt_data = content["data"]
    else:
        alt_data = content

    client = AsyncIOMotorClient(mongo_uri)
    db = client[DB_NAME]
    collection = db[COLLECTION_NAME]

    try:
        print(f"🚀 Pushing alternate names ({len(alt_data)} entries) to MongoDB...")
        # Replace or insert document with _id: "alt"
        await collection.replace_one(
            {"_id": DOC_ID},
            {"_id": DOC_ID, "data": alt_data},
            upsert=True
        )
        print(f"✅ Successfully updated '{DB_NAME}.{COLLECTION_NAME}' (id: '{DOC_ID}').")
    except Exception as e:
        print(f"❌ Failed to push alt names to MongoDB: {e}")
    finally:
        client.close()


async def pull_alt():
    """Downloads the 'alt' document from MongoDB and saves it to data/alt.json."""
    mongo_uri = load_mongo_uri(CONFIG_PATH)

    client = AsyncIOMotorClient(mongo_uri)
    db = client[DB_NAME]
    collection = db[COLLECTION_NAME]

    try:
        print(f"📥 Pulling alternate names data from MongoDB ('{DB_NAME}.{COLLECTION_NAME}')...")
        doc = await collection.find_one({"_id": DOC_ID})

        if not doc or "data" not in doc:
            print(f"⚠️ Document with _id '{DOC_ID}' not found or empty.")
            return

        alt_data = doc["data"]

        # Ensure directory exists
        ALT_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)

        with open(ALT_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(alt_data, f, indent=4, ensure_ascii=False)

        print(f"✅ Successfully pulled {len(alt_data)} entries to '{ALT_JSON_PATH}'.")
    except Exception as e:
        print(f"❌ Failed to pull alt names from MongoDB: {e}")
    finally:
        client.close()


if __name__ == "__main__":
    import sys

    # Quick command-line interface to push or pull
    if len(sys.argv) > 1 and sys.argv[1].lower() == "pull":
        asyncio.run(pull_alt())
    else:
        # Default behavior: push local json to MongoDB
        asyncio.run(push_alt())