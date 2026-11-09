// UI tests under jsdom. Setup:  npm i -D jsdom   (or set JSDOM_DIR to a folder containing node_modules/jsdom)
// Run:                          node --test tests/test_ui.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadWebModules, loadJSDOM, loadFixtures } from "./_web_loader.mjs";

const JSDOM = await loadJSDOM();
const skip = JSDOM ? false : "jsdom not installed (npm i -D jsdom)";
const web = loadWebModules();
const L = await web.import("outfit_logic.js");
const { createOutfitUI, createImageLoader } = await web.import("outfit_ui.js");
const fixtures = loadFixtures();

const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));
const settle = async () => { await tick(); await tick(); await tick(); };

const SCHOOL_TAGS = "school uniform, white shirt, collared shirt, pleated skirt, navy skirt, black kneehighs, loafers, neck ribbon";
const SCHOOL_HOODIE_TAGS = "hoodie, pleated skirt, navy skirt, black kneehighs, loafers, neck ribbon";

function makeEnv({ canSave = true, alpha = 255, favorites = null, confirm = true, random = () => 0,
                   fetchFails = false, imageDelay = () => 0, imageFails = () => false, mutate = null } = {}) {
  const dom = new JSDOM("<!doctype html><body></body>", { pretendToBeVisual: true });
  const { window } = dom;
  const doc = window.document;

  // --- canvas mocks: one recording context per canvas element
  const ctxs = new WeakMap();
  window.HTMLCanvasElement.prototype.getContext = function () {
    if (!ctxs.has(this)) {
      const ctx = {
        log: [], globalAlpha: 1,
        clearRect() { ctx.log.push({ op: "clear" }); },
        drawImage(img, ...args) { ctx.log.push({ op: "draw", url: img.url, args, alpha: ctx.globalAlpha }); },
        getImageData(x, y, w, h) {
          const data = new Uint8ClampedArray(w * h * 4);
          for (let i = 3; i < data.length; i += 4) data[i] = env.alpha;
          return { data };
        },
      };
      ctxs.set(this, ctx);
    }
    return ctxs.get(this);
  };
  window.HTMLCanvasElement.prototype.getBoundingClientRect = () => ({ left: 0, top: 0, width: 256, height: 512, right: 256, bottom: 512 });

  const catalog = structuredClone(fixtures.catalog);
  catalog.can_save = canSave;
  if (mutate) mutate(catalog);

  const store = favorites ? { fav: JSON.stringify(favorites) } : {};
  const env = {
    window, doc, catalog, alpha, ctxs,
    changes: [], saves: [], deletes: [], fetches: [], storageSets: [],
    options: { format: "tags", separator: ", ", prefix: "" },
  };

  env.ui = createOutfitUI({
    doc,
    fetchCatalog: async (force) => {
      env.fetches.push(!!force);
      if (fetchFails) throw new Error("server said no");
      return structuredClone(env.catalog);
    },
    saveOutfit: async (payload) => {
      env.saves.push(payload);
      if (env.saveError) throw new Error(env.saveError);
      const slug = payload.label.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "");
      const outfit = {
        id: `user_${slug}`, label: payload.label, style: payload.style, pieces: payload.pieces,
        prefix: payload.prefix, source: "user", tile_url: `/magik/outfit/asset/user/outfits/user_${slug}.webp`,
      };
      env.catalog.outfits.push(outfit);
      return { outfit };
    },
    deleteOutfit: async (id) => {
      env.deletes.push(id);
      env.catalog.outfits = env.catalog.outfits.filter((o) => o.id !== id);
      return { ok: true };
    },
    loadImage: async (url) => {
      const d = imageDelay(url);
      if (d) await tick(d);
      if (imageFails(url)) throw new Error(`boom ${url}`);
      return { url, width: 512, height: 1024 };
    },
    assetURL: (u) => u,
    onChange: (text) => env.changes.push(text),
    getOptions: () => env.options,
    storage: {
      getItem: (k) => (k === "magik_outfit.favorites" ? store.fav ?? null : null),
      setItem: (k, v) => { store.fav = v; env.storageSets.push([k, v]); },
    },
    random,
    confirmFn: () => confirm,
  });
  doc.body.append(env.ui.element);

  const root = env.ui.element;
  env.q = (sel) => root.querySelector(sel);
  env.qa = (sel) => [...root.querySelectorAll(sel)];
  env.canvas = env.q(".mo-canvas");
  env.click = (node) => node.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  env.tile = (id) => env.q(`.mo-tile[data-outfit-id="${id}"]`);
  env.chip = (slot) => env.q(`.mo-chip[data-slot="${slot}"]`);
  env.pick = (id) => env.q(`.mo-pick[data-piece-id="${id}"]`);
  env.action = (name) => env.q(`[data-action="${name}"]`);
  env.title = () => env.q(".mo-title").textContent;
  env.string = () => env.q(".mo-string").textContent;
  env.lastState = () => JSON.parse(env.changes[env.changes.length - 1]);
  env.lastRender = () => {
    const log = ctxs.get(env.canvas).log;
    const at = log.map((e) => e.op).lastIndexOf("clear");
    return log.slice(at + 1).filter((e) => e.op === "draw").map((e) => e.url);
  };
  env.expectedDraw = (outfitId, overrides = {}) => {
    const idx = L.indexCatalog(env.catalog);
    const map = L.resolve(idx.outfits.get(outfitId) ?? null, overrides, idx.pieces);
    return L.drawList(env.catalog, idx.pieces, map).map((d) => d.url);
  };
  env.hover = (x, y) => env.canvas.dispatchEvent(new window.MouseEvent("pointermove", { clientX: x, clientY: y, bubbles: true }));
  return env;
}

