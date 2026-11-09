"""Tests for the art pipeline (tools/art.py + tools/art_pipeline).

    python -m unittest tests.test_art -v        (needs Pillow, numpy, aiohttp)

The "renderer" in the fake ComfyUI server composites the known placeholder layers onto the uploaded
mannequin image, so every stage of render -> extract -> import can be checked against exact ground truth.
"""
import asyncio
import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
import urllib.request
import uuid

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(PKG, "tools"), PKG):
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    import numpy as np
    from aiohttp import web
    HAVE_DEPS = True
except ImportError:  # pragma: no cover
    HAVE_DEPS = False

from PIL import Image  # noqa: E402

if HAVE_DEPS:
    import art  # noqa: E402
    import outfit_core as oc  # noqa: E402
    from art_pipeline import comfy_client as C  # noqa: E402
    from art_pipeline import extract as E  # noqa: E402
    from art_pipeline import spec as S  # noqa: E402
    from art_pipeline import sync as Y  # noqa: E402

BG = (205, 205, 210)
MANNEQUIN = (172, 172, 178)
DARK_MANNEQUIN = (45, 45, 52)

# ---------------------------------------------------------------- helpers
def placeholder_layers(root=PKG):
    """{piece_id: RGBA layer} and the mannequin alpha, read from the shipped placeholder assets."""
    cat = oc.Catalog(root)
    snap = cat.get()
    layers = {}
    for pid, p in snap.pieces.items():
        with Image.open(os.path.join(root, "assets", p["layer"])) as im:
            layers[pid] = im.convert("RGBA")
    with Image.open(os.path.join(root, "assets", snap.mannequin["base"])) as im:
        mannequin_alpha = im.convert("RGBA").getchannel("A")
    return layers, mannequin_alpha, snap


def mannequin_rgba(alpha, color):
    img = Image.new("RGBA", alpha.size, color + (255,))
    img.putalpha(alpha)
    return img


def over_bg(rgba):
    bg = Image.new("RGBA", rgba.size, BG + (255,))
    bg.alpha_composite(rgba)
    return bg.convert("RGB")


def noisy(img, sigma, rng, shift=0.0):
    a = np.asarray(img, dtype=np.float32) + rng.normal(0, sigma, (img.size[1], img.size[0], 3)) + shift
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


def iou(a, b):
    a, b = np.asarray(a.getchannel("A")) > 127, np.asarray(b.getchannel("A")) > 127
    return float((a & b).sum()) / max(1, int((a | b).sum()))


