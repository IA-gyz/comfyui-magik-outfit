"""Validate the Magik Outfit catalog. Exit code 1 if any error is found.

  python tools/validate_catalog.py
"""
import os
import sys

from PIL import Image

from _common import ASSETS, load_json


def main():
    errors, warnings = [], []
    mannequin = load_json("mannequin.json")
    pieces = load_json("pieces.json")
    outfits = load_json("outfits.json")

    canvas = tuple(mannequin["canvas"])
    slots = mannequin["slots"]
    selectable = {n for n, s in slots.items() if s.get("selectable")}

    def check_image(rel, what, expect_alpha=True):
        path = os.path.join(ASSETS, rel)
        if not os.path.isfile(path):
            errors.append(f"{what}: missing file {rel}")
            return None
        img = Image.open(path)
        if img.size != canvas:
            errors.append(f"{what}: size {img.size} != canvas {canvas} ({rel})")
        if expect_alpha and "A" not in img.getbands():
            errors.append(f"{what}: no alpha channel ({rel})")
        return img

    if not mannequin.get("base"):
        errors.append("mannequin.json: missing 'base'")
    else:
        check_image(mannequin["base"], "mannequin base")

    for slot in mannequin.get("output_order", []):
        if slot not in selectable:
            errors.append(f"output_order: '{slot}' is not a selectable slot")

    ids = set()
    by_id = {}
    for p in pieces:
        pid = p.get("id", "<no id>")
        if pid in ids:
            errors.append(f"duplicate piece id: {pid}")
        ids.add(pid)
        by_id[pid] = p
        if p.get("slot") not in selectable:
            errors.append(f"{pid}: slot '{p.get('slot')}' is not a selectable slot")
        elif not pid.startswith(p["slot"] + "_"):
            warnings.append(f"{pid}: id does not start with its slot '{p['slot']}_'")
        for key in ("tags", "nl", "label"):
            if not p.get(key):
                errors.append(f"{pid}: empty '{key}'")
        for key in ("hides", "conflicts"):
            for s in p.get(key, []):
                if s not in selectable:
                    errors.append(f"{pid}: {key} references unknown slot '{s}'")
                if s == p.get("slot"):
                    errors.append(f"{pid}: {key} references its own slot")
        img = check_image(p.get("layer", ""), pid)
        if img is not None and "A" in img.getbands() and img.getchannel("A").getbbox() is None:
            errors.append(f"{pid}: layer is fully transparent")
        if p.get("back_layer"):
            check_image(p["back_layer"], f"{pid} back_layer")

    oids = set()
    for o in outfits:
        oid = o.get("id", "<no id>")
        if oid in oids:
            errors.append(f"duplicate outfit id: {oid}")
        oids.add(oid)
        if not o.get("pieces"):
            errors.append(f"{oid}: no pieces")
        for slot, pid in o.get("pieces", {}).items():
            p = by_id.get(pid)
            if p is None:
                errors.append(f"{oid}: unknown piece '{pid}'")
            elif p["slot"] != slot:
                errors.append(f"{oid}: piece '{pid}' is slot '{p['slot']}', listed under '{slot}'")
            elif any(c in o["pieces"] for c in p.get("conflicts", [])):
                errors.append(f"{oid}: '{pid}' conflicts with another piece in the same outfit")
        if o.get("tile") and not os.path.isfile(os.path.join(ASSETS, o["tile"])):
            warnings.append(f"{oid}: tile not baked yet ({o['tile']}); run bake_tiles.py")

    for w in warnings:
        print("WARN ", w)
    for e in errors:
        print("ERROR", e)
    print(f"{len(pieces)} pieces, {len(outfits)} outfits: {len(errors)} errors, {len(warnings)} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