async function ready(opts) {
  const env = makeEnv(opts);
  await env.ui.ready;
  await settle();
  return env;
}

test("initial render: grid, chips, empty preview, no change events", { skip }, async () => {
  const env = await ready();
  assert.equal(env.qa(".mo-tile").length, 4);
  assert.equal(env.title(), "No outfit selected");
  assert.equal(env.string(), "(nothing selected)");
  assert.equal(env.qa(".mo-chip").length, 8); // every slot that has pieces (gloves has none)
  assert.deepEqual(env.lastRender(), [env.catalog.base_url]);
  for (const a of ["reset", "clear", "save"]) assert.ok(env.action(a).disabled, `${a} should start disabled`);
  assert.equal(env.changes.length, 0);
  assert.equal(env.q(".mo-status").textContent, "");
  assert.equal(env.qa("img").length, 4); // tile images use the catalog's tile URLs
  assert.deepEqual(env.qa("img").map((i) => i.getAttribute("src")), env.catalog.outfits.map((o) => o.tile_url));
});

test("selecting an outfit updates state, title, chips, string and layered preview", { skip }, async () => {
  const env = await ready();
  env.click(env.tile("school_01"));
  await settle();
  assert.deepEqual(env.changes, ['{"v":1,"outfit":"school_01","overrides":{}}']);
  assert.equal(env.title(), "Sailor uniform");
  assert.ok(env.tile("school_01").classList.contains("is-selected"));
  assert.ok(!env.tile("casual_01").classList.contains("is-selected"));
  assert.equal(env.string(), SCHOOL_TAGS);
  assert.equal(env.chip("top").querySelector(".mo-chip-value").textContent, "Blouse");
  assert.deepEqual(env.lastRender(), env.expectedDraw("school_01"));
  assert.equal(env.lastRender().length, 6); // base + top, bottom, legwear, footwear, accessory
  assert.ok(env.action("reset").disabled && !env.action("clear").disabled);
});