def jload(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def jdump(obj, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)


def rtext(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def png(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def workflow_template(mask=True):
    wf = {
        "1": {"class_type": "LoadImage", "inputs": {"image": "x.png"}, "_meta": {"title": "MAGIK_BASE_IMAGE"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["9", 0]}, "_meta": {"title": "MAGIK_PROMPT"}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["9", 0]}, "_meta": {"title": "MAGIK_NEGATIVE"}},
        "4": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": 20}, "_meta": {"title": "MAGIK_SEED"}},
        "5": {"class_type": "SaveImage", "inputs": {"images": ["4", 0], "filename_prefix": "magik"}, "_meta": {"title": "MAGIK_OUTPUT"}},
        "9": {"class_type": "CLIPLoader", "inputs": {"clip_name": "c.safetensors"}, "_meta": {"title": "Load CLIP"}},
    }
    if mask:
        wf["6"] = {"class_type": "LoadImage", "inputs": {"image": "m.png"}, "_meta": {"title": "MAGIK_MASK"}}
    return wf


class FakeComfy:
    """Just enough of ComfyUI's HTTP API: /upload/image, /prompt, /history/<id>, /view."""

    def __init__(self, renderer=None, polls=1):
        self.renderer = renderer
        self.polls = polls
        self.uploads, self.jobs, self.workflows = {}, {}, []
        self.fail_prompt = None
        self.fail_job = None
        self.no_image = False

    def app(self):
        app = web.Application(client_max_size=64 * 1024 ** 2)
        app.router.add_post("/upload/image", self.upload)
        app.router.add_post("/prompt", self.prompt)
        app.router.add_get("/history/{pid}", self.history)
        app.router.add_get("/view", self.view)
        return app

    async def upload(self, request):
        name = data = None
        async for part in await request.multipart():
            if part.name == "image":
                name, data = part.filename, await part.read()
            else:
                await part.read()
        self.uploads[name] = data
        return web.json_response({"name": name, "subfolder": "", "type": "input"})

    async def prompt(self, request):
        body = await request.json()
        wf = body["prompt"]
        self.workflows.append(wf)
        if self.fail_prompt:
            return web.json_response({
                "error": {"type": "prompt_outputs_failed_validation", "message": self.fail_prompt, "details": ""},
                "node_errors": {"2": {"errors": [{"message": "Value not in list", "details": "text"}]}}}, status=400)
        by_title = {n.get("_meta", {}).get("title"): (nid, n) for nid, n in wf.items()}
        base = Image.open(io.BytesIO(self.uploads[by_title["MAGIK_BASE_IMAGE"][1]["inputs"]["image"]])).convert("RGB")
        text = by_title["MAGIK_PROMPT"][1]["inputs"]["text"]
        pid = uuid.uuid4().hex
        self.jobs[pid] = {"png": png(self.renderer(text, base, wf)) if self.renderer else b"",
                          "calls": 0, "out": by_title["MAGIK_OUTPUT"][0]}
        return web.json_response({"prompt_id": pid, "number": 1, "node_errors": {}})

    async def history(self, request):
        pid = request.match_info["pid"]
        job = self.jobs.get(pid)
        if job is None:
            return web.json_response({})
        job["calls"] += 1
        if job["calls"] < self.polls:
            return web.json_response({})
        if self.fail_job:
            return web.json_response({pid: {"outputs": {}, "status": {
                "status_str": "error", "completed": False,
                "messages": [["execution_error", {"node_type": "KSampler", "exception_message": self.fail_job}]]}}})
        images = [] if self.no_image else [{"filename": f"{pid}.png", "subfolder": "", "type": "output"}]
        return web.json_response({pid: {"outputs": {job["out"]: {"images": images}},
                                        "status": {"status_str": "success", "completed": True}}})

    async def view(self, request):
        pid = request.query["filename"].rsplit(".", 1)[0]
        return web.Response(body=self.jobs[pid]["png"], content_type="image/png")


class ServerThread:
    def __init__(self, fake):
        self.fake = fake
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.runner = web.AppRunner(self.fake.app())
        self.loop.run_until_complete(self.runner.setup())
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        self.loop.run_until_complete(site.start())
        self.port = site._server.sockets[0].getsockname()[1]
        self.ready.set()
        self.loop.run_forever()
        self.loop.run_until_complete(self.runner.cleanup())
        self.loop.close()

    def __enter__(self):
        self.thread.start()
        assert self.ready.wait(10), "fake ComfyUI did not start"
        self.url = f"http://127.0.0.1:{self.port}"
        return self

    def __exit__(self, *exc):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)


def synthetic_renderer(spec, layers, sigma=0.0, seed=3):
    """Composite the requested piece onto the uploaded base, like a perfect (or noisy) edit model."""
    rng = np.random.default_rng(seed)
    descs = sorted(((p["render"]["desc"], p["id"]) for p in spec["pieces"]), key=lambda t: -len(t[0]))

    def render(prompt, base_rgb, workflow):
        pid = next(i for d, i in descs if d in prompt)
        comp = base_rgb.convert("RGBA")
        comp.alpha_composite(layers[pid])
        out = comp.convert("RGB")
        return noisy(out, sigma, rng) if sigma else out
    return render


# =========================================================== spec / prompts
@unittest.skipUnless(HAVE_DEPS, "numpy/aiohttp needed")
class SpecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, _, snap = placeholder_layers()
        cls.spec = S.default_spec(list(snap.pieces.values()), list(snap.outfits.values()))

    def test_default_spec_from_catalog(self):
        self.assertEqual((len(self.spec["pieces"]), len(self.spec["outfits"])), (14, 4))
        self.assertEqual(S.validate_spec(self.spec), [])
        p = next(x for x in self.spec["pieces"] if x["id"] == "footwear_boots")
        self.assertEqual((p["z"], p["hides"], p["render"]), (55, ["legwear"], {"desc": "boots"}))
        self.assertNotIn("layer", p)          # layer paths are managed by `import`, not authored

    def test_prompts(self):
        by_id = {p["id"]: p for p in self.spec["pieces"]}
        text, neg = S.build_prompt(self.spec, by_id["footwear_sneakers"])
        self.assertEqual(text, "Dress the mannequin in white sneakers. Only change the feet. Keep the pose, framing, "
                               "lighting, background and the mannequin itself exactly unchanged. Do not add any "
                               "other clothing or accessories.")
        self.assertIn("watermark", neg)
        text, _ = S.build_prompt(self.spec, by_id["top_blouse"])          # slot without a hint: no double space
        self.assertTrue(text.startswith("Dress the mannequin in a white blouse. Keep the pose"), text)
        custom = copy.deepcopy(by_id["top_blouse"])
        custom["render"] = {"prompt": "  Put on   a sailor top. ", "negative": "hats"}
        self.assertEqual(S.build_prompt(self.spec, custom), ("Put on a sailor top.", "hats"))

    def test_validation_errors(self):
        def broken(mutate):
            spec = copy.deepcopy(self.spec)
            mutate(spec)
            return "\n".join(S.validate_spec(spec))
        self.assertIn("duplicated", broken(lambda s: s["pieces"].append(copy.deepcopy(s["pieces"][0]))))
        self.assertIn("lo must be smaller", broken(lambda s: s["extract"].update(lo=50, hi=40)))
        self.assertIn("region 'top'", broken(lambda s: s["regions"].update(top=[10, 10, 5, 5])))
        self.assertIn("unique", broken(lambda s: s.update(variants=[])))
        self.assertIn("no base image", broken(lambda s: s.update(variants=[{"name": "a", "base": ""}])))
        self.assertIn("render needs", broken(lambda s: s["pieces"][0].update(render={})))
        self.assertIn("unknown piece", broken(lambda s: s["outfits"][0]["pieces"].update(top="nope")))
        self.assertIn("no region", broken(lambda s: s["pieces"][0].update(slot="cape")))

    def test_load_fills_defaults_and_rejects_bad_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "spec.json")
            minimal = {k: self.spec[k] for k in ("version", "canvas", "pieces")}
            with open(path, "w") as f:
                json.dump(minimal, f)
            loaded = S.load_spec(path)
            self.assertEqual(loaded["extract"]["lo"], 14)
            self.assertEqual(loaded["variants"][0]["name"], "gray")
            self.assertIn("top", loaded["regions"])
            minimal["canvas"] = [0, 5]
            with open(path, "w") as f:
                json.dump(minimal, f)
            with self.assertRaises(S.SpecError):
                S.load_spec(path)

    def test_region_mask_and_layer_names(self):
        spec = copy.deepcopy(self.spec)
        mask = S.region_mask(spec, "top", 24)
        self.assertEqual(mask.size, (512, 1024))
        self.assertEqual(int((np.asarray(mask) > 0).sum()), (422 - 90 + 48) * (500 - 180 + 48))
        self.assertEqual(int((np.asarray(S.region_mask(spec, "top", 0)) > 0).sum()), 332 * 320)
        wide = S.region_mask(spec, "accessory", 100)                     # clamped to the canvas
        self.assertEqual(int((np.asarray(wide) > 0).sum()), 512 * 1024)
        self.assertEqual(S.piece_layer_name({"id": "top_hoodie", "slot": "top"}), "hoodie")
        self.assertEqual(S.piece_layer_name({"id": "oddname", "slot": "top"}), "oddname")


# ===================================================== workflow patching
@unittest.skipUnless(HAVE_DEPS, "numpy/aiohttp needed")
class WorkflowTests(unittest.TestCase):
    def test_check_and_find(self):
        ok, problems, found = C.check_workflow(workflow_template())
        self.assertEqual((ok, problems), (True, []))
        self.assertEqual({k: v for k, v in found.items() if v},
                         {"base_image": ["1"], "prompt": ["2"], "negative": ["3"], "seed": ["4"], "mask": ["6"], "output": ["5"]})
        ok, problems, _ = C.check_workflow(workflow_template(mask=False))
        self.assertTrue(ok)                                              # mask node is optional

    def test_problems_are_reported(self):
        wf = workflow_template()
        del wf["5"]
        wf["7"] = copy.deepcopy(wf["2"])                                  # a second MAGIK_PROMPT
        ok, problems, _ = C.check_workflow(wf)
        self.assertFalse(ok)
        self.assertTrue(any("MAGIK_OUTPUT" in p for p in problems))
        self.assertTrue(any("2 nodes are titled 'MAGIK_PROMPT'" in p for p in problems))
        ui_format = {"nodes": [{"id": 1}], "links": []}                   # a UI-format export, not API format
        ok, problems, _ = C.check_workflow(ui_format)
        self.assertFalse(ok)
        self.assertIn("API-format", problems[0])

    def test_prepare_patches_a_copy(self):
        template = workflow_template()
        original = copy.deepcopy(template)
        wf, out = C.prepare_workflow(template, base_name="base_gray.png", prompt="P", negative="N",
                                     seed=99, mask_name="mask_top.png")
        self.assertEqual(out, "5")
        self.assertEqual(wf["1"]["inputs"]["image"], "base_gray.png")
        self.assertEqual((wf["2"]["inputs"]["text"], wf["3"]["inputs"]["text"]), ("P", "N"))
        self.assertEqual(wf["4"]["inputs"]["seed"], 99)
        self.assertEqual(wf["6"]["inputs"]["image"], "mask_top.png")
        self.assertEqual(wf["2"]["inputs"]["clip"], ["9", 0])             # untouched links survive
        self.assertEqual(template, original)                               # the template itself is never modified

    def test_optional_nodes_and_key_variants(self):
        wf = workflow_template(mask=False)
        del wf["3"]
        wf["4"]["inputs"] = {"noise_seed": 5}
        out, _ = C.prepare_workflow(wf, base_name="b.png", prompt="P", negative="N", seed=7, mask_name="m.png")
        self.assertEqual(out["4"]["inputs"]["noise_seed"], 7)             # noise_seed is understood
        wf["2"]["inputs"] = {"prompt": "x"}
        out, _ = C.prepare_workflow(wf, base_name="b.png", prompt="P")
        self.assertEqual(out["2"]["inputs"]["prompt"], "P")
        out, _ = C.prepare_workflow(wf, base_name="b.png", prompt="P", text_key="prompt")
        self.assertEqual(out["2"]["inputs"]["prompt"], "P")

    def test_unusable_text_and_seed_inputs(self):
        wf = workflow_template()
        wf["2"]["inputs"]["text"] = ["8", 0]                               # linked, so it cannot be set
        with self.assertRaisesRegex(C.WorkflowError, "no text input"):
            C.prepare_workflow(wf, base_name="b.png", prompt="P")
        with self.assertRaisesRegex(C.WorkflowError, "no input named 'nope'"):
            C.prepare_workflow(workflow_template(), base_name="b.png", prompt="P", text_key="nope")
        wf = workflow_template()
        wf["4"]["inputs"] = {"steps": 20}
        with self.assertRaisesRegex(C.WorkflowError, "seed"):
            C.prepare_workflow(wf, base_name="b.png", prompt="P", seed=1)
        with self.assertRaisesRegex(C.WorkflowError, "MAGIK_OUTPUT"):
            C.prepare_workflow({k: v for k, v in workflow_template().items() if k != "5"}, base_name="b", prompt="P")


