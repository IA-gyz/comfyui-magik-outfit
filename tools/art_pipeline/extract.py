"""Turn "mannequin wearing one piece" renders into transparent full-canvas layers.

The mannequin render without clothes (the *base*) is a known background, so for every pixel
    render = alpha * garment + (1 - alpha) * base
gives alpha from the colour difference and the garment colour by solving for `garment`
(which also removes the grey mannequin fringe on soft edges).

A garment that is close to the mannequin's colour has little contrast. Rendering the piece on a
second, differently coloured mannequin (a *variant*) fixes that: the per-pixel best contrast wins.
"""
import numpy as np
from PIL import Image, ImageDraw


# ------------------------------------------------------------ morphology
def _reduce(a, r, op, pad_mode):
    if r <= 0:
        return a
    out = a
    for axis in (0, 1):
        pad = [(0, 0)] * a.ndim
        pad[axis] = (r, r)
        padded = np.pad(out, pad, mode=pad_mode)
        n = out.shape[axis]
        acc = None
        for d in range(2 * r + 1):
            sl = [slice(None)] * a.ndim
            sl[axis] = slice(d, d + n)
            chunk = padded[tuple(sl)]
            acc = chunk.copy() if acc is None else op(acc, chunk, out=acc)
        out = acc
    return out


def dilate(mask, r):
    return _reduce(mask.astype(np.uint8), r, np.maximum, "edge").astype(bool)


def erode(mask, r):
    return _reduce(mask.astype(np.uint8), r, np.minimum, "edge").astype(bool)


def box_sum(a, r):
    return _reduce(a.astype(np.int16), r, np.add, "constant")


def _label(binary):
    """4-connected labelling of a small boolean array. Returns (labels, areas, touches_border)."""
    h, w = binary.shape
    data = bytes(binary.astype(np.uint8).ravel())
    labels = [0] * (h * w)
    areas, touches = [0], [False]
    n = 0
    for start in np.flatnonzero(binary.ravel()).tolist():
        if labels[start]:
            continue
        n += 1
        labels[start] = n
        stack, area, border = [start], 0, False
        while stack:
            p = stack.pop()
            area += 1
            y, x = divmod(p, w)
            if x == 0 or y == 0 or x == w - 1 or y == h - 1:
                border = True
            if x > 0 and data[p - 1] and not labels[p - 1]:
                labels[p - 1] = n; stack.append(p - 1)
            if x < w - 1 and data[p + 1] and not labels[p + 1]:
                labels[p + 1] = n; stack.append(p + 1)
            if y > 0 and data[p - w] and not labels[p - w]:
                labels[p - w] = n; stack.append(p - w)
            if y < h - 1 and data[p + w] and not labels[p + w]:
                labels[p + w] = n; stack.append(p + w)
        areas.append(area)
        touches.append(border)
    return np.array(labels, dtype=np.int32).reshape(h, w), areas, touches


def _bbox(mask):
    ys, xs = np.nonzero(mask)
    if ys.size == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def remove_small(mask, min_area):
    """Drop connected components smaller than `min_area` px."""
    box = _bbox(mask)
    if box is None:
        return mask
    x0, y0, x1, y1 = box
    labels, areas, _ = _label(mask[y0:y1, x0:x1])
    keep = np.array([False] + [a >= min_area for a in areas[1:]])
    out = np.zeros_like(mask)
    out[y0:y1, x0:x1] = keep[labels]
    return out


def fill_small_holes(mask, hole_area):
    """Fill background pockets smaller than `hole_area` px that are enclosed by the mask."""
    box = _bbox(mask)
    if box is None:
        return mask
    x0, y0, x1, y1 = box
    crop = np.pad(mask[y0:y1, x0:x1], 1, constant_values=False)
    labels, areas, touches = _label(~crop)
    fill = np.array([False] + [(not touches[i]) and areas[i] < hole_area for i in range(1, len(areas))])
    filled = crop | fill[labels]
    out = mask.copy()
    out[y0:y1, x0:x1] = filled[1:-1, 1:-1]
    return out


def cleanup(core, params):
    """Smooth a raw garment mask: majority filter, remove specks, fill pinholes, close thin gaps."""
    clean = box_sum(core, 1) >= 5
    clean = remove_small(clean, params["min_area"])
    clean = fill_small_holes(clean, params["hole_area"])
    return erode(dilate(clean, 2), 2) | clean


def count_components(mask, min_area):
    box = _bbox(mask)
    if box is None:
        return 0
    x0, y0, x1, y1 = box
    _, areas, _ = _label(mask[y0:y1, x0:x1])
    return sum(1 for a in areas[1:] if a >= min_area)


# ------------------------------------------------------------- extraction
def _as_array(img, size):
    img = img.convert("RGB")
    if img.size != size:
        img = img.resize(size, Image.LANCZOS)
    return np.asarray(img, dtype=np.float32)


