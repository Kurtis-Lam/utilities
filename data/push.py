import asyncio
import json
from pathlib import Path
from motor.motor_asyncio import AsyncIOMotorClient

CONFIG_PATH = Path("config.json")
DB_NAME = "utilities"
COLLECTION_NAME = "constdata"


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


async def push_data():
    # Fetch Mongo URI dynamically from config.json
    mongo_uri = load_mongo_uri(CONFIG_PATH)

    client = AsyncIOMotorClient(mongo_uri)
    db = client[DB_NAME]
    collection = db[COLLECTION_NAME]

    files_to_push = [
        ("pokedex", Path("data/pokedex.json")),
        ("pokevars", Path("data/pokevars.json")),
        ("alt", Path("data/alt.json")),
    ]

    for doc_id, file_path in files_to_push:
        if not file_path.exists():
            print(f"⚠️ Warning: File '{file_path}' does not exist. Skipping...")
            continue

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Upsert into utilities.constdata collection
            result = await collection.update_one(
                {"_id": doc_id},
                {"$set": {"data": data}},
                upsert=True,
            )

            if result.upserted_id:
                print(f"✅ Inserted '{doc_id}' document into MongoDB ({file_path}).")
            else:
                print(f"✅ Updated '{doc_id}' document in MongoDB ({file_path}).")

        except Exception as e:
            print(f"❌ Failed to push {file_path} to MongoDB: {e}")

    client.close()


if __name__ == "__main__":
    asyncio.run(push_data())