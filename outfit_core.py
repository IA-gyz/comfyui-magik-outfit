"""Magik Outfit core: catalog, selection logic, string assembly, compositing.

No ComfyUI imports and no relative imports, so it can be tested standalone and
shared with tools/. The V3 node (magik_outfit.py) and the aiohttp routes
(outfit_routes.py) are thin wrappers around this module.
"""
import json
import os
import re
import shutil
import threading
import urllib.parse
import uuid
from collections import OrderedDict

from PIL import Image

FORMATS = ("tags", "natural", "both")
SOURCES = ("builtin", "user")
IMAGE_EXTS = (".webp", ".png")
MAX_LABEL = 60
MAX_USER_OUTFITS = 500
MAX_MY_PRESETS = 200
ASSET_URL = "/magik/outfit/asset"
FAVORITE_KINDS = ("outfit", "preset")
COVER_NAME_RE = re.compile(r"^[0-9a-f]{32}\.webp$")
COVER_MAX_BYTES = 4 * 1024 * 1024


class CatalogError(ValueError):
    """Raised for invalid catalog data or invalid save/delete requests."""


# ---------------------------------------------------------------- helpers
def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        return default


def _read_list(path, what, warnings):
    """Read a JSON list for loading. A missing file is normal; a broken one is reported, not hidden."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        warnings.append(f"{what} ignored: {e.__class__.__name__}: {e}")
        return []
    if not isinstance(data, list):
        warnings.append(f"{what} ignored: expected a JSON list, found {type(data).__name__}")
        return []
    return data


def _read_list_strict(path, what):
    """Read a JSON list that is about to be rewritten. Refuses to treat a broken file as empty,
    because writing it back would destroy whatever the user typed into it."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        raise CatalogError(f"{what} cannot be read ({e.__class__.__name__}); fix or remove it first") from None
    if not isinstance(data, list):
        raise CatalogError(f"{what} is not a JSON list; fix or remove it first")
    return data


