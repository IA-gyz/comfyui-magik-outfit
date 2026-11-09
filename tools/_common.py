"""Shared helpers for the Magik Outfit tools.

resolve/draw_list/compose live in outfit_core.py (the same code the node uses);
the JS compositor must match their behaviour.
"""
import json
import os
import sys

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from outfit_core import compose, draw_list, resolve  # noqa: E402,F401  (single source of truth)
DATA = os.path.join(ROOT, "data")
ASSETS = os.path.join(ROOT, "assets")


def load_json(name):
    with open(os.path.join(DATA, name), "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(name, obj):
    path = os.path.join(DATA, name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def load_catalog():
    """Return (mannequin, pieces_by_id, outfits_by_id); order is preserved."""
    mannequin = load_json("mannequin.json")
    pieces = {p["id"]: p for p in load_json("pieces.json")}
    outfits = {o["id"]: o for o in load_json("outfits.json")}
    return mannequin, pieces, outfits


def _layer(path):
    with Image.open(os.path.join(ASSETS, path)) as im:
        return im.convert("RGBA")


def composite(mannequin, pieces, slot_map):
    """Composite the mannequin plus the pieces in `slot_map` ({slot: id}).

    Tools only work on the built-in catalog, so assets always come from ASSETS.
    """
    return compose(mannequin["canvas"], draw_list(mannequin, pieces, slot_map),
                   lambda rel, _source: _layer(rel))
