// Wiring tests: the real web/magik_outfit.js against a fake ComfyUI app/api and a fake node.
// Needs jsdom (npm i -D jsdom, or JSDOM_DIR=...).   Run: node --test tests/test_wiring.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { loadWebModules, loadJSDOM, loadFixtures } from "./_web_loader.mjs";

const JSDOM = await loadJSDOM();
const skip = JSDOM ? false : "jsdom not installed (npm i -D jsdom)";
const fixtures = loadFixtures();
const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));
const settle = async () => { await tick(); await tick(); await tick(); };

const FAKE_APP = `
export const app = {
  graph: { setDirtyCanvas() { globalThis.__fake.graphDirty++; } },
  extensionManager: { workflow: { activeWorkflow: { changeTracker: { checkState() { globalThis.__fake.checkState++; } } } } },
  registerExtension(ext) { globalThis.__fake.extensions.push(ext); },
};`;
const FAKE_API = `
export const api = {
  apiURL: (u) => "/prefix" + u,
  fetchApi: (route, opts) => globalThis.__fake.fetchApi(route, opts),
};`;

function jsonResponse(status, body) {
  return { ok: status >= 200 && status < 300, status, statusText: status === 200 ? "OK" : "Error", json: async () => body };
}

// Builds a fresh world: new jsdom, new module instances (so the shared catalog cache is per test).
async function makeWorld({ catalogStatus = 200 } = {}) {
  const dom = new JSDOM("<!doctype html><head></head><body></body>", { url: "http://localhost/", pretendToBeVisual: true });
  const { window } = dom;
  globalThis.document = window.document;
  globalThis.window = window;

  const ctxs = new WeakMap();
  window.HTMLCanvasElement.prototype.getContext = function () {
    if (!ctxs.has(this)) {
      const ctx = { log: [], globalAlpha: 1, clearRect() { ctx.log.push("clear"); },
        drawImage(img) { ctx.log.push(img.url); },
        getImageData(x, y, w, h) { const data = new Uint8ClampedArray(w * h * 4).fill(255); return { data }; } };
      ctxs.set(this, ctx);
    }
    return ctxs.get(this);
  };
  window.HTMLCanvasElement.prototype.getBoundingClientRect = () => ({ left: 0, top: 0, width: 256, height: 512 });
  window.Image = class {   // jsdom does not load images; resolve them like a browser would
    set src(value) {
      queueMicrotask(() => { this.url = value; this.width = 512; this.height = 1024; this.onload?.(); });
    }
  };

  const fake = {
    extensions: [], graphDirty: 0, checkState: 0, calls: [], catalog: structuredClone(fixtures.catalog), catalogStatus,
    fetchApi: async (route, opts = {}) => {
      fake.calls.push({ route, opts });
      if (route === "/magik/outfit/catalog") {
        return fake.catalogStatus === 200 ? jsonResponse(200, structuredClone(fake.catalog))
          : jsonResponse(fake.catalogStatus, { error: "catalog exploded" });
      }
      if (route === "/magik/outfit/save_outfit") {
        const body = JSON.parse(opts.body);
        if (fake.saveReject) return jsonResponse(400, { error: fake.saveReject });
        const outfit = { id: "user_saved", label: body.label, style: body.style, pieces: body.pieces, prefix: "",
          source: "user", tile_url: "/magik/outfit/asset/user/outfits/user_saved.webp" };
        fake.catalog.outfits.push(outfit);
        return jsonResponse(200, { outfit });
      }
      if (route === "/magik/outfit/delete_outfit") {
        const { id } = JSON.parse(opts.body);
        fake.catalog.outfits = fake.catalog.outfits.filter((o) => o.id !== id);
        return jsonResponse(200, { ok: true });
      }
      return jsonResponse(404, { error: "unknown route" });
    },
  };
  globalThis.__fake = fake;

  const mods = loadWebModules({ app: FAKE_APP, api: FAKE_API });
  await mods.import("magik_outfit.js");
  assert.equal(fake.extensions.length, 1, "extension should register itself on import");
  return { window, fake, ext: fake.extensions[0], ctxs };
}

function widget(name, value) {
  return { name, value, options: {}, callback: function (v) { this.calls = (this.calls ?? 0) + 1; this.lastArg = v; } };
}

function makeNode(comfyClass = "Magik.Outfit") {
  const node = {
    comfyClass, size: [400, 200], dirty: 0, configured: 0,
    widgets: [widget("state", '{"v":1,"outfit":null,"overrides":{}}'), widget("format", "tags"),
              widget("separator", ", "), widget("prefix", "")],
    setSize(s) { this.size = s; },
    setDirtyCanvas() { this.dirty++; },
    addDOMWidget(name, type, element, options) {
      const w = { name, type, element, options };
      this.widgets.push(w);
      return w;
    },
    onConfigure() { this.configured++; },
  };
  return node;
}

