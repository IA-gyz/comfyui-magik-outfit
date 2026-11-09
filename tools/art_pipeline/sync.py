"""Sync extracted layers + the spec into the package catalog (assets/ and data/)."""
import json
import os
import subprocess
import sys

from PIL import Image

from .spec import piece_layer_name

PIECE_COPY_FIELDS = ("slot", "label", "tags", "nl", "z", "hides", "conflicts", "style", "colorable")


class SyncError(RuntimeError):
    pass


def _read(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def _remove_file(path):
    try:
        os.remove(path)
    except OSError:
        pass


class _Backup:
    """Remembers the bytes of every file a sync may touch (or that it did not exist), so a failure
    part-way through leaves the catalog exactly as it was."""

    def __init__(self):
        self.files = {}

    def keep(self, path):
        path = os.path.normpath(path)
        if path in self.files:
            return
        try:
            with open(path, "rb") as f:
                self.files[path] = f.read()
        except OSError:
            self.files[path] = None  # did not exist

    def restore(self):
        for path, data in self.files.items():
            _remove_file(path + ".tmp")
            if data is None:
                _remove_file(path)
            else:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(data)


def sync_catalog(spec, layers_dir, package_root, only=None, prune=False, dry_run=False, run_tools=True):
    """Apply the spec to <package_root>/data and <package_root>/assets.

    - Pieces with an extracted layer in `layers_dir/<id>.png` are written (and added if new).
    - Pieces without a layer are left exactly as they are.
    - prune: pieces (and their layer files) that are not in the spec are removed.
    - Outfits from the spec are written when every piece they use exists; otherwise the existing
      entry (if any) is kept and the outfit is reported as deferred.
    Returns a summary dict.
    """
    data_dir = os.path.join(package_root, "data")
    assets_dir = os.path.join(package_root, "assets")
    canvas = tuple(spec["canvas"])
    existing = _read(os.path.join(data_dir, "pieces.json"), [])
    by_id = {p["id"]: p for p in existing}
    spec_ids = [p["id"] for p in spec["pieces"]]
    wanted = set(only) if only else None
    if wanted:
        unknown = wanted - set(spec_ids)
        if unknown:
            raise SyncError(f"not in the spec: {', '.join(sorted(unknown))}")

    summary = {"written": [], "added": [], "skipped_no_layer": [], "pruned": [],
               "outfits_written": [], "outfits_deferred": [], "dry_run": dry_run}
    new_entries = {}
    layer_writes = []   # (path, PIL image)
    stale_files = []

    for p in spec["pieces"]:
        pid = p["id"]
        if wanted and pid not in wanted:
            continue
        src = os.path.join(layers_dir, f"{pid}.png")
        if not os.path.isfile(src):
            summary["skipped_no_layer"].append(pid)
            continue
        with Image.open(src) as im:
            layer = im.convert("RGBA")
        if layer.size != canvas:
            raise SyncError(f"{pid}: layer is {layer.size[0]}x{layer.size[1]}, canvas is {canvas[0]}x{canvas[1]}")
        rel = f"pieces/{p['slot']}/{piece_layer_name(p)}.webp"
        entry = dict(by_id.get(pid) or {"id": pid, "back_layer": None, "bbox": None})
        for key in PIECE_COPY_FIELDS:
            if key in p:
                entry[key] = p[key]
        old_rel = entry.get("layer")
        entry["layer"] = rel
        entry.setdefault("back_layer", None)
        entry.setdefault("bbox", None)
        entry["bbox"] = None  # recomputed by bake_tiles.py
        new_entries[pid] = entry
        layer_writes.append((os.path.join(assets_dir, rel), layer))
        if old_rel and old_rel != rel:
            stale_files.append(os.path.join(assets_dir, old_rel))
        (summary["written"] if pid in by_id else summary["added"]).append(pid)

    # compose the final piece list: spec order first, then pieces the spec does not mention
    final = []
    for pid in spec_ids:
        if pid in new_entries:
            final.append(new_entries[pid])
        elif pid in by_id:
            final.append(by_id[pid])
    for p in existing:
        if p["id"] in spec_ids:
            continue
        if prune and not wanted:
            summary["pruned"].append(p["id"])
            stale_files.append(os.path.join(assets_dir, p["layer"]))
            if p.get("back_layer"):
                stale_files.append(os.path.join(assets_dir, p["back_layer"]))
        else:
            final.append(p)
    final_ids = {p["id"] for p in final}

    # outfits
    outfits_existing = _read(os.path.join(data_dir, "outfits.json"), [])
    oby_id = {o["id"]: o for o in outfits_existing}
    outfits_final = []
    for o in spec.get("outfits", []):
        if all(pid in final_ids for pid in o.get("pieces", {}).values()):
            entry = dict(oby_id.get(o["id"], {}))
            entry.update({k: o[k] for k in ("id", "label", "style", "pieces", "prefix") if k in o})
            entry.setdefault("tile", f"outfits/{o['id']}.webp")
            outfits_final.append(entry)
            summary["outfits_written"].append(o["id"])
        else:
            summary["outfits_deferred"].append(o["id"])
            if o["id"] in oby_id:
                outfits_final.append(oby_id[o["id"]])
    spec_outfit_ids = {o["id"] for o in spec.get("outfits", [])}
    for o in outfits_existing:
        if o["id"] not in spec_outfit_ids:
            if prune and not wanted:
                stale_files.append(os.path.join(assets_dir, o.get("tile") or f"outfits/{o['id']}.webp"))
            else:
                outfits_final.append(o)
    # an outfit that still points at a removed piece would break the catalog: refuse instead
    for o in outfits_final:
        missing = [pid for pid in o["pieces"].values() if pid not in final_ids]
        if missing:
            raise SyncError(f"outfit '{o['id']}' uses pieces that would no longer exist: {', '.join(missing)} "
                            "(add the outfit to the spec, or sync without --prune)")

    if dry_run:
        return summary

    # bake_tiles rewrites every tile and the piece bboxes, so snapshot those too
    backup = _Backup()
    for path, _ in layer_writes:
        backup.keep(path)
    for path in stale_files:
        backup.keep(path)
    for name in ("pieces.json", "outfits.json"):
        backup.keep(os.path.join(data_dir, name))
    for o in outfits_existing + outfits_final:
        backup.keep(os.path.join(assets_dir, o.get("tile") or f"outfits/{o['id']}.webp"))
    try:
        for path, layer in layer_writes:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            layer.save(tmp, format="WEBP", lossless=True)
            os.replace(tmp, path)
        _write(os.path.join(data_dir, "pieces.json"), final)
        _write(os.path.join(data_dir, "outfits.json"), outfits_final)
        in_use = {os.path.normpath(os.path.join(assets_dir, p[k])) for p in final for k in ("layer", "back_layer") if p.get(k)}
        for path in stale_files:
            if os.path.normpath(path) not in in_use:
                _remove_file(path)

        if run_tools:
            for tool in ("bake_tiles.py", "validate_catalog.py"):
                proc = subprocess.run([sys.executable, os.path.join("tools", tool)], cwd=package_root,
                                      capture_output=True, text=True)
                summary[tool] = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
                if proc.returncode != 0:
                    raise SyncError(f"{tool} failed:\n{proc.stdout}{proc.stderr}")
    except BaseException:
        backup.restore()
        raise
    return summary
