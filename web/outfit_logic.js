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
        Object.defineProperty(overrides, slot, { value: pid, enumerable: true, writable: true, configurable: true });
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
    const piece = pieces.get(pid);
    if (!piece || piece.slot !== slot) continue;
    result[slot] = pid;
    for (const other of Object.keys(result)) {
      if (other === slot) continue;
      if (overrides[other] !== undefined && overrides[other] !== null) continue;
      const otherPiece = pieces.get(result[other]);
      if (piece.conflicts.includes(other) || (otherPiece && otherPiece.conflicts.includes(slot))) {
        delete result[other];
      }
    }
  }
  return result;
}

export function sameMap(a, b) {
  const ka = Object.keys(a);
  const kb = Object.keys(b);
  return ka.length === kb.length && ka.every((k) => a[k] === b[k]);
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
    if (want !== have) overrides[slot] = want;
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
    const p = pieces.get(id);
    if (p) for (const s of p.hides) hidden.add(s);
  }
  const tags = lead ? [lead] : [];
  const natural = lead ? [lead] : [];
  for (const slot of catalog.output_order || []) {
    if (hidden.has(slot)) continue;
    const piece = pieces.get(slotMap[slot]);
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
  const present = Object.values(slotMap).map((id) => pieces.get(id)).filter(Boolean);
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