# ======================================================= image processing
@unittest.skipUnless(HAVE_DEPS, "numpy/aiohttp needed")
class MorphologyTests(unittest.TestCase):
    def test_remove_small_and_count(self):
        m = np.zeros((40, 40), bool)
        m[5:10, 5:10] = True          # 25 px
        m[20, 20] = True              # speck
        m[30:33, 30:33] = True        # 9 px
        self.assertEqual(E.count_components(m, 4), 2)
        kept = E.remove_small(m, 10)
        self.assertEqual((int(kept.sum()), bool(kept[20, 20]), bool(kept[31, 31])), (25, False, False))
        self.assertEqual(E.remove_small(np.zeros((5, 5), bool), 3).sum(), 0)
        self.assertEqual(E.count_components(np.zeros((5, 5), bool), 1), 0)

    def test_fill_small_holes_only(self):
        m = np.zeros((60, 60), bool)
        m[10:40, 10:40] = True
        m[15:17, 15:17] = False       # 4 px pinhole: filled
        m[25:33, 25:33] = False       # 64 px hole: kept (a real gap)
        m[10:14, 20:24] = False       # notch open to the outside: never a "hole"
        out = E.fill_small_holes(m, 20)
        self.assertTrue(out[15, 15] and out[16, 16])
        self.assertFalse(out[28, 28])
        self.assertFalse(out[11, 21])
        self.assertTrue(np.array_equal(E.fill_small_holes(np.zeros((5, 5), bool), 9), np.zeros((5, 5), bool)))

    def test_cleanup_removes_noise_and_keeps_shape(self):
        rng = np.random.default_rng(5)
        m = np.zeros((120, 120), bool)
        m[30:90, 30:90] = True
        noisy_mask = m ^ (rng.random(m.shape) < 0.01)
        out = E.cleanup(noisy_mask, {"min_area": 40, "hole_area": 80})
        self.assertGreater(float((out & m).sum()) / float((out | m).sum()), 0.98)

    def test_dilate_erode(self):
        m = np.zeros((30, 30), bool)
        m[10:20, 10:20] = True
        self.assertEqual(int(E.dilate(m, 2).sum()), 14 * 14)
        self.assertEqual(int(E.erode(m, 2).sum()), 6 * 6)
        self.assertTrue(np.array_equal(E.dilate(m, 0), m))