def extract_layer(bases, renders, region, params, mask=None):
    """Extract one piece.

    bases, renders: parallel lists of PIL images, one pair per mannequin variant.
    region:         PIL "L" mask, 255 where the garment may appear (already grown by the margin).
    mask:           optional PIL "L" garment mask (e.g. from a segmentation node); replaces the
                    colour-difference alpha but still goes through cleanup and the matting step.
    Returns (RGBA layer, stats).
    """
    if len(bases) != len(renders) or not bases:
        raise ValueError("need one base per render, and at least one variant")
    size = region.size
    lo, hi = float(params["lo"]), float(params["hi"])
    B = np.stack([_as_array(b, size) for b in bases])
    R = np.stack([_as_array(r, size) for r in renders])
    diffs = np.abs(R - B).max(axis=3)                   # variants x H x W
    dmax, best = diffs.max(axis=0), diffs.argmax(axis=0)
    inside = np.asarray(region) > 127
    outside = ~inside

    stats = {
        "drift_mean": float(diffs[:, outside].mean()) if outside.any() else 0.0,
        "drift_frac": float((dmax[outside] > lo).mean()) if outside.any() else 0.0,
    }
    # garment-sized change outside the allowed region means the region is too small (or the model added things)
    spill = cleanup((dmax >= hi) & outside, params) if outside.any() else np.zeros_like(inside)
    stats["spill_px"] = int(spill.sum())

    if mask is not None:
        alpha = (np.asarray(mask.convert("L").resize(size, Image.LANCZOS), dtype=np.float32) / 255.0)
    else:
        alpha = np.clip((dmax - lo) / (hi - lo), 0.0, 1.0)
    alpha = alpha * inside

    clean = cleanup(alpha >= 0.5, params)
    interior = erode(clean, 3)
    near = dilate(clean, 2)
    alpha_f = np.where(interior, 1.0, np.where(clean, np.maximum(alpha, 0.5), alpha * near))
    if params.get("erode", 0) > 0:
        alpha_f = _reduce(alpha_f.astype(np.float32), int(params["erode"]), np.minimum, "edge")
    alpha_f = alpha_f.astype(np.float32)

    # solve render = a*C + (1-a)*base for the garment colour C, per variant, then take the best-contrast one
    a3 = np.maximum(alpha_f, 0.05)[None, :, :, None]
    C = np.clip((R - (1.0 - alpha_f)[None, :, :, None] * B) / a3, 0, 255)
    C = np.take_along_axis(C, best[None, :, :, None], axis=0)[0]

    A = np.rint(alpha_f * 255).astype(np.uint8)
    rgb = np.where((A > 0)[:, :, None], np.rint(C), 0).astype(np.uint8)
    layer = Image.fromarray(np.dstack([rgb, A]), "RGBA")

    stats["components"] = count_components(clean, params["min_area"])
    stats["contrast"] = float(dmax[clean].mean()) if clean.any() else 0.0
    return layer, stats


# --------------------------------------------------------------------- QA
def qa_layer(layer, stats, params):
    """Return {'ok': bool, 'issues': [{'level','code','msg'}], 'stats': {...}} for an extracted layer."""
    alpha = np.asarray(layer.getchannel("A"))
    solid = int((alpha > 127).sum())
    w, h = layer.size
    issues = []

    def add(level, code, msg):
        issues.append({"level": level, "code": code, "msg": msg})

    if solid == 0:
        add("error", "empty", "nothing was extracted: the render is identical to the base, or the garment "
            "is outside the slot region")
    else:
        if solid < params["min_pixels"]:
            add("error", "too_small", f"only {solid} px extracted")
        if solid / (w * h) > params["max_coverage"]:
            add("error", "too_large", f"covers {solid / (w * h):.0%} of the canvas; the model probably "
                "changed more than the garment")
        if stats["components"] > params["max_components"]:
            add("warn", "fragmented", f"{stats['components']} separate parts "
                f"(expected at most {params['max_components']})")
        min_contrast = params.get("min_contrast", 40)
        if stats["contrast"] < min_contrast:
            add("warn", "low_contrast", f"mean contrast {stats['contrast']:.0f}/255 (limit {min_contrast}): the "
                "garment is close to the mannequin's colour, so parts of it may be missing; add a second "
                "mannequin variant")
    if stats["spill_px"] > 200:
        add("error", "outside_region", f"{stats['spill_px']} px of garment-sized change outside the slot "
            "region; widen the region in the spec or tighten the prompt")
    if stats["drift_mean"] > params["drift_max"]:
        add("warn", "drift", f"mean change outside the region is {stats['drift_mean']:.1f} (limit "
            f"{params['drift_max']}): the model is redrawing the whole image; use an inpaint-lock workflow "
            "(MAGIK_MASK) or a lower denoise")
    return {"ok": not any(i["level"] == "error" for i in issues), "issues": issues,
            "stats": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in stats.items()}}


# ------------------------------------------------------------ contact sheet
def contact_sheet(layers, base=None, cell=(128, 256), cols=6, labels=None):
    """Grid of layers over a checkerboard (and a faint mannequin) for quick visual review."""
    labels = labels or {}
    items = list(layers.items())
    rows = max(1, -(-len(items) // cols))
    label_h = 14
    sheet = Image.new("RGB", (cols * cell[0], rows * (cell[1] + label_h)), (40, 40, 44))
    checker = Image.new("RGB", cell, (210, 210, 214))
    d = ImageDraw.Draw(checker)
    for y in range(0, cell[1], 8):
        for x in range(0, cell[0], 8):
            if (x // 8 + y // 8) % 2:
                d.rectangle([x, y, x + 7, y + 7], fill=(180, 180, 186))
    base_small = None
    if base is not None:
        base_small = base.convert("RGBA").resize(cell, Image.LANCZOS)
        base_small.putalpha(base_small.getchannel("A").point(lambda v: int(v * 0.35)))
    for i, (name, layer) in enumerate(items):
        x, y = (i % cols) * cell[0], (i // cols) * (cell[1] + label_h)
        tile = checker.convert("RGBA")
        if base_small is not None:
            tile.alpha_composite(base_small)
        tile.alpha_composite(layer.convert("RGBA").resize(cell, Image.LANCZOS))
        sheet.paste(tile.convert("RGB"), (x, y + label_h))
        draw = ImageDraw.Draw(sheet)
        draw.text((x + 3, y + 1), (labels.get(name) or name)[:22], fill=(240, 240, 240))
    return sheet