const q = (node, sel) => node.widgets.find((w) => w.name === "outfit_picker").element.querySelector(sel);
const qa = (node, sel) => [...node.widgets.find((w) => w.name === "outfit_picker").element.querySelectorAll(sel)];
const click = (world, el) => el.dispatchEvent(new world.window.MouseEvent("click", { bubbles: true }));

test("registers the extension and only touches Magik.Outfit nodes", { skip }, async () => {
  const world = await makeWorld();
  assert.equal(world.ext.name, "Magik.Outfit");
  const other = makeNode("KSampler");
  world.ext.nodeCreated(other);
  assert.equal(other.widgets.length, 4);
  assert.equal(other._magikOutfit, undefined);
});

test("node setup: hidden state widget, non-serialized DOM widget, size, stylesheet", { skip }, async () => {
  const world = await makeWorld();
  const node = makeNode();
  world.ext.nodeCreated(node);
  const state = node.widgets[0];
  assert.equal(state.hidden, true);
  assert.deepEqual(state.computeSize(), [0, -4]);
  assert.equal(state.options.hidden, true);

  const dom = node.widgets.at(-1);
  assert.deepEqual([dom.name, dom.type, dom.serialize, dom.options.serialize], ["outfit_picker", "magik_outfit", false, false]);
  assert.equal(dom.options.getMinHeight(), 560);
  assert.equal(dom.options.getValue(), "");
  assert.equal(dom.options.hideOnZoom, false);
  assert.deepEqual(node.size, [580, 780]);

  const link = world.window.document.getElementById("magik-outfit-css");
  assert.ok(link.href.endsWith("/extensions/Magik_Outfit/magik_outfit.css"), link.href);
  world.ext.nodeCreated(node);                       // idempotent
  assert.equal(node.widgets.filter((w) => w.name === "outfit_picker").length, 1);
  world.ext.nodeCreated(makeNode());                 // second node: still one stylesheet
  assert.equal(world.window.document.querySelectorAll("#magik-outfit-css").length, 1);
});

test("a saved larger node size is not shrunk", { skip }, async () => {
  const world = await makeWorld();
  const node = makeNode();
  node.size = [900, 1000];
  world.ext.nodeCreated(node);
  assert.deepEqual(node.size, [900, 1000]);
});

test("the catalog is fetched once for several nodes; assets go through api.apiURL", { skip }, async () => {
  const world = await makeWorld();
  const a = makeNode(), b = makeNode();
  world.ext.nodeCreated(a);
  world.ext.nodeCreated(b);
  await a._magikOutfit.ui.ready;
  await b._magikOutfit.ui.ready;
  await settle();
  const catalogCalls = world.fake.calls.filter((c) => c.route === "/magik/outfit/catalog");
  assert.equal(catalogCalls.length, 1);
  assert.deepEqual(catalogCalls[0].opts, { cache: "no-store" });
  const srcs = qa(a, ".mo-tile img").map((i) => i.getAttribute("src"));
  assert.equal(srcs.length, 4);
  assert.ok(srcs.every((s) => s.startsWith("/prefix/magik/outfit/asset/builtin/outfits/")), srcs.join());
});

test("picking writes the hidden state widget, fires its callback and marks the graph changed", { skip }, async () => {
  const world = await makeWorld();
  const node = makeNode();
  world.ext.nodeCreated(node);
  await node._magikOutfit.ui.ready;
  await settle();
  const state = node.widgets[0];
  const before = { dirty: node.dirty, graph: world.fake.graphDirty, check: world.fake.checkState };

  click(world, q(node, '.mo-tile[data-outfit-id="school_01"]'));
  await settle();
  assert.equal(state.value, '{"v":1,"outfit":"school_01","overrides":{}}');
  assert.equal(state.calls, 1);
  assert.equal(state.lastArg, state.value);
  assert.ok(node.dirty > before.dirty && world.fake.graphDirty > before.graph && world.fake.checkState > before.check);

  click(world, q(node, '.mo-chip[data-slot="top"]'));
  click(world, q(node, '.mo-pick[data-piece-id="top_hoodie"]'));
  await settle();
  assert.deepEqual(JSON.parse(state.value).overrides, { top: "top_hoodie" });
  assert.equal(state.calls, 2);
});