@unittest.skipUnless(HAVE_DEPS, "numpy/aiohttp needed")
class ExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.layers, cls.m_alpha, snap = placeholder_layers()
        cls.spec = S.default_spec(list(snap.pieces.values()), list(snap.outfits.values()))
        cls.params = cls.spec["extract"]
        cls.pieces = {p["id"]: p for p in cls.spec["pieces"]}

    def region(self, pid):
        return S.region_mask(self.spec, self.pieces[pid]["slot"], self.params["region_margin"])

    def run_piece(self, pid, sigma=0.0, shift=0.0, seed=1):
        rng = np.random.default_rng(seed)
        base_rgba = mannequin_rgba(self.m_alpha, MANNEQUIN)
        comp = base_rgba.copy()
        comp.alpha_composite(self.layers[pid])
        base = noisy(over_bg(base_rgba), sigma, rng) if sigma else over_bg(base_rgba)
        render = noisy(over_bg(comp), sigma, rng, shift) if sigma else over_bg(comp)
        layer, stats = E.extract_layer([base], [render], self.region(pid), self.params)
        return layer, stats, E.qa_layer(layer, stats, self.params)

    def test_every_placeholder_piece_is_recovered(self):
        for sigma in (0.0, 3.0):
            for pid in self.pieces:
                layer, stats, qa = self.run_piece(pid, sigma, shift=1.5 if sigma else 0)
                self.assertGreater(iou(layer, self.layers[pid]), 0.97, f"{pid} sigma={sigma}")
                self.assertEqual([i for i in qa["issues"] if i["level"] == "error"], [], f"{pid} sigma={sigma}")

    def test_colours_are_recovered_not_just_the_shape(self):
        layer, _, _ = self.run_piece("top_tshirt", sigma=2.0, shift=1.0)
        truth = self.layers["top_tshirt"]
        core = np.asarray(truth.getchannel("A")) > 250
        err = np.abs(np.asarray(layer.convert("RGB"), float) - np.asarray(truth.convert("RGB"), float))[core]
        self.assertLess(float(err.mean()), 4.0)
        self.assertEqual(layer.mode, "RGBA")
        self.assertEqual(layer.size, (512, 1024))
        a = np.asarray(layer.getchannel("A"))
        rgb = np.asarray(layer.convert("RGB"))
        self.assertEqual(int(rgb[a == 0].sum()), 0)                       # no colour left in fully transparent pixels

    def test_drift_is_flagged_but_does_not_break_the_layer(self):
        layer, stats, qa = self.run_piece("top_blouse", sigma=6.0)
        self.assertGreater(iou(layer, self.layers["top_blouse"]), 0.97)
        self.assertIn("drift", [i["code"] for i in qa["issues"]])
        self.assertTrue(qa["ok"])                                          # a warning, not an error
        _, _, qa_clean = self.run_piece("top_blouse", sigma=0.0)
        self.assertNotIn("drift", [i["code"] for i in qa_clean["issues"]])

    def test_low_contrast_garment_needs_a_second_variant(self):
        tee = self.layers["top_tshirt"]
        gray = Image.new("RGBA", tee.size, MANNEQUIN + (255,))
        gray.putalpha(tee.getchannel("A"))                                 # exactly the mannequin's colour
        bases, renders = [], []
        for col in (MANNEQUIN, DARK_MANNEQUIN):
            b = mannequin_rgba(self.m_alpha, col)
            c = b.copy()
            c.alpha_composite(gray)
            bases.append(over_bg(b))
            renders.append(over_bg(c))
        region = self.region("top_tshirt")
        one, st1 = E.extract_layer(bases[:1], renders[:1], region, self.params)
        qa1 = E.qa_layer(one, st1, self.params)
        self.assertEqual(iou(one, tee), 0.0)
        self.assertIn("empty", [i["code"] for i in qa1["issues"]])
        self.assertFalse(qa1["ok"])
        two, st2 = E.extract_layer(bases, renders, region, self.params)
        self.assertGreater(iou(two, tee), 0.97)
        self.assertTrue(E.qa_layer(two, st2, self.params)["ok"])
        core = np.asarray(tee.getchannel("A")) > 250
        err = np.abs(np.asarray(two.convert("RGB"), float) - np.asarray(gray.convert("RGB"), float))[core]
        self.assertLess(float(err.mean()), 3.0)                            # the true gray, not the dark mannequin's

    def test_marginal_contrast_warns_and_invisible_contrast_is_empty(self):
        tee = self.layers["top_tshirt"]

        def run(delta):
            garment = Image.new("RGBA", tee.size, (MANNEQUIN[0] + delta, MANNEQUIN[1] + delta, MANNEQUIN[2] + delta, 255))
            garment.putalpha(tee.getchannel("A"))
            b = mannequin_rgba(self.m_alpha, MANNEQUIN)
            c = b.copy()
            c.alpha_composite(garment)
            layer, stats = E.extract_layer([over_bg(b)], [over_bg(c)], self.region("top_tshirt"), self.params)
            return layer, E.qa_layer(layer, stats, self.params)

        layer, qa = run(32)                       # visible, but not by much: extracted, with a warning
        self.assertGreater(iou(layer, tee), 0.95)
        self.assertEqual([(i["level"], i["code"]) for i in qa["issues"]], [("warn", "low_contrast")])
        self.assertTrue(qa["ok"])
        layer, qa = run(60)                       # plenty of contrast: no warning
        self.assertEqual(qa["issues"], [])
        layer, qa = run(14)                       # at the noise floor: nothing to extract
        self.assertEqual([i["code"] for i in qa["issues"]], ["empty"])
        self.assertIn("min_contrast", S.DEFAULT_EXTRACT)

    def test_garment_outside_its_region_is_an_error(self):
        # footwear drawn while the spec says "top": a coherent change far outside the top region
        b = mannequin_rgba(self.m_alpha, MANNEQUIN)
        c = b.copy()
        c.alpha_composite(self.layers["footwear_boots"])
        layer, stats = E.extract_layer([over_bg(b)], [over_bg(c)], self.region("top_tshirt"), self.params)
        qa = E.qa_layer(layer, stats, self.params)
        codes = [i["code"] for i in qa["issues"]]
        self.assertIn("outside_region", codes)
        self.assertIn("empty", codes)
        self.assertFalse(qa["ok"])

    def test_model_changing_everything_is_too_large(self):
        b = over_bg(mannequin_rgba(self.m_alpha, MANNEQUIN))
        arr = np.asarray(b, dtype=np.float32)
        r = Image.fromarray(np.clip(arr + 60, 0, 255).astype(np.uint8))   # the whole picture got brighter
        region = S.region_mask(self.spec, "accessory", 0)                  # whole-canvas region
        layer, stats = E.extract_layer([b], [r], region, self.params)
        self.assertIn("too_large", [i["code"] for i in E.qa_layer(layer, stats, self.params)["issues"]])

    def test_fragmented_and_tiny_results(self):
        b = mannequin_rgba(self.m_alpha, MANNEQUIN)
        c = b.copy()
        spots = Image.new("RGBA", b.size, (0, 0, 0, 0))
        for i in range(6):                                                  # six separate squares
            spots.paste((200, 30, 30, 255), (30 + i * 70, 300, 60 + i * 70, 330))
        c.alpha_composite(spots)
        region = S.region_mask(self.spec, "accessory", 0)
        layer, stats = E.extract_layer([over_bg(b)], [over_bg(c)], region, self.params)
        self.assertEqual(stats["components"], 6)
        self.assertIn("fragmented", [i["code"] for i in E.qa_layer(layer, stats, self.params)["issues"]])
        tiny = Image.new("RGBA", b.size, (0, 0, 0, 0))
        tiny.paste((200, 30, 30, 255), (100, 100, 108, 108))                # 64 px
        c2 = b.copy()
        c2.alpha_composite(tiny)
        layer, stats = E.extract_layer([over_bg(b)], [over_bg(c2)], region, self.params)
        self.assertIn("too_small", [i["code"] for i in E.qa_layer(layer, stats, self.params)["issues"]])

    def test_identical_render_is_empty_not_a_crash(self):
        b = over_bg(mannequin_rgba(self.m_alpha, MANNEQUIN))
        layer, stats = E.extract_layer([b], [b.copy()], self.region("top_tshirt"), self.params)
        qa = E.qa_layer(layer, stats, self.params)
        self.assertEqual(int(np.asarray(layer.getchannel("A")).sum()), 0)
        self.assertEqual([i["code"] for i in qa["issues"]], ["empty"])

    def test_external_mask_replaces_the_difference_alpha(self):
        # a gray garment that the colour difference cannot see, but a segmentation mask can
        tee = self.layers["top_tshirt"]
        gray = Image.new("RGBA", tee.size, MANNEQUIN + (255,))
        gray.putalpha(tee.getchannel("A"))
        b = mannequin_rgba(self.m_alpha, MANNEQUIN)
        c = b.copy()
        c.alpha_composite(gray)
        mask = tee.getchannel("A")
        layer, stats = E.extract_layer([over_bg(b)], [over_bg(c)], self.region("top_tshirt"), self.params, mask=mask)
        self.assertGreater(iou(layer, tee), 0.97)

    def test_rejects_mismatched_inputs_and_resizes_renders(self):
        b = over_bg(mannequin_rgba(self.m_alpha, MANNEQUIN))
        with self.assertRaises(ValueError):
            E.extract_layer([b, b], [b], self.region("top_tshirt"), self.params)
        with self.assertRaises(ValueError):
            E.extract_layer([], [], self.region("top_tshirt"), self.params)
        c = mannequin_rgba(self.m_alpha, MANNEQUIN)
        c.alpha_composite(self.layers["top_tshirt"])
        big = over_bg(c).resize((1024, 2048), Image.LANCZOS)                # an upscaled render still lines up
        layer, _ = E.extract_layer([b], [big], self.region("top_tshirt"), self.params)
        self.assertEqual(layer.size, (512, 1024))
        self.assertGreater(iou(layer, self.layers["top_tshirt"]), 0.95)

    def test_contact_sheet_dimensions(self):
        sheet = E.contact_sheet({pid: lay for pid, lay in self.layers.items()})
        self.assertEqual(sheet.size, (6 * 128, 3 * (256 + 14)))
        self.assertEqual(E.contact_sheet({}).size, (6 * 128, 256 + 14))


