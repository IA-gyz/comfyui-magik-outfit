"""Magik Outfit core: catalog, selection logic, string assembly, compositing.

No ComfyUI imports and no relative imports, so it can be tested standalone and
shared with tools/. The V3 node (magik_outfit.py) and the aiohttp routes
(outfit_routes.py) are thin wrappers around this module.
"""
import json
import os
import re
import threading
import urllib.parse
from collections import OrderedDict

from PIL import Image

FORMATS = ("tags", "natural", "both")
SOURCES = ("builtin", "user")
IMAGE_EXTS = (".webp", ".png")
MAX_LABEL = 60
MAX_USER_OUTFITS = 500
ASSET_URL = "/magik/outfit/asset"


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
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


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
    except ValueError:
        return None, {}
    if not isinstance(data, dict):
        return None, {}
    outfit_id = data.get("outfit")
    outfit_id = outfit_id if isinstance(outfit_id, str) else None
    overrides = {}
    raw = data.get("overrides")
    if isinstance(raw, dict):
        for slot, pid in raw.items():
            if isinstance(slot, str) and (pid is None or isinstance(pid, str)):
                overrides[slot] = pid
    return outfit_id, overrides


def resolve(outfit, overrides, pieces):
    """Resolve an outfit + overrides to {slot: piece_id}.

    - Slots missing from `overrides` keep the outfit's piece.
    - An override of None removes the slot.
    - Conflicts are symmetric: a chosen piece drops the slots it lists and any
      existing piece that lists the chosen piece's slot (a top over a dress
      drops the dress). A slot that is itself overridden with a piece is left
      alone, so explicit UI state is never second-guessed.
    - Unknown ids and pieces sent to the wrong slot are ignored.
    """
    overrides = overrides or {}
    result = {}
    if outfit:
        result = {s: p for s, p in outfit["pieces"].items() if p in pieces}
    for slot, pid in overrides.items():
        if pid is None:
            result.pop(slot, None)
            continue
        piece = pieces.get(pid)
        if piece is None or piece["slot"] != slot:
            continue
        result[slot] = pid
        for other in list(result):
            if other == slot or overrides.get(other) is not None:
                continue
            other_piece = pieces.get(result[other])
            if other in piece.get("conflicts", []) or (
                other_piece and slot in other_piece.get("conflicts", [])
            ):
                result.pop(other, None)
    return result


def build_string(slot_map, outfit, modified, mannequin, pieces, fmt="tags",
                 separator=", ", prefix=""):
    """Assemble the output STRING from a resolved {slot: piece_id} map."""
    fmt = fmt if fmt in FORMATS else "tags"
    sep = _unescape(separator if isinstance(separator, str) else ", ")
    lead = ""
    if outfit and not modified:
        lead = _clean_text(outfit.get("prefix", ""), MAX_LABEL)

    tags, natural = ([lead] if lead else []), ([lead] if lead else [])
    for slot in mannequin.get("output_order", []):
        piece = pieces.get(slot_map.get(slot))
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
    present = [pieces[pid] for pid in slot_map.values() if pid in pieces]
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


# ---------------------------------------------------------------- catalog
class Snapshot:
    def __init__(self, mannequin, pieces, outfits, warnings):
        self.mannequin = mannequin
        self.pieces = pieces      # OrderedDict id -> piece (with 'source')
        self.outfits = outfits    # OrderedDict id -> outfit (with 'source')
        self.warnings = warnings


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
                 for n in ("mannequin.json", "pieces.json", "outfits.json")]
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
        if not isinstance(mannequin, dict) or "slots" not in mannequin or "base" not in mannequin:
            raise CatalogError("data/mannequin.json is missing or invalid")
        selectable = {n for n, s in mannequin["slots"].items() if s.get("selectable")}

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
        return Snapshot(mannequin, pieces, outfits, warnings)

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
