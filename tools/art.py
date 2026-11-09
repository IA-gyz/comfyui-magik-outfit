#!/usr/bin/env python3
"""Magik Outfit art pipeline.

  init-spec       build art/spec.json from the current catalog (then edit prompts/regions by hand)
  check           validate the spec
  check-workflow  verify a ComfyUI API workflow has the MAGIK_* titled nodes
  masks           write the slot-region masks (for an inpaint-lock workflow, MAGIK_MASK)
  render          run a ComfyUI workflow once per piece (and mannequin variant)
  extract         turn renders into transparent layers + QA report + contact sheet
  import          write the layers and spec fields into assets/ and data/, bake tiles, validate

Typical run:
  python tools/art.py init-spec
  python tools/art.py check-workflow my_edit_workflow_api.json
  python tools/art.py render --workflow my_edit_workflow_api.json --only top_blouse
  python tools/art.py extract --only top_blouse   # look at art/work/contact_sheet.png
  python tools/art.py import  --only top_blouse
"""
import argparse
import io
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from PIL import Image  # noqa: E402

from art_pipeline import extract as ex  # noqa: E402
from art_pipeline import spec as sp  # noqa: E402
from art_pipeline.comfy_client import (  # noqa: E402
    TITLES, ComfyClient, ComfyError, WorkflowError, check_workflow, prepare_workflow)
from art_pipeline.sync import SyncError, sync_catalog  # noqa: E402


class Paths:
    def __init__(self, root, spec=None, work=None):
        self.root = os.path.abspath(root)
        self.spec = os.path.abspath(spec) if spec else os.path.join(self.root, "art", "spec.json")
        self.work = os.path.abspath(work) if work else os.path.join(self.root, "art", "work")

    def renders(self, variant):
        return os.path.join(self.work, "renders", variant)

    @property
    def layers(self):
        return os.path.join(self.work, "layers")

    @property
    def masks(self):
        return os.path.join(self.work, "masks")

    @property
    def qa(self):
        return os.path.join(self.work, "qa.json")


def _selected(spec, only):
    if not only:
        return spec["pieces"]
    known = {p["id"] for p in spec["pieces"]}
    unknown = [i for i in only if i not in known]
    if unknown:
        raise SystemExit(f"not in the spec: {', '.join(unknown)}")
    return [p for p in spec["pieces"] if p["id"] in set(only)]


# ---------------------------------------------------------------- commands
def cmd_init_spec(args, paths, out):
    if os.path.exists(paths.spec) and not args.force:
        out(f"{paths.spec} already exists (use --force to overwrite)")
        return 1
    data = os.path.join(paths.root, "data")
    with open(os.path.join(data, "pieces.json"), encoding="utf-8") as f:
        pieces = json.load(f)
    with open(os.path.join(data, "outfits.json"), encoding="utf-8") as f:
        outfits = json.load(f)
    with open(os.path.join(data, "mannequin.json"), encoding="utf-8") as f:
        canvas = json.load(f)["canvas"]
    sp.save_spec(sp.default_spec(pieces, outfits, canvas), paths.spec)
    out(f"wrote {paths.spec} ({len(pieces)} pieces, {len(outfits)} outfits)")
    return 0


def cmd_check(args, paths, out):
    try:
        spec = sp.load_spec(paths.spec)
    except (OSError, ValueError) as e:
        out(str(e))
        return 1
    out(f"spec OK: {len(spec['pieces'])} pieces, {len(spec['outfits'])} outfits, "
        f"{len(spec['variants'])} mannequin variant(s)")
    for v in spec["variants"]:
        base = os.path.join(paths.work, v["base"])
        out(f"  variant {v['name']}: base {'found' if os.path.isfile(base) else 'MISSING'} ({base})")
    return 0


def cmd_check_workflow(args, paths, out):
    with open(args.workflow, encoding="utf-8") as f:
        wf = json.load(f)
    ok, problems, found = check_workflow(wf)
    for role, title in TITLES.items():
        ids = found.get(role, [])
        out(f"  {title:18s} {', '.join(ids) if ids else '-'}")
    for p in problems:
        out(f"PROBLEM: {p}")
    out("workflow OK" if ok else "workflow NOT usable")
    return 0 if ok else 1


def cmd_masks(args, paths, out):
    spec = sp.load_spec(paths.spec)
    os.makedirs(paths.masks, exist_ok=True)
    for slot in spec["regions"]:
        sp.region_mask(spec, slot, spec["extract"]["region_margin"]).save(os.path.join(paths.masks, f"{slot}.png"))
    out(f"wrote {len(spec['regions'])} region masks to {paths.masks}")
    return 0