test("restoring a workflow: onConfigure re-syncs the UI from the state widget without writing it back", { skip }, async () => {
  const world = await makeWorld();
  const node = makeNode();
  world.ext.nodeCreated(node);
  await node._magikOutfit.ui.ready;
  await settle();
  const state = node.widgets[0];

  state.value = '{"v":1,"outfit":"winter_01","overrides":{"top":"top_tshirt"}}'; // as restored by LiteGraph
  node.onConfigure({});
  await settle();
  assert.equal(node.configured, 1, "the original onConfigure must still run");
  assert.equal(q(node, ".mo-title").textContent, "Custom (based on Winter layers)");
  assert.equal(state.calls ?? 0, 0, "loading must not write the widget back");
});

test("format / separator / prefix widgets keep the live string in step and keep their own callbacks", { skip }, async () => {
  const world = await makeWorld();
  const node = makeNode();
  world.ext.nodeCreated(node);
  await node._magikOutfit.ui.ready;
  await settle();
  click(world, q(node, '.mo-tile[data-outfit-id="casual_01"]'));
  assert.equal(q(node, ".mo-string").textContent, "t-shirt, jeans, denim, sneakers");

  const [, format, separator, prefix] = node.widgets;
  format.value = "natural"; separator.value = " | "; prefix.value = "wearing";
  format.callback("natural");
  assert.equal(q(node, ".mo-string").textContent, "wearing a t-shirt | jeans | white sneakers");
  assert.equal(format.calls, 1, "the widget's original callback must still run");
  prefix.value = "";
  prefix.callback("");
  assert.equal(q(node, ".mo-string").textContent, "a t-shirt | jeans | white sneakers");
});

test("save and delete POST JSON through api.fetchApi; server errors reach the user", { skip }, async () => {
  const world = await makeWorld();
  const node = makeNode();
  world.ext.nodeCreated(node);
  await node._magikOutfit.ui.ready;
  await settle();
  click(world, q(node, '.mo-tile[data-outfit-id="school_01"]'));
  click(world, q(node, '.mo-chip[data-slot="top"]'));
  click(world, q(node, '.mo-pick[data-piece-id="top_hoodie"]'));
  await settle();

  world.fake.saveReject = "too many saved outfits";
  click(world, q(node, '[data-action="save"]'));
  q(node, '[data-role="save-name"]').value = "Cozy";
  click(world, q(node, '[data-action="confirm-save"]'));
  await settle();
  assert.equal(q(node, ".mo-error").textContent, "too many saved outfits");

  world.fake.saveReject = null;
  click(world, q(node, '[data-action="confirm-save"]'));
  await settle();
  const saves = world.fake.calls.filter((c) => c.route === "/magik/outfit/save_outfit");
  assert.equal(saves.length, 2);
  assert.equal(saves[1].opts.method, "POST");
  assert.deepEqual(saves[1].opts.headers, { "Content-Type": "application/json" });
  assert.deepEqual(JSON.parse(saves[1].opts.body), {
    label: "Cozy", style: ["school"], prefix: "",
    pieces: { top: "top_hoodie", bottom: "bottom_pleated_skirt", legwear: "legwear_kneesocks",
              footwear: "footwear_loafers", accessory: "accessory_ribbon" },
  });
  assert.equal(JSON.parse(node.widgets[0].value).outfit, "user_saved");
  assert.equal(world.fake.calls.filter((c) => c.route === "/magik/outfit/catalog").length, 2); // forced reload after saving

  const del = q(node, '[data-del="user_saved"]');
  assert.ok(del);
  world.window.confirm = () => true; // the UI's default confirm uses the jsdom window
  click(world, del);
  await settle();
  const deletes = world.fake.calls.filter((c) => c.route === "/magik/outfit/delete_outfit");
  assert.equal(deletes.length, 1);
  assert.deepEqual(JSON.parse(deletes[0].opts.body), { id: "user_saved" });
  assert.equal(qa(node, ".mo-tile").length, 4);
});

test("a failed catalog fetch is reported, and Reload retries instead of caching the failure", { skip }, async () => {
  const world = await makeWorld({ catalogStatus: 500 });
  const node = makeNode();
  world.ext.nodeCreated(node);
  await node._magikOutfit.ui.ready;
  await settle();
  assert.equal(q(node, ".mo-title").textContent, "Catalog unavailable");
  assert.match(q(node, ".mo-status").textContent, /catalog exploded/);

  world.fake.catalogStatus = 200;
  click(world, q(node, '[data-action="reload"]'));
  await settle();
  assert.equal(qa(node, ".mo-tile").length, 4);
  assert.equal(q(node, ".mo-title").textContent, "No outfit selected");
  assert.equal(world.fake.calls.filter((c) => c.route === "/magik/outfit/catalog").length, 2);
});