def _write_json_atomic(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:  # LF on every OS: files stay byte-stable
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def _write_json_atomic_with_backup(path, obj):
    """Keep a .bak of the previous file before replacing it (favorites / my presets)."""
    if os.path.exists(path):
        try:
            shutil.copyfile(path, path + ".bak")
        except OSError:
            pass
    _write_json_atomic(path, obj)


def _read_json_strict(path, what):
    """Like _read_list_strict but for a JSON object: refuse to treat a broken file as empty."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise CatalogError(f"{what} cannot be read ({e.__class__.__name__}); fix or remove it first") from None


# ------------------------------------------------------------- favorites
def _default_favorites():
    return {kind: {"groups": [{"id": "default", "name": "Default Favorites", "isSystem": True}], "items": []}
            for kind in FAVORITE_KINDS}


def _normalize_favorites(data):
    if not isinstance(data, dict):
        data = {}
    out = {}
    for kind in FAVORITE_KINDS:
        section = data.get(kind)
        if not isinstance(section, dict):
            section = {}
        groups = [g for g in section.get("groups", []) if isinstance(g, dict)
                  and isinstance(g.get("id"), str) and g["id"] and isinstance(g.get("name"), str)]
        groups = [{"id": g["id"], "name": _clean_text(g["name"], 40),
                   "isSystem": bool(g.get("isSystem"))} for g in groups][:50]
        if not any(g["id"] == "default" for g in groups):
            groups.insert(0, {"id": "default", "name": "Default Favorites", "isSystem": True})
        items, seen = [], set()
        for it in section.get("items", []):
            if not (isinstance(it, dict) and isinstance(it.get("key"), str) and it["key"] and it["key"] not in seen):
                continue
            gids = it.get("groupIds")
            gids = [g for g in gids if isinstance(g, str) and g] if isinstance(gids, list) else []
            if not gids:
                gids = ["default"]
            seen.add(it["key"])
            items.append({"key": it["key"][:200], "nickname": _clean_text(it.get("nickname"), 60),
                          "groupIds": gids[:20]})
        out[kind] = {"groups": groups, "items": items[:2000]}
    return out


def _clean_my_preset(raw, seen):
    if not (isinstance(raw, dict) and isinstance(raw.get("name"), str) and raw["name"].strip()
            and isinstance(raw.get("tags"), str) and raw["tags"].strip()):
        return None
    pid = raw.get("id")
    if not (isinstance(pid, str) and pid and pid not in seen):
        pid = "mp_" + uuid.uuid4().hex[:12]
    seen.add(pid)
    cover = raw.get("cover")
    return {
        "id": pid[:64],
        "name": _clean_text(raw.get("name"), 80),
        "tags": _clean_text(raw.get("tags"), 400),
        "note": _clean_text(raw.get("note"), 200),
        "cover": cover if isinstance(cover, str) and COVER_NAME_RE.match(cover) else None,
    }


def _clean_text(value, maxlen):
    if not isinstance(value, str):
        return ""
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    return re.sub(r"\s+", " ", value).strip()[:maxlen]


def _clean_styles(value):
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:8]:
        text = _clean_text(item, 24).lower()
        if text and text not in out:
            out.append(text)
    return out


def _clean_labels(value, limit=8, maxlen=60):
    """Free-form labels (preset categories/traits): keep case and Unicode, just sanitize."""
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:limit]:
        text = _clean_text(item, maxlen)
        if text and text not in out:
            out.append(text)
    return out


def _asset_url(source, rel):
    return f"{ASSET_URL}/{source}/{urllib.parse.quote(rel)}"


def _unescape(text):
    return text.replace("\\n", "\n").replace("\\t", "\t")


# ------------------------------------------------------------------ state
def parse_state(text):
    """Parse the hidden widget JSON -> (outfit_id | None, overrides dict).

    Tolerant: anything malformed yields an empty selection rather than an error.
    """
    try:
        data = json.loads(text) if isinstance(text, str) and text.strip() else {}
    except (ValueError, RecursionError):
        return None, {}
    if not isinstance(data, dict):
        return None, {}
    outfit_id = data.get("outfit")
    outfit_id = outfit_id if isinstance(outfit_id, str) else None
    overrides = {}
    raw = data.get("overrides")
    if isinstance(raw, dict):
        for slot, pid in raw.items():
            if not isinstance(slot, str):
                continue
            if pid is None or isinstance(pid, str):
                overrides[slot] = pid
            elif isinstance(pid, dict) and set(pid) == {"tags"} and isinstance(pid["tags"], str):
                # a tag-only slot: {"tags": "..."} carries a raw prompt tag instead of a piece id
                overrides[slot] = {"tags": _clean_text(pid["tags"], 200)}
    return outfit_id, overrides


def resolve(outfit, overrides, pieces):
    """Resolve an outfit + overrides to {slot: piece_id}.

    - Slots missing from `overrides` keep the outfit's piece.
    - An override of None removes the slot.
    - Conflicts are symmetric: a chosen piece drops the slots it lists and any
      existing piece that lists the chosen piece's slot (a top over a dress
      drops the dress). A slot that is itself overridden with a piece is left
      alone, so explicit UI state is never second-guessed.
    - A tag-only override {"tags": ...} occupies its slot as-is (no piece, no conflicts).
    - Unknown ids and pieces sent to the wrong slot are ignored.
    """
    overrides = overrides or {}
    result = {}
    if outfit:
        result = {s: p for s, p in outfit["pieces"].items() if isinstance(p, str) and p in pieces}
    for slot, pid in overrides.items():
        if pid is None:
            result.pop(slot, None)
            continue
        if isinstance(pid, dict):
            result[slot] = pid
            continue
        piece = pieces.get(pid)
        if piece is None or piece["slot"] != slot:
            continue
        result[slot] = pid
        for other in list(result):
            if other == slot or overrides.get(other) is not None:
                continue
            other_piece = pieces.get(result[other]) if isinstance(result[other], str) else None
            if other in piece.get("conflicts", []) or (
                other_piece and slot in other_piece.get("conflicts", [])
            ):
                result.pop(other, None)
    return result


def build_string(slot_map, outfit, modified, mannequin, pieces, fmt="tags",
                 separator=", ", prefix=""):
    """Assemble the output STRING from a resolved {slot: piece_id} map.

    Slots hidden by another selected piece's `hides` are omitted (the same rule
    draw_list applies), so the text describes what is drawn.
    """
    fmt = fmt if fmt in FORMATS else "tags"
    sep = _unescape(separator if isinstance(separator, str) else ", ")
    lead = ""
    if outfit and not modified:
        lead = _clean_text(outfit.get("prefix", ""), MAX_LABEL)

    hidden = set()
    for pid in slot_map.values():
        p = pieces.get(pid) if isinstance(pid, str) else None
        if p:
            hidden.update(p.get("hides", []))
    tags, natural = ([lead] if lead else []), ([lead] if lead else [])
    for slot in mannequin.get("output_order", []):
        if slot in hidden:
            continue
        value = slot_map.get(slot)
        if isinstance(value, dict):
            t = value.get("tags", "").strip()
            if t:
                tags.append(t)
                natural.append(t.replace("_", " "))
            continue
        piece = pieces.get(value) if isinstance(value, str) else None
        if not piece:
            continue
        if piece.get("tags", "").strip():
            tags.append(piece["tags"].strip())
        if piece.get("nl", "").strip():
            natural.append(piece["nl"].strip())

    pre = prefix.strip() if isinstance(prefix, str) else ""

    def with_prefix(text):
        return f"{pre} {text}" if pre and text else text

    if fmt == "tags":
        return with_prefix(sep.join(tags))
    if fmt == "natural":
        return with_prefix(sep.join(natural))
    return with_prefix(sep.join(natural)) + "\n" + sep.join(tags)


def draw_list(mannequin, pieces, slot_map):
    """Return [(relative_path, source)] in draw order (base first)."""
    zs = {name: s["z"] for name, s in mannequin["slots"].items()}
    present = [pieces[pid] for pid in slot_map.values() if isinstance(pid, str) and pid in pieces]
    hidden = set()
    for p in present:
        hidden.update(p.get("hides", []))

    items = [(zs["body"], mannequin["base"], "builtin")]
    for p in present:
        if p["slot"] in hidden:
            continue
        z = p["z"] if p.get("z") is not None else zs[p["slot"]]
        items.append((z, p["layer"], p.get("source", "builtin")))
        if p.get("back_layer"):
            items.append((zs["back"], p["back_layer"], p.get("source", "builtin")))
    items.sort(key=lambda t: t[0])  # stable: ties keep insertion order
    return [(rel, src) for _, rel, src in items]


def compose(canvas_size, order, loader):
    """Alpha-composite the layers in `order`, loading each with loader(rel, source)."""
    canvas = Image.new("RGBA", tuple(canvas_size), (0, 0, 0, 0))
    for rel, source in order:
        layer = loader(rel, source)
        if layer.size != tuple(canvas_size):
            layer = layer.resize(tuple(canvas_size), Image.LANCZOS)
        canvas.alpha_composite(layer)
    return canvas


# ------------------------------------------------------------ attire pools
# Danbooru attire tag pools bundled as data/attire.json (sections of the Danbooru
# "attire" tag-group wiki; data and generator from Comfyui-Anima-Tools, MIT,
# (c) nregret). One main garment plus layered pieces, so drawn slots cannot
# contradict each other.
ATTIRE_MAIN_VARIANTS = ("top_bottom", "uniform", "traditional")
ATTIRE_LAYERED = (("decoration", 1.0), ("socks", 0.6), ("shoes", 0.6))
ATTIRE_SLOT_MAP = {
    "top": "top", "bottom": "bottom", "socks": "legwear", "shoes": "footwear",
    "decoration": "accessory", "uniform": "onepiece", "traditional": "onepiece",
}


def draw_attire(attire, rng):
    """Draw one outfit from the attire tag pools -> {slot: tag}."""
    pools = (attire or {}).get("slots") or {}
    if not (pools.get("top") and pools.get("bottom") and pools.get("uniform") and pools.get("traditional")):
        return {}
    drawn = {}
    variant = rng.choice(ATTIRE_MAIN_VARIANTS)
    if variant == "top_bottom":
        drawn[ATTIRE_SLOT_MAP["top"]] = rng.choice(pools["top"])
        drawn[ATTIRE_SLOT_MAP["bottom"]] = rng.choice(pools["bottom"])
    else:
        drawn[ATTIRE_SLOT_MAP[variant]] = rng.choice(pools[variant])
    for slot, probability in ATTIRE_LAYERED:
        pool = pools.get(slot) or []
        if not pool:
            continue
        if probability < 1 and rng.random() >= probability:
            continue
        drawn[ATTIRE_SLOT_MAP[slot]] = rng.choice(pool)
    return drawn


def _tag_key(tag):
    return " ".join(str(tag).replace("_", " ").lower().split())


def random_state_text(catalog, source="catalog", seed=-1):
    """Draw a random selection and serialize it as the hidden state JSON.

    source: "catalog" (a random outfit id), "attire" (drawn from the danbooru
    pools, mapped onto pieces where possible), "any" (either). seed < 0 draws
    fresh every call; a fixed seed makes the draw reproducible. Returns None
    when there is nothing to draw from.
    """
    import random
    snap = catalog.get() if hasattr(catalog, "get") else catalog
    rng = random.SystemRandom() if (seed is None or seed < 0) else random.Random(seed)
    if source not in ("catalog", "attire", "any"):
        source = "catalog"
    if source == "any":
        source = rng.choice(("catalog", "attire"))
    if source == "attire" and snap.attire:
        drawn = draw_attire(snap.attire, rng)
        if drawn:
            return json.dumps({"v": 1, "outfit": None,
                               "overrides": attire_to_slot_map(drawn, snap.pieces, rng)},
                              ensure_ascii=False)
    ids = list(snap.outfits)
    if not ids:
        return None
    return json.dumps({"v": 1, "outfit": rng.choice(ids), "overrides": {}}, ensure_ascii=False)


def preset_to_slot_map(preset, pieces, attire):
    """Turn a clothing preset's tag list into a {slot: piece_id | {"tags": tag}} map.

    A tag that appears in a catalog piece's tags selects that piece (so it shows
    in the preview); a tag from the attire pools becomes a tag-only entry in the
    matching slot; anything else (character/pose words) is skipped. A one-piece
    garment drops top/bottom, like the conflict rules.
    """
    pools = (attire or {}).get("slots") or {}
    pool_slot = {}
    for pool, tags in pools.items():
        slot = ATTIRE_SLOT_MAP.get(pool, pool)
        for t in tags:
            pool_slot[_tag_key(t)] = slot
    out = {}
    for raw in str(preset.get("tags", "")).split(","):
        tag = raw.strip()
        if not tag:
            continue
        key = _tag_key(tag)
        piece = next((p for p in pieces.values()
                      if key in {_tag_key(t) for t in p.get("tags", "").split(",")}), None)
        if piece is not None:
            out[piece["slot"]] = piece["id"]
            continue
        slot = pool_slot.get(key)
        if slot:
            out[slot] = {"tags": tag}
    if "onepiece" in out:
        out.pop("top", None)
        out.pop("bottom", None)
    return out


def attire_to_slot_map(slot_tags, pieces, rng=None):
    """Map drawn attire tags to catalog pieces whose tags contain the drawn tag;
    slots without a matching piece keep the raw tag as {"tags": tag}."""
    import random
    rng = rng if rng is not None else random.Random()
    out = {}
    for slot, tag in slot_tags.items():
        key = _tag_key(tag)
        candidates = [p for p in pieces.values() if p["slot"] == slot
                      and key in {_tag_key(t) for t in p.get("tags", "").split(",")}]
        if candidates:
            out[slot] = rng.choice(candidates)["id"]
        else:
            out[slot] = {"tags": tag}
    return out


# ---------------------------------------------------------------- catalog
class Snapshot:
    def __init__(self, mannequin, pieces, outfits, warnings, attire=None, presets=None):
        self.mannequin = mannequin
        self.pieces = pieces      # OrderedDict id -> piece (with 'source')
        self.outfits = outfits    # OrderedDict id -> outfit (with 'source')
        self.warnings = warnings
        self.attire = attire      # {"slots": {pool: [tag, ...]}, "vocabulary": [...]} or None
        self.presets = presets or []  # clothing presets from data/presets.json


class Catalog:
    """Built-in catalog merged with the optional user catalog.

    Built-in: <builtin_dir>/data/*.json + <builtin_dir>/assets/
    User:     <user_dir>/{pieces,outfits}.json + <user_dir>/assets/
    Built-in ids always win over colliding user ids.
    """

    def __init__(self, builtin_dir, user_dir=None):
        self.builtin_dir = os.path.abspath(builtin_dir)
        self.user_dir = os.path.abspath(user_dir) if user_dir else None
        self._lock = threading.RLock()
        self._snap = None
        self._stamp = None

    # -- file bookkeeping
    def _data_files(self):
        files = [os.path.join(self.builtin_dir, "data", n)
                 for n in ("mannequin.json", "pieces.json", "outfits.json", "attire.json", "presets.json")]
        if self.user_dir:
            files += [os.path.join(self.user_dir, n) for n in ("pieces.json", "outfits.json")]
        return files

    def _asset_root(self, source):
        if source == "builtin":
            return os.path.join(self.builtin_dir, "assets")
        if source == "user" and self.user_dir:
            return os.path.join(self.user_dir, "assets")
        return None

    def stamp(self):
        out = []
        for path in self._data_files():
            try:
                st = os.stat(path)
                out.append((path, st.st_mtime_ns, st.st_size))
            except OSError:
                out.append((path, None, None))
        return tuple(out)

    def get(self):
        with self._lock:
            stamp = self.stamp()
            if self._snap is None or stamp != self._stamp:
                self._snap = self._load()
                self._stamp = stamp
            return self._snap

    # -- loading / validation
    def _load(self):
        warnings = []
        mannequin = _read_json(os.path.join(self.builtin_dir, "data", "mannequin.json"), None)
        if not isinstance(mannequin, dict) or "slots" not in mannequin \
                or not isinstance(mannequin.get("base"), str) or not mannequin["base"]:
            raise CatalogError("data/mannequin.json is missing or invalid")
        for key in ("canvas", "tile"):
            size = mannequin.get(key)
            if not (isinstance(size, list) and len(size) == 2
                    and all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in size)):
                raise CatalogError(f"data/mannequin.json: '{key}' must be [width, height]")
        slots = mannequin["slots"]
        if not isinstance(slots, dict):
            raise CatalogError("data/mannequin.json: 'slots' must be an object")
        for name, s in slots.items():
            if not isinstance(s, dict) or not isinstance(s.get("z"), int) or isinstance(s.get("z"), bool):
                raise CatalogError(f"data/mannequin.json: slot '{name}' needs an integer 'z'")
        for required in ("body", "back"):
            if required not in slots:
                raise CatalogError(f"data/mannequin.json: missing the required slot '{required}'")
        selectable = {n for n, s in slots.items() if s.get("selectable")}
        attire = self._load_attire(warnings)
        presets = self._load_presets(warnings)

        pieces, outfits = OrderedDict(), OrderedDict()
        for source in SOURCES:
            if source == "builtin":
                base = os.path.join(self.builtin_dir, "data")
            elif self.user_dir:
                base = self.user_dir
            else:
                continue
            pdata = _read_list(os.path.join(base, "pieces.json"), f"{source} pieces.json", warnings)
            odata = _read_list(os.path.join(base, "outfits.json"), f"{source} outfits.json", warnings)
            for raw in pdata:
                piece = self._clean_piece(raw, source, selectable, warnings)
                if piece is None:
                    continue
                if piece["id"] in pieces:
                    warnings.append(f"piece '{piece['id']}' ({source}) ignored: id already used")
                    continue
                pieces[piece["id"]] = piece
            for raw in odata:
                outfit = self._clean_outfit(raw, source, pieces, warnings)
                if outfit is None:
                    continue
                if outfit["id"] in outfits:
                    warnings.append(f"outfit '{outfit['id']}' ({source}) ignored: id already used")
                    continue
                outfits[outfit["id"]] = outfit
        return Snapshot(mannequin, pieces, outfits, warnings, attire, presets)

    def _load_presets(self, warnings):
        """Load the optional data/presets.json clothing gallery (curated tag sets with previews)."""
        path = os.path.join(self.builtin_dir, "data", "presets.json")
        if not os.path.exists(path):
            return []
        data = _read_list(path, "presets.json", warnings)
        out, seen = [], set()
        for raw in data:
            if not isinstance(raw, dict):
                warnings.append("a preset was ignored: not an object")
                continue
            pid = raw.get("id")
            if not (isinstance(pid, str) and pid and pid not in seen):
                warnings.append("a preset was ignored: missing or duplicate id")
                continue
            if not (isinstance(raw.get("name"), str) and raw["name"].strip()
                    and isinstance(raw.get("tags"), str) and raw["tags"].strip()):
                warnings.append(f"preset '{pid}' ignored: needs a name and tags")
                continue
            seen.add(pid)
            out.append({
                "id": pid,
                "name": _clean_text(raw.get("name"), 80),
                "name_zh": _clean_text(raw.get("name_zh"), 80),
                "tags": _clean_text(raw.get("tags"), 400),
                "tags_zh": _clean_text(raw.get("tags_zh"), 400),
                "categories": _clean_labels(raw.get("categories")),
                "traits": _clean_labels(raw.get("traits"), limit=12, maxlen=40),
                "image": raw.get("image") if isinstance(raw.get("image"), str) and raw["image"] else None,
                "preview": _clean_text(raw.get("preview"), 300),
            })
        return out

    def _load_attire(self, warnings):
        """Load the optional data/attire.json tag pools; a broken file is reported, not hidden."""
        path = os.path.join(self.builtin_dir, "data", "attire.json")
        if not os.path.exists(path):
            return None
        data = _read_json(path, None)
        if not isinstance(data, dict):
            warnings.append("attire.json ignored: expected a JSON object")
            return None
        slots, vocabulary = {}, []
        for key, value in data.items():
            if key == "vocabulary":
                if isinstance(value, list):
                    vocabulary = [t.strip() for t in value if isinstance(t, str) and t.strip()]
                continue
            if isinstance(value, list):
                slots[key] = [t.strip() for t in value if isinstance(t, str) and t.strip()]
        if not slots:
            warnings.append("attire.json ignored: no slot pools found")
            return None
        return {"slots": slots, "vocabulary": vocabulary}

    @staticmethod
    def _clean_piece(raw, source, selectable, warnings):
        if not isinstance(raw, dict):
            return None
        pid, slot, layer = raw.get("id"), raw.get("slot"), raw.get("layer")
        if not (isinstance(pid, str) and pid and isinstance(layer, str) and layer):
            warnings.append(f"a {source} piece was ignored: missing id or layer")
            return None
        if not isinstance(slot, str) or slot not in selectable:
            warnings.append(f"piece '{pid}' ignored: unknown slot {slot!r}")
            return None
        z = raw.get("z")
        bbox = raw.get("bbox")
        back = raw.get("back_layer")
        return {
            "id": pid, "slot": slot, "source": source,
            "label": _clean_text(raw.get("label"), 80) or pid,
            "tags": _clean_text(raw.get("tags"), 200),
            "nl": _clean_text(raw.get("nl"), 200),
            "layer": layer,
            "back_layer": back if isinstance(back, str) and back else None,
            "z": z if isinstance(z, int) and not isinstance(z, bool) else None,
            "hides": [s for s in raw["hides"] if isinstance(s, str) and s in selectable and s != slot]
            if isinstance(raw.get("hides"), list) else [],
            "conflicts": [s for s in raw["conflicts"] if isinstance(s, str) and s in selectable and s != slot]
            if isinstance(raw.get("conflicts"), list) else [],
            "style": _clean_styles(raw.get("style")),
            "colorable": bool(raw.get("colorable", False)),
            "bbox": bbox if isinstance(bbox, list) and len(bbox) == 4
            and all(isinstance(v, int) for v in bbox) else None,
        }

    @staticmethod
    def _clean_outfit(raw, source, pieces, warnings):
        if not isinstance(raw, dict):
            return None
        oid, plist = raw.get("id"), raw.get("pieces")
        if not (isinstance(oid, str) and oid and isinstance(plist, dict) and plist):
            warnings.append(f"a {source} outfit was ignored: missing id or pieces")
            return None
        for slot, pid in plist.items():
            piece = pieces.get(pid) if isinstance(pid, str) else None
            if piece is None or piece["slot"] != slot:
                warnings.append(f"outfit '{oid}' ignored: bad piece {pid!r} for slot '{slot}'")
                return None
        tile = raw.get("tile")
        return {
            "id": oid, "source": source,
            "label": _clean_text(raw.get("label"), MAX_LABEL) or oid,
            "style": _clean_styles(raw.get("style")),
            "pieces": dict(plist),
            "prefix": _clean_text(raw.get("prefix"), MAX_LABEL),
            "tile": tile if isinstance(tile, str) and tile else None,
        }

    # -- public view (what the frontend receives)
    def public(self):
        snap = self.get()
        m = snap.mannequin
        pieces = []
        for p in snap.pieces.values():
            item = {k: p[k] for k in ("id", "slot", "label", "tags", "nl", "z", "hides",
                                       "conflicts", "style", "colorable", "bbox", "source")}
            item["layer_url"] = _asset_url(p["source"], p["layer"])
            item["back_layer_url"] = _asset_url(p["source"], p["back_layer"]) if p["back_layer"] else None
            pieces.append(item)
        outfits = []
        for o in snap.outfits.values():
            item = {k: o[k] for k in ("id", "label", "style", "pieces", "prefix", "source")}
            item["tile_url"] = _asset_url(o["source"], o["tile"]) if o["tile"] else None
            outfits.append(item)
        return {
            "version": 1,
            "canvas": m["canvas"], "tile": m["tile"],
            "base_url": _asset_url("builtin", m["base"]),
            "slots": m["slots"], "output_order": m.get("output_order", []),
            "pieces": pieces, "outfits": outfits,
            "attire": snap.attire, "presets": snap.presets,
            "can_save": bool(self.user_dir),
            "warnings": snap.warnings,
        }

    # -- assets
    def find_asset(self, source, rel):
        """Absolute path of an image asset, or None. Guards against traversal."""
        root = self._asset_root(source) if source in SOURCES else None
        if not root or not isinstance(rel, str) or not rel or "\x00" in rel:
            return None
        rel = rel.replace("\\", "/")
        if rel.startswith("/"):
            return None
        root_real = os.path.realpath(root)
        path = os.path.realpath(os.path.join(root_real, rel))
        try:
            if os.path.commonpath([path, root_real]) != root_real:
                return None
        except ValueError:
            return None
        if not path.lower().endswith(IMAGE_EXTS) or not os.path.isfile(path):
            return None
        return path

    def _open_layer(self, rel, source):
        path = self.find_asset(source, rel)
        if path is None:
            raise CatalogError(f"missing asset {source}/{rel}")
        with Image.open(path) as im:
            return im.convert("RGBA")

    def composite(self, slot_map, snap=None):
        snap = snap or self.get()
        order = draw_list(snap.mannequin, snap.pieces, slot_map)
        return compose(snap.mannequin["canvas"], order, self._open_layer)

    # -- user outfits
    def save_user_outfit(self, label, pieces_map, style=None, prefix=""):
        """Validate and store a user outfit, bake its tile, return the entry."""
        if not self.user_dir:
            raise CatalogError("user directory unavailable; cannot save outfits")
        label = _clean_text(label, MAX_LABEL)
        if not label:
            raise CatalogError("outfit needs a name")
        if not isinstance(pieces_map, dict) or not pieces_map:
            raise CatalogError("outfit needs at least one piece")
        snap = self.get()
        chosen = {}
        for slot, pid in pieces_map.items():
            if isinstance(pid, dict):
                raise CatalogError(f"slot '{slot}' is a raw tag entry; saved outfits need catalog pieces")
            piece = snap.pieces.get(pid) if isinstance(pid, str) else None
            if piece is None or piece["slot"] != slot:
                raise CatalogError(f"unknown piece for slot '{slot}'")
            chosen[slot] = pid
        for slot, pid in chosen.items():
            for c in snap.pieces[pid]["conflicts"]:
                if c in chosen:
                    raise CatalogError(f"'{pid}' conflicts with the '{c}' piece")

        with self._lock:
            path = os.path.join(self.user_dir, "outfits.json")
            existing = _read_list_strict(path, "your saved outfits.json")
            if len(existing) >= MAX_USER_OUTFITS:
                raise CatalogError("too many saved outfits")
            taken = set(snap.outfits) | {o.get("id") for o in existing if isinstance(o, dict)}
            slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:32] or "outfit"
            oid, n = f"user_{slug}", 2
            while oid in taken:
                oid, n = f"user_{slug}_{n}", n + 1

            tile_rel = f"outfits/{oid}.webp"
            tile_path = os.path.join(self.user_dir, "assets", tile_rel)
            os.makedirs(os.path.dirname(tile_path), exist_ok=True)
            tile = self.composite(chosen, snap).resize(tuple(snap.mannequin["tile"]), Image.LANCZOS)
            tmp = tile_path + ".tmp"
            tile.save(tmp, format="WEBP", lossless=True)
            os.replace(tmp, tile_path)

            entry = {"id": oid, "label": label, "style": _clean_styles(style),
                     "pieces": chosen, "prefix": _clean_text(prefix, MAX_LABEL), "tile": tile_rel}
            existing.append(entry)
            _write_json_atomic(path, existing)
        return entry

    def delete_user_outfit(self, outfit_id):
        if not self.user_dir:
            raise CatalogError("user directory unavailable")
        with self._lock:
            path = os.path.join(self.user_dir, "outfits.json")
            existing = _read_list_strict(path, "your saved outfits.json")
            keep = [o for o in existing if not (isinstance(o, dict) and o.get("id") == outfit_id)]
            if len(keep) == len(existing):
                raise CatalogError("no such user outfit")
            removed = next(o for o in existing if isinstance(o, dict) and o.get("id") == outfit_id)
            _write_json_atomic(path, keep)
            tile = self.find_asset("user", removed.get("tile"))
            if tile:
                try:
                    os.remove(tile)
                except OSError:
                    pass

    # -- favorites (server-persisted, grouped; model from Comfyui-Anima-Tools, MIT)
    def _favorites_path(self):
        return os.path.join(self.user_dir, "favorites.json") if self.user_dir else None

    def load_favorites(self):
        path = self._favorites_path()
        if path is None or not os.path.exists(path):
            return _default_favorites()
        data = _read_json(path, None)  # a broken file reads as None here and is reported, not hidden
        if data is None:
            print("[Magik Outfit] warning: favorites.json is not valid JSON; using defaults")
            return _default_favorites()
        return _normalize_favorites(data)

    def save_favorites(self, payload):
        """Merge {kind: {groups, items}} into favorites.json (atomic write + .bak backup)."""
        if not self.user_dir:
            raise CatalogError("user directory unavailable; cannot save favorites")
        if not isinstance(payload, dict):
            raise CatalogError("favorites payload must be a JSON object")
        with self._lock:
            path = self._favorites_path()
            existing = _normalize_favorites(_read_json_strict(path, "your favorites.json"))
            merged = existing
            for kind in FAVORITE_KINDS:
                if kind in payload:
                    merged[kind] = _normalize_favorites({kind: payload[kind]})[kind]
            _write_json_atomic_with_backup(path, merged)
        return merged

    # -- my presets (the user's own tag presets with optional covers)
    def _my_presets_dir(self):
        if not self.user_dir:
            return None
        path = os.path.join(self.user_dir, "my_presets")
        os.makedirs(os.path.join(path, "covers"), exist_ok=True)
        return path

    def load_my_presets(self):
        d = self._my_presets_dir()
        if d is None:
            return {"available": False, "items": []}
        data = _read_json(os.path.join(d, "my_presets.json"), {}) or {}
        items_raw = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items_raw, list):
            items_raw = []
        seen = set()
        items = []
        for raw in items_raw:
            item = _clean_my_preset(raw, seen)
            if item:
                item["cover_url"] = f"/magik/outfit/mypresets/cover/{item['cover']}" if item["cover"] else None
                items.append(item)
        return {"available": True, "items": items}

    def save_my_presets(self, items):
        d = self._my_presets_dir()
        if d is None:
            raise CatalogError("user directory unavailable; cannot save presets")
        if not isinstance(items, list):
            raise CatalogError("my presets payload must be an items list")
        seen = set()
        clean = []
        for raw in items[:MAX_MY_PRESETS]:
            item = _clean_my_preset(raw, seen)
            if item:
                clean.append(item)
        with self._lock:
            path = os.path.join(d, "my_presets.json")
            _write_json_atomic_with_backup(path, {"items": clean})
            # covers are uploaded right before the save that references them: prune unreferenced ones
            used = {it["cover"] for it in clean if it["cover"]}
            covers = os.path.join(d, "covers")
            for name in os.listdir(covers):
                if COVER_NAME_RE.match(name) and name not in used:
                    try:
                        os.remove(os.path.join(covers, name))
                    except OSError:
                        pass
        for it in clean:
            it["cover_url"] = f"/magik/outfit/mypresets/cover/{it['cover']}" if it["cover"] else None
        return {"available": True, "items": clean}

    def save_my_preset_cover(self, data):
        d = self._my_presets_dir()
        if d is None:
            raise CatalogError("user directory unavailable; cannot upload covers")
        data = bytes(data) if isinstance(data, (bytes, bytearray)) else b""
        if len(data) > COVER_MAX_BYTES or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
            raise CatalogError("expected a WebP image up to 4 MB")
        name = uuid.uuid4().hex + ".webp"
        with open(os.path.join(d, "covers", name), "wb") as f:
            f.write(data)
        return name

    def find_my_preset_cover(self, name):
        """Absolute path of a preset cover, or None. Guards against traversal."""
        d = self._my_presets_dir()
        if d is None or not isinstance(name, str) or not COVER_NAME_RE.match(name):
            return None
        root = os.path.realpath(os.path.join(d, "covers"))
        path = os.path.realpath(os.path.join(root, name))
        try:
            if os.path.commonpath([path, root]) != root:
                return None
        except ValueError:
            return None
        return path if os.path.isfile(path) else None
