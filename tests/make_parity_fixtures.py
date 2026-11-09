"""Generate tests/parity_fixtures.json from the Python implementation.

The JS logic (web/outfit_logic.js) must reproduce these results exactly.
Re-run after changing the catalog or the Python logic:

    python tests/make_parity_fixtures.py
"""
import json
import os
import random
import sys

PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PKG_DIR)

import outfit_core as oc  # noqa: E402

SEED = 20261021
N_CASES = 800

RAW_STATES = [
    "", "   ", "garbage", "[]", "42", "null", '{"outfit": 5, "overrides": []}',
    '{"v":1,"outfit":"school_01","overrides":{"top":"top_hoodie","legwear":null}}',
    '{"outfit":"x","overrides":{"a":null,"b":"c","d":1,"e":["x"],"f":{"g":1}}}',
    '{"outfit":null,"overrides":{}}', '{"overrides":{"top":"top_tshirt"}}',
    '{"v":1,"outfit":null,"overrides":{"top":{"tags":"hoodie"},"bottom":null}}',
    '{"overrides":{"top":{"tags":"  spaced   out  "}}}',
    '{"overrides":{"top":{"tags":5}}}', '{"overrides":{"top":{"tags":"x","extra":1}}}',
    '{"overrides":{"top":{}}}',
]


def main():
    cat = oc.Catalog(PKG_DIR, None)
    snap = cat.get()
    public = cat.public()
    rng = random.Random(SEED)

    selectable = [n for n, s in snap.mannequin["slots"].items() if s.get("selectable")]
    all_piece_ids = list(snap.pieces)
    outfit_ids = list(snap.outfits)

    cases = []
    for _ in range(N_CASES):
        outfit_id = rng.choice(outfit_ids + [None, "does_not_exist"])
        outfit = snap.outfits.get(outfit_id)
        overrides = {}
        for slot in selectable:
            if rng.random() < 0.35:
                roll = rng.random()
                if roll < 0.25:
                    overrides[slot] = None
                elif roll < 0.35:
                    overrides[slot] = rng.choice(all_piece_ids)      # maybe the wrong slot
                elif roll < 0.40:
                    overrides[slot] = "unknown_piece"
                elif roll < 0.45:
                    overrides[slot] = {"tags": rng.choice(["hoodie", "school uniform", "blazer",
                                                           "x" * 250, "", "black_kneehighs"])}
                else:
                    in_slot = [p["id"] for p in snap.pieces.values() if p["slot"] == slot]
                    if in_slot:
                        overrides[slot] = rng.choice(in_slot)
        fmt = rng.choice(list(oc.FORMATS) + ["bogus"])
        separator = rng.choice([", ", " | ", "\\n", "\\t", "", " ; "])
        prefix = rng.choice(["", "wearing", "  wearing  ", "  ", "\ufeffwearing\x85", "\U0001F457 wearing",
                             "\u00a0wearing\u3000", "wearing\u2003now"])

        slot_map = oc.resolve(outfit, overrides, snap.pieces)
        modified = outfit is None or slot_map != outfit["pieces"]
        text = oc.build_string(slot_map, outfit, modified, snap.mannequin, snap.pieces,
                               fmt, separator, prefix)
        order = [oc._asset_url(src, rel) for rel, src in oc.draw_list(snap.mannequin, snap.pieces, slot_map)]
        cases.append({
            "outfit": outfit_id, "overrides": overrides, "format": fmt,
            "separator": separator, "prefix": prefix,
            "expected": {"slot_map": slot_map, "modified": modified, "string": text, "draw": order},
        })

    states = [{"raw": raw, "expected": dict(zip(("outfit", "overrides"), oc.parse_state(raw)))}
              for raw in RAW_STATES]

    # fuzz: random JSON documents (wrong types, nesting, truncation) in the hidden state widget
    fuzz_pool = ["school_01", "casual_01", "ghost", "top", "bottom", "onepiece", "footwear", "legwear", "top_hoodie",
                 "top_blouse", "onepiece_sundress", "bottom_jeans", "footwear_boots", "", "__proto__", "constructor"]

    def junk(depth=0):
        roll = rng.random()
        if roll < 0.15: return None
        if roll < 0.25: return rng.choice([True, False])
        if roll < 0.35: return rng.choice([0, 1, -5, 2 ** 40, 3.5])
        if roll < 0.65 or depth > 2: return rng.choice(fuzz_pool + ["x" * 40, "\u00e9\U0001F457"])
        if roll < 0.8: return [junk(depth + 1) for _ in range(rng.randint(0, 3))]
        return {rng.choice(fuzz_pool): junk(depth + 1) for _ in range(rng.randint(0, 3))}

    fuzz = []
    for _ in range(400):
        if rng.random() < 0.6:
            doc = {"v": 1, "outfit": rng.choice(outfit_ids + [None, "ghost", 5, ["x"], {}]),
                   "overrides": {rng.choice(selectable + ["cape", "", "__proto__"]):
                                 rng.choice(all_piece_ids + [None, "junk", 3, ["a"], {},
                                                            {"tags": rng.choice(fuzz_pool)},
                                                            {"tags": 3}, {"tags": "x", "y": 1}])
                                 for _ in range(rng.randint(0, 5))}}
        else:
            doc = junk()
        text = json.dumps(doc)
        if rng.random() < 0.1:
            text = text[: rng.randint(0, len(text))]                    # truncated / invalid JSON
        outfit_id, overrides = oc.parse_state(text)
        outfit = snap.outfits.get(outfit_id) if outfit_id else None
        slot_map = oc.resolve(outfit, overrides, snap.pieces)
        modified = outfit is None or slot_map != outfit["pieces"]
        fuzz.append({"text": text, "outfit": outfit_id, "overrides": overrides, "slot_map": slot_map,
                     "string": oc.build_string(slot_map, outfit, modified, snap.mannequin, snap.pieces,
                                               "both", " | ", "wearing")})

    # text cleaning: emoji, BOM, NEL, odd spaces, control characters, non-strings, truncation
    raw_texts = ["  a  b  ", "tab\there", "x" * 59 + "\U0001F600\U0001F600", "\U0001F457" * 70, "\ufeffbom\ufeff",
                 "nel\x85x", "\u00a0nbsp\u00a0", "a\u2003em\u2003space", "ctrl\x01\x7fchars", "line\nbreak", 5, None,
                 "\u00e9" * 70, "z" * 100, "\x85lead", "trail\x85", "\u200bzero\u200bwidth", "", "   ", "\x1cx\x1f"]
    texts = [{"input": t, "max": m, "expected": oc._clean_text(t, m)} for t in raw_texts for m in (60, 5, 80)]
    strips = [{"input": t, "expected": t.strip()} for t in
              ["\ufeffx\ufeff", "\x85x\x85", " \u00a0x\u3000 ", "\x1cx\x1f", "  plain  ", "\u200bx\u200b", ""]]

    out = os.path.join(PKG_DIR, "tests", "parity_fixtures.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"catalog": public, "cases": cases, "states": states, "texts": texts, "strips": strips,
                   "fuzz": fuzz}, f, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {len(cases)} cases + {len(states)} state samples -> {out} ({os.path.getsize(out)} bytes)")


if __name__ == "__main__":
    main()
