// Magik Outfit gallery adapters.
//
// Adapter contract (ported from Comfyui-Anima-Tools' selector descriptors, MIT, (c) nregret).
// The gallery popup (gallery_popup.js) is fully generic and only talks to adapters:
//
//   {
//     kind: "outfit" | "preset" | "mypreset",   // tab id
//     label: string,                            // tab label (already translated)
//     ready: () => bool,
//     data: () => items,                        // the full list for this tab
//     keyOf: (item) => string,                  // stable key (favorites, selection)
//     titleOf: (item) => string,
//     subtitleOf: (item) => string,             // optional small line under the title
//     previewOf: (item) => string | null,       // image URL (already resolved through apiURL)
//     searchText: (item) => string,             // everything the search box matches
//     filters: () => [{ id, label, options: [{ value, label, count }] }],  // filter-panel sections
//     factsOf: (item) => [{ label, value }],    // detail panel
//     tagsOf: (item) => string,                 // prompt tags in the detail panel (click to copy)
//     isSelected: (item) => bool,               // matches the node's current selection
//     onApply: (item) => void,                  // commit this item to the node
//     canDelete: (item) => bool,
//     onDelete: (item) => void,
//   }
import { buildString, collectStyles, presetCategories, resolve } from "./outfit_logic.js";
import { t } from "./i18n.js";

function withCounts(items, getValue) {
  const counts = new Map();
  for (const item of items) {
    for (const v of getValue(item)) counts.set(v, (counts.get(v) || 0) + 1);
  }
  return [...counts.entries()].map(([value, count]) => ({ value, label: value, count }));
}

export function buildAdapters(ctx) {
  const { catalog, idx, myPresets, assetURL, getOutfitId, getLastPresetKey,
          applyOutfit, applyPresetItem, removeOutfit, removeMyPreset, confirmFn } = ctx;

  const outfitTags = (o) => {
    const slotMap = resolve(o, null, idx.pieces);
    return buildString(slotMap, o, false, catalog, idx.pieces, "tags", ", ", "");
  };

  const adapters = [
    {
      kind: "outfit",
      label: t("Outfits"),
      ready: () => Array.isArray(catalog?.outfits) && catalog.outfits.length > 0,
      data: () => catalog.outfits,
      keyOf: (o) => o.id,
      titleOf: (o) => o.label,
      subtitleOf: (o) => (o.style || []).join(", "),
      previewOf: (o) => (o.tile_url ? assetURL(o.tile_url) : null),
      searchText: (o) => [o.label, o.prefix, ...(o.style || []),
        ...Object.values(o.pieces || {}).map((pid) => idx.pieces.get(pid)?.label || "")].join(" "),
      filters: () => [{
        id: "style",
        label: t("Style"),
        options: withCounts(catalog.outfits, (o) => o.style || []),
      }],
      factsOf: (o) => [
        { label: t("Source"), value: o.source },
        { label: t("Pieces"), value: String(Object.keys(o.pieces || {}).length) },
        { label: t("Prefix"), value: o.prefix || "-" },
      ],
      tagsOf: outfitTags,
      isSelected: (o) => o.id === getOutfitId(),
      onApply: (o) => applyOutfit(o.id),
      canDelete: (o) => o.source === "user",
      onDelete: (o) => { if (confirmFn(t('Delete the saved outfit “{label}”?', { label: o.label }))) removeOutfit(o); },
    },
    {
      kind: "preset",
      label: t("Presets"),
      ready: () => Array.isArray(catalog?.presets) && catalog.presets.length > 0,
      data: () => catalog.presets,
      keyOf: (p) => `preset:${p.id}`,
      titleOf: (p) => p.name,
      subtitleOf: (p) => p.name_zh || "",
      previewOf: (p) => p.preview || null,
      searchText: (p) => [p.name, p.name_zh, p.tags, p.tags_zh].join(" "),
      filters: () => [{
        id: "category",
        label: t("Category"),
        options: withCounts(catalog.presets, (p) => p.categories || []),
      }],
      factsOf: (p) => [
        { label: t("Categories"), value: (p.categories || []).join(" / ") || "-" },
        { label: t("Traits"), value: String((p.traits || []).length) },
      ],
      tagsOf: (p) => p.tags,
      isSelected: (p) => getLastPresetKey() === `preset:${p.id}`,
      onApply: (p) => applyPresetItem(p, `preset:${p.id}`),
      canDelete: () => false,
      onDelete: () => {},
    },
    {
      kind: "mypreset",
      label: t("My presets"),
      ready: () => Array.isArray(myPresets) && myPresets.length > 0,
      data: () => myPresets,
      keyOf: (p) => `my:${p.id}`,
      titleOf: (p) => p.name,
      subtitleOf: (p) => p.note || "",
      previewOf: (p) => (p.cover_url ? assetURL(p.cover_url) : null),
      searchText: (p) => [p.name, p.note, p.tags].join(" "),
      filters: () => [],
      factsOf: (p) => [{ label: t("Note"), value: p.note || "-" }],
      tagsOf: (p) => p.tags,
      isSelected: (p) => getLastPresetKey() === `my:${p.id}`,
      onApply: (p) => applyPresetItem(p, `my:${p.id}`),
      canDelete: () => true,
      onDelete: (p) => { if (confirmFn(t("Delete {label}", { label: p.name }))) removeMyPreset(p.id); },
    },
  ];
  return adapters.filter((a) => a.ready());
}
