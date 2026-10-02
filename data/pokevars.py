import asyncio
import json
from pathlib import Path
from motor.motor_asyncio import AsyncIOMotorClient

CONFIG_PATH = Path("config.json")
POKEVARS_JSON_PATH = Path("data/pokevars.json")
DB_NAME = "utilities"
COLLECTION_NAME = "constdata"
DOC_ID = "pokevars"


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


async def push_pokevars():
    """Reads data/pokevars.json and updates the 'pokevars' document in MongoDB."""
    mongo_uri = load_mongo_uri(CONFIG_PATH)

    if not POKEVARS_JSON_PATH.exists():
        print(f"❌ Error: Local file '{POKEVARS_JSON_PATH}' does not exist.")
        return

    with open(POKEVARS_JSON_PATH, "r", encoding="utf-8") as f:
        content = json.load(f)

    # Safely extract list if file contains {"_id": "pokevars", "data": [...]} or raw list
    if isinstance(content, dict) and "data" in content:
        pokevars_data = content["data"]
    else:
        pokevars_data = content

    client = AsyncIOMotorClient(mongo_uri)
    db = client[DB_NAME]
    collection = db[COLLECTION_NAME]

    try:
        print(f"🚀 Pushing pokevars data ({len(pokevars_data)} entries) to MongoDB...")
        # Replace or insert document with _id: "pokevars"
        await collection.replace_one(
            {"_id": DOC_ID},
            {"_id": DOC_ID, "data": pokevars_data},
            upsert=True
        )
        print(f"✅ Successfully updated '{DB_NAME}.{COLLECTION_NAME}' (id: '{DOC_ID}').")
    except Exception as e:
        print(f"❌ Failed to push pokevars to MongoDB: {e}")
    finally:
        client.close()


async def pull_pokevars():
    """Downloads the 'pokevars' document from MongoDB and saves it to data/pokevars.json."""
    mongo_uri = load_mongo_uri(CONFIG_PATH)

    client = AsyncIOMotorClient(mongo_uri)
    db = client[DB_NAME]
    collection = db[COLLECTION_NAME]

    try:
        print(f"📥 Pulling pokevars data from MongoDB ('{DB_NAME}.{COLLECTION_NAME}')...")
        doc = await collection.find_one({"_id": DOC_ID})

        if not doc:
            print(f"⚠️ Document with _id '{DOC_ID}' not found.")
            return

        # Ensure directory exists
        POKEVARS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)

        # Output in standard JSON format matching local structure
        output_data = {
            "_id": DOC_ID,
            "data": doc.get("data", [])
        }

        with open(POKEVARS_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(output_data, f, indent=4, ensure_ascii=False)

        print(f"✅ Successfully pulled {len(output_data['data'])} entries to '{POKEVARS_JSON_PATH}'.")
    except Exception as e:
        print(f"❌ Failed to pull pokevars from MongoDB: {e}")
    finally:
        client.close()


if __name__ == "__main__":
    import sys

    # Quick command-line interface to push or pull
    if len(sys.argv) > 1 and sys.argv[1].lower() == "pull":
        asyncio.run(pull_pokevars())
    else:
        # Default behavior: push local json to MongoDB
        asyncio.run(push_pokevars())