test("changing a piece: picker, custom state, changed chip, reset", { skip }, async () => {
  const env = await ready();
  env.click(env.tile("school_01"));
  env.click(env.chip("top"));
  await settle();
  assert.ok(!env.q(".mo-picker").hidden);
  assert.equal(env.q(".mo-picker-title").textContent, "Choose top");
  assert.equal(env.qa(".mo-pick").length, 4);                       // None + 3 tops
  assert.ok(env.pick("top_blouse").classList.contains("is-selected"));

  env.click(env.pick("top_hoodie"));
  await settle();
  assert.deepEqual(env.lastState(), { v: 1, outfit: "school_01", overrides: { top: "top_hoodie" } });
  assert.ok(env.q(".mo-picker").hidden);
  assert.equal(env.title(), "Custom (based on Sailor uniform)");
  assert.ok(env.chip("top").classList.contains("is-changed"));
  assert.equal(env.string(), SCHOOL_HOODIE_TAGS);                   // outfit prefix dropped once modified
  assert.deepEqual(env.lastRender(), env.expectedDraw("school_01", { top: "top_hoodie" }));
  assert.ok(!env.action("reset").disabled && !env.action("save").disabled);

  env.click(env.action("reset"));
  await settle();
  assert.deepEqual(env.lastState(), { v: 1, outfit: "school_01", overrides: {} });
  assert.equal(env.title(), "Sailor uniform");
  assert.equal(env.string(), SCHOOL_TAGS);
});

test("conflicts: a top over a dress removes the dress; the picker warns about replaced slots", { skip }, async () => {
  const env = await ready();
  env.click(env.tile("summer_01"));
  env.click(env.chip("top"));
  env.click(env.pick("top_tshirt"));
  await settle();
  assert.deepEqual(env.lastState().overrides, { onepiece: null, top: "top_tshirt" });
  assert.equal(env.chip("onepiece").querySelector(".mo-chip-value").textContent, "—");
  assert.ok(env.chip("onepiece").classList.contains("is-changed"));
  assert.ok(!env.lastRender().some((u) => u.includes("sundress")));

  env.click(env.chip("onepiece"));
  await settle();
  assert.equal(env.pick("onepiece_sundress").querySelector(".mo-pick-note").textContent, "replaces top, bottom");
});

test("chip × removes a piece; Clear empties the set", { skip }, async () => {
  const env = await ready();
  env.click(env.tile("school_01"));
  env.click(env.q('[data-clear="legwear"]'));
  await settle();
  assert.deepEqual(env.lastState().overrides, { legwear: null });
  assert.ok(env.chip("legwear").classList.contains("is-empty"));
  assert.equal(env.q('[data-clear="legwear"]'), null);
  env.click(env.action("clear"));
  await settle();
  assert.deepEqual(env.lastState(), { v: 1, outfit: null, overrides: {} });
  assert.equal(env.title(), "No outfit selected");
  assert.deepEqual(env.lastRender(), [env.catalog.base_url]);
});

test("None in the picker removes the slot", { skip }, async () => {
  const env = await ready();
  env.click(env.tile("school_01"));
  env.click(env.chip("footwear"));
  env.click(env.q('.mo-pick[data-slot-pick="footwear"]'));
  await settle();
  assert.deepEqual(env.lastState().overrides, { footwear: null });
  assert.ok(env.q(".mo-picker").hidden);
});

test("hovering the preview highlights the slot; clicking opens its picker", { skip }, async () => {
  const env = await ready();
  env.click(env.tile("school_01"));
  await settle();
  env.hover(128, 175); // canvas coords (256, 350): inside the blouse
  assert.ok(env.chip("top").classList.contains("is-hover"));
  assert.match(env.q(".mo-hint").textContent, /^Top: Blouse/);
  assert.ok(env.canvas.classList.contains("is-hot"));
  // a real click carries coordinates; the hit test uses them
  env.canvas.dispatchEvent(new env.window.MouseEvent("click", { clientX: 128, clientY: 175, bubbles: true }));
  await settle();
  assert.equal(env.q(".mo-picker-title").textContent, "Choose top");

  env.hover(5, 5);     // empty corner
  assert.ok(!env.chip("top").classList.contains("is-hover"));
  assert.equal(env.q(".mo-hint").textContent, "");
  env.canvas.dispatchEvent(new env.window.MouseEvent("pointerleave"));
  assert.ok(!env.canvas.classList.contains("is-hot"));
});

