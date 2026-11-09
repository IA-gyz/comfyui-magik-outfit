// Magik Outfit: picker UI (plain DOM + canvas, no ComfyUI imports).
// magik_outfit.js wires it into the node; tests drive it under jsdom with mocked deps.

import {
  attireToSlotMap, buildString, collectStyles, drawAttire, drawList, filterOutfits, indexCatalog,
  isModified, parseState, pickPiece, resolve, serializeState,
} from "./outfit_logic.js";

const FAV_KEY = "magik_outfit.favorites";
const ALPHA_CACHE_MAX = 24;
const SLOT_LABELS = { onepiece: "One-piece" };

export function slotLabel(slot) {
  if (Object.prototype.hasOwnProperty.call(SLOT_LABELS, slot)) return SLOT_LABELS[slot];
  return slot.charAt(0).toUpperCase() + slot.slice(1);
}

function el(doc, tag, props = {}, children = []) {
  const node = doc.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2).toLowerCase(), v);
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of [].concat(children)) if (c) node.append(c);
  return node;
}

// Loads each URL once and remembers it, but only the most recently used `limit` images (a big catalog
// would otherwise keep every decoded layer alive). clear() forgets everything, e.g. after the art changed.
export function createImageLoader(doc, limit = 64) {
  const cache = new Map(); // url -> Promise<Image>, least recently used first
  const load = (url) => {
    if (cache.has(url)) {
      const hit = cache.get(url);
      cache.delete(url);
      cache.set(url, hit);
      return hit;
    }
    const promise = new Promise((resolveImg, reject) => {
      const img = new doc.defaultView.Image();
      img.decoding = "async";
      img.onload = () => resolveImg(img);
      img.onerror = () => { if (cache.get(url) === promise) cache.delete(url); reject(new Error(`could not load ${url}`)); };
      img.src = url;
    });
    cache.set(url, promise);
    if (cache.size > limit) cache.delete(cache.keys().next().value);
    return promise;
  };
  load.clear = () => cache.clear();
  return load;
}