# =============================================================== client
@unittest.skipUnless(HAVE_DEPS, "numpy/aiohttp needed")
class ClientTests(unittest.TestCase):
    def setUp(self):
        self.image = Image.new("RGB", (64, 128), (10, 20, 30))
        self.fake = FakeComfy(renderer=lambda text, base, wf: Image.new("RGB", (64, 128), (200, 100, 50)), polls=3)

    def test_upload_prompt_wait_view(self):
        with ServerThread(self.fake) as srv:
            client = C.ComfyClient(srv.url, timeout=10, poll=0.01)
            name = client.upload_image(png(self.image), "base_gray.png")
            self.assertEqual(name, "base_gray.png")
            self.assertEqual(self.fake.uploads["base_gray.png"][:4], b"\x89PNG")
            wf, out = C.prepare_workflow(workflow_template(), base_name=name, prompt="hello", seed=5)
            data = client.run(wf, out)
            self.assertEqual(Image.open(io.BytesIO(data)).convert("RGB").getpixel((3, 3)), (200, 100, 50))
            sent = self.fake.workflows[0]
            self.assertEqual((sent["2"]["inputs"]["text"], sent["4"]["inputs"]["seed"], sent["1"]["inputs"]["image"]),
                             ("hello", 5, "base_gray.png"))
            self.assertEqual(next(iter(self.fake.jobs.values()))["calls"], 3)   # it really polled until done

    def test_rejected_prompt_shows_the_node_errors(self):
        self.fake.fail_prompt = "Prompt outputs failed validation"
        with ServerThread(self.fake) as srv:
            client = C.ComfyClient(srv.url, timeout=5, poll=0.01)
            client.upload_image(png(self.image), "b.png")
            wf, out = C.prepare_workflow(workflow_template(), base_name="b.png", prompt="x")
            with self.assertRaises(C.ComfyError) as cm:
                client.run(wf, out)
            msg = str(cm.exception)
            self.assertIn("HTTP 400", msg)
            self.assertIn("Prompt outputs failed validation", msg)
            self.assertIn("node 2: Value not in list", msg)

    def test_failed_job_reports_the_exception(self):
        self.fake.fail_job = "CUDA out of memory"
        with ServerThread(self.fake) as srv:
            client = C.ComfyClient(srv.url, timeout=5, poll=0.01)
            client.upload_image(png(self.image), "b.png")
            wf, out = C.prepare_workflow(workflow_template(), base_name="b.png", prompt="x")
            with self.assertRaisesRegex(C.ComfyError, "job failed: KSampler: CUDA out of memory"):
                client.run(wf, out)

    def test_timeout_and_missing_output(self):
        self.fake.polls = 10 ** 9
        with ServerThread(self.fake) as srv:
            client = C.ComfyClient(srv.url, timeout=0.3, poll=0.05)
            client.upload_image(png(self.image), "b.png")
            wf, out = C.prepare_workflow(workflow_template(), base_name="b.png", prompt="x")
            with self.assertRaisesRegex(C.ComfyError, "timed out"):
                client.run(wf, out)
        fake = FakeComfy(renderer=self.fake.renderer, polls=1)
        fake.no_image = True
        with ServerThread(fake) as srv:
            client = C.ComfyClient(srv.url, timeout=5, poll=0.01)
            client.upload_image(png(self.image), "b.png")
            wf, out = C.prepare_workflow(workflow_template(), base_name="b.png", prompt="x")
            with self.assertRaisesRegex(C.ComfyError, "produced no image"):
                client.run(wf, out)

    def test_local_server_ignores_proxy_settings_but_remote_ones_honor_them(self):
        dead_proxy = {"http_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9"}
        with unittest.mock.patch.dict(os.environ, dead_proxy):
            for no_proxy in ("no_proxy", "NO_PROXY"):
                os.environ.pop(no_proxy, None)
            with ServerThread(self.fake) as srv:
                client = C.ComfyClient(srv.url, timeout=5, poll=0.01, request_timeout=3)   # 127.0.0.1 behind a dead proxy
                self.assertEqual(client.upload_image(png(self.image), "p.png"), "p.png")
            remote = C.ComfyClient("http://comfy.example.invalid:8188")
            proxies = [h.proxies for h in remote._opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
            self.assertTrue(any(p.get("http") == "http://127.0.0.1:9" for p in proxies), proxies)
            local = [h.proxies for h in C.ComfyClient("http://localhost:8188")._opener.handlers
                     if isinstance(h, urllib.request.ProxyHandler)]
            self.assertFalse(any(local), local)       # no proxy handler is active for a loopback server

    def test_unreachable_server(self):
        client = C.ComfyClient("http://127.0.0.1:9", timeout=1, poll=0.01, request_timeout=2)
        with self.assertRaisesRegex(C.ComfyError, "cannot reach ComfyUI"):
            client.upload_image(b"x", "a.png")


# ================================================================ pipeline
@unittest.skipUnless(HAVE_DEPS, "numpy/aiohttp needed")
class PipelineTests(unittest.TestCase):
    """The whole loop on a temporary copy of the package, against the fake ComfyUI."""

    @classmethod
    def setUpClass(cls):
        cls.layers, cls.m_alpha, cls.snap = placeholder_layers()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="magik_art_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "pkg")
        shutil.copytree(PKG, self.root, ignore=shutil.ignore_patterns("__pycache__", "tests", "art", "node_modules"))
        self.work = os.path.join(self.root, "art", "work")
        os.makedirs(self.work)
        self.lines = []
        self.out = self.lines.append

    def cli(self, *argv):
        self.lines.clear()
        return art.main(["--root", self.root, *argv], out=self.out)

    def text(self):
        return "\n".join(self.lines)

    def write_bases(self, variants=(("base_gray.png", MANNEQUIN),)):
        for name, col in variants:
            over_bg(mannequin_rgba(self.m_alpha, col)).save(os.path.join(self.work, name))

    def write_workflow(self, mask=True):
        path = os.path.join(self.tmp, "wf.json")
        with open(path, "w") as f:
            json.dump(workflow_template(mask), f)
        return path

    def spec(self):
        return S.load_spec(os.path.join(self.root, "art", "spec.json"))

    def piece_iou(self, pid):
        entry = next(p for p in jload(os.path.join(self.root, "data", "pieces.json")) if p["id"] == pid)
        with Image.open(os.path.join(self.root, "assets", entry["layer"])) as im:
            return iou(im.convert("RGBA"), self.layers[pid])

    def test_full_loop_render_extract_import(self):
        self.assertEqual(self.cli("init-spec"), 0)
        self.assertEqual(self.cli("init-spec"), 1)                         # refuses to overwrite
        self.assertIn("already exists", self.text())
        self.assertEqual(self.cli("init-spec", "--force"), 0)
        spec = self.spec()
        self.write_bases()
        self.assertEqual(self.cli("check"), 0)
        self.assertIn("base found", self.text())
        self.assertEqual(self.cli("masks"), 0)
        self.assertTrue(os.path.isfile(os.path.join(self.work, "masks", "top.png")))
        wf = self.write_workflow()
        self.assertEqual(self.cli("check-workflow", wf), 0)
        self.assertIn("workflow OK", self.text())

        fake = FakeComfy(synthetic_renderer(spec, self.layers, sigma=2.0), polls=2)
        with ServerThread(fake) as srv:
            args = ["render", "--workflow", wf, "--server", srv.url, "--poll", "0.01"]
            self.assertEqual(self.cli(*args, "--dry-run"), 0)
            self.assertIn("14 job(s) would run", self.text())
            self.assertEqual(len(fake.jobs), 0)                            # --dry-run never contacts the server

            self.assertEqual(self.cli(*args), 0, self.text())
            self.assertEqual(len(fake.jobs), 14)
            self.assertEqual(len(os.listdir(os.path.join(self.work, "renders", "gray"))), 14)
            self.assertEqual(sorted(k for k in fake.uploads if k.startswith("magik_outfit_mask_")),
                             sorted(f"magik_outfit_mask_{s}.png" for s in {p["slot"] for p in spec["pieces"]}))
            first = fake.workflows[0]
            self.assertEqual(first["4"]["inputs"]["seed"], spec["seed"])    # one fixed seed for every piece
            self.assertTrue(first["6"]["inputs"]["image"].startswith("magik_outfit_mask_"))
            self.assertEqual(first["1"]["inputs"]["image"], "magik_outfit_base_gray.png")

            self.assertEqual(self.cli(*args), 0)                           # re-run: everything exists
            self.assertIn("nothing to render", self.text())
            self.assertEqual(len(fake.jobs), 14)
            self.assertEqual(self.cli(*args, "--only", "top_blouse", "--force"), 0)
            self.assertEqual(len(fake.jobs), 15)

        self.assertEqual(self.cli("extract"), 0, self.text())
        self.assertEqual(len(os.listdir(os.path.join(self.work, "layers"))), 14)
        self.assertTrue(os.path.isfile(os.path.join(self.work, "contact_sheet.png")))
        report = jload(os.path.join(self.work, "qa.json"))
        self.assertTrue(all(r["ok"] for r in report.values()), self.text())

        # wipe the catalog layers so that `import` visibly restores them from the extracted ones
        for p in self.snap.pieces.values():
            Image.new("RGBA", (512, 1024), (255, 0, 255, 255)).save(os.path.join(self.root, "assets", p["layer"]))
        self.assertEqual(self.cli("import", "--prune"), 0, self.text())
        self.assertIn("validate_catalog.py: 14 pieces, 4 outfits: 0 errors, 0 warnings", self.text())
        for pid in self.layers:
            self.assertGreater(self.piece_iou(pid), 0.97, pid)
        pieces = {p["id"]: p for p in jload(os.path.join(self.root, "data", "pieces.json"))}
        self.assertTrue(all(p["bbox"] for p in pieces.values()))           # bake filled the bboxes
        self.assertEqual(pieces["footwear_boots"]["z"], 55)
        self.assertEqual(pieces["footwear_boots"]["hides"], ["legwear"])

    def test_import_is_blocked_by_qa_errors_unless_overridden(self):
        self.cli("init-spec")
        spec = self.spec()
        self.write_bases()
        wf = self.write_workflow(mask=False)
        fake = FakeComfy(synthetic_renderer(spec, self.layers))
        with ServerThread(fake) as srv:
            self.assertEqual(self.cli("render", "--workflow", wf, "--server", srv.url, "--poll", "0.01",
                                      "--only", "top_tshirt", "bottom_jeans"), 0, self.text())
        self.assertEqual(self.cli("extract", "--only", "top_tshirt", "bottom_jeans"), 0, self.text())
        self.assertEqual(self.cli("import"), 0, self.text())               # pieces without a layer are just skipped
        self.assertIn("skipped_no_layer", self.text())

        qa_path = os.path.join(self.work, "qa.json")
        report = jload(qa_path)
        report["bottom_jeans"]["ok"] = False
        report["bottom_jeans"]["issues"] = [{"level": "error", "code": "empty", "msg": "x"}]
        jdump(report, qa_path)
        before = rtext(os.path.join(self.root, "data", "pieces.json"))
        self.assertEqual(self.cli("import", "--only", "bottom_jeans"), 1)
        self.assertIn("blocked by QA errors: bottom_jeans", self.text())
        self.assertEqual(rtext(os.path.join(self.root, "data", "pieces.json")), before)   # nothing was written
        self.assertEqual(self.cli("import", "--only", "bottom_jeans", "--allow-qa-errors"), 0, self.text())
        os.remove(qa_path)
        self.assertEqual(self.cli("import"), 1)
        self.assertIn("run `extract` first", self.text())

    def test_two_variants_rescue_a_gray_garment_end_to_end(self):
        self.cli("init-spec")
        spec_path = os.path.join(self.root, "art", "spec.json")
        spec = jload(spec_path)
        spec["pieces"].append({"id": "top_graytee", "slot": "top", "label": "Gray tee", "tags": "gray shirt",
                               "nl": "a gray tee", "z": None, "hides": [], "conflicts": [], "style": ["casual"],
                               "colorable": False, "render": {"desc": "a plain gray tee"}})
        jdump(spec, spec_path)
        tee = self.layers["top_tshirt"]
        gray = Image.new("RGBA", tee.size, MANNEQUIN + (255,))
        gray.putalpha(tee.getchannel("A"))
        layers = dict(self.layers, top_graytee=gray)
        wf = self.write_workflow()

        # one mannequin variant: the gray tee is invisible, and the QA says so
        self.write_bases()
        with ServerThread(FakeComfy(synthetic_renderer(S.load_spec(spec_path), layers))) as srv:
            self.assertEqual(self.cli("render", "--workflow", wf, "--server", srv.url, "--poll", "0.01",
                                      "--only", "top_graytee"), 0, self.text())
        self.assertEqual(self.cli("extract", "--only", "top_graytee"), 1)
        self.assertIn("error:empty", self.text())
        self.assertEqual(self.cli("import", "--only", "top_graytee"), 1)
        self.assertIn("blocked by QA errors", self.text())

        # add a dark mannequin variant: now it works, and the other variant's render is reused
        spec = jload(spec_path)
        spec["variants"].append({"name": "dark", "base": "base_dark.png"})
        jdump(spec, spec_path)
        self.write_bases((("base_gray.png", MANNEQUIN), ("base_dark.png", DARK_MANNEQUIN)))
        with ServerThread(FakeComfy(synthetic_renderer(S.load_spec(spec_path), layers))) as srv:
            self.assertEqual(self.cli("render", "--workflow", wf, "--server", srv.url, "--poll", "0.01",
                                      "--only", "top_graytee"), 0, self.text())
            self.assertEqual(len(srv.fake.jobs), 1)                        # only the missing dark render ran
        self.assertEqual(self.cli("extract", "--only", "top_graytee"), 0, self.text())
        self.assertEqual(self.cli("import", "--only", "top_graytee"), 0, self.text())
        self.assertIn("added: top_graytee", self.text())
        entry = next(p for p in jload(os.path.join(self.root, "data", "pieces.json")) if p["id"] == "top_graytee")
        self.assertEqual((entry["layer"], entry["tags"]), ("pieces/top/graytee.webp", "gray shirt"))
        with Image.open(os.path.join(self.root, "assets", entry["layer"])) as im:
            self.assertGreater(iou(im.convert("RGBA"), gray), 0.97)

    def test_render_failure_modes(self):
        self.cli("init-spec")
        spec = self.spec()
        wf = self.write_workflow()
        # missing base image
        self.assertEqual(self.cli("render", "--workflow", wf, "--only", "top_blouse"), 1)
        self.assertIn("base image for variant 'gray' not found", self.text())
        # base with the wrong aspect ratio
        Image.new("RGB", (500, 500)).save(os.path.join(self.work, "base_gray.png"))
        self.assertEqual(self.cli("render", "--workflow", wf, "--only", "top_blouse"), 1)
        self.assertIn("does not match the canvas aspect", self.text())
        self.write_bases()
        # unreachable server: caught at the first upload, one clear message
        self.assertEqual(self.cli("render", "--workflow", wf, "--server", "http://127.0.0.1:9"), 1)
        self.assertTrue(self.text().startswith("error: cannot reach ComfyUI at http://127.0.0.1:9"), self.text())
        self.assertEqual(self.text().count("FAILED"), 0)
        # the server vanishing in the middle of a run: report it once and stop instead of failing every piece
        good = png(Image.new("RGB", (512, 1024), (9, 9, 9)))
        lost = C.ComfyError("cannot reach ComfyUI at http://x: connection reset")
        with unittest.mock.patch.object(C.ComfyClient, "upload_image", return_value="b.png"), \
                unittest.mock.patch.object(C.ComfyClient, "run", side_effect=[good, lost, good]):
            self.assertEqual(self.cli("render", "--workflow", wf, "--only", "top_blouse", "top_tshirt", "bottom_jeans"), 1)
        # jobs run in spec order: top_tshirt, top_blouse, ..., bottom_jeans
        self.assertIn("top_tshirt (gray) ok", self.text())
        self.assertIn("top_blouse (gray) FAILED: cannot reach ComfyUI", self.text())
        self.assertIn("stopping: the server is unreachable", self.text())
        self.assertNotIn("bottom_jeans", self.text())                           # the third job never started
        self.assertEqual(sorted(os.listdir(os.path.join(self.work, "renders", "gray"))), ["top_tshirt.png"])
        shutil.rmtree(os.path.join(self.work, "renders"))
        # unusable workflow
        bad = os.path.join(self.tmp, "bad.json")
        jdump({"1": {"class_type": "KSampler", "inputs": {}}}, bad)
        self.assertEqual(self.cli("render", "--workflow", bad), 1)
        self.assertIn("workflow not usable", self.text())
        self.assertEqual(self.cli("check-workflow", bad), 1)
        self.assertIn("workflow NOT usable", self.text())
        # a failing job does not stop the others, and the exit code says so
        fake = FakeComfy(synthetic_renderer(spec, self.layers))
        fake.fail_job = "CUDA out of memory"
        with ServerThread(fake) as srv:
            self.assertEqual(self.cli("render", "--workflow", wf, "--server", srv.url, "--poll", "0.01",
                                      "--only", "top_blouse", "top_tshirt"), 1)
        self.assertEqual(self.text().count("FAILED"), 2)
        self.assertIn("2 failed: top_blouse, top_tshirt", self.text())
        self.assertEqual(len(os.listdir(os.path.join(self.work, "renders", "gray"))) if os.path.isdir(
            os.path.join(self.work, "renders", "gray")) else 0, 0)
        # unknown piece id
        with self.assertRaises(SystemExit):
            self.cli("render", "--workflow", wf, "--only", "nope")

    def test_extract_skips_pieces_without_renders_and_needs_bases(self):
        self.cli("init-spec")
        self.assertEqual(self.cli("extract"), 1)
        self.assertIn("base image for variant 'gray' not found", self.text())
        self.write_bases()
        self.assertEqual(self.cli("extract"), 0)
        self.assertIn("no layers extracted", self.text())
        self.assertIn("top_blouse: skipped (render missing for variant gray)", self.text())

    def test_sync_prune_and_outfit_protection(self):
        self.cli("init-spec")
        spec = self.spec()
        layers_dir = os.path.join(self.work, "layers")
        os.makedirs(layers_dir)
        for pid, lay in self.layers.items():
            lay.save(os.path.join(layers_dir, f"{pid}.png"))
        # dry run touches nothing
        before = rtext(os.path.join(self.root, "data", "pieces.json"))
        summary = Y.sync_catalog(spec, layers_dir, self.root, dry_run=True)
        self.assertEqual((len(summary["written"]), summary["dry_run"]), (14, True))
        self.assertEqual(rtext(os.path.join(self.root, "data", "pieces.json")), before)
        # removing a piece that outfits still use must be refused, not silently break the catalog
        smaller = copy.deepcopy(spec)
        smaller["pieces"] = [p for p in smaller["pieces"] if p["id"] != "legwear_kneesocks"]
        with self.assertRaisesRegex(Y.SyncError, "legwear_kneesocks"):
            Y.sync_catalog(smaller, layers_dir, self.root, prune=True)
        # removing the piece AND the outfits that use it is fine, and the files are cleaned up
        smaller["outfits"] = [o for o in smaller["outfits"] if o["id"] != "school_01"]
        summary = Y.sync_catalog(smaller, layers_dir, self.root, prune=True)
        self.assertEqual(summary["pruned"], ["legwear_kneesocks"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "assets", "pieces", "legwear", "kneesocks.webp")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "assets", "outfits", "school_01.webp")))
        self.assertIn("0 errors", summary["validate_catalog.py"])
        # wrong-sized layer is rejected before anything is written
        Image.new("RGBA", (10, 10)).save(os.path.join(layers_dir, "top_tshirt.png"))
        with self.assertRaisesRegex(Y.SyncError, "layer is 10x10"):
            Y.sync_catalog(smaller, layers_dir, self.root)
        with self.assertRaisesRegex(Y.SyncError, "not in the spec"):
            Y.sync_catalog(smaller, layers_dir, self.root, only=["ghost"])

    # ---- safety: nothing the user made may be lost by a failed or careless run
    def tree_hash(self):
        digest = hashlib.sha256()
        for sub in ("data", "assets"):
            for folder, _, files in sorted(os.walk(os.path.join(self.root, sub))):
                for f in sorted(files):
                    path = os.path.join(folder, f)
                    digest.update(os.path.relpath(path, self.root).encode())
                    with open(path, "rb") as fh:
                        digest.update(fh.read())
        return digest.hexdigest()

    def red_square(self, path):
        img = Image.new("RGBA", (512, 1024), (0, 0, 0, 0))
        img.paste((200, 30, 30, 255), (200, 200, 300, 400))
        img.save(path)

    def test_failed_import_rolls_back_everything(self):
        self.cli("init-spec")
        spec_path = os.path.join(self.root, "art", "spec.json")
        spec = jload(spec_path)
        spec["pieces"] = [p for p in spec["pieces"] if p["id"] != "bottom_shorts"]   # --prune would delete it
        jdump(spec, spec_path)
        layers_dir = os.path.join(self.work, "layers")
        os.makedirs(layers_dir)
        self.red_square(os.path.join(layers_dir, "top_tshirt.png"))                     # a good layer ...
        Image.new("RGBA", (512, 1024), (0, 0, 0, 0)).save(os.path.join(layers_dir, "top_blouse.png"))  # ... and an invalid one
        before = self.tree_hash()
        self.assertEqual(self.cli("import", "--prune", "--allow-qa-errors"), 1)
        self.assertIn("import failed", self.text())
        self.assertIn("fully transparent", self.text())
        self.assertEqual(self.tree_hash(), before)                                       # byte-for-byte what it was
        stray = [f for d, _, fs in os.walk(self.root) for f in fs if f.endswith(".tmp")]
        self.assertEqual(stray, [])
        self.assertTrue(os.path.isfile(os.path.join(self.root, "assets", "pieces", "bottom", "shorts.webp")))
        self.assertIn("bottom_shorts", [q["id"] for q in jload(os.path.join(self.root, "data", "pieces.json"))])

        os.remove(os.path.join(layers_dir, "top_blouse.png"))                            # fix the problem: the same import works
        self.assertEqual(self.cli("import", "--prune", "--allow-qa-errors"), 0, self.text())
        self.assertNotIn("bottom_shorts", [q["id"] for q in jload(os.path.join(self.root, "data", "pieces.json"))])
        self.assertFalse(os.path.exists(os.path.join(self.root, "assets", "pieces", "bottom", "shorts.webp")))
        # real art is now in the catalog, so the placeholder generator must refuse to overwrite it
        done = subprocess.run([sys.executable, os.path.join("tools", "make_placeholders.py")], cwd=self.root,
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("refusing to run", done.stdout)

    def test_prune_is_reported_as_skipped_with_only(self):
        self.cli("init-spec")
        spec_path = os.path.join(self.root, "art", "spec.json")
        spec = jload(spec_path)
        spec["pieces"] = [p for p in spec["pieces"] if p["id"] != "bottom_shorts"]
        jdump(spec, spec_path)
        os.makedirs(os.path.join(self.work, "layers"))
        self.red_square(os.path.join(self.work, "layers", "top_tshirt.png"))
        self.assertEqual(self.cli("import", "--prune", "--only", "top_tshirt", "--allow-qa-errors"), 0, self.text())
        self.assertIn("--prune is ignored when --only is given", self.text())
        self.assertIn("bottom_shorts", [q["id"] for q in jload(os.path.join(self.root, "data", "pieces.json"))])

    def test_placeholder_generator_refuses_to_overwrite_real_work(self):
        def generate(*flags):
            return subprocess.run([sys.executable, os.path.join("tools", "make_placeholders.py"), *flags],
                                  cwd=self.root, capture_output=True, text=True)
        layer = os.path.join(self.root, "assets", "pieces", "top", "blouse.webp")
        pristine = self.tree_hash()
        self.assertEqual(generate().returncode, 0)                    # the untouched placeholder set may be regenerated
        self.assertEqual(self.tree_hash(), pristine)                  # and regeneration is deterministic

        self.red_square(layer)                                         # the user drops in real art
        mine = rtext(os.path.join(self.root, "data", "pieces.json"))
        edited = self.tree_hash()
        refused = generate()
        self.assertEqual(refused.returncode, 1)
        self.assertIn("refusing to run", refused.stdout)
        self.assertEqual(self.tree_hash(), edited)                    # nothing was touched

        with open(os.path.join(self.root, "data", "pieces.json"), "w", encoding="utf-8") as f:
            f.write(mine.replace("Blouse", "My blouse"))              # a catalog edit alone is protected too
        self.assertEqual(generate().returncode, 1)

        self.assertEqual(generate("--force").returncode, 0)           # --force is the explicit way out
        self.assertNotIn("My blouse", rtext(os.path.join(self.root, "data", "pieces.json")))
        self.assertEqual(generate().returncode, 0)                    # ... and the marker was refreshed

    def test_new_outfit_is_deferred_until_its_pieces_exist(self):
        self.cli("init-spec")
        spec_path = os.path.join(self.root, "art", "spec.json")
        spec = jload(spec_path)
        spec["pieces"].append({"id": "top_newtee", "slot": "top", "label": "New tee", "tags": "t", "nl": "a new tee",
                               "hides": [], "conflicts": [], "style": [], "colorable": False,
                               "render": {"desc": "a new tee"}})
        spec["outfits"].append({"id": "new_01", "label": "New", "style": [], "prefix": "",
                                "pieces": {"top": "top_newtee", "bottom": "bottom_jeans"}})
        jdump(spec, spec_path)
        layers_dir = os.path.join(self.work, "layers")
        os.makedirs(layers_dir)
        self.layers["top_tshirt"].save(os.path.join(layers_dir, "top_tshirt.png"))
        summary = Y.sync_catalog(S.load_spec(spec_path), layers_dir, self.root, run_tools=False)
        self.assertEqual(summary["outfits_deferred"], ["new_01"])
        outfits = {o["id"] for o in jload(os.path.join(self.root, "data", "outfits.json"))}
        self.assertNotIn("new_01", outfits)
        self.layers["top_tshirt"].save(os.path.join(layers_dir, "top_newtee.png"))
        summary = Y.sync_catalog(S.load_spec(spec_path), layers_dir, self.root)
        self.assertEqual((summary["added"], "new_01" in summary["outfits_written"]), (["top_newtee"], True))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "assets", "outfits", "new_01.webp")))


if __name__ == "__main__":
    unittest.main()