test("hit testing respects transparent pixels", { skip }, async () => {
  const env = await ready({ alpha: 0 });
  env.click(env.tile("school_01"));
  await settle();
  env.hover(128, 175);
  assert.ok(!env.chip("top").classList.contains("is-hover"));
  env.canvas.dispatchEvent(new env.window.MouseEvent("click", { clientX: 128, clientY: 175, bubbles: true }));
  await settle();
  assert.ok(env.q(".mo-picker").hidden);
});

test("piece thumbnails crop around the piece bbox over a faint mannequin", { skip }, async () => {
  const env = await ready();
  env.click(env.chip("top"));
  await settle();
  const hoodie = env.catalog.pieces.find((p) => p.id === "top_hoodie");
  const thumb = env.pick("top_hoodie").querySelector("canvas");
  const draws = env.ctxs.get(thumb).log.filter((e) => e.op === "draw");
  assert.deepEqual(draws.map((d) => d.url), [env.catalog.base_url, hoodie.layer_url]);
  assert.deepEqual(draws.map((d) => d.alpha), [0.35, 1]);
  for (const d of draws) {
    const [sx, sy, sw, sh, dx, dy, dw, dh] = d.args;
    assert.ok(sx >= 0 && sy >= 0 && sx + sw <= 512 && sy + sh <= 1024, "source rect inside the canvas");
    assert.ok(dx >= 0 && dy >= 0 && dx + dw <= 80.001 && dy + dh <= 100.001, "dest rect inside the thumbnail");
    assert.ok(sx <= hoodie.bbox[0] && sx + sw >= hoodie.bbox[2], "source rect covers the bbox horizontally");
  }
});

test("search, style filter and favorites", { skip }, async () => {
  const env = await ready({ favorites: ["winter_01"] });
  const search = env.q(".mo-search");
  search.value = "sailor";
  search.dispatchEvent(new env.window.Event("input", { bubbles: true }));
  assert.deepEqual(env.qa(".mo-tile").map((t) => t.dataset.outfitId), ["school_01"]);
  search.value = "zzz";
  search.dispatchEvent(new env.window.Event("input", { bubbles: true }));
  assert.equal(env.q(".mo-empty").textContent, "No outfits match.");
  search.value = "";
  search.dispatchEvent(new env.window.Event("input", { bubbles: true }));
  assert.equal(env.qa(".mo-tile").length, 4);

  env.click(env.q('[data-style="winter"]'));
  assert.deepEqual(env.qa(".mo-tile").map((t) => t.dataset.outfitId), ["winter_01"]);
  assert.equal(env.q('[data-style="winter"]').getAttribute("aria-pressed"), "true");
  env.click(env.q('[data-style="winter"]'));
  assert.equal(env.qa(".mo-tile").length, 4);

  // preloaded favorite shows as on; toggling does not select the outfit
  assert.equal(env.q('[data-fav="winter_01"]').textContent, "★");
  env.click(env.q('[data-fav="casual_01"]'));
  assert.deepEqual(JSON.parse(env.storageSets.at(-1)[1]).sort(), ["casual_01", "winter_01"]);
  assert.equal(env.changes.length, 0);
  env.click(env.q('[data-role="fav-filter"]'));
  assert.deepEqual(env.qa(".mo-tile").map((t) => t.dataset.outfitId), ["casual_01", "winter_01"]);
  env.click(env.q('[data-fav="casual_01"]'));
  assert.deepEqual(env.qa(".mo-tile").map((t) => t.dataset.outfitId), ["winter_01"]);
});

