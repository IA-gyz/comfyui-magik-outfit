// Magik Outfit: pure logic (no DOM, no ComfyUI imports).
// Mirrors outfit_core.py; tests/test_logic.mjs checks parity against fixtures
// generated from the Python code, so keep the two in sync.

export const FORMATS = ["tags", "natural", "both"];
export const STATE_VERSION = 1;
const MAX_LABEL = 60;

// Python's str.isspace() set, which is what `\s` and .strip() use in outfit_core.py. JavaScript's own
// \s / trim() differ (they include U+FEFF and omit U+0085), so use this set to stay identical.
const PY_SPACE = "\\t\\n\\v\\f\\r\\x1c-\\x20\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
const SPACE_RUN = new RegExp(`[${PY_SPACE}]+`, "g");
const SPACE_EDGES = new RegExp(`^[${PY_SPACE}]+|[${PY_SPACE}]+$`, "g");

export function pyStrip(text) {
  return text.replace(SPACE_EDGES, "");
}

export function cleanText(value, maxlen) {
  if (typeof value !== "string") return "";
  const text = pyStrip(value.replace(/[\x00-\x1f\x7f]/g, " ").replace(SPACE_RUN, " "));
  return Array.from(text).slice(0, maxlen).join(""); // code points, like Python (never splits an emoji)
}

// ------------------------------------------------------------------ state
// defineProperty creates a plain own entry even for keys like "__proto__" (a workflow file can be
// shared, so the state is untrusted); a normal assignment would hit the prototype setter instead.
function setOwn(obj, key, value) {
  Object.defineProperty(obj, key, { value, enumerable: true, writable: true, configurable: true });
}

export function parseState(text) {
  let data;
  try {
    data = typeof text === "string" && text.trim() ? JSON.parse(text) : {};
  } catch {
    return { outfit: null, overrides: {} };
  }
  if (data === null || typeof data !== "object" || Array.isArray(data)) {
    return { outfit: null, overrides: {} };
  }
  const outfit = typeof data.outfit === "string" ? data.outfit : null;
  const overrides = {};
  const raw = data.overrides;
  if (raw !== null && typeof raw === "object" && !Array.isArray(raw)) {
    for (const [slot, pid] of Object.entries(raw)) {
      // defineProperty creates a plain own entry even for keys like "__proto__" (a workflow file can be
      // shared, so the state is untrusted); a normal assignment would hit the prototype setter instead
      if (pid === null || typeof pid === "string") {
        setOwn(overrides, slot, pid);
      } else if (pid !== null && typeof pid === "object" && !Array.isArray(pid)
                 && Object.keys(pid).length === 1 && typeof pid.tags === "string") {
        // a tag-only slot: {"tags": "..."} carries a raw prompt tag instead of a piece id
        setOwn(overrides, slot, { tags: cleanText(pid.tags, 200) });
      }
    }
  }
  return { outfit, overrides };
}

export function serializeState(outfit, overrides) {
  return JSON.stringify({ v: STATE_VERSION, outfit: outfit ?? null, overrides: overrides ?? {} });
}

// ---------------------------------------------------------------- catalog
export function indexCatalog(catalog) {
  const pieces = new Map();
  for (const p of catalog.pieces) pieces.set(p.id, p);
  const outfits = new Map();
  for (const o of catalog.outfits) outfits.set(o.id, o);
  const bySlot = new Map();
  for (const p of catalog.pieces) {
    if (!bySlot.has(p.slot)) bySlot.set(p.slot, []);
    bySlot.get(p.slot).push(p);
  }
  // UI slot order: the output order first, then any other selectable slot by z (top-most first).
  const selectable = Object.entries(catalog.slots)
    .filter(([, s]) => s.selectable)
    .map(([name]) => name);
  const order = (catalog.output_order || []).filter((s) => selectable.includes(s));
  const rest = selectable
    .filter((s) => !order.includes(s))
    .sort((a, b) => catalog.slots[b].z - catalog.slots[a].z);
  return { pieces, outfits, bySlot, slotOrder: [...order, ...rest] };
}

// -------------------------------------------------------------- selection
// pieces: Map id -> piece. Returns {slot: pieceId}.
export function resolve(outfit, overrides, pieces) {
  overrides = overrides || {};
  const result = {};
  if (outfit) {
    for (const [s, p] of Object.entries(outfit.pieces)) if (pieces.has(p)) result[s] = p;
  }
  for (const [slot, pid] of Object.entries(overrides)) {
    if (pid === null) {
      delete result[slot];
      continue;
    }
    if (typeof pid === "object") { // tag-only slot: explicit content, no piece conflicts
      setOwn(result, slot, pid);
      continue;
    }
    const piece = pieces.get(pid);
    if (!piece || piece.slot !== slot) continue;
    setOwn(result, slot, pid);
    for (const other of Object.keys(result)) {
      if (other === slot) continue;
      if (overrides[other] !== undefined && overrides[other] !== null) continue;
      const otherPiece = typeof result[other] === "string" ? pieces.get(result[other]) : undefined;
      if (piece.conflicts.includes(other) || (otherPiece && otherPiece.conflicts.includes(slot))) {
        delete result[other];
      }
    }
  }
  return result;
}