export function createOutfitUI(deps) {
  const {
    fetchCatalog, saveOutfit, deleteOutfit,
    assetURL = (u) => u,
    onChange = () => {},
    getOptions = () => ({ format: "tags", separator: ", ", prefix: "" }),
    getRandomSource = () => "catalog",
    doc = document,
    storage = null,
    random = Math.random,
    confirmFn = (msg) => doc.defaultView?.confirm?.(msg) ?? true,
  } = deps;
  const loadImage = deps.loadImage ?? createImageLoader(doc);

  // ------------------------------------------------------------- state
  let catalog = null;
  let idx = null;
  let outfitId = null;
  let overrides = {};
  let query = "";
  let styleFilter = null;
  let favOnly = false;
  let openSlot = null;
  let hoverSlot = null;
  let renderToken = 0;
  let loadFailed = false;
  let items = [];                 // last drawn layers (for hit-testing)
  const loaded = new Map();       // url -> image
  const alphaCache = new Map();   // url -> Uint8Array | null
  const chipEls = new Map();      // slot -> chip element
  let favorites = new Set();
  try {
    const raw = storage?.getItem(FAV_KEY);
    if (raw) favorites = new Set(JSON.parse(raw).filter((x) => typeof x === "string"));
  } catch { /* ignore broken storage */ }

  function current() {
    const outfit = idx ? idx.outfits.get(outfitId) ?? null : null;
    const slotMap = idx ? resolve(outfit, overrides, idx.pieces) : {};
    return { outfit, slotMap, modified: idx ? isModified(outfit, slotMap) : false };
  }

  // --------------------------------------------------------------- DOM
  const root = el(doc, "div", { class: "mo-root", tabindex: "-1" });
  const titleEl = el(doc, "span", { class: "mo-title" });
  const btnRandom = el(doc, "button", { class: "mo-btn", type: "button", "data-action": "random", text: "Random", title: "Pick a random outfit (respects the filters below)", onClick: randomOutfit });
  const btnReset = el(doc, "button", { class: "mo-btn", type: "button", "data-action": "reset", text: "Reset", title: "Back to the outfit as it was", onClick: () => { const { outfit } = current(); if (outfit) commit(outfit.id, {}); } });
  const btnClear = el(doc, "button", { class: "mo-btn", type: "button", "data-action": "clear", text: "Clear", title: "Remove every piece", onClick: () => commit(null, {}) });
  const btnSave = el(doc, "button", { class: "mo-btn", type: "button", "data-action": "save", text: "Save as outfit…", onClick: openSave });
  const btnReload = el(doc, "button", { class: "mo-btn mo-btn-icon", type: "button", "data-action": "reload", text: "↻", title: "Reload catalog", "aria-label": "Reload catalog", onClick: () => loadCatalog(true) });
  const header = el(doc, "div", { class: "mo-header" }, [titleEl, el(doc, "span", { class: "mo-spacer" }), btnRandom, btnReset, btnClear, btnSave, btnReload]);

  const canvas = el(doc, "canvas", { class: "mo-canvas", width: "256", height: "512" });
  const hint = el(doc, "div", { class: "mo-hint", text: "" });
  const stage = el(doc, "div", { class: "mo-stage" }, [canvas, hint]);
  const chipsEl = el(doc, "div", { class: "mo-chips" });
  const stringEl = el(doc, "pre", { class: "mo-string", title: "Output of this node" });
  const side = el(doc, "div", { class: "mo-side" }, [chipsEl, stringEl]);
  const main = el(doc, "div", { class: "mo-main" }, [stage, side]);

  const pickerTitle = el(doc, "strong", { class: "mo-picker-title" });
  const pickerGrid = el(doc, "div", { class: "mo-picker-grid" });
  const picker = el(doc, "div", { class: "mo-panel mo-picker", hidden: true }, [
    el(doc, "div", { class: "mo-panel-head" }, [pickerTitle, el(doc, "span", { class: "mo-spacer" }),
      el(doc, "button", { class: "mo-btn mo-btn-icon", type: "button", "data-action": "close-picker", text: "×", "aria-label": "Close", onClick: closePicker })]),
    pickerGrid,
  ]);

  const saveInput = el(doc, "input", { class: "mo-input", type: "text", maxlength: "60", placeholder: "Outfit name", "data-role": "save-name" });
  const saveError = el(doc, "div", { class: "mo-error" });
  const savePanel = el(doc, "div", { class: "mo-panel mo-save", hidden: true }, [
    el(doc, "div", { class: "mo-save-row" }, [saveInput,
      el(doc, "button", { class: "mo-btn", type: "button", "data-action": "confirm-save", text: "Save", onClick: doSave }),
      el(doc, "button", { class: "mo-btn", type: "button", "data-action": "cancel-save", text: "Cancel", onClick: closeSave })]),
    saveError,
  ]);
  saveInput.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); doSave(); } });

  const searchEl = el(doc, "input", { class: "mo-input mo-search", type: "search", placeholder: "Search outfits…", "data-role": "search", onInput: () => { query = searchEl.value; renderGrid(); } });
  const favBtn = el(doc, "button", { class: "mo-chipbtn", type: "button", "data-role": "fav-filter", text: "★ Favorites", "aria-pressed": "false", onClick: () => { favOnly = !favOnly; favBtn.classList.toggle("is-on", favOnly); favBtn.setAttribute("aria-pressed", String(favOnly)); renderGrid(); } });
  const stylesEl = el(doc, "div", { class: "mo-styles" });
  const toolbar = el(doc, "div", { class: "mo-toolbar" }, [searchEl, favBtn, stylesEl]);
  const gridEl = el(doc, "div", { class: "mo-grid" });
  const statusEl = el(doc, "div", { class: "mo-status" });
  const browser = el(doc, "div", { class: "mo-browser" }, [toolbar, gridEl]);

  root.append(header, main, picker, savePanel, browser, statusEl);
  root.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (openSlot) closePicker();
    else if (!savePanel.hidden) closeSave();
  });
  // keep wheel scrolling inside our lists instead of zooming the graph canvas
  for (const scroller of [root, gridEl, pickerGrid, stringEl, chipsEl]) {
    scroller.addEventListener("wheel", (e) => {
      if (scroller.scrollHeight > scroller.clientHeight) e.stopPropagation();
    }, { passive: true });
  }

  // ------------------------------------------------------ state changes
  function commit(newOutfitId, newOverrides) {
    outfitId = newOutfitId;
    overrides = newOverrides;
    update();
    onChange(serializeState(outfitId, overrides));
  }

  function setPiece(slot, pieceId) {
    if (!idx) return;
    const { outfit } = current();
    commit(outfit ? outfit.id : null, pickPiece(outfit, overrides, slot, pieceId, idx.pieces));
  }

  function selectOutfit(id) { commit(id, {}); }

  function randomOutfit() {
    if (!catalog) return;
    const source = getRandomSource();
    const useAttire = source === "attire" || (source === "any" && random() < 0.5);
    if (useAttire && catalog.attire?.slots) {
      const drawn = drawAttire(catalog.attire, random);
      if (Object.keys(drawn).length) {
        commit(null, attireToSlotMap(drawn, idx.pieces, random));
        return;
      }
    }
    const list = filterOutfits(catalog.outfits, { query, style: styleFilter, favorites: favOnly ? favorites : null });
    if (list.length) selectOutfit(list[Math.floor(random() * list.length)].id);
  }

  // ---------------------------------------------------------- rendering
  function setStatus(text, kind = "") {
    statusEl.textContent = text;
    statusEl.className = "mo-status" + (kind ? ` is-${kind}` : "");
  }

  function renderHeader() {
    if (!idx) {
      titleEl.textContent = loadFailed ? "Catalog unavailable" : "Loading…";
      btnReset.disabled = btnClear.disabled = btnSave.disabled = true;
      return;
    }
    const { outfit, slotMap, modified } = current();
    const count = Object.keys(slotMap).length;
    const hasRaw = Object.values(slotMap).some((v) => v !== null && typeof v === "object");
    if (outfit) titleEl.textContent = modified ? `Custom (based on ${outfit.label})` : outfit.label;
    else if (outfitId) titleEl.textContent = `Outfit “${outfitId}” not found`;
    else titleEl.textContent = count ? "Custom set" : "No outfit selected";
    btnReset.disabled = !(outfit && modified);
    btnClear.disabled = count === 0;
    btnSave.disabled = !(modified && count > 0 && catalog.can_save) || hasRaw;
    btnSave.title = hasRaw ? "Tag-only slots have no art to bake; remove them before saving"
      : catalog.can_save ? "Keep this custom set as a new outfit tile"
      : "Saving is unavailable (no user directory)";
    btnRandom.disabled = false;
  }

  function renderChips() {
    chipsEl.replaceChildren();
    chipEls.clear();
    if (!idx) return;
    const { outfit, slotMap } = current();
    for (const slot of idx.slotOrder) {
      if (!idx.bySlot.has(slot)) continue;
      const value = slotMap[slot];
      const piece = typeof value === "string" ? idx.pieces.get(value) : null;
      const rawTags = value && typeof value === "object" ? value.tags : "";
      const changed = !!outfit && (typeof value === "object" || (value ?? null) !== (outfit.pieces[slot] ?? null));
      const row = el(doc, "div", {
        class: "mo-chip" + (piece || rawTags ? "" : " is-empty") + (rawTags ? " is-raw" : "") + (changed ? " is-changed" : ""),
        "data-slot": slot, role: "button", tabindex: "0",
        title: `Change ${slotLabel(slot).toLowerCase()}`,
        onClick: () => openPicker(slot),
        onKeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openPicker(slot); } },
      }, [
        el(doc, "span", { class: "mo-chip-slot", text: slotLabel(slot) }),
        el(doc, "span", { class: "mo-chip-value", text: piece ? piece.label : (rawTags || "—") }),
      ]);
      if (piece || rawTags) {
        row.append(el(doc, "button", {
          class: "mo-chip-clear", type: "button", "data-clear": slot, text: "×",
          title: `Remove ${slotLabel(slot).toLowerCase()}`, "aria-label": `Remove ${slotLabel(slot)}`,
          onClick: (e) => { e.stopPropagation(); setPiece(slot, null); },
        }));
      }
      if (hoverSlot === slot) row.classList.add("is-hover");
      chipEls.set(slot, row);
      chipsEl.append(row);
    }
  }

  function renderString() {
    if (!idx) { stringEl.textContent = ""; return; }
    const { outfit, slotMap, modified } = current();
    const o = getOptions();
    const text = buildString(slotMap, outfit, modified, catalog, idx.pieces, o.format, o.separator, o.prefix);
    stringEl.textContent = text || "(nothing selected)";
    stringEl.classList.toggle("is-empty", !text);
  }

  async function renderPreview() {
    const token = ++renderToken;
    if (!idx) return;
    const { slotMap } = current();
    const list = drawList(catalog, idx.pieces, slotMap);
    let images;
    try {
      images = await Promise.all(list.map((it) => loadImage(assetURL(it.url))));
    } catch (err) {
      if (token === renderToken) setStatus(`Preview failed: ${err.message}`, "error");
      return;
    }
    if (token !== renderToken) return; // a newer render superseded this one
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    loaded.clear();
    list.forEach((it, i) => {
      ctx.drawImage(images[i], 0, 0, canvas.width, canvas.height);
      loaded.set(it.url, images[i]);
    });
    items = list;
  }

  function renderStyles() {
    stylesEl.replaceChildren();
    if (!catalog) return;
    for (const s of collectStyles(catalog.outfits)) {
      stylesEl.append(el(doc, "button", {
        class: "mo-chipbtn" + (styleFilter === s ? " is-on" : ""), type: "button", "data-style": s,
        text: s, "aria-pressed": String(styleFilter === s),
        onClick: () => { styleFilter = styleFilter === s ? null : s; renderStyles(); renderGrid(); },
      }));
    }
  }

  function updateTileSelection() {
    const { outfit } = current();
    for (const t of gridEl.querySelectorAll(".mo-tile")) {
      const on = !!outfit && t.getAttribute("data-outfit-id") === outfit.id;
      t.classList.toggle("is-selected", on);
      t.setAttribute("aria-pressed", String(on));
    }
  }

  function renderGrid() {
    gridEl.replaceChildren();
    if (!catalog) return;
    const list = filterOutfits(catalog.outfits, { query, style: styleFilter, favorites: favOnly ? favorites : null });
    if (!list.length) {
      gridEl.append(el(doc, "div", { class: "mo-empty", text: catalog.outfits.length ? "No outfits match." : "No outfits yet." }));
      return;
    }
    for (const o of list) gridEl.append(makeTile(o));
    updateTileSelection();
  }

  function makeTile(o) {
    const isFav = favorites.has(o.id);
    const tile = el(doc, "div", {
      class: "mo-tile", "data-outfit-id": o.id, role: "button", tabindex: "0", title: o.label,
      onClick: () => selectOutfit(o.id),
      onKeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectOutfit(o.id); } },
    }, [
      o.tile_url
        ? el(doc, "img", { src: assetURL(o.tile_url), alt: o.label, loading: "lazy", decoding: "async", draggable: "false" })
        : el(doc, "div", { class: "mo-tile-noimg" }),
      el(doc, "span", { class: "mo-tile-label", text: o.label }),
      el(doc, "button", {
        class: "mo-fav" + (isFav ? " is-on" : ""), type: "button", "data-fav": o.id, text: isFav ? "★" : "☆",
        title: isFav ? "Remove from favorites" : "Add to favorites", "aria-pressed": String(isFav),
        onClick: (e) => { e.stopPropagation(); toggleFavorite(o.id); },
      }),
    ]);
    if (o.source === "user") {
      tile.append(el(doc, "button", {
        class: "mo-del", type: "button", "data-del": o.id, text: "×", title: "Delete this saved outfit",
        "aria-label": `Delete ${o.label}`, onClick: (e) => { e.stopPropagation(); removeOutfit(o); },
      }));
    }
    return tile;
  }

  function toggleFavorite(id) {
    if (favorites.has(id)) favorites.delete(id); else favorites.add(id);
    try { storage?.setItem(FAV_KEY, JSON.stringify([...favorites])); } catch { /* storage may be unavailable */ }
    renderGrid();
  }

  function update() {
    renderHeader();
    renderChips();
    renderString();
    updateTileSelection();
    if (openSlot) renderPicker();
    renderPreview();
  }

  // ------------------------------------------------------- piece picker
  function openPicker(slot) {
    if (!idx || !idx.bySlot.has(slot)) return;
    closeSave();
    openSlot = slot;
    picker.hidden = false;
    renderPicker();
  }

  function closePicker() {
    openSlot = null;
    picker.hidden = true;
    pickerGrid.replaceChildren();
  }

  function renderPicker() {
    if (!openSlot || !idx) return;
    const slot = openSlot;
    const { slotMap } = current();
    pickerTitle.textContent = `Choose ${slotLabel(slot).toLowerCase()}`;
    pickerGrid.replaceChildren();
    const none = el(doc, "div", {
      class: "mo-pick is-none" + (slotMap[slot] ? "" : " is-selected"), "data-slot-pick": slot, "data-piece-id": "",
      role: "button", tabindex: "0", title: `No ${slotLabel(slot).toLowerCase()}`,
      onClick: () => { setPiece(slot, null); closePicker(); },
    }, [el(doc, "div", { class: "mo-pick-none", text: "∅" }), el(doc, "span", { class: "mo-pick-label", text: "None" })]);
    pickerGrid.append(none);
    for (const piece of idx.bySlot.get(slot)) {
      const thumb = el(doc, "canvas", { class: "mo-pick-thumb", width: "80", height: "100" });
      const replaces = [...new Set([...piece.conflicts, ...piece.hides])].map((s) => slotLabel(s).toLowerCase());
      const tile = el(doc, "div", {
        class: "mo-pick" + (slotMap[slot] === piece.id ? " is-selected" : ""), "data-piece-id": piece.id,
        role: "button", tabindex: "0", title: replaces.length ? `${piece.label} (affects ${replaces.join(", ")})` : piece.label,
        onClick: () => { setPiece(slot, piece.id); closePicker(); },
        onKeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setPiece(slot, piece.id); closePicker(); } },
      }, [thumb, el(doc, "span", { class: "mo-pick-label", text: piece.label })]);
      if (replaces.length) tile.append(el(doc, "span", { class: "mo-pick-note", text: `replaces ${replaces.join(", ")}` }));
      pickerGrid.append(tile);
      drawThumb(thumb, piece);
    }
  }

  async function drawThumb(thumb, piece) {
    try {
      const [base, layer] = await Promise.all([loadImage(assetURL(catalog.base_url)), loadImage(assetURL(piece.layer_url))]);
      if (!thumb.isConnected) return;
      const [cw, ch] = catalog.canvas;
      const pad = 16;
      const b = piece.bbox ?? [0, 0, cw, ch];
      const sx = Math.max(0, b[0] - pad), sy = Math.max(0, b[1] - pad);
      const sw = Math.min(cw, b[2] + pad) - sx, sh = Math.min(ch, b[3] + pad) - sy;
      const k = Math.min(thumb.width / sw, thumb.height / sh);
      const dw = sw * k, dh = sh * k, dx = (thumb.width - dw) / 2, dy = (thumb.height - dh) / 2;
      const ctx = thumb.getContext("2d");
      ctx.clearRect(0, 0, thumb.width, thumb.height);
      // base and layer share the canvas size, so they share the same source rectangle
      const rx = base.width / cw, ry = base.height / ch;
      ctx.globalAlpha = 0.35;
      ctx.drawImage(base, sx * rx, sy * ry, sw * rx, sh * ry, dx, dy, dw, dh);
      ctx.globalAlpha = 1;
      const lx = layer.width / cw, ly = layer.height / ch;
      ctx.drawImage(layer, sx * lx, sy * ly, sw * lx, sh * ly, dx, dy, dw, dh);
    } catch { /* leave the thumbnail blank; the label still works */ }
  }

  // ------------------------------------------------- preview hit testing
  function alphaAt(url, x, y) {
    const img = loaded.get(url);
    if (!img) return 255;
    let data = alphaCache.get(url);
    if (data === undefined) {
      try {
        const [cw, ch] = catalog.canvas;
        const c = doc.createElement("canvas");
        c.width = cw; c.height = ch;
        const cx = c.getContext("2d");
        cx.drawImage(img, 0, 0, cw, ch);
        const px = cx.getImageData(0, 0, cw, ch).data;
        data = new Uint8Array(cw * ch);
        for (let i = 0; i < data.length; i++) data[i] = px[i * 4 + 3];
      } catch { data = null; }
      alphaCache.set(url, data);
      if (alphaCache.size > ALPHA_CACHE_MAX) alphaCache.delete(alphaCache.keys().next().value);
    }
    if (!data) return 255;
    return data[Math.floor(y) * catalog.canvas[0] + Math.floor(x)] ?? 0;
  }

  function pieceAt(ev) {
    if (!catalog) return null;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    const x = ((ev.clientX - rect.left) / rect.width) * catalog.canvas[0];
    const y = ((ev.clientY - rect.top) / rect.height) * catalog.canvas[1];
    for (let i = items.length - 1; i >= 0; i--) {
      const { piece, url } = items[i];
      if (!piece) continue;
      const b = piece.bbox;
      if (b && (x < b[0] || x >= b[2] || y < b[1] || y >= b[3])) continue;
      if (alphaAt(url, x, y) > 16) return piece;
    }
    return null;
  }

  function setHover(slot) {
    if (slot === hoverSlot) return;
    chipEls.get(hoverSlot)?.classList.remove("is-hover");
    hoverSlot = slot;
    chipEls.get(slot)?.classList.add("is-hover");
  }

  canvas.addEventListener("pointermove", (ev) => {
    const piece = pieceAt(ev);
    setHover(piece ? piece.slot : null);
    canvas.classList.toggle("is-hot", !!piece);
    hint.textContent = piece ? `${slotLabel(piece.slot)}: ${piece.label}. Click to change` : "";
  });
  canvas.addEventListener("pointerleave", () => { setHover(null); canvas.classList.remove("is-hot"); hint.textContent = ""; });
  canvas.addEventListener("click", (ev) => { const piece = pieceAt(ev); if (piece) openPicker(piece.slot); });

  // ------------------------------------------------------------ saving
  function openSave() {
    const { outfit, modified } = current();
    if (!idx || !modified || !catalog.can_save) return;
    closePicker();
    savePanel.hidden = false;
    saveError.textContent = "";
    saveInput.value = outfit ? `${outfit.label} custom`.slice(0, 60) : "My outfit";
    saveInput.focus?.();
    saveInput.select?.();
  }

  function closeSave() { savePanel.hidden = true; saveError.textContent = ""; }

  async function doSave() {
    const label = saveInput.value.trim();
    if (!label) { saveError.textContent = "Give the outfit a name."; return; }
    const { outfit, slotMap } = current();
    try {
      const res = await saveOutfit({ label, pieces: slotMap, style: outfit ? outfit.style : [], prefix: "" });
      await loadCatalog(true);
      commit(res.outfit.id, {});
      closeSave();
      setStatus(`Saved “${res.outfit.label}”.`, "ok");
    } catch (err) {
      saveError.textContent = err.message || "Could not save the outfit.";
    }
  }

  async function removeOutfit(o) {
    if (!confirmFn(`Delete the saved outfit “${o.label}”?`)) return;
    const wasSelected = outfitId === o.id;
    const keep = wasSelected ? current().slotMap : null;
    try {
      await deleteOutfit(o.id);
    } catch (err) {
      setStatus(err.message || "Could not delete the outfit.", "error");
      return;
    }
    await loadCatalog(true);
    if (wasSelected) commit(null, keep); // keep the look as a custom set instead of blanking the preview
  }

  // ----------------------------------------------------------- loading
  async function loadCatalog(force = false) {
    if (force) { // the art on disk may have changed: forget every image we hold
      loaded.clear();
      alphaCache.clear();
      loadImage.clear?.();
    }
    setStatus("Loading outfits…");
    try {
      catalog = await fetchCatalog(force);
      idx = indexCatalog(catalog);
    } catch (err) {
      catalog = null;
      idx = null;
      loadFailed = true;
      setStatus(`Could not load the outfit catalog: ${err.message}`, "error");
      update();
      return;
    }
    loadFailed = false;
    const n = catalog.warnings?.length ?? 0;
    setStatus(n ? `${n} catalog warning${n > 1 ? "s" : ""}: ${catalog.warnings.join("; ")}` : "", n ? "warn" : "");
    if (styleFilter && !collectStyles(catalog.outfits).includes(styleFilter)) styleFilter = null;
    if (openSlot && !idx.bySlot.has(openSlot)) closePicker();
    renderStyles();
    renderGrid();
    update();
  }

  const ready = loadCatalog(false);

  return {
    element: root,
    ready,
    setState(text) {
      const s = parseState(text);
      outfitId = s.outfit;
      overrides = s.overrides;
      update();
    },
    getState() { return { outfit: outfitId, overrides: { ...overrides }, text: serializeState(outfitId, overrides) }; },
    updateOptions() { renderString(); },
    reload(force = true) { return loadCatalog(force); },
  };
}