test("random respects the filters", { skip }, async () => {
  let env = await ready({ random: () => 0 });
  env.click(env.action("random"));
  assert.equal(env.lastState().outfit, "casual_01");
  env = await ready({ random: () => 0.999 });
  env.click(env.action("random"));
  assert.equal(env.lastState().outfit, "winter_01");
  env = await ready({ random: () => 0 });
  env.click(env.q('[data-style="school"]'));
  env.click(env.action("random"));
  assert.equal(env.lastState().outfit, "school_01");
});

test("save as outfit: success, validation and failure", { skip }, async () => {
  const env = await ready();
  assert.ok(env.action("save").disabled);                      // nothing modified yet
  env.click(env.tile("school_01"));
  env.click(env.chip("top"));
  env.click(env.pick("top_hoodie"));
  await settle();

  env.click(env.action("save"));
  assert.ok(!env.q(".mo-save").hidden);
  assert.equal(env.q('[data-role="save-name"]').value, "Sailor uniform custom");

  env.q('[data-role="save-name"]').value = "   ";
  env.click(env.action("confirm-save"));
  await settle();
  assert.equal(env.q(".mo-error").textContent, "Give the outfit a name.");
  assert.equal(env.saves.length, 0);

  env.saveError = "too many saved outfits";
  env.q('[data-role="save-name"]').value = "My look";
  env.click(env.action("confirm-save"));
  await settle();
  assert.equal(env.q(".mo-error").textContent, "too many saved outfits");
  assert.ok(!env.q(".mo-save").hidden);                        // stays open so the user can retry

  env.saveError = null;
  env.click(env.action("confirm-save"));
  await settle();
  assert.equal(env.saves.length, 2);
  assert.deepEqual(env.saves[1], {
    label: "My look", style: ["school"], prefix: "",
    pieces: { top: "top_hoodie", bottom: "bottom_pleated_skirt", legwear: "legwear_kneesocks",
              footwear: "footwear_loafers", accessory: "accessory_ribbon" },
  });
  assert.equal(env.fetches.at(-1), true);                      // catalog was force-reloaded
  assert.deepEqual(env.lastState(), { v: 1, outfit: "user_my_look", overrides: {} });
  assert.equal(env.qa(".mo-tile").length, 5);
  assert.ok(env.tile("user_my_look").classList.contains("is-selected"));
  assert.ok(env.q(".mo-save").hidden);
  assert.match(env.q(".mo-status").textContent, /Saved/);
  assert.equal(env.title(), "My look");
  assert.ok(env.q('[data-del="user_my_look"]'));
  assert.equal(env.q('[data-del="school_01"]'), null);         // built-in outfits cannot be deleted
});

test("saving is disabled without a user directory", { skip }, async () => {
  const env = await ready({ canSave: false });
  env.click(env.tile("school_01"));
  env.click(env.chip("top"));
  env.click(env.pick("top_hoodie"));
  await settle();
  assert.ok(env.action("save").disabled);
  assert.match(env.action("save").title, /unavailable/);
});

test("deleting a saved outfit keeps the look as a custom set; cancel does nothing", { skip }, async () => {
  const env = await ready({ confirm: false, mutate: (c) => c.outfits.push({
    id: "user_mine", label: "Mine", style: [], pieces: { top: "top_tshirt", bottom: "bottom_jeans" },
    prefix: "", source: "user", tile_url: "/magik/outfit/asset/user/outfits/user_mine.webp" }) });
  env.click(env.tile("user_mine"));
  await settle();
  const before = env.changes.length;
  env.click(env.q('[data-del="user_mine"]'));
  await settle();
  assert.equal(env.deletes.length, 0);                          // confirm was declined
  assert.equal(env.changes.length, before);

  const env2 = await ready({ mutate: (c) => c.outfits.push({
    id: "user_mine", label: "Mine", style: [], pieces: { top: "top_tshirt", bottom: "bottom_jeans" },
    prefix: "", source: "user", tile_url: "/magik/outfit/asset/user/outfits/user_mine.webp" }) });
  env2.click(env2.tile("user_mine"));
  await settle();
  env2.click(env2.q('[data-del="user_mine"]'));
  await settle();
  assert.deepEqual(env2.deletes, ["user_mine"]);
  assert.equal(env2.qa(".mo-tile").length, 4);
  assert.deepEqual(env2.lastState(), { v: 1, outfit: null, overrides: { top: "top_tshirt", bottom: "bottom_jeans" } });
  assert.equal(env2.title(), "Custom set");
  assert.deepEqual(env2.lastRender(), env2.expectedDraw(null, { top: "top_tshirt", bottom: "bottom_jeans" }));
});