function sameValue(a, b) {
  if (a === b) return true;
  // tag-only entries ({"tags": ...}) are the only object values in a slot map
  if (a && b && typeof a === "object" && typeof b === "object" && !Array.isArray(a) && !Array.isArray(b)) {
    return a.tags === b.tags;
  }
  return false;
}

export function sameMap(a, b) {
  const ka = Object.keys(a);
  const kb = Object.keys(b);
  return ka.length === kb.length && ka.every((k) => sameValue(a[k], b[k]));
}

export function isModified(outfit, slotMap) {
  return !outfit || !sameMap(slotMap, outfit.pieces);
}

// Overrides that turn `outfit` into `slotMap`: changed slots get the new piece id,
// slots that the outfit has but slotMap lacks get an explicit null.
export function diffOverrides(outfit, slotMap) {
  const base = outfit ? outfit.pieces : {};
  const overrides = {};
  for (const slot of new Set([...Object.keys(base), ...Object.keys(slotMap)])) {
    const want = slotMap[slot] ?? null;
    const have = base[slot] ?? null;
    if (!sameValue(want, have)) setOwn(overrides, slot, want);
  }
  return overrides;
}

// Pick `pieceId` (or null = none) for `slot`; returns the new overrides.
// Conflicting pieces are removed in both directions, so the result is explicit and consistent.
export function pickPiece(outfit, overrides, slot, pieceId, pieces) {
  const next = { ...resolve(outfit, overrides, pieces) };
  if (pieceId === null) {
    delete next[slot];
  } else {
    const piece = pieces.get(pieceId);
    if (!piece || piece.slot !== slot) return overrides;
    next[slot] = pieceId;
    for (const other of Object.keys(next)) {
      if (other === slot) continue;
      const otherPiece = pieces.get(next[other]);
      if (piece.conflicts.includes(other) || (otherPiece && otherPiece.conflicts.includes(slot))) {
        delete next[other];
      }
    }
  }
  return diffOverrides(outfit, next);
}

// ----------------------------------------------------------------- string
function unescapeSep(text) {
  return text.replaceAll("\\n", "\n").replaceAll("\\t", "\t");
}

export function buildString(slotMap, outfit, modified, catalog, pieces, fmt = "tags", separator = ", ", prefix = "") {
  if (!FORMATS.includes(fmt)) fmt = "tags";
  const sep = unescapeSep(typeof separator === "string" ? separator : ", ");
  const lead = outfit && !modified ? cleanText(outfit.prefix ?? "", MAX_LABEL) : "";
  const hidden = new Set();
  for (const id of Object.values(slotMap)) {
    const p = typeof id === "string" ? pieces.get(id) : undefined;
    if (p) for (const s of p.hides) hidden.add(s);
  }
  const tags = lead ? [lead] : [];
  const natural = lead ? [lead] : [];
  for (const slot of catalog.output_order || []) {
    if (hidden.has(slot)) continue;
    const value = slotMap[slot];
    if (value && typeof value === "object") {
      const t = pyStrip(value.tags ?? "");
      if (t) {
        tags.push(t);
        natural.push(t.replaceAll("_", " "));
      }
      continue;
    }
    const piece = typeof value === "string" ? pieces.get(value) : undefined;
    if (!piece) continue;
    if (pyStrip(piece.tags)) tags.push(pyStrip(piece.tags));
    if (pyStrip(piece.nl)) natural.push(pyStrip(piece.nl));
  }
  const pre = typeof prefix === "string" ? pyStrip(prefix) : "";
  const withPrefix = (text) => (pre && text ? `${pre} ${text}` : text);
  if (fmt === "tags") return withPrefix(tags.join(sep));
  if (fmt === "natural") return withPrefix(natural.join(sep));
  return withPrefix(natural.join(sep)) + "\n" + tags.join(sep);
}

// ---------------------------------------------------------------- drawing
// Layer URLs in draw order (base first). Mirrors draw_list() in outfit_core.py.
export function drawList(catalog, pieces, slotMap) {
  const zs = {};
  for (const [name, s] of Object.entries(catalog.slots)) zs[name] = s.z;
  const present = Object.values(slotMap).map((id) => (typeof id === "string" ? pieces.get(id) : undefined)).filter(Boolean);
  const hidden = new Set();
  for (const p of present) for (const s of p.hides) hidden.add(s);
  const items = [{ z: zs.body, url: catalog.base_url, piece: null }];
  for (const p of present) {
    if (hidden.has(p.slot)) continue;
    items.push({ z: p.z !== null && p.z !== undefined ? p.z : zs[p.slot], url: p.layer_url, piece: p });
    if (p.back_layer_url) items.push({ z: zs.back, url: p.back_layer_url, piece: p });
  }
  items.sort((a, b) => a.z - b.z); // Array.prototype.sort is stable
  return items;
}

