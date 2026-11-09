"""The art spec: one authoring file holding pieces, outfits, render prompts and extraction settings.

    python tools/art.py init-spec     # build it from the current catalog, then edit by hand
"""
import copy
import json
import os

from PIL import Image, ImageDraw

SPEC_VERSION = 1

DEFAULT_PROMPT = {
    "template": ("Dress the mannequin in {desc}. {hint}Keep the pose, framing, lighting, background and the "
                 "mannequin itself exactly unchanged. Do not add any other clothing or accessories."),
    "negative": "extra clothing, other garments, text, watermark, different pose, different background",
    "slot_hints": {
        "footwear": "Only change the feet. ",
        "legwear": "Only change the legs. ",
        "headwear": "Only change the head. ",
    },
}

DEFAULT_EXTRACT = {
    "lo": 14,             # colour difference (0-255) below which a pixel is surely mannequin
    "hi": 40,             # ... above which it is surely garment; in between is a soft edge
    "region_margin": 24,  # px the slot region is grown by before restricting the layer to it
    "min_area": 40,       # specks smaller than this (px) are removed
    "hole_area": 80,      # holes smaller than this (px) are filled
    "erode": 0,           # px to shrink the alpha by (removes a grey fringe)
    "max_components": 4,  # QA warns above this many separate parts
    "drift_max": 6.0,     # QA warns when the mean change OUTSIDE the slot region exceeds this
    "min_pixels": 150,    # QA errors on layers smaller than this
    "min_contrast": 40,   # QA warns when the garment differs from the mannequin by less than this on average
    "max_coverage": 0.45, # QA errors on layers covering more than this share of the canvas
}

# Rectangles (x0, y0, x1, y1) in canvas pixels where each slot's garment may appear.
# Tuned for the placeholder mannequin: edit them to match your real mannequin.
DEFAULT_REGIONS = {
    "headwear": [140, 0, 372, 200],
    "top": [90, 180, 422, 500],
    "outerwear": [80, 170, 432, 520],
    "bottom": [140, 440, 372, 960],
    "onepiece": [120, 170, 392, 960],
    "legwear": [170, 540, 342, 1000],
    "footwear": [160, 830, 352, 1024],
    "gloves": [60, 380, 452, 520],
    "accessory": [0, 0, 512, 1024],
}

PIECE_FIELDS = ("id", "slot", "label", "tags", "nl", "z", "hides", "conflicts", "style", "colorable")


class SpecError(ValueError):
    pass


def default_spec(pieces, outfits, canvas=(512, 1024)):
    """Build a starter spec from catalog pieces/outfits (lists of dicts as in data/*.json)."""
    spec_pieces = []
    for p in pieces:
        item = {k: copy.deepcopy(p[k]) for k in PIECE_FIELDS if k in p}
        item["render"] = {"desc": p.get("nl") or p["label"]}
        spec_pieces.append(item)
    spec_outfits = [{k: copy.deepcopy(o[k]) for k in ("id", "label", "style", "pieces", "prefix") if k in o}
                    for o in outfits]
    return {
        "version": SPEC_VERSION,
        "canvas": list(canvas),
        "seed": 424242,
        "prompt": copy.deepcopy(DEFAULT_PROMPT),
        "variants": [{"name": "gray", "base": "base_gray.png"}],
        "extract": copy.deepcopy(DEFAULT_EXTRACT),
        "regions": copy.deepcopy(DEFAULT_REGIONS),
        "pieces": spec_pieces,
        "outfits": spec_outfits,
    }


def load_spec(path):
    with open(path, "r", encoding="utf-8") as f:
        spec = json.load(f)
    # fill defaults so older/minimal specs keep working
    spec.setdefault("seed", 424242)
    prompt = copy.deepcopy(DEFAULT_PROMPT)
    prompt.update(spec.get("prompt", {}))
    spec["prompt"] = prompt
    extract = copy.deepcopy(DEFAULT_EXTRACT)
    extract.update(spec.get("extract", {}))
    spec["extract"] = extract
    regions = copy.deepcopy(DEFAULT_REGIONS)
    regions.update(spec.get("regions", {}))
    spec["regions"] = regions
    spec.setdefault("variants", [{"name": "gray", "base": "base_gray.png"}])
    spec.setdefault("outfits", [])
    errors = validate_spec(spec)
    if errors:
        raise SpecError("invalid spec:\n  " + "\n  ".join(errors))
    return spec


def save_spec(spec, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(spec, f, indent=2, ensure_ascii=False)
        f.write("\n")


def validate_spec(spec):
    errors = []
    canvas = spec.get("canvas")
    if not (isinstance(canvas, list) and len(canvas) == 2 and all(isinstance(v, int) and v > 0 for v in canvas)):
        errors.append("canvas must be [width, height]")
    ex = spec.get("extract", {})
    if not ex.get("lo", 0) < ex.get("hi", 1):
        errors.append("extract.lo must be smaller than extract.hi")
    names = [v.get("name") for v in spec.get("variants", [])]
    if not names or any(not isinstance(n, str) or not n for n in names) or len(set(names)) != len(names):
        errors.append("variants need unique non-empty names")
    for v in spec.get("variants", []):
        if not v.get("base"):
            errors.append(f"variant '{v.get('name')}' has no base image")
    for slot, rect in spec.get("regions", {}).items():
        if not (isinstance(rect, list) and len(rect) == 4 and rect[0] < rect[2] and rect[1] < rect[3]):
            errors.append(f"region '{slot}' must be [x0, y0, x1, y1] with x0<x1 and y0<y1")
    seen = set()
    for p in spec.get("pieces", []):
        pid = p.get("id")
        if not pid or pid in seen:
            errors.append(f"piece id missing or duplicated: {pid!r}")
        seen.add(pid)
        if not p.get("slot"):
            errors.append(f"{pid}: no slot")
        elif p["slot"] not in spec.get("regions", {}):
            errors.append(f"{pid}: slot '{p['slot']}' has no region")
        if not (p.get("render", {}).get("desc") or p.get("render", {}).get("prompt")):
            errors.append(f"{pid}: render needs a 'desc' (or a full 'prompt')")
    for o in spec.get("outfits", []):
        for slot, pid in o.get("pieces", {}).items():
            if pid not in seen:
                errors.append(f"outfit '{o.get('id')}' uses unknown piece '{pid}'")
    return errors


def build_prompt(spec, piece):
    """Return (prompt, negative) for a spec piece."""
    render = piece.get("render", {})
    prompts = spec["prompt"]
    if render.get("prompt"):
        text = render["prompt"]
    else:
        hint = prompts.get("slot_hints", {}).get(piece["slot"], "")
        text = prompts["template"].format(desc=render["desc"], slot=piece["slot"], hint=hint)
    return " ".join(text.split()), render.get("negative") or prompts.get("negative", "")


def region_rect(spec, slot):
    return tuple(spec["regions"][slot])


def region_mask(spec, slot, margin=0):
    """Binary L mask (255 inside) of the slot's region, optionally grown by `margin` px."""
    w, h = spec["canvas"]
    x0, y0, x1, y1 = region_rect(spec, slot)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rectangle([max(0, x0 - margin), max(0, y0 - margin),
                                    min(w, x1 + margin) - 1, min(h, y1 + margin) - 1], fill=255)
    return mask


def piece_layer_name(piece):
    """File stem used in assets/pieces/<slot>/<stem>.webp (id without its slot prefix)."""
    pid, slot = piece["id"], piece["slot"]
    return pid[len(slot) + 1:] if pid.startswith(slot + "_") else pid