test("setState (workflow load) renders without emitting changes; missing outfit is reported", { skip }, async () => {
  const env = await ready();
  env.ui.setState('{"v":1,"outfit":"winter_01","overrides":{"top":"top_tshirt"}}');
  await settle();
  assert.equal(env.changes.length, 0);
  assert.equal(env.title(), "Custom (based on Winter layers)");
  assert.equal(env.string(), "t-shirt, jacket, open jacket, jeans, denim, boots");
  assert.deepEqual(env.lastRender(), env.expectedDraw("winter_01", { top: "top_tshirt" }));
  env.ui.setState('{"v":1,"outfit":"ghost","overrides":{}}');
  assert.equal(env.title(), "Outfit “ghost” not found");
  env.ui.setState("garbage");
  assert.equal(env.title(), "No outfit selected");
  assert.deepEqual(env.ui.getState(), { outfit: null, overrides: {}, text: '{"v":1,"outfit":null,"overrides":{}}' });
});

test("updateOptions refreshes the live string preview", { skip }, async () => {
  const env = await ready();
  env.click(env.tile("casual_01"));
  assert.equal(env.string(), "t-shirt, jeans, denim, sneakers");
  env.options = { format: "natural", separator: " | ", prefix: "wearing" };
  env.ui.updateOptions();
  assert.equal(env.string(), "wearing a t-shirt | jeans | white sneakers");
  env.options = { format: "both", separator: ", ", prefix: "" };
  env.ui.updateOptions();
  assert.equal(env.string(), "a t-shirt, jeans, white sneakers\nt-shirt, jeans, denim, sneakers");
});

