// Gallery popup tests under jsdom. Setup:  npm i -D jsdom  (or JSDOM_DIR=...)
// Run:  node --test tests/test_gallery.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadWebModules, loadJSDOM, loadFixtures } from "./_web_loader.mjs";

const JSDOM = await loadJSDOM();
const skip = JSDOM ? false : "jsdom not installed (npm i -D jsdom)";
const web = loadWebModules();
const L = await web.import("outfit_logic.js");
const { openGallery } = await web.import("gallery_popup.js");
const { buildAdapters } = await web.import("gallery_adapters.js");
const fixtures = loadFixtures();

const tick = () => new Promise((r) => setTimeout(r, 0));

function makeEnv() {
  const dom = new JSDOM("<!doctype html><body></body>", { pretendToBeVisual: true });
  const doc = dom.window.document;
  const catalog = structuredClone(fixtures.catalog);
  const idx = L.indexCatalog(catalog);
  const applied = [];
  const favs = new Set();
  let groups = [{ id: "default", name: "Default Favorites", isSystem: true }];
  const favStore = {
    isFav: (k) => favs.has(k),
    toggle: (k) => { favs.has(k) ? favs.delete(k) : favs.add(k); },
    groups: () => groups,
    groupIdsOf: () => ["default"],
    setGroups: () => {},
    addGroup: (name) => { const id = "g" + groups.length; groups = [...groups, { id, name }]; return id; },
  };
  const env = {
    doc, applied, favs, favStore,
    adapters: buildAdapters({
      catalog, idx, myPresets: [], assetURL: (u) => u,
      getOutfitId: () => null,
      getLastPresetKey: () => null,
      applyOutfit: (id) => applied.push(["outfit", id]),
      applyPresetItem: (p, key) => applied.push([key, p.id]),
      removeOutfit: () => {},
      removeMyPreset: () => {},
      confirmFn: () => true,
    }),
  };
  env.q = (sel) => doc.querySelector(sel);
  env.qa = (sel) => [...doc.querySelectorAll(sel)];
  env.click = (node) => node.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  env.open = () => { env.popup = openGallery({ doc, adapters: env.adapters, favStore, random: () => 0.5 }); return env.popup; };
  return env;
}

test("gallery opens with adapter tabs and a paged card grid", { skip }, () => {
  const env = makeEnv();
  env.open();
  assert.ok(env.q(".mg-overlay"), "overlay in DOM");
  assert.deepEqual(env.qa(".mg-tab").map((t) => t.dataset.tab), ["outfit", "preset"]); // mypreset empty -> hidden
  assert.equal(env.qa(".mg-card").length, 4); // the 4 catalog outfits
  assert.ok(env.q(".mg-pager").hidden, "no pager for a single page");
  assert.equal(env.q('[data-role="mg-apply"]').disabled, true);
});

test("search filters cards; clicking a card selects it and Apply commits + closes", { skip }, async () => {
  const env = makeEnv();
  env.open();
  const search = env.q('[data-role="mg-search"]');
  search.value = "sailor";
  search.dispatchEvent(new env.doc.defaultView.Event("input", { bubbles: true }));
  await tick();
  assert.equal(env.qa(".mg-card").length, 1);
  assert.equal(env.qa(".mg-card")[0].dataset.key, "school_01");
  env.click(env.qa(".mg-card")[0]);
  assert.ok(env.q(".mg-card.is-selected"));
  assert.equal(env.q('[data-role="mg-apply"]').disabled, false);
  env.click(env.q('[data-role="mg-apply"]'));
  assert.deepEqual(env.applied, [["outfit", "school_01"]]);
  assert.ok(!env.q(".mg-overlay"), "popup closed after apply");
});

test("favorites rail + tab badge, star toggle", { skip }, () => {
  const env = makeEnv();
  env.open();
  env.click(env.qa(".mg-card")[0].querySelector(".mg-fav")); // favorite casual_01
  assert.ok(env.favs.has("casual_01"));
  assert.ok(env.q('[data-tab="outfit"]').textContent.includes("★"));
  env.click(env.q('[data-rail="fav"]'));
  assert.equal(env.qa(".mg-card").length, 1);
  assert.equal(env.qa(".mg-card")[0].dataset.key, "casual_01");
});

test("detail panel: tags, facts, favorite checkbox, group add, Select applies", { skip }, () => {
  const env = makeEnv();
  env.open();
  env.click(env.qa(".mg-card")[3].querySelector(".mg-info")); // winter_01 details
  const detail = env.q('[data-role="mg-detail"]');
  assert.equal(detail.hidden, false);
  const tags = env.q('[data-role="mg-tags"]').textContent;
  assert.match(tags, /hoodie/);
  assert.ok(env.qa(".mg-fact").length >= 3);
  // favorite via the detail checkbox
  const favToggle = detail.querySelector('[data-role="mg-fav-toggle"]');
  favToggle.checked = true;
  favToggle.dispatchEvent(new env.doc.defaultView.Event("change", { bubbles: true }));
  assert.ok(env.favs.has("winter_01"));
  // add a group via the detail panel
  const input = detail.querySelector('[data-role="mg-newgroup"]');
  input.value = "Cozy";
  env.click(detail.querySelector('[data-role="mg-addgroup"]'));
  assert.ok(env.favStore.groups().some((g) => g.name === "Cozy"));
  // Select applies and closes
  env.click(detail.querySelector('[data-role="mg-detail-apply"]'));
  assert.deepEqual(env.applied, [["outfit", "winter_01"]]);
  assert.ok(!env.q(".mg-overlay"));
});

test("presets tab pages 430 items 24 per page and filters by category", { skip }, () => {
  const env = makeEnv();
  env.open();
  env.click(env.q('[data-tab="preset"]'));
  assert.equal(env.qa(".mg-card").length, 24);
  assert.equal(env.q(".mg-page-label").textContent, "1 / 18");
  env.click(env.q('[data-role="mg-next"]'));
  assert.equal(env.q(".mg-page-label").textContent, "2 / 18");
  assert.equal(env.qa(".mg-card").length, 24);
  // filter panel with category counts
  env.click(env.q('[data-role="mg-filters-toggle"]'));
  const catChips = env.qa('[data-filter="category"]');
  assert.ok(catChips.length >= 5);
  env.click(catChips[0]);
  const total = parseInt(env.q(".mg-title").textContent.split("·")[1], 10);
  assert.ok(total > 0 && total < 430);
});

test("Esc closes without applying", { skip }, () => {
  const env = makeEnv();
  env.open();
  env.click(env.qa(".mg-card")[0]);
  env.doc.dispatchEvent(new env.doc.defaultView.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  assert.ok(!env.q(".mg-overlay"));
  assert.deepEqual(env.applied, []);
});