def _png_bytes(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def cmd_render(args, paths, out):
    spec = sp.load_spec(paths.spec)
    with open(args.workflow, encoding="utf-8") as f:
        template = json.load(f)
    ok, problems, found = check_workflow(template)
    if not ok:
        out("workflow not usable: " + "; ".join(problems))
        return 1
    pieces = _selected(spec, args.only)
    canvas = tuple(spec["canvas"])
    client = ComfyClient(args.server, timeout=args.timeout, poll=args.poll)
    margin = spec["extract"]["region_margin"]

    jobs = []
    for p in pieces:
        for v in spec["variants"]:
            dest = os.path.join(paths.renders(v["name"]), f"{p['id']}.png")
            if os.path.exists(dest) and not args.force:
                continue
            jobs.append((p, v, dest))
    if args.dry_run:
        for p, v, dest in jobs:
            prompt, negative = sp.build_prompt(spec, p)
            out(f"{p['id']} [{v['name']}]: {prompt}")
        out(f"{len(jobs)} job(s) would run")
        return 0
    if not jobs:
        out("nothing to render (use --force to redo existing renders)")
        return 0

    try:
        uploaded_bases, uploaded_masks = {}, {}
        for v in spec["variants"]:
            if not any(j[1] is v for j in jobs):
                continue
            base_path = os.path.join(paths.work, v["base"])
            if not os.path.isfile(base_path):
                out(f"base image for variant '{v['name']}' not found: {base_path}")
                return 1
            with Image.open(base_path) as im:
                if abs(im.width / im.height - canvas[0] / canvas[1]) > 0.01:
                    out(f"base image {im.width}x{im.height} does not match the canvas aspect {canvas[0]}x{canvas[1]}")
                    return 1
            with open(base_path, "rb") as f:
                data = f.read()
            uploaded_bases[v["name"]] = client.upload_image(data, f"magik_outfit_base_{v['name']}.png")

        failures = []
        for n, (p, v, dest) in enumerate(jobs, 1):
            label = f"[{n}/{len(jobs)}] {p['id']} ({v['name']})"
            t0 = time.monotonic()
            try:
                mask_name = None
                if found["mask"]:
                    if p["slot"] not in uploaded_masks:
                        mask = sp.region_mask(spec, p["slot"], margin)
                        uploaded_masks[p["slot"]] = client.upload_image(_png_bytes(mask), f"magik_outfit_mask_{p['slot']}.png")
                    mask_name = uploaded_masks[p["slot"]]
                prompt, negative = sp.build_prompt(spec, p)
                wf, output_node = prepare_workflow(
                    template, base_name=uploaded_bases[v["name"]], prompt=prompt, negative=negative,
                    seed=spec["seed"], mask_name=mask_name, text_key=args.text_key)
                data = client.run(wf, output_node)
                with Image.open(io.BytesIO(data)) as im:
                    im.load()
                    if abs(im.width / im.height - canvas[0] / canvas[1]) > 0.01:
                        raise ComfyError(f"result is {im.width}x{im.height}, which does not match the canvas aspect")
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    im.convert("RGB").save(dest)
                out(f"{label} ok ({time.monotonic() - t0:.1f}s)")
            except (ComfyError, WorkflowError, OSError, ValueError) as e:
                out(f"{label} FAILED: {e}")
                failures.append(p["id"])
                if isinstance(e, ComfyError) and "cannot reach" in str(e):
                    out("stopping: the server is unreachable")
                    break
        if failures:
            out(f"{len(failures)} failed: {', '.join(sorted(set(failures)))}")
            return 1
        return 0
    except (ComfyError, WorkflowError) as e:
        out(f"error: {e}")
        return 1


def cmd_extract(args, paths, out):
    spec = sp.load_spec(paths.spec)
    params = spec["extract"]
    pieces = _selected(spec, args.only)
    canvas = tuple(spec["canvas"])
    os.makedirs(paths.layers, exist_ok=True)

    bases = {}
    for v in spec["variants"]:
        path = os.path.join(paths.work, v["base"])
        if not os.path.isfile(path):
            out(f"base image for variant '{v['name']}' not found: {path}")
            return 1
        bases[v["name"]] = Image.open(path).convert("RGB")

    previous = {}
    if os.path.isfile(paths.qa):
        with open(paths.qa, encoding="utf-8") as f:
            previous = json.load(f)
    report = dict(previous)
    layers, errors = {}, 0
    for p in pieces:
        renders = []
        for v in spec["variants"]:
            path = os.path.join(paths.renders(v["name"]), f"{p['id']}.png")
            renders.append(Image.open(path).convert("RGB") if os.path.isfile(path) else None)
        if any(r is None for r in renders):
            out(f"{p['id']}: skipped (render missing for variant "
                f"{', '.join(v['name'] for v, r in zip(spec['variants'], renders) if r is None)})")
            continue
        mask = None
        if args.mask_dir:
            mpath = os.path.join(args.mask_dir, f"{p['id']}.png")
            if os.path.isfile(mpath):
                mask = Image.open(mpath).convert("L")
        region = sp.region_mask(spec, p["slot"], params["region_margin"])
        layer, stats = ex.extract_layer([bases[v["name"]] for v in spec["variants"]], renders, region, params, mask)
        if layer.size != canvas:
            layer = layer.resize(canvas, Image.LANCZOS)
        layer.save(os.path.join(paths.layers, f"{p['id']}.png"))
        qa = ex.qa_layer(layer, stats, params)
        report[p["id"]] = qa
        layers[p["id"]] = layer
        marks = ", ".join(f"{i['level']}:{i['code']}" for i in qa["issues"]) or "ok"
        out(f"{p['id']:28s} {marks}")
        for i in qa["issues"]:
            out(f"    {i['level']}: {i['msg']}")
        errors += 0 if qa["ok"] else 1

    with open(paths.qa, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    if layers:
        ex.contact_sheet(layers).save(os.path.join(paths.work, "contact_sheet.png"))
        out(f"wrote {len(layers)} layer(s) to {paths.layers}; review {os.path.join(paths.work, 'contact_sheet.png')}")
    else:
        out("no layers extracted (run render first)")
    return 1 if errors else 0


def cmd_import(args, paths, out):
    spec = sp.load_spec(paths.spec)
    only = args.only
    if not args.allow_qa_errors:
        if not os.path.isfile(paths.qa):
            out("no QA report: run `extract` first (or pass --allow-qa-errors)")
            return 1
        with open(paths.qa, encoding="utf-8") as f:
            report = json.load(f)
        candidates = only or [p["id"] for p in spec["pieces"]]
        blocked = [pid for pid in candidates if pid in report and not report[pid]["ok"]]
        if blocked:
            out("blocked by QA errors: " + ", ".join(blocked) + " (fix them, or pass --allow-qa-errors)")
            return 1
        if only:
            unextracted = [pid for pid in only if pid not in report]
            if unextracted:
                out("no QA report for: " + ", ".join(unextracted))
                return 1
    if args.prune and only:
        out("note: --prune is ignored when --only is given (it needs the whole spec); nothing will be removed")
    try:
        summary = sync_catalog(spec, paths.layers, paths.root, only=only, prune=args.prune, dry_run=args.dry_run)
    except SyncError as e:
        out(f"import failed: {e}")
        return 1
    for key in ("written", "added", "skipped_no_layer", "pruned", "outfits_written", "outfits_deferred"):
        if summary[key]:
            out(f"{key}: {', '.join(summary[key])}")
    if args.dry_run:
        out("dry run: nothing was written")
    else:
        for tool in ("bake_tiles.py", "validate_catalog.py"):
            out(f"{tool}: {summary.get(tool, '')}")
    return 0


# -------------------------------------------------------------------- main
def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=os.path.dirname(HERE), help=argparse.SUPPRESS)
    ap.add_argument("--spec", help="spec file (default: art/spec.json)")
    ap.add_argument("--work", help="work folder (default: art/work)")
    sub = ap.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init-spec")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init_spec)

    sub.add_parser("check").set_defaults(fn=cmd_check)

    s = sub.add_parser("check-workflow")
    s.add_argument("workflow")
    s.set_defaults(fn=cmd_check_workflow)

    sub.add_parser("masks").set_defaults(fn=cmd_masks)

    s = sub.add_parser("render")
    s.add_argument("--workflow", required=True, help="API-format workflow (Save (API Format))")
    s.add_argument("--server", default="http://127.0.0.1:8188")
    s.add_argument("--only", nargs="+", metavar="ID")
    s.add_argument("--force", action="store_true", help="re-render pieces that already have a render")
    s.add_argument("--text-key", help="name of the prompt node's text input (default: auto-detect)")
    s.add_argument("--timeout", type=float, default=900.0, help="seconds to wait per job")
    s.add_argument("--poll", type=float, default=0.5)
    s.add_argument("--dry-run", action="store_true", help="print the prompts without contacting ComfyUI")
    s.set_defaults(fn=cmd_render)

    s = sub.add_parser("extract")
    s.add_argument("--only", nargs="+", metavar="ID")
    s.add_argument("--mask-dir", help="folder of <piece_id>.png garment masks (e.g. from a segmentation node)")
    s.set_defaults(fn=cmd_extract)

    s = sub.add_parser("import")
    s.add_argument("--only", nargs="+", metavar="ID")
    s.add_argument("--prune", action="store_true", help="remove pieces/outfits that are not in the spec")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--allow-qa-errors", action="store_true")
    s.set_defaults(fn=cmd_import)
    return ap


def main(argv=None, out=print):
    args = build_parser().parse_args(argv)
    paths = Paths(args.root, args.spec, args.work)
    try:
        return args.fn(args, paths, out)
    except (sp.SpecError, FileNotFoundError, json.JSONDecodeError) as e:
        out(f"error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