test("Escape closes the picker and the save panel", { skip }, async () => {
  const env = await ready();
  env.click(env.chip("top"));
  assert.ok(!env.q(".mo-picker").hidden);
  env.q(".mo-picker").dispatchEvent(new env.window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  assert.ok(env.q(".mo-picker").hidden);
});

test("catalog load failure is reported and the UI stays inert", { skip }, async () => {
  const env = await ready({ fetchFails: true });
  assert.equal(env.title(), "Catalog unavailable");
  assert.match(env.q(".mo-status").textContent, /server said no/);
  assert.ok(env.q(".mo-status").classList.contains("is-error"));
  for (const a of ["reset", "clear", "save"]) assert.ok(env.action(a).disabled);
  env.ui.setState('{"v":1,"outfit":"school_01","overrides":{}}'); // must not throw
  env.click(env.action("random"));
  assert.equal(env.changes.length, 0);
});

test("a failing layer image is reported instead of breaking the node", { skip }, async () => {
  const env = await ready({ imageFails: (u) => u.includes("/pieces/top/blouse") });
  env.click(env.tile("school_01"));
  await settle();
  assert.match(env.q(".mo-status").textContent, /^Preview failed: boom/);
  assert.equal(env.title(), "Sailor uniform");                   // state and output are unaffected
  assert.equal(env.string(), SCHOOL_TAGS);
});

test("a slow stale render never paints over a newer selection", { skip }, async () => {
  const env = await ready({ imageDelay: (u) => (u.includes("/pieces/top/blouse") ? 40 : 0) });
  env.click(env.tile("school_01")); // blouse loads slowly
  env.click(env.tile("casual_01"));
  await tick(80);
  await settle();
  assert.deepEqual(env.lastRender(), env.expectedDraw("casual_01"));
  const all = env.ctxs.get(env.canvas).log.filter((e) => e.op === "draw").map((e) => e.url);
  assert.ok(!all.some((u) => u.includes("/pieces/top/blouse")), "stale render must not draw");
});

test("catalog text is rendered as text, never as HTML", { skip }, async () => {
  const evil = '<img src=x onerror="globalThis.pwned=1">';
  const env = await ready({ mutate: (c) => {
    c.outfits[0].label = evil;
    c.pieces.find((p) => p.id === "top_tshirt").label = evil;
  } });
  assert.equal(env.q(".mo-tile-label").textContent, evil);
  assert.equal(env.qa(".mo-tile img").length, 4);                 // only the four real tile images
  env.click(env.tile("casual_01"));
  await settle();
  assert.equal(env.title(), evil);
  assert.equal(env.chip("top").querySelector(".mo-chip-value").textContent, evil);
  assert.equal(env.qa("img").length, 4);
  assert.equal(env.window.pwned, undefined);
});

test("Reload re-fetches layer images instead of showing a stale cache (default loader)", { skip }, async () => {
  const dom = new JSDOM("<body></body>", { pretendToBeVisual: true });
  const { window } = dom;
  window.HTMLCanvasElement.prototype.getContext = () => ({ clearRect() {}, drawImage() {}, getImageData: () => ({ data: new Uint8ClampedArray(4) }) });
  const requested = [];
  window.Image = class {
    set src(v) { requested.push(v); queueMicrotask(() => { this.url = v; this.width = 512; this.height = 1024; this.onload?.(); }); }
  };
  const ui = createOutfitUI({ doc: window.document, fetchCatalog: async () => structuredClone(fixtures.catalog) });
  window.document.body.append(ui.element);
  await ui.ready;
  await settle();
  ui.element.querySelector('.mo-tile[data-outfit-id="school_01"]').dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  const blouse = "/magik/outfit/asset/builtin/pieces/top/blouse.webp";
  const count = () => requested.filter((u) => u === blouse).length;
  assert.equal(count(), 1);
  ui.element.querySelector('.mo-tile[data-outfit-id="casual_01"]').dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  ui.element.querySelector('.mo-tile[data-outfit-id="school_01"]').dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await settle();
  assert.equal(count(), 1, "a normal re-render uses the cache");
  await ui.reload(true);                                  // the art on disk changed; the user pressed Reload
  await settle();
  assert.equal(count(), 2, "Reload must fetch the layer again");
  await ui.reload(false);
  await settle();
  assert.equal(count(), 2, "a plain refresh keeps the cache");
});

test("image loader: caches, evicts least-recently-used, does not cache failures, clear() empties", { skip }, async () => {
  const loads = [];
  const fakeDoc = { defaultView: { Image: class {
    set src(v) { loads.push(v); queueMicrotask(() => { if (v.includes("bad")) this.onerror?.(); else { this.url = v; this.onload?.(); } }); }
  } } };
  const load = createImageLoader(fakeDoc, 3);
  await load("a"); await load("a");
  assert.deepEqual(loads, ["a"]);
  await load("b"); await load("c");
  await load("a");                       // a becomes the most recently used: order is now b, c, a
  await load("d");                       // over the limit of 3: evicts b
  assert.deepEqual(loads, ["a", "b", "c", "d"]);
  await load("a");                       // still cached
  assert.deepEqual(loads, ["a", "b", "c", "d"]);
  await load("b");                       // was evicted, so it loads again
  assert.deepEqual(loads, ["a", "b", "c", "d", "b"]);
  await assert.rejects(load("bad1"), /could not load bad1/);
  await assert.rejects(load("bad1"), /could not load bad1/);
  assert.equal(loads.filter((u) => u === "bad1").length, 2, "failures are retried, never cached");
  load.clear();
  const before = loads.length;
  await load("a");
  assert.equal(loads.length, before + 1, "clear() forgets everything");
});