// ---------------------------------------------------------------- attire
// Danbooru attire tag pools bundled as data/attire.json (from Comfyui-Anima-Tools, MIT, (c) nregret).
export const ATTIRE_MAIN_VARIANTS = ["top_bottom", "uniform", "traditional"];
export const ATTIRE_LAYERED = [["decoration", 1.0], ["socks", 0.6], ["shoes", 0.6]];
export const ATTIRE_SLOT_MAP = {
  top: "top", bottom: "bottom", socks: "legwear", shoes: "footwear",
  decoration: "accessory", uniform: "onepiece", traditional: "onepiece",
};

// Draws one outfit from the pools: exactly one main garment (a top+bottom pair, a uniform or a
// traditional outfit), then decoration always and legwear/footwear with their probabilities.
export function drawAttire(attire, rng) {
  const pools = attire?.slots ?? {};
  if (!(pools.top?.length && pools.bottom?.length && pools.uniform?.length && pools.traditional?.length)) return {};
  const pick = (pool) => pool[Math.floor(rng() * pool.length)];
  const drawn = {};
  const variant = ATTIRE_MAIN_VARIANTS[Math.floor(rng() * ATTIRE_MAIN_VARIANTS.length)];
  if (variant === "top_bottom") {
    drawn[ATTIRE_SLOT_MAP.top] = pick(pools.top);
    drawn[ATTIRE_SLOT_MAP.bottom] = pick(pools.bottom);
  } else {
    drawn[ATTIRE_SLOT_MAP[variant]] = pick(pools[variant]);
  }
  for (const [slot, probability] of ATTIRE_LAYERED) {
    const pool = pools[slot] ?? [];
    if (!pool.length) continue;
    if (probability < 1 && rng() >= probability) continue;
    drawn[ATTIRE_SLOT_MAP[slot]] = pick(pool);
  }
  return drawn;
}

function tagKey(tag) {
  return String(tag).replaceAll("_", " ").toLowerCase().split(/\s+/).filter(Boolean).join(" ");
}

// Slots whose drawn tag appears in a catalog piece's tags get that piece; the rest keep the raw tag.
export function attireToSlotMap(slotTags, pieces, rng) {
  const out = {};
  for (const [slot, tag] of Object.entries(slotTags)) {
    const key = tagKey(tag);
    const candidates = [...pieces.values()].filter(
      (p) => p.slot === slot && String(p.tags ?? "").split(",").map(tagKey).includes(key));
    out[slot] = candidates.length
      ? (rng ? candidates[Math.floor(rng() * candidates.length)] : candidates[0]).id
      : { tags: tag };
  }
  return out;
}

// Turns a clothing preset's tag list into a {slot: pieceId | {tags}} map: a tag found in a
// piece's tags selects the piece, a tag from the attire pools becomes a tag-only entry in the
// matching slot, anything else is skipped. A one-piece garment drops top/bottom.
export function presetToSlotMap(preset, pieces, attire) {
  const pools = attire?.slots ?? {};
  const poolSlot = {};
  for (const [pool, tags] of Object.entries(pools)) {
    const slot = ATTIRE_SLOT_MAP[pool] ?? pool;
    for (const t of tags) poolSlot[tagKey(t)] = slot;
  }
  const out = {};
  for (const raw of String(preset?.tags ?? "").split(",")) {
    const tag = raw.trim();
    if (!tag) continue;
    const key = tagKey(tag);
    const piece = [...pieces.values()].find(
      (p) => String(p.tags ?? "").split(",").map(tagKey).includes(key));
    if (piece) { out[piece.slot] = piece.id; continue; }
    const slot = poolSlot[key];
    if (slot) out[slot] = { tags: tag };
  }
  if ("onepiece" in out) { delete out.top; delete out.bottom; }
  return out;
}

// Bilingual search: matches name, localized name and tags in both languages.
export function presetMatches(preset, query) {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return [preset.name, preset.name_zh, preset.tags, preset.tags_zh]
    .some((s) => String(s ?? "").toLowerCase().includes(q));
}

export function presetCategories(presets) {
  const set = new Set();
  for (const p of presets) for (const c of p.categories ?? []) set.add(c);
  return [...set].sort();
}

// ---------------------------------------------------------------- browsing
export function filterOutfits(outfits, { query = "", style = null, favorites = null } = {}) {
  const q = query.trim().toLowerCase();
  return outfits.filter((o) => {
    if (style && !o.style.includes(style)) return false;
    if (favorites && !favorites.has(o.id)) return false;
    if (!q) return true;
    return o.label.toLowerCase().includes(q) || o.style.some((s) => s.includes(q));
  });
}

export function collectStyles(outfits) {
  const set = new Set();
  for (const o of outfits) for (const s of o.style) set.add(s);
  return [...set].sort();
}
