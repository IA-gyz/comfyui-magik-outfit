"""Bake outfit tiles and fill piece bboxes.

  python tools/bake_tiles.py            # all outfits + all bboxes
  python tools/bake_tiles.py school_01  # only the named outfits (bboxes still updated)
"""
import os
import sys

from PIL import Image

from _common import ASSETS, load_catalog, load_json, resolve, composite, save_json


def update_bboxes(pieces):
    changed = 0
    for p in pieces.values():
        img = Image.open(os.path.join(ASSETS, p["layer"])).convert("RGBA")
        box = img.getchannel("A").getbbox()
        new = list(box) if box else None
        if p.get("bbox") != new:
            p["bbox"] = new
            changed += 1
    return changed


def main(argv):
    mannequin, pieces, outfits = load_catalog()

    changed = update_bboxes(pieces)
    if changed:
        # Preserve the on-disk order and any extra keys by re-reading the list.
        on_disk = load_json("pieces.json")
        for entry in on_disk:
            entry["bbox"] = pieces[entry["id"]]["bbox"]
        save_json("pieces.json", on_disk)

    wanted = set(argv) if argv else None
    tw, th = mannequin["tile"]
    os.makedirs(os.path.join(ASSETS, "outfits"), exist_ok=True)
    baked = 0
    for oid, outfit in outfits.items():
        if wanted and oid not in wanted:
            continue
        slot_map = resolve(outfit, None, pieces)
        tile = composite(mannequin, pieces, slot_map).resize((tw, th), Image.LANCZOS)
        out = os.path.join(ASSETS, outfit.get("tile") or f"outfits/{oid}.webp")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        tile.save(out, lossless=True)
        baked += 1
    print(f"bboxes updated: {changed}; tiles baked: {baked}")


if __name__ == "__main__":
    main(sys.argv[1:])
