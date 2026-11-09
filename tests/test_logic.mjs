// Run from the package folder:  node --test tests/test_logic.mjs
// (needs tests/parity_fixtures.json: python tests/make_parity_fixtures.py)
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadWebModules, loadFixtures } from "./_web_loader.mjs";

const L = await loadWebModules().import("outfit_logic.js");
const fixtures = loadFixtures();
const { catalog } = fixtures;
const idx = L.indexCatalog(catalog);

// tiny seeded RNG so property tests are reproducible
function rng(seed) {
  let s = seed >>> 0;
  return () => ((s = (Math.imul(s, 1664525) + 1013904223) >>> 0) / 2 ** 32);
}

test("parseState matches Python on all sample inputs", () => {
  for (const { raw, expected } of fixtures.states) {
    assert.deepEqual(L.parseState(raw), expected, JSON.stringify(raw));
  }
  assert.deepEqual(L.parseState(null), { outfit: null, overrides: {} });
  assert.deepEqual(L.parseState(5), { outfit: null, overrides: {} });
});

test(`hostile state documents: JS == Python on ${fixtures.fuzz.length} random cases`, () => {
  let valid = 0, empty = 0;
  for (const [i, c] of fixtures.fuzz.entries()) {
    const label = `fuzz ${i}: ${c.text.slice(0, 80)}`;
    const parsed = L.parseState(c.text);
    assert.deepEqual(parsed, { outfit: c.outfit, overrides: c.overrides }, label);
    const outfit = parsed.outfit ? idx.outfits.get(parsed.outfit) ?? null : null;
    const slotMap = L.resolve(outfit, parsed.overrides, idx.pieces);
    assert.deepEqual(slotMap, c.slot_map, label);
    assert.deepEqual(Object.keys(slotMap), Object.keys(c.slot_map), label + " (key order)");
    const modified = L.isModified(outfit, slotMap);
    assert.equal(L.buildString(slotMap, outfit, modified, catalog, idx.pieces, "both", " | ", "wearing"), c.string, label);
    if (outfit) valid++;
    if (!Object.keys(slotMap).length) empty++;
  }
  assert.ok(valid > 30 && empty > 20, `weak sample: ${valid} with an outfit, ${empty} empty`);
});

test("cleanText and pyStrip match Python on emoji, BOM, NEL, odd spaces and truncation", () => {
  for (const c of fixtures.texts) {
    assert.equal(L.cleanText(c.input, c.max), c.expected, `cleanText(${JSON.stringify(c.input).slice(0, 40)}, ${c.max})`);
  }
  for (const c of fixtures.strips) assert.equal(L.pyStrip(c.input), c.expected, JSON.stringify(c.input));
  // truncation never leaves half of an emoji behind
  const cut = L.cleanText("x".repeat(59) + "\u{1F600}\u{1F600}", 60);
  assert.equal(cut, "x".repeat(59) + "\u{1F600}");
  assert.ok(!/[\ud800-\udbff](?![\udc00-\udfff])/.test(cut), "no lone surrogate");
});

test("a hostile state cannot touch prototypes (workflow files can be shared)", () => {
  const evil = '{"outfit":"school_01","overrides":{"__proto__":null,"constructor":"x","top":"top_hoodie"}}';
  const { outfit, overrides } = L.parseState(evil);
  assert.equal(outfit, "school_01");
  assert.equal(Object.getPrototypeOf(overrides), Object.prototype, "the overrides object keeps its prototype");
  assert.ok(Object.prototype.hasOwnProperty.call(overrides, "__proto__"), "the key is kept as plain data, like in Python");
  assert.deepEqual(Object.keys(overrides), ["__proto__", "constructor", "top"]);
  // nothing leaks into every other object
  const resolved = L.resolve(idx.outfits.get("school_01"), overrides, idx.pieces);
  assert.equal(resolved.top, "top_hoodie");
  assert.equal(({}).top, undefined);
  assert.equal(Object.getPrototypeOf(resolved), Object.prototype);
  // a prototype-shaped object as a value is rejected like any other non-string
  assert.deepEqual(L.parseState('{"overrides":{"top":{"__proto__":{"polluted":1}}}}').overrides, {});
  assert.equal(({}).polluted, undefined);
  // and the state survives a round trip with the odd key intact
  assert.deepEqual(L.parseState(L.serializeState("school_01", overrides)).overrides, overrides);
});

test("serializeState <-> parseState round trip", () => {
  const text = L.serializeState("school_01", { top: "top_hoodie", legwear: null });
  assert.equal(text, '{"v":1,"outfit":"school_01","overrides":{"top":"top_hoodie","legwear":null}}');
  assert.deepEqual(L.parseState(text), { outfit: "school_01", overrides: { top: "top_hoodie", legwear: null } });
  assert.equal(L.serializeState(undefined, undefined), '{"v":1,"outfit":null,"overrides":{}}');
});

test(`resolve / buildString / drawList match Python on ${fixtures.cases.length} randomized cases`, () => {
  let withPrefix = 0, modifiedCount = 0;
  for (const [i, c] of fixtures.cases.entries()) {
    const outfit = idx.outfits.get(c.outfit) ?? null;
    const slotMap = L.resolve(outfit, c.overrides, idx.pieces);
    const label = `case ${i}: ${JSON.stringify({ outfit: c.outfit, overrides: c.overrides })}`;
    assert.deepEqual(slotMap, c.expected.slot_map, label);
    // key order matters for draw-order ties, so compare it explicitly
    assert.deepEqual(Object.keys(slotMap), Object.keys(c.expected.slot_map), label + " (key order)");
    const modified = L.isModified(outfit, slotMap);
    assert.equal(modified, c.expected.modified, label);
    const text = L.buildString(slotMap, outfit, modified, catalog, idx.pieces, c.format, c.separator, c.prefix);
    assert.equal(text, c.expected.string, label);
    const draw = L.drawList(catalog, idx.pieces, slotMap).map((d) => d.url);
    assert.deepEqual(draw, c.expected.draw, label + " (draw order)");
    if (outfit && !modified && outfit.prefix) withPrefix++;
    if (modified) modifiedCount++;
  }
  // make sure the sample actually exercised the interesting paths
  assert.ok(withPrefix > 10, `only ${withPrefix} unmodified-with-prefix cases`);
  assert.ok(modifiedCount > 100, `only ${modifiedCount} modified cases`);
});

