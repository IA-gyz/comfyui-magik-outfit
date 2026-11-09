// Magik Outfit: ComfyUI wiring. All picker logic lives in outfit_ui.js / outfit_logic.js.
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { createOutfitUI } from "./outfit_ui.js";

const NODE_ID = "Magik.Outfit";
const MIN_HEIGHT = 560;
const MIN_NODE_SIZE = [580, 780];
const OPTION_WIDGETS = ["format", "separator", "prefix"];

// ------------------------------------------------------------ stylesheet
function ensureCSS() {
  if (document.getElementById("magik-outfit-css")) return;
  const link = document.createElement("link");
  link.id = "magik-outfit-css";
  link.rel = "stylesheet";
  link.href = new URL("./magik_outfit.css", import.meta.url).href;
  document.head.appendChild(link);
}

// -------------------------------------------------------------- backend
let catalogPromise = null; // shared by every Magik Outfit node in the graph

async function errorText(res) {
  try {
    const data = await res.json();
    if (data && data.error) return data.error;
  } catch { /* not JSON */ }
  return `${res.status} ${res.statusText}`.trim();
}

function fetchCatalog(force = false) {
  if (!catalogPromise || force) {
    const p = (async () => {
      const res = await api.fetchApi("/magik/outfit/catalog", { cache: "no-store" });
      if (!res.ok) throw new Error(await errorText(res));
      return res.json();
    })();
    p.catch(() => { if (catalogPromise === p) catalogPromise = null; }); // allow a retry after a failure
    catalogPromise = p;
  }
  return catalogPromise;
}

async function postJSON(route, body) {
  const res = await api.fetchApi(route, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await errorText(res));
  return res.json();
}

async function getJSON(route) {
  const res = await api.fetchApi(route, { cache: "no-store" });
  if (!res.ok) throw new Error(await errorText(res));
  return res.json();
}

// ------------------------------------------------------------ node setup
function findWidget(node, name) {
  return node.widgets?.find((w) => w.name === name);
}

// The state widget only carries JSON for the backend; the picker replaces its UI.
function hideWidget(widget) {
  widget.hidden = true;                       // current frontend
  widget.computeSize = () => [0, -4];         // legacy layout
  widget.options = { ...(widget.options ?? {}), hidden: true };
}

function notifyChanged(node) {
  node.setDirtyCanvas?.(true, true);
  try {
    app.graph?.setDirtyCanvas?.(true, true);
    // lets undo/redo and the "unsaved changes" marker notice the programmatic edit
    app.extensionManager?.workflow?.activeWorkflow?.changeTracker?.checkState?.();
  } catch { /* best effort */ }
}

function safeStorage() {
  try { return window.localStorage; } catch { return null; }
}

function setupNode(node) {
  const stateWidget = findWidget(node, "state");
  if (!stateWidget || node._magikOutfit) return;
  hideWidget(stateWidget);
  ensureCSS();

  const optionValue = (name, fallback) => findWidget(node, name)?.value ?? fallback;
  const ui = createOutfitUI({
    fetchCatalog,
    saveOutfit: (payload) => postJSON("/magik/outfit/save_outfit", payload),
    deleteOutfit: (id) => postJSON("/magik/outfit/delete_outfit", { id }),
    assetURL: (url) => api.apiURL(url),
    storage: safeStorage(),
    getOptions: () => ({
      format: optionValue("format", "tags"),
      separator: optionValue("separator", ", "),
      prefix: optionValue("prefix", ""),
    }),
    getRandomSource: () => optionValue("random_source", "catalog"),
    fetchFavorites: () => getJSON("/magik/outfit/favorites"),
    saveFavorites: (payload) => postJSON("/magik/outfit/favorites", payload),
    fetchMyPresets: () => getJSON("/magik/outfit/mypresets"),
    saveMyPresets: (items) => postJSON("/magik/outfit/mypresets", { items }),
    uploadMyPresetCover: async (blob) => {
      const res = await api.fetchApi("/magik/outfit/mypresets/cover", { method: "POST", body: blob });
      if (!res.ok) throw new Error(await errorText(res));
      return (await res.json()).cover;
    },
    onChange: (text) => {
      stateWidget.value = text;
      stateWidget.callback?.(text);
      notifyChanged(node);
    },
  });

  const domWidget = node.addDOMWidget("outfit_picker", "magik_outfit", ui.element, {
    serialize: false,
    hideOnZoom: false,
    getValue: () => "",
    setValue: () => {},
    getMinHeight: () => MIN_HEIGHT,
  });
  domWidget.serialize = false; // never part of widgets_values

  // keep the live string preview in step with the text widgets
  for (const name of OPTION_WIDGETS) {
    const w = findWidget(node, name);
    if (!w) continue;
    const original = w.callback;
    w.callback = function (...args) {
      const result = original?.apply(this, args);
      ui.updateOptions();
      return result;
    };
  }

  const sync = () => ui.setState(stateWidget.value);
  const originalConfigure = node.onConfigure;
  node.onConfigure = function (...args) {
    const result = originalConfigure?.apply(this, args);
    sync(); // widget values are restored after the node is created
    return result;
  };

  node.setSize?.([Math.max(node.size?.[0] ?? 0, MIN_NODE_SIZE[0]), Math.max(node.size?.[1] ?? 0, MIN_NODE_SIZE[1])]);
  node._magikOutfit = { ui, sync };
  sync();
}

app.registerExtension({
  name: "Magik.Outfit",
  async setup() {
    // randomize-on-run: the backend drew a selection at queue time, show it in the picker
    api.addEventListener("magik.outfit_resolved", (ev) => {
      const nodes = ev.detail?.nodes || {};
      for (const [id, values] of Object.entries(nodes)) {
        const node = app.graph?.getNodeById?.(Number(id)) ?? app.graph?.getNodeById?.(id);
        if (!node?._magikOutfit || !values?.state) continue;
        const w = node.widgets?.find((x) => x.name === "state");
        if (w) {
          w.value = values.state;
          w.callback?.(values.state);
        }
        node._magikOutfit.sync();
        node.setDirtyCanvas?.(true, true);
      }
    });
  },
  nodeCreated(node) {
    if (node.comfyClass === NODE_ID) setupNode(node);
  },
});
