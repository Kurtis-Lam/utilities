"""
Normalizes image filenames so they match what the bot looks up in MongoDB.

  Non-shiny (data/pokeimgs):   flabébé.png      -> flabebe.png
                               nidoran♂️.png     -> nidoran-m.png
                               nidoran♀️.png     -> nidoran-f.png
  Shiny (data/pokeimgs/shinies):
                               shiny_pikachu.png     -> shiny pikachu.png
                               shiny_10_zygarde.png  -> shiny 10% zygarde.png
                               shiny_flab_b.png      -> shiny flabebe.png
                               shiny_farfetch_d.png  -> shiny farfetch'd.png

Accents become plain letters (é -> e); the bot converts names the same way when it fetches.
Shiny slugs lose accents/symbols, so each one is matched to a non-shiny image to recover the
real name. Run this BEFORE `python mongo_sync.py push pokeimgs` / `push shinies`.

Usage:
    python rename_shinies.py            # preview, then asks to confirm
    python rename_shinies.py --yes      # skip the confirmation
    python rename_shinies.py --shinies DIR --plain DIR
"""

import argparse
import re
import unicodedata
from collections import defaultdict
from pathlib import Path


def image_id(name: str) -> str:
    """Canonical id/filename stem: lowercase, é -> e, Nidoran symbols -> -m / -f."""
    text = unicodedata.normalize("NFC", name).replace("♂", "-m").replace("♀", "-f")
    nfd = unicodedata.normalize("NFD", text)
    stripped = "".join(c for c in nfd if unicodedata.category(c) != "Mn")  # also drops U+FE0F
    return " ".join(unicodedata.normalize("NFC", stripped).lower().split())


def slug_variants(name: str) -> set:
    """
    Slugs a shiny file could have been generated with for this name:
      lossy   - every non-ascii/non-alnum char becomes '_'  (flabébé -> flab_b, farfetch'd -> farfetch_d)
      stripped - accents removed first, symbols dropped      (flabébé -> flabebe)
    """
    lossy = re.sub(r"[^a-z0-9]+", "_", unicodedata.normalize("NFC", name).lower()).strip("_")
    nfd = unicodedata.normalize("NFD", name)
    plain = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    plain = re.sub(r"[^\w\s-]", "", plain)
    stripped = re.sub(r"[^a-z0-9]+", "_", " ".join(plain.split()).lower()).strip("_")
    return {lossy, stripped} - {""}


def same_file(a: Path, b: Path) -> bool:
    try:
        return a.samefile(b)
    except OSError:
        return False


def plan_renames(pairs):
    """pairs: [(src, target)] -> (ok, skipped) with collision checks (also between planned targets)."""
    ok, skipped, claimed = [], [], {}
    for src, dst in pairs:
        if src.name == dst.name:
            continue
        key = dst.name.lower()
        if key in claimed:
            skipped.append((src.name, f"same target as {claimed[key]}: {dst.name}"))
        elif dst.exists() and not same_file(src, dst):
            skipped.append((src.name, f"target exists: {dst.name}"))
        else:
            claimed[key] = src.name
            ok.append((src, dst))
    return ok, skipped


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--shinies", type=Path, default=Path("data/pokeimgs/shinies"))
    parser.add_argument("--plain", type=Path, default=Path("data/pokeimgs"))
    parser.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    args = parser.parse_args()

    if not args.shinies.is_dir():
        raise SystemExit(f"❌ Shiny folder not found: {args.shinies}")
    if not args.plain.is_dir():
        raise SystemExit(f"❌ Non-shiny folder not found: {args.plain}")

    # ---- 1) non-shiny files ------------------------------------------------
    plain_files = sorted(args.plain.glob("*.png"))
    plain_plan, plain_skipped = plan_renames(
        [(p, p.with_name(f"{image_id(p.stem)}.png")) for p in plain_files]
    )

    # slug -> final non-shiny ids (what each shiny should be named after)
    by_slug = defaultdict(set)
    for p in plain_files:
        final = image_id(p.stem)
        for slug in slug_variants(p.stem) | slug_variants(final):
            by_slug[slug].add(final)
    by_slug["nidoran_m"].add("nidoran-m")
    by_slug["nidoran_f"].add("nidoran-f")

    # ---- 2) shiny files ----------------------------------------------------
    shiny_pairs, guessed, skipped = [], [], list(plain_skipped)
    for path in sorted(args.shinies.glob("*.png")):
        if not path.stem.startswith("shiny_"):
            # already "shiny xyz" style -> only normalize accents/symbols
            if path.stem.startswith("shiny "):
                shiny_pairs.append((path, path.with_name(f"{image_id(path.stem)}.png")))
            continue
        slug = path.stem[len("shiny_"):]
        matches = sorted(by_slug.get(slug, ()))
        if len(matches) == 1:
            real = matches[0]
        elif len(matches) > 1:
            skipped.append((path.name, f"ambiguous ({', '.join(matches)}) - rename manually, e.g. shiny nidoran-m.png"))
            continue
        else:
            real = image_id(slug.replace("_", " "))
            guessed.append((path.name, f"shiny {real}.png"))
        shiny_pairs.append((path, path.with_name(f"shiny {real}.png")))
    shiny_plan, shiny_skipped = plan_renames(shiny_pairs)
    skipped += shiny_skipped

    # ---- report --------------------------------------------------------------
    for label, plan in (("non-shiny", plain_plan), ("shiny", shiny_plan)):
        print(f"\n[{label}] {len(plan)} to rename")
        for src, dst in plan:
            print(f"  {src.name}  ->  {dst.name}")
    if guessed:
        print(f"\n⚠️ {len(guessed)} shinies had no matching non-shiny image (name guessed from the slug):")
        for name, new in guessed:
            print(f"  {name}  ->  {new}")
    if skipped:
        print(f"\n⏭️ {len(skipped)} skipped:")
        for name, reason in skipped:
            print(f"  {name}: {reason}")

    total = len(plain_plan) + len(shiny_plan)
    if not total:
        print("\nNothing to rename.")
        return
    if not args.yes and input(f"\nRename these {total} files? [y/N]: ").strip().lower() not in ("y", "yes"):
        print("Aborted.")
        return

    for src, dst in plain_plan + shiny_plan:
        src.rename(dst)
    print(f"✅ Renamed {total} file(s).")


if __name__ == "__main__":
    main()