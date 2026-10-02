"""
Unified MongoDB sync tool for Pokémon data.

Run it, then pick:
  1) an action  -> push (local -> MongoDB) or pull (MongoDB -> local)
  2) a dataset  -> pokedex, pokevars, alt, pokeimgs, or all

You can also skip the menus:
    python mongo_sync.py push pokedex
    python mongo_sync.py pull all
"""

import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from motor.motor_asyncio import AsyncIOMotorClient

CONFIG_PATH = Path("config.json")
DB_NAME = "utilities"


# --------------------------------------------------------------------------- #
# Dataset definitions
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class JsonDoc:
    """A single JSON file stored as {"_id": doc_id, "data": ...} in a collection."""
    name: str
    path: Path
    collection: str
    doc_id: str


@dataclass(frozen=True)
class ImageSet:
    """A folder of .png files stored as one document per image ({"_id": name, "image": bytes})."""
    name: str
    directory: Path
    collection: str


DATASETS = {
    "pokedex": JsonDoc("pokedex", Path("data/pokedex.json"), "constdata", "pokedex"),
    "pokevars": JsonDoc("pokevars", Path("data/pokevars.json"), "constdata", "pokevars"),
    "alt": JsonDoc("alt", Path("data/alt.json"), "constdata", "alt"),
    "pokeimgs": ImageSet("pokeimgs", Path("data/pokeimgs"), "pokeimgs"),
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def load_mongo_uri(config_file: Path = CONFIG_PATH) -> str:
    """Reads MONGO_URI from config.json."""
    if not config_file.exists():
        raise FileNotFoundError(f"Configuration file not found at: {config_file}")

    with open(config_file, "r", encoding="utf-8") as f:
        config = json.load(f)

    mongo_uri = config.get("MONGO_URI")
    if not mongo_uri:
        raise KeyError("'MONGO_URI' key missing from config.json")

    return mongo_uri


def unwrap(content):
    """Accepts either raw data or a {"_id": ..., "data": ...} wrapper and returns the raw data."""
    if isinstance(content, dict) and set(content.keys()) == {"_id", "data"}:
        return content["data"]
    return content


def confirm(prompt: str) -> bool:
    return input(f"{prompt} [y/N]: ").strip().lower() in ("y", "yes")


# --------------------------------------------------------------------------- #
# JSON document datasets (pokedex, pokevars, alt)
# --------------------------------------------------------------------------- #
async def push_json(ds: JsonDoc, db):
    if not ds.path.exists():
        print(f"❌ [{ds.name}] Local file '{ds.path}' does not exist.")
        return

    with open(ds.path, "r", encoding="utf-8") as f:
        data = unwrap(json.load(f))

    print(f"🚀 [{ds.name}] Pushing {len(data)} entries to '{DB_NAME}.{ds.collection}'...")
    await db[ds.collection].replace_one(
        {"_id": ds.doc_id},
        {"_id": ds.doc_id, "data": data},
        upsert=True,
    )
    print(f"✅ [{ds.name}] Updated document '{ds.doc_id}'.")


async def pull_json(ds: JsonDoc, db):
    print(f"📥 [{ds.name}] Pulling from '{DB_NAME}.{ds.collection}'...")
    doc = await db[ds.collection].find_one({"_id": ds.doc_id})

    if not doc or "data" not in doc:
        print(f"⚠️ [{ds.name}] Document '{ds.doc_id}' not found or empty.")
        return

    data = doc["data"]
    ds.path.parent.mkdir(parents=True, exist_ok=True)
    with open(ds.path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

    print(f"✅ [{ds.name}] Saved {len(data)} entries to '{ds.path}'.")


# --------------------------------------------------------------------------- #
# Image dataset (pokeimgs)
# --------------------------------------------------------------------------- #
async def push_images(ds: ImageSet, db):
    if not ds.directory.exists():
        print(f"❌ [{ds.name}] Directory '{ds.directory}' does not exist.")
        return

    img_files = sorted(ds.directory.glob("*.png"))
    if not img_files:
        print(f"⚠️ [{ds.name}] No .png files found in '{ds.directory}'. Aborting.")
        return

    if not confirm(
        f"⚠️ [{ds.name}] This DELETES everything in '{DB_NAME}.{ds.collection}' "
        f"and uploads {len(img_files)} images. Continue?"
    ):
        print(f"⏭️ [{ds.name}] Skipped.")
        return

    collection = db[ds.collection]

    print(f"🗑️ [{ds.name}] Deleting existing documents...")
    deleted = await collection.delete_many({})
    print(f"✅ [{ds.name}] Removed {deleted.deleted_count} document(s).")

    documents = [
        {"_id": p.stem.strip().lower(), "image": p.read_bytes()} for p in img_files
    ]

    print(f"🚀 [{ds.name}] Uploading {len(documents)} images...")
    result = await collection.insert_many(documents)
    print(f"✅ [{ds.name}] Inserted {len(result.inserted_ids)} images.")


async def pull_images(ds: ImageSet, db):
    print(f"📥 [{ds.name}] Pulling from '{DB_NAME}.{ds.collection}'...")
    ds.directory.mkdir(parents=True, exist_ok=True)

    count = 0
    async for doc in db[ds.collection].find({}):
        image = doc.get("image")
        if image is None:
            continue
        (ds.directory / f"{doc['_id']}.png").write_bytes(bytes(image))
        count += 1

    if count == 0:
        print(f"⚠️ [{ds.name}] No images found.")
    else:
        print(f"✅ [{ds.name}] Saved {count} images to '{ds.directory}'.")


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
async def run(action: str, names: list[str]):
    client = AsyncIOMotorClient(load_mongo_uri())
    db = client[DB_NAME]

    try:
        for name in names:
            ds = DATASETS[name]
            try:
                if isinstance(ds, JsonDoc):
                    await (push_json if action == "push" else pull_json)(ds, db)
                else:
                    await (push_images if action == "push" else pull_images)(ds, db)
            except Exception as e:
                print(f"❌ [{name}] Failed to {action}: {e}")
    finally:
        client.close()


def choose(prompt: str, options: list[str]) -> str:
    """Numbered menu; accepts the number or the option text."""
    print(f"\n{prompt}")
    for i, opt in enumerate(options, 1):
        print(f"  {i}) {opt}")

    while True:
        raw = input("> ").strip().lower()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        if raw in options:
            return raw
        print("Invalid choice, try again.")


def main():
    args = [a.lower() for a in sys.argv[1:]]

    if len(args) == 2 and args[0] in ("push", "pull") and (args[1] == "all" or args[1] in DATASETS):
        action, target = args
    else:
        action = choose(
            "What do you want to do?",
            ["push", "pull"],
        )
        print("  (push = local -> MongoDB, pull = MongoDB -> local)")
        target = choose(
            f"Which dataset to {action}?",
            list(DATASETS.keys()) + ["all"],
        )

    names = list(DATASETS.keys()) if target == "all" else [target]
    asyncio.run(run(action, names))


if __name__ == "__main__":
    main()