"""Tests for Magik Outfit. Run from the package folder:

    python -m unittest discover -s tests -v

Core + route tests need only Pillow and aiohttp. Node tests need comfy_api
(i.e. a ComfyUI checkout on sys.path) and are skipped otherwise.
"""
import importlib
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

from PIL import Image, ImageChops

PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PKG_DIR not in sys.path:
    sys.path.insert(0, PKG_DIR)

import outfit_core as oc  # noqa: E402


def fresh_catalog(with_user=True):
    tmp = tempfile.mkdtemp(prefix="magik_outfit_test_")
    user = os.path.join(tmp, "user") if with_user else None
    return oc.Catalog(PKG_DIR, user), tmp


def random_junk(rng, depth=0):
    """A random JSON-able value of any type: the kind of thing a hand-edited file or hostile client sends."""
    pool = ["school_01", "top", "bottom", "onepiece", "footwear", "top_hoodie", "onepiece_sundress", "", "__proto__",
            "x" * 300, "\u00e9\U0001F457", "\x00\x01", "a\nb", "../../etc/passwd", "<img src=x onerror=1>"]
    roll = rng.random()
    if roll < 0.12:
        return None
    if roll < 0.22:
        return rng.choice([True, False])
    if roll < 0.34:
        return rng.choice([0, 1, -5, 2 ** 70, 3.5, float("1e300")])
    if roll < 0.65 or depth > 2:
        return rng.choice(pool)
    if roll < 0.82:
        return [random_junk(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    return {rng.choice(pool): random_junk(rng, depth + 1) for _ in range(rng.randint(0, 4))}


class FuzzTests(unittest.TestCase):
    def test_random_state_documents_never_raise(self):
        import random
        rng = random.Random(99)
        cat, tmp = fresh_catalog(False)
        self.addCleanup(shutil.rmtree, tmp, True)
        snap = cat.get()
        for i in range(1500):
            doc = random_junk(rng)
            text = json.dumps(doc)
            if rng.random() < 0.2:
                text = text[: rng.randint(0, len(text))]
            if rng.random() < 0.05:
                text = rng.choice([None, 5, [], b"bytes", {"a": 1}])      # not even a string
            outfit_id, overrides = oc.parse_state(text)
            outfit = snap.outfits.get(outfit_id) if outfit_id else None
            slot_map = oc.resolve(outfit, overrides, snap.pieces)
            for slot, pid in slot_map.items():                              # whatever comes out is a consistent selection
                self.assertEqual(snap.pieces[pid]["slot"], slot)
                for other, other_pid in slot_map.items():
                    self.assertNotIn(other, snap.pieces[pid]["conflicts"], (text, slot_map))
            fmt = rng.choice(list(oc.FORMATS) + ["x", None, 5])
            sep = rng.choice([", ", "\\n", "", None, 5])
            pre = rng.choice(["", "w", None, 5, "\ufeff"])
            out = oc.build_string(slot_map, outfit, outfit is None or slot_map != outfit["pieces"],
                                  snap.mannequin, snap.pieces, fmt, sep, pre)
            self.assertIsInstance(out, str)

    def test_garbage_widget_values_cannot_break_assembly(self):
        cat, tmp = fresh_catalog(False)
        self.addCleanup(shutil.rmtree, tmp, True)
        snap = cat.get()
        outfit = snap.outfits["school_01"]
        slot_map = oc.resolve(outfit, {}, snap.pieces)
        for fmt in (None, 5, "", "TAGS", ["tags"]):
            self.assertTrue(oc.build_string(slot_map, outfit, False, snap.mannequin, snap.pieces, fmt).startswith("school uniform"))
        self.assertTrue(oc.build_string(slot_map, outfit, False, snap.mannequin, snap.pieces, "tags", None, None)
                        .startswith("school uniform, white shirt"))        # a non-string separator falls back to ", "


class ParseStateTests(unittest.TestCase):
    def test_tolerant(self):
        for bad in ("", "   ", "garbage", "[]", "42", None, 5):
            self.assertEqual(oc.parse_state(bad), (None, {}), bad)

    def test_valid_and_filtered(self):
        text = json.dumps({"v": 1, "outfit": "school_01",
                           "overrides": {"top": "top_hoodie", "legwear": None, "bottom": 7,
                                         "footwear": ["x"], "gloves": {"a": 1}}})
        self.assertEqual(oc.parse_state(text),
                         ("school_01", {"top": "top_hoodie", "legwear": None}))
        self.assertEqual(oc.parse_state('{"outfit": 5, "overrides": []}'), (None, {}))


class ResolveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat, cls.tmp = fresh_catalog(False)
        snap = cls.cat.get()
        cls.p, cls.o, cls.m = snap.pieces, snap.outfits, snap.mannequin

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_swap_remove_custom(self):
        s = self.o["school_01"]
        r = oc.resolve(s, {"top": "top_hoodie"}, self.p)
        self.assertEqual((r["top"], r["bottom"]), ("top_hoodie", "bottom_pleated_skirt"))
        self.assertNotIn("legwear", oc.resolve(s, {"legwear": None}, self.p))
        self.assertEqual(oc.resolve(None, {"top": "top_tshirt"}, self.p), {"top": "top_tshirt"})

    def test_ignores_bad_overrides(self):
        r = oc.resolve(self.o["school_01"], {"top": "bogus", "bottom": "top_hoodie"}, self.p)
        self.assertEqual((r["top"], r["bottom"]), ("top_blouse", "bottom_pleated_skirt"))

    def test_conflicts_are_symmetric(self):
        r = oc.resolve(self.o["school_01"], {"onepiece": "onepiece_sundress"}, self.p)
        self.assertTrue("top" not in r and "bottom" not in r and r["legwear"] == "legwear_kneesocks")
        for override in ({"top": "top_tshirt"}, {"bottom": "bottom_shorts"}):
            r = oc.resolve(self.o["summer_01"], override, self.p)
            self.assertNotIn("onepiece", r)
            self.assertEqual(r["headwear"], "headwear_beret")
        r = oc.resolve(self.o["summer_01"], {"top": "top_tshirt", "onepiece": None}, self.p)
        self.assertEqual(r, {"headwear": "headwear_beret", "footwear": "footwear_sneakers",
                             "top": "top_tshirt"})

    def test_modified_detection(self):
        s = self.o["school_01"]
        self.assertEqual(oc.resolve(s, {}, self.p), s["pieces"])
        self.assertEqual(oc.resolve(s, {"top": "top_blouse"}, self.p), s["pieces"])  # same piece = unmodified
        self.assertNotEqual(oc.resolve(s, {"top": "top_hoodie"}, self.p), s["pieces"])


class BuildStringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat, cls.tmp = fresh_catalog(False)
        snap = cls.cat.get()
        cls.p, cls.o, cls.m = snap.pieces, snap.outfits, snap.mannequin

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def build(self, outfit_id, overrides=None, **kw):
        outfit = self.o.get(outfit_id)
        slot_map = oc.resolve(outfit, overrides or {}, self.p)
        modified = outfit is None or slot_map != outfit["pieces"]
        return oc.build_string(slot_map, outfit, modified, self.m, self.p, **kw)

    def test_tags_natural_both(self):
        tags = ("school uniform, white shirt, collared shirt, pleated skirt, navy skirt, "
                "black kneehighs, loafers, neck ribbon")
        nat = ("school uniform, a white blouse, a navy pleated skirt, black knee socks, "
               "brown loafers, a pink neck ribbon")
        self.assertEqual(self.build("school_01", fmt="tags"), tags)
        self.assertEqual(self.build("school_01", fmt="natural"), nat)
        self.assertEqual(self.build("school_01", fmt="both"), nat + "\n" + tags)
        self.assertEqual(self.build("school_01", fmt="nonsense"), tags)  # unknown format -> tags

    def test_outfit_prefix_dropped_when_modified(self):
        text = self.build("school_01", {"top": "top_hoodie"}, fmt="tags")
        self.assertEqual(text, "hoodie, pleated skirt, navy skirt, black kneehighs, loafers, neck ribbon")

    def test_widget_prefix_and_separator(self):
        self.assertEqual(self.build("casual_01", fmt="natural", prefix=" wearing "),
                         "wearing a t-shirt, jeans, white sneakers")
        # the separator joins pieces; a piece's own tags ("jeans, denim") stay together
        self.assertEqual(self.build("casual_01", fmt="tags", separator=" | "),
                         "t-shirt | jeans, denim | sneakers")
        self.assertEqual(self.build("casual_01", fmt="tags", separator="\\n"),
                         "t-shirt\njeans, denim\nsneakers")

    def test_empty_selection(self):
        self.assertEqual(self.build(None, {}, fmt="tags"), "")
        self.assertEqual(self.build(None, {}, fmt="tags", prefix="wearing"), "")

    def test_output_order(self):
        text = self.build("winter_01", fmt="tags")
        self.assertEqual(text, "hoodie, jacket, open jacket, jeans, denim, boots")


class AttireTests(unittest.TestCase):
    """Tag-only overrides ({"tags": ...}) and the danbooru attire pools (data/attire.json)."""

    def setUp(self):
        self.cat, self.tmp = fresh_catalog(False)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.snap = self.cat.get()
        self.mannequin = self.snap.mannequin
        self.pieces = self.snap.pieces

    def test_public_includes_attire_pools(self):
        pub = self.cat.public()
        self.assertIn("attire", pub)
        for required in ("top", "bottom", "uniform", "traditional", "socks", "shoes", "decoration"):
            self.assertTrue(pub["attire"]["slots"].get(required), required)
        self.assertTrue(pub["attire"]["vocabulary"])

    def test_parse_state_tag_entries(self):
        self.assertEqual(oc.parse_state('{"overrides":{"top":{"tags":"hoodie"}}}'),
                         (None, {"top": {"tags": "hoodie"}}))
        self.assertEqual(oc.parse_state('{"overrides":{"top":{"tags":"  a\\tb  "}}}'),
                         (None, {"top": {"tags": "a b"}}))  # cleaned like piece text
        for raw in ('{"overrides":{"top":{"tags":5}}}', '{"overrides":{"top":{"tags":"x","extra":1}}}',
                    '{"overrides":{"top":["tags"]}}', '{"overrides":{"top":{}}}'):
            self.assertEqual(oc.parse_state(raw), (None, {}), raw)

    def test_resolve_tag_entries(self):
        sm = oc.resolve(None, {"top": {"tags": "hoodie"}, "onepiece": "onepiece_sundress"}, self.pieces)
        self.assertEqual(sm, {"top": {"tags": "hoodie"}, "onepiece": "onepiece_sundress"})
        # an explicit tag-only override is never second-guessed by a conflicting piece
        sm = oc.resolve(self.snap.outfits["school_01"], {"onepiece": "onepiece_sundress"}, self.pieces)
        self.assertEqual(sm, {"onepiece": "onepiece_sundress", "legwear": "legwear_kneesocks",
                              "footwear": "footwear_loafers", "accessory": "accessory_ribbon"})
        # ...but a tag-only slot of the outfit itself is dropped by the conflict
        sm = oc.resolve(None, {"top": {"tags": "x"}, "onepiece": "onepiece_sundress"}, self.pieces)
        self.assertIn("top", sm)  # explicit override wins
        # a dict inside an outfit's pieces (never produced by the catalog) must not crash resolve
        sm = oc.resolve({"pieces": {"top": {"tags": "x"}, "onepiece": "onepiece_sundress"}}, {}, self.pieces)
        self.assertEqual(sm, {"onepiece": "onepiece_sundress"})

    def test_build_string_tag_entries(self):
        sm = {"top": {"tags": "school_uniform"}, "footwear": "footwear_boots"}
        self.assertEqual(oc.build_string(sm, None, True, self.mannequin, self.pieces, "tags", ", ", ""),
                         "school_uniform, boots")
        self.assertEqual(oc.build_string(sm, None, True, self.mannequin, self.pieces, "natural", ", ", ""),
                         "school uniform, boots")
        both = oc.build_string(sm, None, True, self.mannequin, self.pieces, "both", ", ", "")
        self.assertEqual(both, "school uniform, boots\nschool_uniform, boots")

    def test_draw_list_skips_tag_entries(self):
        sm = {"top": {"tags": "hoodie"}, "footwear": "footwear_boots"}
        order = oc.draw_list(self.mannequin, self.pieces, sm)
        self.assertEqual([rel for rel, _ in order],
                         ["base/mannequin.webp", "pieces/footwear/boots.webp"])

    def test_draw_attire_deterministic_and_structured(self):
        import random
        attire = self.snap.attire
        a = oc.draw_attire(attire, random.Random(7))
        self.assertEqual(a, oc.draw_attire(attire, random.Random(7)))  # seeded = reproducible
        self.assertGreaterEqual(len(a), 2)
        self.assertTrue(("top" in a and "bottom" in a) or "onepiece" in a)  # exactly one main garment
        pools = {t for v in attire["slots"].values() for t in v}
        self.assertTrue(all(t in pools for t in a.values()))
        for slot in a:
            self.assertIn(slot, ("top", "bottom", "onepiece", "accessory", "legwear", "footwear"))
        self.assertEqual(oc.draw_attire({"slots": {}}, random.Random(1)), {})
        self.assertEqual(oc.draw_attire(None, random.Random(1)), {})

    def test_attire_to_slot_map_matches_pieces_by_tag(self):
        import random
        rng = random.Random(3)
        sm = oc.attire_to_slot_map({"top": "hoodie", "bottom": "jeans", "footwear": "no-such-tag"},
                                   self.pieces, rng)
        self.assertEqual(sm["top"], "top_hoodie")
        self.assertEqual(sm["bottom"], "bottom_jeans")
        self.assertEqual(sm["footwear"], {"tags": "no-such-tag"})
        sm = oc.attire_to_slot_map({"legwear": "black_kneehighs"}, self.pieces, rng)  # underscores match spaces
        self.assertEqual(sm["legwear"], "legwear_kneesocks")

    def test_save_rejects_tag_entries(self):
        cat, tmp = fresh_catalog(True)
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(cat.user_dir)
        with self.assertRaises(oc.CatalogError):
            cat.save_user_outfit("Raw", {"top": {"tags": "hoodie"}})

    def test_broken_or_missing_attire(self):
        tmp = tempfile.mkdtemp(prefix="magik_builtin_")
        self.addCleanup(shutil.rmtree, tmp, True)
        shutil.copytree(os.path.join(PKG_DIR, "data"), os.path.join(tmp, "data"))
        with open(os.path.join(tmp, "data", "attire.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        snap = oc.Catalog(tmp, None).get()
        self.assertIsNone(snap.attire)
        self.assertTrue(any("attire.json" in w for w in snap.warnings))
        os.remove(os.path.join(tmp, "data", "attire.json"))
        snap = oc.Catalog(tmp, None).get()
        self.assertIsNone(snap.attire)
        self.assertEqual(snap.warnings, [])  # a missing file is normal, not a warning


class PresetTests(unittest.TestCase):
    """data/presets.json clothing gallery and preset_to_slot_map."""

    def setUp(self):
        self.cat, self.tmp = fresh_catalog(False)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.snap = self.cat.get()

    def test_presets_load_and_are_served(self):
        self.assertGreaterEqual(len(self.snap.presets), 400)
        pub = self.cat.public()
        self.assertEqual(len(pub["presets"]), len(self.snap.presets))
        first = pub["presets"][0]
        for key in ("id", "name", "name_zh", "tags", "categories", "traits", "preview"):
            self.assertIn(key, first)

    def test_preset_to_slot_map(self):
        pieces = self.snap.pieces
        attire = self.snap.attire
        # a tag that exists in a piece's tags selects the piece
        sm = oc.preset_to_slot_map({"tags": "hoodie, jeans, sneakers"}, pieces, attire)
        self.assertEqual(sm, {"top": "top_hoodie", "bottom": "bottom_jeans", "footwear": "footwear_sneakers"})
        # a tag from the attire pools but not in any piece becomes a tag-only entry in the mapped slot
        sm = oc.preset_to_slot_map({"tags": "raincoat, pantyhose"}, pieces, attire)
        self.assertEqual(sm, {"top": {"tags": "raincoat"}, "legwear": {"tags": "pantyhose"}})
        # tags in neither pieces nor pools are skipped
        sm = oc.preset_to_slot_map({"tags": "1girl, looking at viewer, smile"}, pieces, attire)
        self.assertEqual(sm, {})
        # a one-piece piece drops top/bottom
        sm = oc.preset_to_slot_map({"tags": "hoodie, sundress"}, pieces, attire)
        self.assertEqual(sm, {"onepiece": "onepiece_sundress"})

    def test_broken_or_missing_presets(self):
        tmp = tempfile.mkdtemp(prefix="magik_builtin_")
        self.addCleanup(shutil.rmtree, tmp, True)
        shutil.copytree(os.path.join(PKG_DIR, "data"), os.path.join(tmp, "data"))
        with open(os.path.join(tmp, "data", "presets.json"), "w", encoding="utf-8") as f:
            f.write("{nope")
        snap = oc.Catalog(tmp, None).get()
        self.assertEqual(snap.presets, [])
        self.assertTrue(any("presets.json" in w for w in snap.warnings))
        os.remove(os.path.join(tmp, "data", "presets.json"))
        snap = oc.Catalog(tmp, None).get()
        self.assertEqual(snap.presets, [])
        self.assertEqual(snap.warnings, [])  # missing is normal, not a warning


class CatalogTests(unittest.TestCase):
    def test_builtin_loads_clean(self):
        cat, tmp = fresh_catalog(False)
        self.addCleanup(shutil.rmtree, tmp, True)
        snap = cat.get()
        self.assertEqual((len(snap.pieces), len(snap.outfits), snap.warnings), (14, 4, []))
        self.assertFalse(cat.public()["can_save"])

    def test_user_merge_collisions_and_junk(self):
        cat, tmp = fresh_catalog()
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(cat.user_dir)
        user_piece = {"id": "top_tshirt", "slot": "top", "layer": "pieces/top/x.webp"}  # collides
        good = {"id": "top_custom", "slot": "top", "label": "Custom", "tags": "x", "nl": "an x",
                "layer": "pieces/top/custom.webp"}
        junk = [user_piece, good, {"id": "bad_slot", "slot": "nope", "layer": "a.webp"}, 5, {"slot": "top"}]
        with open(os.path.join(cat.user_dir, "pieces.json"), "w") as f:
            json.dump(junk, f)
        with open(os.path.join(cat.user_dir, "outfits.json"), "w") as f:
            json.dump([{"id": "u_ok", "pieces": {"top": "top_custom"}},
                       {"id": "u_bad", "pieces": {"top": "does_not_exist"}}], f)
        snap = cat.get()
        self.assertEqual(snap.pieces["top_tshirt"]["source"], "builtin")  # builtin wins
        self.assertEqual(snap.pieces["top_custom"]["source"], "user")
        self.assertIn("u_ok", snap.outfits)
        self.assertNotIn("u_bad", snap.outfits)
        self.assertGreaterEqual(len(snap.warnings), 4)

    def test_reloads_when_files_change(self):
        cat, tmp = fresh_catalog()
        self.addCleanup(shutil.rmtree, tmp, True)
        first = cat.get()
        self.assertIs(first, cat.get())  # cached
        os.makedirs(cat.user_dir)
        with open(os.path.join(cat.user_dir, "outfits.json"), "w") as f:
            json.dump([{"id": "u1", "pieces": {"top": "top_tshirt"}}], f)
        self.assertIn("u1", cat.get().outfits)

    def test_public_urls_resolve_to_files(self):
        cat, tmp = fresh_catalog(False)
        self.addCleanup(shutil.rmtree, tmp, True)
        pub = cat.public()
        urls = [pub["base_url"]] + [p["layer_url"] for p in pub["pieces"]] \
            + [o["tile_url"] for o in pub["outfits"]]
        self.assertEqual(len(urls), 1 + 14 + 4)
        for url in urls:
            _, _, rest = url.partition("/magik/outfit/asset/")
            source, _, rel = rest.partition("/")
            self.assertIsNotNone(cat.find_asset(source, rel), url)


class AssetGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat, cls.tmp = fresh_catalog(False)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_blocks_traversal_and_junk(self):
        f = self.cat.find_asset
        self.assertIsNotNone(f("builtin", "base/mannequin.webp"))
        for rel in ("../data/pieces.json", "..\\data\\pieces.json", "base/../../data/pieces.json",
                    "/etc/passwd", "", "base", "base/nope.webp", "\x00", None, 5):
            self.assertIsNone(f("builtin", rel), rel)
        self.assertIsNone(f("user", "base/mannequin.webp"))      # no user dir
        self.assertIsNone(f("elsewhere", "base/mannequin.webp"))  # unknown source

    def test_blocks_symlink_escape(self):
        link = os.path.join(self.tmp, "assets_link_test")
        os.makedirs(os.path.join(self.tmp, "b", "assets"))
        os.makedirs(os.path.join(self.tmp, "secret"))
        shutil.copy(os.path.join(PKG_DIR, "assets", "base", "mannequin.webp"),
                    os.path.join(self.tmp, "secret", "s.webp"))
        os.symlink(os.path.join(self.tmp, "secret"), os.path.join(self.tmp, "b", "assets", "leak"))
        cat = oc.Catalog(os.path.join(self.tmp, "b"))
        self.assertIsNone(cat.find_asset("builtin", "leak/s.webp"))
        del link


class CompositeTests(unittest.TestCase):
    def test_matches_baked_tiles(self):
        cat, tmp = fresh_catalog(False)
        self.addCleanup(shutil.rmtree, tmp, True)
        snap = cat.get()
        for oid, outfit in snap.outfits.items():
            img = cat.composite(oc.resolve(outfit, {}, snap.pieces), snap)
            img = img.resize(tuple(snap.mannequin["tile"]), Image.LANCZOS)
            with Image.open(os.path.join(PKG_DIR, "assets", outfit["tile"])) as im:
                baked = im.convert("RGBA")
            self.assertIsNone(ImageChops.difference(img, baked).getbbox(), oid)

    def test_hides_and_conflict_in_draw_list(self):
        cat, tmp = fresh_catalog(False)
        self.addCleanup(shutil.rmtree, tmp, True)
        snap = cat.get()
        with_socks = oc.draw_list(snap.mannequin, snap.pieces,
                                  {"legwear": "legwear_kneesocks", "footwear": "footwear_boots"})
        self.assertFalse(any("kneesocks" in rel for rel, _ in with_socks))  # boots hide legwear


class UserOutfitTests(unittest.TestCase):
    def setUp(self):
        self.cat, self.tmp = fresh_catalog()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_save_and_delete(self):
        entry = self.cat.save_user_outfit(
            "  My  Cozy\tSet! ", {"top": "top_hoodie", "bottom": "bottom_shorts"},
            style=["Casual", "casual", "  "], prefix="cozy")
        self.assertEqual((entry["id"], entry["label"], entry["style"]),
                         ("user_my_cozy_set", "My Cozy Set!", ["casual"]))
        tile = os.path.join(self.cat.user_dir, "assets", entry["tile"])
        with Image.open(tile) as im:
            self.assertEqual(im.size, (192, 384))
        out = [o for o in self.cat.public()["outfits"] if o["id"] == entry["id"]][0]
        self.assertEqual(out["source"], "user")
        self.assertTrue(out["tile_url"].startswith("/magik/outfit/asset/user/outfits/"))
        self.assertIsNotNone(self.cat.find_asset("user", entry["tile"]))
        # same name again gets a suffix instead of overwriting
        again = self.cat.save_user_outfit("My Cozy Set!", {"top": "top_tshirt"})
        self.assertEqual(again["id"], "user_my_cozy_set_2")
        # persisted + picked up by the node-side catalog
        self.assertIn(entry["id"], self.cat.get().outfits)
        self.cat.delete_user_outfit(entry["id"])
        self.assertNotIn(entry["id"], self.cat.get().outfits)
        self.assertFalse(os.path.exists(tile))

    def test_rejections(self):
        save = self.cat.save_user_outfit
        with self.assertRaises(oc.CatalogError):
            save("", {"top": "top_tshirt"})
        with self.assertRaises(oc.CatalogError):
            save("x", {})
        with self.assertRaises(oc.CatalogError):
            save("x", {"top": "nope"})
        with self.assertRaises(oc.CatalogError):
            save("x", {"bottom": "top_tshirt"})                              # piece in wrong slot
        with self.assertRaises(oc.CatalogError):
            save("x", {"onepiece": "onepiece_sundress", "top": "top_tshirt"})  # conflict
        with self.assertRaises(oc.CatalogError):
            self.cat.delete_user_outfit("casual_01")                         # builtin
        with self.assertRaises(oc.CatalogError):
            oc.Catalog(PKG_DIR, None).save_user_outfit("x", {"top": "top_tshirt"})  # no user dir
        self.assertFalse(os.path.exists(os.path.join(self.cat.user_dir, "outfits.json")))


class RobustnessTests(unittest.TestCase):
    """Hand-edited user JSON must never take the whole catalog (or the node) down."""

    GOOD = {"id": "top_custom", "slot": "top", "label": "Custom", "tags": "x", "nl": "an x",
            "layer": "pieces/top/custom.webp"}

    def catalog_with(self, pieces=None, outfits=None, raw_pieces=None, raw_outfits=None):
        cat, tmp = fresh_catalog()
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(cat.user_dir)
        for name, obj, raw in (("pieces.json", pieces, raw_pieces), ("outfits.json", outfits, raw_outfits)):
            if obj is not None or raw is not None:
                with open(os.path.join(cat.user_dir, name), "w", encoding="utf-8") as f:
                    f.write(raw if raw is not None else json.dumps(obj))
        return cat

    def test_wrongly_typed_values_are_skipped_not_fatal(self):
        bad_pieces = [
            {"id": "a", "slot": ["top"], "layer": "a.webp"},
            {"id": "b", "slot": {"x": 1}, "layer": "a.webp"},
            {"id": "c", "slot": "top", "layer": "a.webp", "hides": [["top"], {"x": 1}, 7, "bottom"]},
            {"id": "d", "slot": "top", "layer": "a.webp", "conflicts": [{"x": 1}, None, "legwear", "top"]},
            {"id": "e", "slot": "top", "layer": "a.webp", "bbox": [1, 2, "3", 4.5], "z": "high",
             "style": {"a": 1}, "label": 5, "tags": None, "nl": []},
            self.GOOD,
        ]
        bad_outfits = [
            {"id": "o1", "pieces": {"top": ["top_custom"]}},
            {"id": "o2", "pieces": {"top": {"x": 1}}},
            {"id": "o3", "pieces": ["top_custom"]},
            {"id": "o4", "pieces": {"top": 5}},
            {"id": "ok", "pieces": {"top": "top_custom"}},
        ]
        snap = self.catalog_with(bad_pieces, bad_outfits).get()
        self.assertNotIn("a", snap.pieces)
        self.assertNotIn("b", snap.pieces)
        self.assertEqual(snap.pieces["c"]["hides"], ["bottom"])            # junk filtered, the valid slot kept
        self.assertEqual(snap.pieces["d"]["conflicts"], ["legwear"])       # and its own slot is never a conflict
        self.assertEqual((snap.pieces["e"]["z"], snap.pieces["e"]["bbox"], snap.pieces["e"]["label"]), (None, None, "e"))
        self.assertIn("top_custom", snap.pieces)
        self.assertEqual(sorted(o for o in snap.outfits if o.startswith(("o", "ok"))), ["ok"])
        self.assertEqual((len(snap.pieces) - 14, len(snap.outfits) - 4), (4, 1))   # the builtin catalog is intact
        self.assertGreaterEqual(len(snap.warnings), 6)

    def test_broken_files_are_reported_not_silently_ignored(self):
        for kind, raw, expect in (("syntax error", '[{"id": "x", ', "ValueError|JSONDecodeError"),
                                  ("a dict", '{"a": 1}', "expected a JSON list, found dict"),
                                  ("a string", '"nope"', "expected a JSON list, found str")):
            cat = self.catalog_with(raw_outfits=raw)
            snap = cat.get()
            self.assertEqual(len(snap.outfits), 4, kind)
            self.assertTrue(any("user outfits.json ignored" in w for w in snap.warnings), (kind, snap.warnings))
            self.assertTrue(any(re.search(expect, w) for w in snap.warnings), (kind, snap.warnings))
        snap = self.catalog_with(raw_pieces="not json at all").get()
        self.assertTrue(any("user pieces.json ignored" in w for w in snap.warnings))
        self.assertEqual(len(snap.pieces), 14)

    def test_saving_never_overwrites_a_file_it_cannot_read(self):
        mine = '[{"id": "my_precious", "label": "Hand-made", "pieces": {"top": "top_tshirt"}},   '   # truncated
        cat = self.catalog_with(raw_outfits=mine)
        path = os.path.join(cat.user_dir, "outfits.json")
        for action in (lambda: cat.save_user_outfit("New", {"top": "top_hoodie"}),
                       lambda: cat.delete_user_outfit("my_precious")):
            with self.assertRaisesRegex(oc.CatalogError, "cannot be read.*fix or remove it first"):
                action()
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), mine)                           # byte-for-byte untouched
        self.assertFalse(os.path.exists(os.path.join(cat.user_dir, "assets", "outfits", "user_new.webp")))
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"not": "a list"}')
        with self.assertRaisesRegex(oc.CatalogError, "not a JSON list"):
            cat.save_user_outfit("New", {"top": "top_hoodie"})
        with open(path, "w", encoding="utf-8") as f:                       # once the user fixes the file, saving works
            f.write('[{"id": "my_precious", "label": "Hand-made", "pieces": {"top": "top_tshirt"}}]')
        cat.save_user_outfit("New", {"top": "top_hoodie"})
        with open(path, encoding="utf-8") as f:
            ids = [o["id"] for o in json.load(f)]
        self.assertEqual(ids, ["my_precious", "user_new"])                 # nothing lost

    def test_one_broken_file_does_not_affect_the_node_output(self):
        cat = self.catalog_with(raw_outfits="[[[")
        snap = cat.get()
        outfit = snap.outfits["school_01"]
        text = oc.build_string(oc.resolve(outfit, {}, snap.pieces), outfit, False, snap.mannequin, snap.pieces)
        self.assertTrue(text.startswith("school uniform, white shirt"))


# ------------------------------------------------------------- routes
try:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    HAVE_AIOHTTP = True
except ImportError:  # pragma: no cover
    HAVE_AIOHTTP = False


@unittest.skipUnless(HAVE_AIOHTTP, "aiohttp not installed")
class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import outfit_routes
        self.cat, self.tmp = fresh_catalog()
        routes = web.RouteTableDef()
        outfit_routes.add_routes(routes, self.cat)
        app = web.Application()
        app.add_routes(routes)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_catalog_and_assets(self):
        r = await self.client.get("/magik/outfit/catalog")
        self.assertEqual(r.status, 200)
        data = await r.json()
        self.assertEqual((len(data["pieces"]), len(data["outfits"]), data["can_save"]), (14, 4, True))
        a = await self.client.get(data["pieces"][0]["layer_url"])
        self.assertEqual((a.status, a.headers["Content-Type"], a.headers["Cache-Control"]),
                         (200, "image/webp", "no-cache"))
        self.assertTrue((await a.read()).startswith(b"RIFF"))
        t = await self.client.get(data["outfits"][0]["tile_url"])
        self.assertEqual(t.status, 200)

    async def test_asset_404s(self):
        for path in ("/magik/outfit/asset/builtin/..%2Fdata%2Fpieces.json",
                     "/magik/outfit/asset/builtin/data/pieces.json",
                     "/magik/outfit/asset/builtin/pieces/top/missing.webp",
                     "/magik/outfit/asset/user/anything.webp",
                     "/magik/outfit/asset/other/base/mannequin.webp"):
            r = await self.client.get(path)
            self.assertEqual(r.status, 404, path)

    async def test_save_and_delete(self):
        r = await self.client.post("/magik/outfit/save_outfit",
                                   json={"label": "Route Test", "pieces": {"top": "top_hoodie"}})
        self.assertEqual(r.status, 200)
        outfit = (await r.json())["outfit"]
        self.assertEqual((outfit["id"], outfit["source"]), ("user_route_test", "user"))
        tile = await self.client.get(outfit["tile_url"])
        self.assertEqual(tile.status, 200)
        d = await self.client.post("/magik/outfit/delete_outfit", json={"id": outfit["id"]})
        self.assertEqual(d.status, 200)
        d = await self.client.post("/magik/outfit/delete_outfit", json={"id": outfit["id"]})
        self.assertEqual(d.status, 404)

    async def test_fuzzed_requests_never_cause_a_server_error(self):
        import random
        rng = random.Random(7)
        statuses = {}
        for i in range(300):
            roll = rng.random()
            if roll < 0.08:
                kwargs = {"data": rng.choice([b"", b"{", b"\xff\xfe", b"[1,2", b"null"]),
                          "headers": {"Content-Type": "application/json"}}
            elif roll < 0.25:
                kwargs = {"json": random_junk(rng)}
            else:
                # mostly valid requests, each with one field perturbed, so saves really happen
                valid = {s: p for s, p in {"top": "top_hoodie", "bottom": "bottom_jeans", "footwear": "footwear_boots",
                                           "headwear": "headwear_beret"}.items() if rng.random() < 0.7} or {"top": "top_tshirt"}
                body = {"label": rng.choice(["Look", "  My   look ", "\U0001F457 dress", "x" * 200, "tab\tname"]),
                        "pieces": valid, "style": rng.choice([["casual"], [], "casual", None]), "prefix": rng.choice(["", "cozy"])}
                if rng.random() < 0.5:
                    body[rng.choice(["label", "pieces", "style", "prefix"])] = random_junk(rng)
                kwargs = {"json": body}
            route = rng.choice(["/magik/outfit/save_outfit", "/magik/outfit/delete_outfit"])
            r = await self.client.post(route, **kwargs)
            statuses[r.status] = statuses.get(r.status, 0) + 1
            self.assertIn(r.status, (200, 400, 404), (route, kwargs, r.status, await r.text()))
            body = await r.json()                                                # always JSON, never an HTML error page
            self.assertTrue("error" in body or "outfit" in body or "ok" in body, body)
        self.assertGreater(statuses.get(200, 0), 5, statuses)                    # the fuzz really did reach valid saves
        self.assertGreater(statuses.get(400, 0), 50, statuses)
        # the catalog is still healthy afterwards, and every saved outfit is consistent
        cat = await (await self.client.get("/magik/outfit/catalog")).json()
        ids = {p["id"]: p for p in cat["pieces"]}
        for o in cat["outfits"]:
            for slot, pid in o["pieces"].items():
                self.assertEqual(ids[pid]["slot"], slot)
        self.assertEqual(cat["warnings"], [])
        for path in ("/magik/outfit/asset/user/x/../../../etc/passwd", "/magik/outfit/asset/builtin/%00",
                     "/magik/outfit/asset/%2e%2e/%2e%2e/etc/passwd", "/magik/outfit/asset/builtin/../data/pieces.json"):
            r = await self.client.get(path)
            self.assertIn(r.status, (400, 404), path)

    async def test_save_refuses_to_clobber_a_broken_user_file(self):
        os.makedirs(self.cat.user_dir, exist_ok=True)
        path = os.path.join(self.cat.user_dir, "outfits.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("[{oops")
        r = await self.client.post("/magik/outfit/save_outfit", json={"label": "x", "pieces": {"top": "top_hoodie"}})
        self.assertEqual(r.status, 400)
        self.assertIn("cannot be read", (await r.json())["error"])
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "[{oops")
        cat = await (await self.client.get("/magik/outfit/catalog")).json()
        self.assertTrue(any("user outfits.json ignored" in w for w in cat["warnings"]))   # the UI can show it

    async def test_bad_requests(self):
        r = await self.client.post("/magik/outfit/save_outfit", data=b"not json",
                                   headers={"Content-Type": "application/json"})
        self.assertEqual(r.status, 400)
        r = await self.client.post("/magik/outfit/save_outfit", json=[1, 2])
        self.assertEqual(r.status, 400)
        r = await self.client.post("/magik/outfit/save_outfit",
                                   json={"label": "x", "pieces": {"top": "nope"}})
        self.assertEqual(r.status, 400)
        self.assertIn("error", await r.json())
        r = await self.client.post("/magik/outfit/delete_outfit", json={"id": 5})
        self.assertEqual(r.status, 400)


# --------------------------------------------------------------- node
try:
    from comfy_api.latest import io as comfy_io  # noqa: F401
    HAVE_COMFY_API = True
except Exception:  # pragma: no cover
    HAVE_COMFY_API = False


@unittest.skipUnless(HAVE_COMFY_API, "comfy_api not importable (run inside ComfyUI to enable)")
class NodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        parent = os.path.dirname(PKG_DIR)
        if parent not in sys.path:
            sys.path.insert(0, parent)
        cls.pkg = importlib.import_module(os.path.basename(PKG_DIR))
        cls.node = cls.pkg.magik_outfit.MagikOutfit

    def test_schema(self):
        s = self.node.GET_SCHEMA()
        self.assertEqual((s.node_id, s.category), ("Magik.Outfit", "Magik/Prompt"))
        self.assertEqual([i.id for i in s.inputs], ["state", "format", "separator", "prefix"])
        self.assertTrue(s.inputs[0].socketless)
        self.assertEqual(s.inputs[1].options, ["tags", "natural", "both"])
        self.assertEqual([o.id for o in s.outputs], ["outfit"])

    def test_execute(self):
        state = json.dumps({"v": 1, "outfit": "school_01", "overrides": {"top": "top_hoodie"}})
        out = self.node.execute(state=state, format="natural", separator=", ", prefix="wearing")
        self.assertEqual(out.result, ("wearing a hoodie, a navy pleated skirt, black knee socks, "
                                      "brown loafers, a pink neck ribbon",))
        empty = self.node.execute(state=self.pkg.magik_outfit.DEFAULT_STATE, format="tags",
                                  separator=", ", prefix="")
        self.assertEqual(empty.result, ("",))
        junk = self.node.execute(state="not json", format="tags", separator=", ", prefix="")
        self.assertEqual(junk.result, ("",))

    def test_execute_survives_hostile_widget_values(self):
        import random
        rng = random.Random(3)
        for i in range(400):
            state = json.dumps(random_junk(rng))
            if rng.random() < 0.15:
                state = state[: rng.randint(0, len(state))]
            out = self.node.execute(state=state, format=rng.choice(["tags", "natural", "both", "bogus", ""]),
                                    separator=rng.choice([", ", "\\n", "", "\t\t", "\u2028"]),
                                    prefix=rng.choice(["", "wearing", "  ", "\U0001F457", "x" * 500]))
            self.assertIsInstance(out.result[0], str)

    def test_fingerprint_and_entrypoint(self):
        fp = self.node.fingerprint_inputs(state="{}", format="tags", separator=", ", prefix="")
        self.assertIsInstance(fp, str)
        import asyncio
        ext = asyncio.run(self.pkg.comfy_entrypoint())
        nodes = asyncio.run(ext.get_node_list())
        self.assertEqual(nodes, [self.node])
        self.assertEqual(self.pkg.WEB_DIRECTORY, "./web")


if __name__ == "__main__":
    unittest.main()
