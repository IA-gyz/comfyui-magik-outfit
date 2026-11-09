// Magik Outfit gallery popup: one shared modal shell driven entirely by adapters
// (see gallery_adapters.js for the contract). Layout follows Comfyui-Anima-Tools'
// selector popup (MIT, (c) nregret): top bar with tabs + counts, left rail
// (All / Favorites / Selected), collapsible filter panel, toolbar with sort +
// search + active-filter chips, paged card grid, detail panel, footer with Apply.
import { t } from "./i18n.js";

const PAGE_SIZE = 24;

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

function copyText(doc, text) {
  const done = () => {};
  try {
    if (doc.defaultView?.navigator?.clipboard?.writeText) {
      doc.defaultView.navigator.clipboard.writeText(text).then(done, done);
      return true;
    }
  } catch { /* fall through */ }
  try {
    const ta = el(doc, "textarea", { text });
    doc.body.append(ta);
    ta.select();
    doc.execCommand?.("copy");
    ta.remove();
    return true;
  } catch { return false; }
}

export function openGallery({ doc, adapters, favStore, random = Math.random, onApplied = () => {} }) {
  if (!adapters.length) return null;

  let active = adapters[0];
  let rail = "all";                       // all | fav | selected
  let filterValues = {};                  // filterId -> value
  let query = "";
  let page = 0;
  let sortMode = "default";
  let showFilters = false;
  const selectedKeys = new Map();         // adapter.kind -> key (this popup session)
  let detailKey = null;

  const overlay = el(doc, "div", { class: "mg-overlay", "data-role": "mg-overlay" });
  const dialog = el(doc, "div", { class: "mg-dialog", role: "dialog", "aria-modal": "true", "data-role": "mg-dialog" });
  overlay.append(dialog);

  const tabsEl = el(doc, "div", { class: "mg-tabs" });
  const filterToggle = el(doc, "button", { class: "mg-btn", type: "button", "data-role": "mg-filters-toggle", text: t("Filters"), onClick: () => { showFilters = !showFilters; renderFilters(); } });
  const closeBtn = el(doc, "button", { class: "mg-btn mg-btn-icon", type: "button", "data-role": "mg-close", text: "×", "aria-label": t("Close"), onClick: close });
  const topbar = el(doc, "div", { class: "mg-topbar" }, [tabsEl, filterToggle, closeBtn]);

  const railEl = el(doc, "div", { class: "mg-rail" });
  const filtersEl = el(doc, "div", { class: "mg-filters", hidden: true });
  const titleEl = el(doc, "div", { class: "mg-title" });
  const sortEl = el(doc, "select", { class: "mg-input", "data-role": "mg-sort", onChange: () => { sortMode = sortEl.value; page = 0; renderGrid(); } },
    ["default", "name-asc", "name-desc", "fav", "random"].map((v) => el(doc, "option", { value: v, text: t(v) })));
  sortEl.value = "default";
  const searchEl = el(doc, "input", { class: "mg-input mg-search", type: "search", placeholder: t("Search…"), "data-role": "mg-search", onInput: () => { query = searchEl.value; page = 0; renderGrid(); } });
  const chipsEl = el(doc, "div", { class: "mg-chips" });
  const moreBtn = el(doc, "button", { class: "mg-btn", type: "button", "data-role": "mg-more", text: t("More"), onClick: (e) => renderMoreMenu(e.currentTarget) });
  const toolbar = el(doc, "div", { class: "mg-toolbar" }, [titleEl, sortEl, searchEl, moreBtn, chipsEl]);

  const gridEl = el(doc, "div", { class: "mg-grid", "data-role": "mg-grid" });
  const pagerPrev = el(doc, "button", { class: "mg-btn mg-btn-icon", type: "button", "data-role": "mg-prev", text: "‹", title: t("Previous page"), onClick: () => { if (page > 0) { page--; renderGrid(); } } });
  const pagerNext = el(doc, "button", { class: "mg-btn mg-btn-icon", type: "button", "data-role": "mg-next", text: "›", title: t("Next page"), onClick: () => { page++; renderGrid(); } });
  const pagerLabel = el(doc, "span", { class: "mg-page-label" });
  const pager = el(doc, "div", { class: "mg-pager" }, [pagerPrev, pagerLabel, pagerNext]);

  const detailEl = el(doc, "div", { class: "mg-detail", hidden: true, "data-role": "mg-detail" });
  const mainEl = el(doc, "div", { class: "mg-main" }, [filtersEl, toolbar, gridEl, pager]);
  const bodyEl = el(doc, "div", { class: "mg-body" }, [railEl, mainEl, detailEl]);

  const statusEl = el(doc, "span", { class: "mg-status", "data-role": "mg-status", text: "" });
  const applyBtn = el(doc, "button", { class: "mg-btn mg-btn-primary", type: "button", "data-role": "mg-apply", text: t("Apply"), onClick: applySelection });
  const footerClose = el(doc, "button", { class: "mg-btn", type: "button", "data-action": "mg-close2", text: t("Close"), onClick: close });
  const footer = el(doc, "div", { class: "mg-footer" }, [statusEl, el(doc, "span", { class: "mg-spacer" }), applyBtn, footerClose]);

  dialog.append(topbar, bodyEl, footer);

  function currentItems() {
    let items = active.data();
    if (rail === "fav") items = items.filter((x) => favStore.isFav(active.keyOf(x)));
    else if (rail === "selected") items = items.filter((x) => selectedKeys.get(active.kind) === active.keyOf(x) || active.isSelected(x));
    for (const [fid, val] of Object.entries(filterValues)) {
      if (!val) continue;
      const section = active.filters().find((f) => f.id === fid);
      if (!section) continue;
      items = items.filter((x) => {
        if (active.kind === "outfit" && fid === "style") return (x.style || []).includes(val);
        if (fid === "category") return (x.categories || []).includes(val);
        return true;
      });
    }
    const q = query.trim().toLowerCase();
    if (q) items = items.filter((x) => active.searchText(x).toLowerCase().includes(q));
    items = items.slice();
    if (sortMode === "name-asc") items.sort((a, b) => active.titleOf(a).localeCompare(active.titleOf(b)));
    else if (sortMode === "name-desc") items.sort((a, b) => active.titleOf(b).localeCompare(active.titleOf(a)));
    else if (sortMode === "fav") items.sort((a, b) => (favStore.isFav(active.keyOf(b)) ? 1 : 0) - (favStore.isFav(active.keyOf(a)) ? 1 : 0));
    else if (sortMode === "random") {
      for (let i = items.length - 1; i > 0; i--) {
        const j = Math.floor(random() * (i + 1));
        [items[i], items[j]] = [items[j], items[i]];
      }
    }
    return items;
  }

  function renderTabs() {
    tabsEl.replaceChildren();
    for (const a of adapters) {
      const favCount = a.data().filter((x) => favStore.isFav(a.keyOf(x))).length;
      tabsEl.append(el(doc, "button", {
        class: "mg-tab" + (a === active ? " is-on" : ""), type: "button", "data-tab": a.kind,
        text: favCount ? `${a.label} (${favCount}★)` : a.label,
        onClick: () => { active = a; filterValues = {}; page = 0; detailKey = null; renderAll(); },
      }));
    }
  }

  function renderRail() {
    railEl.replaceChildren();
    for (const [id, label] of [["all", t("All")], ["fav", t("Favorites")], ["selected", t("Selected")]]) {
      railEl.append(el(doc, "button", {
        class: "mg-railbtn" + (rail === id ? " is-on" : ""), type: "button", "data-rail": id, text: label,
        onClick: () => { rail = id; page = 0; renderGrid(); renderRail(); },
      }));
    }
  }

  function renderFilters() {
    filtersEl.hidden = !showFilters;
    filterToggle.classList.toggle("is-on", showFilters);
    filtersEl.replaceChildren();
    if (!showFilters) return;
    if (rail === "fav") {
      // on the Favorites rail the filter panel lists the favorite groups
      const groups = favStore.groups();
      const sec = el(doc, "div", { class: "mg-filter-sec" }, [el(doc, "div", { class: "mg-filter-label", text: t("Groups") })]);
      for (const g of groups) {
        sec.append(el(doc, "button", {
          class: "mg-chipbtn" + (filterValues.__group === g.id ? " is-on" : ""), type: "button", "data-group": g.id,
          text: g.name, onClick: () => {
            filterValues.__group = filterValues.__group === g.id ? null : g.id;
            page = 0; renderFilters(); renderGrid();
          },
        }));
      }
      filtersEl.append(sec);
      return;
    }
    for (const section of active.filters()) {
      const sec = el(doc, "div", { class: "mg-filter-sec" }, [el(doc, "div", { class: "mg-filter-label", text: section.label })]);
      for (const opt of section.options) {
        sec.append(el(doc, "button", {
          class: "mg-chipbtn" + (filterValues[section.id] === opt.value ? " is-on" : ""), type: "button",
          "data-filter": section.id, "data-value": opt.value, text: `${opt.label} (${opt.count})`,
          onClick: () => {
            filterValues[section.id] = filterValues[section.id] === opt.value ? null : opt.value;
            page = 0; renderFilters(); renderGrid();
          },
        }));
      }
      filtersEl.append(sec);
    }
  }

  function renderToolbar() {
    const items = currentItems();
    titleEl.textContent = `${active.label} · ${items.length}`;
    chipsEl.replaceChildren();
    const addChip = (label, onClear) => chipsEl.append(el(doc, "button", {
      class: "mg-chipbtn mg-chip-active", type: "button", text: `${label} ×`,
      onClick: () => { onClear(); page = 0; renderAll(); },
    }));
    for (const [fid, val] of Object.entries(filterValues)) {
      if (!val) continue;
      if (fid === "__group") {
        const g = favStore.groups().find((x) => x.id === val);
        addChip(g ? g.name : val, () => { filterValues.__group = null; });
      } else {
        addChip(val, () => { filterValues[fid] = null; });
      }
    }
    if (query) addChip(`“${query}”`, () => { query = ""; searchEl.value = ""; });
  }

  function renderGrid() {
    renderToolbar();
    gridEl.replaceChildren();
    const items = currentItems();
    const pages = Math.max(1, Math.ceil(items.length / PAGE_SIZE));
    if (page >= pages) page = pages - 1;
    if (page < 0) page = 0;
    if (!items.length) {
      gridEl.append(el(doc, "div", { class: "mg-empty", text: t("No items") }));
    }
    const selKey = selectedKeys.get(active.kind);
    for (const item of items.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE)) {
      const key = active.keyOf(item);
      const isFav = favStore.isFav(key);
      const isSel = selKey === key || active.isSelected(item);
      const card = el(doc, "div", {
        class: "mg-card" + (isSel ? " is-selected" : ""), "data-key": key, role: "button", tabindex: "0",
        title: active.titleOf(item),
        onClick: () => { selectedKeys.set(active.kind, key); renderGrid(); renderStatus(); },
        onKeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectedKeys.set(active.kind, key); renderGrid(); renderStatus(); } },
      }, [
        active.previewOf(item)
          ? el(doc, "img", { src: active.previewOf(item), alt: active.titleOf(item), loading: "lazy", decoding: "async", draggable: "false" })
          : el(doc, "div", { class: "mg-card-noimg" }),
        el(doc, "span", { class: "mg-card-label", text: active.titleOf(item) }),
        el(doc, "button", {
          class: "mg-fav" + (isFav ? " is-on" : ""), type: "button", "data-fav": key, text: isFav ? "★" : "☆",
          title: isFav ? t("Remove from favorites") : t("Add to favorites"), "aria-pressed": String(isFav),
          onClick: (e) => { e.stopPropagation(); favStore.toggle(key); renderTabs(); renderGrid(); },
        }),
        el(doc, "button", {
          class: "mg-info", type: "button", "data-info": key, text: "ⓘ", title: t("Details"),
          onClick: (e) => { e.stopPropagation(); detailKey = key; renderDetail(); },
        }),
      ]);
      gridEl.append(card);
    }
    pager.hidden = pages <= 1;
    pagerLabel.textContent = pages > 1 ? `${page + 1} / ${pages}` : "";
    pagerPrev.disabled = page <= 0;
    pagerNext.disabled = page >= pages - 1;
    renderStatus();
  }

  function findItem(key) {
    return active.data().find((x) => active.keyOf(x) === key) || null;
  }

  function renderDetail() {
    detailEl.hidden = detailKey === null;
    if (detailKey === null) { detailEl.replaceChildren(); return; }
    const item = findItem(detailKey);
    if (!item) { detailKey = null; detailEl.hidden = true; return; }
    const key = active.keyOf(item);
    const isFav = favStore.isFav(key);
    const tags = active.tagsOf(item) || "";
    const groups = favStore.groups();
    const recGroups = favStore.groupIdsOf ? favStore.groupIdsOf(key) : ["default"];
    detailEl.replaceChildren(
      el(doc, "div", { class: "mg-detail-head" }, [
        el(doc, "strong", { text: active.titleOf(item) }),
        el(doc, "button", { class: "mg-btn mg-btn-icon", type: "button", "data-role": "mg-detail-close", text: "×", "aria-label": t("Close"), onClick: () => { detailKey = null; renderDetail(); } }),
      ]),
      active.previewOf(item) ? el(doc, "img", { class: "mg-detail-img", src: active.previewOf(item), alt: active.titleOf(item) }) : null,
      el(doc, "div", { class: "mg-detail-label", text: t("Prompt tags") }),
      el(doc, "div", { class: "mg-detail-tags", "data-role": "mg-tags", text: tags || "-",
        title: t("Click to copy"), onClick: () => { if (tags && copyText(doc, tags)) setStatus(t("Copied!")); } }),
      el(doc, "div", { class: "mg-detail-facts" },
        active.factsOf(item).map((f) => el(doc, "div", { class: "mg-fact" }, [
          el(doc, "span", { class: "mg-fact-label", text: f.label }),
          el(doc, "span", { text: String(f.value) }),
        ]))),
      el(doc, "label", { class: "mg-favrow" }, [
        el(doc, "input", { type: "checkbox", "data-role": "mg-fav-toggle", ...(isFav ? { checked: true } : {}) }),
        el(doc, "span", { text: t("Favorite") }),
      ]),
      el(doc, "div", { class: "mg-detail-label", text: t("Groups") }),
      el(doc, "div", { class: "mg-detail-groups", "data-role": "mg-groups" },
        groups.map((g) => el(doc, "label", { class: "mg-grouprow" }, [
          el(doc, "input", { type: "checkbox", "data-group": g.id, ...(recGroups.includes(g.id) ? { checked: true } : {}) }),
          el(doc, "span", { text: g.name }),
        ]))),
      el(doc, "div", { class: "mg-newgroup" }, [
        el(doc, "input", { class: "mg-input", type: "text", placeholder: t("New group name…"), "data-role": "mg-newgroup" }),
        el(doc, "button", { class: "mg-btn", type: "button", "data-role": "mg-addgroup", text: t("Add"), onClick: (e) => {
          const input = detailEl.querySelector("[data-role=mg-newgroup]");
          const name = input?.value?.trim();
          if (name && favStore.addGroup) { favStore.addGroup(name); input.value = ""; renderDetail(); }
        } }),
      ]),
      el(doc, "div", { class: "mg-detail-actions" }, [
        el(doc, "button", { class: "mg-btn mg-btn-primary", type: "button", "data-role": "mg-detail-apply", text: t("Select"), onClick: () => applyItem(item) }),
        active.canDelete(item)
          ? el(doc, "button", { class: "mg-btn mg-btn-danger", type: "button", "data-role": "mg-detail-delete", text: t("Delete"), onClick: () => { active.onDelete(item); detailKey = null; renderDetail(); renderAll(); } })
          : null,
      ]),
    );
    const favToggle = detailEl.querySelector("[data-role=mg-fav-toggle]");
    favToggle.addEventListener("change", () => favStore.toggle(key));
    for (const cb of detailEl.querySelectorAll("[data-group]")) {
      if (cb.dataset.role === "mg-fav-toggle" || cb.closest(".mg-newgroup")) continue;
      cb.addEventListener("change", () => {
        const ids = [...detailEl.querySelectorAll(".mg-detail-groups input[data-group]:checked")].map((c) => c.dataset.group);
        favStore.setGroups(key, ids);
      });
    }
  }

  function renderMoreMenu(anchor) {
    const existing = doc.querySelector(".mg-menu");
    if (existing) { existing.remove(); return; }
    const key = selectedKeys.get(active.kind);
    const item = key ? findItem(key) : null;
    const menu = el(doc, "div", { class: "mg-menu", role: "menu" }, [
      el(doc, "button", { class: "mg-menu-item", type: "button", "data-role": "mg-copy", text: t("Copy tags"),
        onClick: () => { const tags = item ? active.tagsOf(item) : ""; if (tags && copyText(doc, tags)) setStatus(t("Copied!")); menu.remove(); } }),
      el(doc, "button", { class: "mg-menu-item", type: "button", "data-role": "mg-clear", text: t("Clear selection"),
        onClick: () => { selectedKeys.delete(active.kind); menu.remove(); renderGrid(); } }),
    ]);
    anchor.append(menu);
    const closeMenu = (e) => { if (!menu.contains(e.target)) { menu.remove(); doc.removeEventListener("pointerdown", closeMenu); } };
    doc.addEventListener("pointerdown", closeMenu);
  }

  function renderStatus() {
    const key = selectedKeys.get(active.kind);
    statusEl.textContent = key ? t("Selected {count}", { count: 1 }) + `: ${findItem(key) ? active.titleOf(findItem(key)) : ""}` : t("No selection");
    applyBtn.disabled = !key;
  }

  function applyItem(item) {
    active.onApply(item);
    onApplied(active, item);
    close();
  }

  function applySelection() {
    const key = selectedKeys.get(active.kind);
    const item = key ? findItem(key) : null;
    if (item) applyItem(item);
  }

  function renderAll() {
    renderTabs();
    renderRail();
    renderFilters();
    renderGrid();
    renderDetail();
  }

  function onKeydown(e) {
    if (e.key === "Escape") { e.preventDefault(); close(); }
  }
  function onOverlayClick(e) {
    if (e.target === overlay) close();
  }

  function close() {
    doc.removeEventListener("keydown", onKeydown);
    overlay.removeEventListener("click", onOverlayClick);
    overlay.remove();
    if (returnFocus?.focus) returnFocus.focus();
  }

  const returnFocus = doc.activeElement;
  doc.addEventListener("keydown", onKeydown);
  overlay.addEventListener("click", onOverlayClick);
  doc.body.append(overlay);
  renderAll();
  return { close, element: overlay };
}