test("pickPiece keeps state explicit, conflict-free and idempotent (property test)", () => {
  const rand = rng(7);
  const slots = idx.slotOrder;
  const outfitIds = [null, ...idx.outfits.keys()];
  let steps = 0;
  for (let run = 0; run < 150; run++) {
    const outfit = idx.outfits.get(outfitIds[Math.floor(rand() * outfitIds.length)]) ?? null;
    let overrides = {};
    for (let n = 0; n < 12; n++) {
      const slot = slots[Math.floor(rand() * slots.length)];
      const options = idx.bySlot.get(slot) ?? [];
      const pickNone = rand() < 0.3 || options.length === 0;
      const piece = pickNone ? null : options[Math.floor(rand() * options.length)];
      const before = L.resolve(outfit, overrides, idx.pieces);
      overrides = L.pickPiece(outfit, overrides, slot, piece ? piece.id : null, idx.pieces);
      const after = L.resolve(outfit, overrides, idx.pieces);
      steps++;

      // the chosen slot holds the chosen piece (or nothing)
      assert.equal(after[slot] ?? null, piece ? piece.id : null);
      // no two present pieces conflict, in either direction
      for (const [sa, ia] of Object.entries(after)) {
        for (const [sb, ib] of Object.entries(after)) {
          if (sa === sb) continue;
          assert.ok(!idx.pieces.get(ia).conflicts.includes(sb), `${ia} conflicts with ${ib}`);
        }
      }
      // slots the pick did not touch and that do not conflict keep their piece
      for (const [s, id] of Object.entries(before)) {
        if (s === slot) continue;
        const dropped = !(s in after);
        if (dropped) {
          const conflicting = piece && (piece.conflicts.includes(s) || idx.pieces.get(id).conflicts.includes(slot));
          assert.ok(conflicting, `${id} vanished without a conflict`);
        } else {
          assert.equal(after[s], id);
        }
      }
      // overrides are the minimal explicit diff: re-deriving them is a no-op
      assert.deepEqual(L.diffOverrides(outfit, after), overrides);
      // a null override only ever appears where the outfit actually has a piece
      for (const [s, id] of Object.entries(overrides)) {
        if (id === null) assert.ok(outfit && s in outfit.pieces, `useless null override for ${s}`);
      }
    }
  }
  assert.equal(steps, 150 * 12);
});

test("pickPiece rejects wrong-slot pieces and unknown ids without changing state", () => {
  const outfit = idx.outfits.get("school_01");
  const start = { top: "top_hoodie" };
  assert.equal(L.pickPiece(outfit, start, "bottom", "top_tshirt", idx.pieces), start);
  assert.equal(L.pickPiece(outfit, start, "bottom", "nope", idx.pieces), start);
});

test("concrete UI scenarios", () => {
  const school = idx.outfits.get("school_01");
  const summer = idx.outfits.get("summer_01");
  // top over a dress drops the dress and records it as an explicit null
  let ov = L.pickPiece(summer, {}, "top", "top_tshirt", idx.pieces);
  assert.deepEqual(ov, { onepiece: null, top: "top_tshirt" });
  // choosing the dress on the school outfit removes top + bottom explicitly
  ov = L.pickPiece(school, {}, "onepiece", "onepiece_sundress", idx.pieces);
  assert.deepEqual(ov, { top: null, bottom: null, onepiece: "onepiece_sundress" });
  // picking the outfit's own piece again is a no-op
  assert.deepEqual(L.pickPiece(school, { top: "top_hoodie" }, "top", "top_blouse", idx.pieces), {});
  // fully custom set (no outfit): overrides are the map itself
  ov = L.pickPiece(null, {}, "top", "top_tshirt", idx.pieces);
  assert.deepEqual(ov, { top: "top_tshirt" });
  // removing a slot of the outfit records an explicit null; removing an absent slot records nothing
  assert.deepEqual(L.pickPiece(school, {}, "legwear", null, idx.pieces), { legwear: null });
  assert.deepEqual(L.pickPiece(school, {}, "gloves", null, idx.pieces), {});
});

test("indexCatalog slot order, filters and styles", () => {
  assert.deepEqual(idx.slotOrder, catalog.output_order);
  assert.equal(idx.bySlot.get("top").length, 3);
  const all = catalog.outfits;
  assert.deepEqual(L.collectStyles(all), ["casual", "school", "summer", "winter"]);
  assert.deepEqual(L.filterOutfits(all, { style: "school" }).map((o) => o.id), ["school_01"]);
  assert.deepEqual(L.filterOutfits(all, { query: " SAILOR " }).map((o) => o.id), ["school_01"]);
  assert.deepEqual(L.filterOutfits(all, { query: "winter" }).map((o) => o.id), ["winter_01"]);
  assert.deepEqual(L.filterOutfits(all, { favorites: new Set(["casual_01", "summer_01"]) }).map((o) => o.id),
    ["casual_01", "summer_01"]);
  assert.equal(L.filterOutfits(all, { query: "zzz" }).length, 0);
});
