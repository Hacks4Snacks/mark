"use strict";

// Collections: the grid, a single collection (overview + members + scoped Ask),
// the create/edit dialog, the add-to-collection menu, and saved-search rules.

import { api } from "../api.js";
import { activityColChart, sessionBarChart } from "../charts.js";
import { showOnly, setLayoutWide, state } from "../state.js";
import {
  $, $$, debounce, esc, fmtCost, fmtDate, fmtDuration, fmtRelativeTime, srcMeta, toast, withTransition,
} from "../utils.js";
import { icon } from "../icons.js";
import { cardHTML, showList } from "./list.js";
import { openSession, teardownReading } from "./detail.js";
import { streamAsk } from "./ask.js";

const COLL_COLORS = new Set(["purple", "cyan", "green", "amber", "rose"]);

let collDialogState = { mode: "create", id: null, rule: null, pendingSession: null };
let gridCollections = [];
let gridQuery = "";
let gridKind = "all";
let gridSort = "pinned";
let gridToolbarWired = false;
let memberSort = "recent";

const collIconOf = (c) => (c && c.icon) || "\u25A6";

function collAccentClass(color) {
  return color && COLL_COLORS.has(color) ? `coll-accent-${color}` : "";
}

export function ruleIsEmpty(r) {
  if (!r) return true;
  return !(r.q || r.repo || r.source || (r.tags && r.tags.length) || r.date_from || r.date_to);
}

export function currentRule() {
  const rule = {};
  if (state.q) rule.q = state.q;
  if (state.mode && state.mode !== "hybrid") rule.mode = state.mode;
  if (state.source) rule.source = state.source;
  if (state.repo) rule.repo = state.repo;
  if (state.tags.size) rule.tags = [...state.tags];
  if (state.dateFrom) rule.date_from = state.dateFrom;
  if (state.dateTo) rule.date_to = state.dateTo;
  return rule;
}

function ruleParts(r) {
  if (ruleIsEmpty(r)) return [];
  const parts = [];
  if (r.q) parts.push({ html: `matching \u201C${esc(r.q)}\u201D` });
  if (r.source) parts.push({ html: `${srcMeta(r.source).icon} ${esc(srcMeta(r.source).label)}` });
  if (r.repo) parts.push({ html: `${icon("folder")} ${esc(r.repo)}` });
  (r.tags || []).forEach((t) => parts.push({ html: `# ${esc(t)}` }));
  if (r.date_from || r.date_to) {
    parts.push({
      html: `${icon("calendar")} ${esc(r.date_from || "\u2026")} ${icon("arrow-right", { size: 13 })} ${esc(r.date_to || "\u2026")}`,
    });
  }
  if (r.mode && r.mode !== "hybrid" && r.q) {
    parts.push({ html: esc(r.mode) });
  }
  return parts;
}

function ruleSummary(r) {
  if (ruleIsEmpty(r)) return `<span class="muted">manual selection only</span>`;
  return ruleParts(r).map((p) => `<span class="crs-chip">${p.html}</span>`).join("");
}

function rulePreviewHTML(r, max = 2) {
  const parts = ruleParts(r);
  if (!parts.length) return "";
  const shown = parts.slice(0, max).map((p) => `<span class="crs-chip">${p.html}</span>`).join("");
  const extra = parts.length - max;
  return shown + (extra > 0 ? `<span class="crs-chip crs-more">+${extra} filter${extra === 1 ? "" : "s"}</span>` : "");
}

export function toggleSaveCollectionBtn() {
  const btn = $("#saveCollectionBtn");
  if (btn) btn.hidden = ruleIsEmpty(currentRule());
}

function wireGridToolbar() {
  if (gridToolbarWired) return;
  gridToolbarWired = true;
  $("#collGridSearch")?.addEventListener("input", debounce((e) => {
    gridQuery = e.target.value.trim().toLowerCase();
    renderCollectionsGrid();
  }, 180));
  $$("#collKindToggle button").forEach((b) =>
    b.addEventListener("click", () => {
      gridKind = b.dataset.kind;
      $$("#collKindToggle button").forEach((x) => x.classList.toggle("active", x === b));
      renderCollectionsGrid();
    })
  );
  $("#collGridSort")?.addEventListener("change", (e) => {
    gridSort = e.target.value;
    renderCollectionsGrid();
  });
}

function sortCollections(cols) {
  const list = [...cols];
  if (gridSort === "name") {
    list.sort((a, b) => (a.name || "").localeCompare(b.name || ""));
  } else if (gridSort === "sessions") {
    list.sort((a, b) => (b.count || 0) - (a.count || 0));
  } else if (gridSort === "updated") {
    list.sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")));
  } else {
    list.sort((a, b) => {
      const pd = (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0);
      if (pd) return pd;
      return String(b.updated_at || "").localeCompare(String(a.updated_at || ""));
    });
  }
  return list;
}

function filterCollections(cols) {
  return cols.filter((c) => {
    if (gridKind === "auto" && ruleIsEmpty(c.rule)) return false;
    if (gridKind === "manual" && !ruleIsEmpty(c.rule)) return false;
    if (!gridQuery) return true;
    const hay = `${c.name || ""} ${c.description || ""}`.toLowerCase();
    return hay.includes(gridQuery);
  });
}

function sortMembers(members, sort) {
  const list = [...members];
  if (sort === "oldest") {
    list.sort((a, b) => String(a.updated_at || a.created_at).localeCompare(String(b.updated_at || b.created_at)));
  } else if (sort === "turns") {
    list.sort((a, b) => (b.turn_count || 0) - (a.turn_count || 0));
  } else if (sort === "title") {
    list.sort((a, b) => (a.title || "").localeCompare(b.title || ""));
  } else {
    list.sort((a, b) => String(b.updated_at || b.created_at).localeCompare(String(a.updated_at || a.created_at)));
  }
  return list;
}

export async function showCollections(opts = {}) {
  const leaving = state.view !== "collections";
  state.view = "collections";
  state.currentId = null;
  state.currentCollectionId = null;
  teardownReading();
  const apply = () => { setLayoutWide(true); showOnly("#collectionsView"); };
  if (leaving) withTransition(apply); else apply();
  if (!opts.fromHash) location.hash = "#/collections";
  wireGridToolbar();
  loadCollectionsGrid();
}

async function loadCollectionsGrid() {
  const host = $("#collectionsGrid");
  host.innerHTML = `<div class="lib-loading muted">Loading\u2026</div>`;
  try {
    gridCollections = await api("/api/collections");
    renderCollectionsGrid();
  } catch (e) {
    host.innerHTML = `<div class="empty"><div class="big">${icon("alert", { size: 40 })}</div>${esc(e.message)}</div>`;
  }
}

function renderCollectionsGrid() {
  const host = $("#collectionsGrid");
  const filtered = sortCollections(filterCollections(gridCollections));
  const totalSessions = gridCollections.reduce((n, c) => n + (c.count || 0), 0);
  const agg = $("#collectionsAggregate");
  if (agg) {
    if (gridCollections.length) {
      agg.hidden = false;
      agg.textContent = `${gridCollections.length} collection${gridCollections.length === 1 ? "" : "s"} · ${totalSessions} session${totalSessions === 1 ? "" : "s"} total`;
    } else {
      agg.hidden = true;
      agg.textContent = "";
    }
  }
  if (!gridCollections.length) {
    host.innerHTML = emptyGridHTML();
    wireEmptyGridActions();
    return;
  }
  if (!filtered.length) {
    host.innerHTML = `<div class="empty"><div class="big">${icon("search", { size: 40 })}</div>No collections match your filter.</div>`;
    return;
  }
  host.innerHTML = filtered.map(collectionCardHTML).join("");
  $$("#collectionsGrid .coll-card").forEach((el) =>
    el.addEventListener("click", (e) => {
      if (e.target.closest(".coll-pin-btn")) return;
      openCollection(el.dataset.id);
    })
  );
  $$("#collectionsGrid .coll-pin-btn").forEach((btn) =>
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleCollectionPin(btn.dataset.id, btn.dataset.pinned !== "1");
    })
  );
}

function emptyGridHTML() {
  return `<div class="empty coll-empty">
    <div class="big">${icon("layers", { size: 40 })}</div>
    <h3>No collections yet</h3>
    <p class="muted">Auto-updating collections follow a saved search. Manual collections hold sessions you pick by hand.</p>
    <div class="coll-empty-actions">
      <button type="button" class="btn btn-primary" id="collEmptyCreate">${icon("plus")} Create collection</button>
      <button type="button" class="btn" id="collEmptySearch">${icon("search")} Search &amp; save as collection</button>
    </div>
  </div>`;
}

function wireEmptyGridActions() {
  $("#collEmptyCreate")?.addEventListener("click", () => openCollectionDialog({ mode: "create" }));
  $("#collEmptySearch")?.addEventListener("click", () => {
    showList();
    setTimeout(() => $("#search")?.focus(), 60);
  });
}

function collectionCardHTML(c) {
  const n = c.count || 0;
  const auto = !ruleIsEmpty(c.rule);
  const accent = collAccentClass(c.color);
  const pinned = !!c.pinned;
  const kindLabel = auto ? "auto-updating" : "manual";
  const kindIcon = auto ? icon("sparkles", { size: 12 }) : icon("archive", { size: 12 });
  const updated = fmtRelativeTime(c.updated_at);
  const preview = auto ? `<div class="coll-card-rules">${rulePreviewHTML(c.rule, 2)}</div>` : "";
  return `<div class="coll-card ${accent}${pinned ? " coll-card-pinned" : ""}" data-id="${esc(c.id)}">
    <button type="button" class="coll-pin-btn${pinned ? " on" : ""}" data-id="${esc(c.id)}" data-pinned="${pinned ? "1" : "0"}" title="${pinned ? "Unpin" : "Pin to top"}" aria-label="${pinned ? "Unpin collection" : "Pin collection"}">${icon("star", { size: 14 })}</button>
    <div class="coll-card-icon">${esc(collIconOf(c))}</div>
    <div class="coll-card-body">
      <h3>${esc(c.name)}</h3>
      ${c.description ? `<p class="coll-card-desc">${esc(c.description)}</p>` : ""}
      ${preview}
      <div class="coll-card-meta">
        <span class="pill">${n} session${n === 1 ? "" : "s"}</span>
        <span class="pill coll-kind${auto ? " coll-kind-auto" : ""}">${kindIcon} ${kindLabel}</span>
        ${updated ? `<span class="pill coll-updated">${esc(updated)}</span>` : ""}
      </div>
    </div>
  </div>`;
}

async function toggleCollectionPin(id, pin) {
  try {
    await api(`/api/collections/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pinned: pin }),
    });
    toast(pin ? "Collection pinned" : "Collection unpinned");
    await loadCollectionsGrid();
  } catch (e) { toast(e.message, true); }
}

export async function openCollection(id, opts = {}) {
  state.currentCollectionId = id;
  state.currentId = null;
  teardownReading();
  try {
    const c = await api("/api/collections/" + encodeURIComponent(id));
    const leaving = state.view !== "collection";
    state.view = "collection";
    const apply = () => { setLayoutWide(true); showOnly("#collectionView"); renderCollection(c); };
    if (leaving) withTransition(apply); else apply();
    if (!opts.fromHash) location.hash = "#/collection/" + encodeURIComponent(id);
    window.scrollTo({ top: 0, behavior: "smooth" });
  } catch (e) {
    toast(e.message, true);
    showCollections();
  }
}

function collAskExamples(c, ov) {
  const examples = [
    `Summarize what I worked on in \u201C${c.name}\u201D`,
    "What were the main topics in this collection?",
  ];
  const top = (ov.topics || [])[0];
  if (top && top.tag) examples.push(`What did I learn about ${top.tag}?`);
  examples.push("Which sessions took the longest?");
  return examples.slice(0, 4);
}

function renderCollAskExamples(c, ov) {
  const host = $("#collAskExamples");
  if (!host) return;
  if ($("#collAskAnswer") && !$("#collAskAnswer").hidden) { host.hidden = true; return; }
  host.innerHTML =
    `<div class="ask-ex-label">${icon("sparkles", { size: 13 })} Try asking</div>` +
    `<div class="ask-ex-chips">${
      collAskExamples(c, ov).map((q) => `<button class="ask-ex" type="button">${esc(q)}</button>`).join("")
    }</div>`;
  host.hidden = false;
  $$(".ask-ex", host).forEach((b) =>
    b.addEventListener("click", () => {
      $("#collAskInput").value = b.textContent;
      $("#collAskForm").requestSubmit();
    })
  );
}

function renderCollection(c) {
  const ov = c.overview || {};
  const t = ov.totals || {};
  const view = $("#collectionView");
  const accent = collAccentClass(c.color);
  memberSort = "recent";

  const stats = [
    [`${t.sessions || 0}`, "sessions", ""],
    [fmtCost(t.cost || 0), "est. spend", "usage-stat-cost"],
    [`${t.files || 0}`, "files touched", ""],
    [fmtDuration(t.duration) || "0s", "time", ""],
    [`${t.premium || 0}`, "premium reqs", ""],
  ].map(([v, k, cls]) =>
    `<div class="usage-stat${cls ? ` ${cls}` : ""}"><div class="us-val">${v}</div><div class="us-key">${k}</div></div>`
  ).join("");

  const span = (ov.date_min || ov.date_max)
    ? `<span class="pill">${icon("calendar")} ${fmtDate(ov.date_min)} \u2013 ${fmtDate(ov.date_max)}</span>` : "";

  const topics = (ov.topics || []).slice(0, 10)
    .map((tp) => `<span class="t">${esc(tp.tag)}<em>${tp.count || ""}</em></span>`).join("");

  const activityChart = activityColChart(ov.by_day || [], { title: "Sessions over time" });
  const sourceChart = sessionBarChart("By source", ov.by_source || [], "source");

  const members = sortMembers(c.members || [], memberSort);
  const membersHTML = members.length
    ? members.map((m) =>
      `<div class="coll-member">${cardHTML(m)}<button class="coll-remove" data-id="${esc(m.id)}" title="Remove from this collection" aria-label="Remove from collection">${icon("x", { size: 14 })}</button></div>`
    ).join("")
    : `<div class="empty"><div class="big">${icon("archive", { size: 40 })}</div>No sessions in this collection yet.${ruleIsEmpty(c.rule) ? " Open a conversation and use \u201CCollection\u201D to add it." : ""}</div>`;

  const pinned = !!c.pinned;

  view.innerHTML = `
    <div class="detail-head">
      <div class="detail-top">
        <span class="back" id="collBack" role="button" tabindex="0">${icon("arrow-left", { size: 15 })} All collections</span>
        <div class="detail-actions">
          <button class="btn btn-ghost coll-pin-detail${pinned ? " on" : ""}" id="collPinDetail" title="${pinned ? "Unpin" : "Pin to top"}" aria-label="${pinned ? "Unpin collection" : "Pin collection"}">${icon("star")} ${pinned ? "Pinned" : "Pin"}</button>
          <button class="btn btn-ghost" id="collEdit" title="Edit collection">${icon("pencil")} Edit</button>
          <button class="btn btn-ghost" id="collDelete" title="Delete this collection">${icon("trash")} Delete</button>
        </div>
      </div>
      <h1><span class="coll-title-icon ${accent}">${esc(collIconOf(c))}</span> ${esc(c.name)}</h1>
      ${c.description ? `<p class="detail-summary">${esc(c.description)}</p>` : ""}
    </div>

    <div class="coll-rule-card">
      <div class="coll-rule-card-head">
        <span class="crs-label">Auto-includes</span>
        <button type="button" class="btn btn-ghost coll-rule-edit" id="collEditFilters">${icon("pencil", { size: 14 })} Edit filters</button>
      </div>
      <div class="coll-rule-line">${ruleSummary(c.rule)} ${span}</div>
      <p class="coll-rule-hint muted">Manual adds and removes stick across re-syncs.</p>
    </div>

    <div class="coll-dashboard">
      <div class="coll-dashboard-main">
        <div class="usage-stats coll-stats">${stats}</div>
        ${sourceChart}
      </div>
      <div class="coll-dashboard-side">
        ${activityChart}
        ${topics ? `<div class="coll-topics"><h4>Topics</h4><div class="card-tags">${topics}</div></div>` : ""}
      </div>
    </div>

    <div class="coll-ask" id="collAskPanel">
      <button type="button" class="coll-ask-toggle" id="collAskToggle" aria-expanded="true">
        <span>${icon("sparkles")} Ask this collection</span>
        ${icon("chevron-down", { size: 16 })}
      </button>
      <div class="coll-ask-body" id="collAskBody">
        <form class="ask-form" id="collAskForm">
          <textarea id="collAskInput" rows="2" placeholder="Ask a question answered only from these conversations\u2026"></textarea>
          <select id="collAskLimit" class="ask-limit" title="How many conversations to draw from" aria-label="Sources to consider">
            <option value="6">6 sources</option>
            <option value="8" selected>8 sources</option>
            <option value="12">12 sources</option>
            <option value="16">16 sources</option>
            <option value="20">20 sources</option>
          </select>
          <button class="btn btn-primary" id="collAskSend" type="submit">Ask</button>
        </form>
        <div id="collAskStatus" class="ask-status" hidden></div>
        <div id="collAskExamples" class="ask-examples" hidden></div>
        <div id="collAskSources" class="ask-sources" hidden></div>
        <div id="collAskAnswer" class="ask-answer md" hidden></div>
      </div>
    </div>

    <div class="list-head coll-members-head">
      <h2>Sessions</h2>
      <span class="muted">${(c.members || []).length} session${(c.members || []).length === 1 ? "" : "s"}</span>
      <select id="collMemberSort" class="coll-member-sort" aria-label="Sort sessions">
        <option value="recent">Most recent</option>
        <option value="oldest">Oldest</option>
        <option value="turns">Longest</option>
        <option value="title">Title A–Z</option>
      </select>
    </div>
    <div class="results coll-members">${membersHTML}</div>`;

  $("#collBack").addEventListener("click", () => showCollections());
  $("#collEdit").addEventListener("click", () => openCollectionDialog({ mode: "edit", coll: c }));
  $("#collEditFilters").addEventListener("click", () => openCollectionDialog({ mode: "edit", coll: c, focusRule: true }));
  $("#collDelete").addEventListener("click", () => deleteCollection(c));
  $("#collPinDetail").addEventListener("click", async () => {
    await toggleCollectionPin(c.id, !c.pinned);
    openCollection(c.id, { fromHash: true });
  });

  $("#collMemberSort").addEventListener("change", (e) => {
    memberSort = e.target.value;
    const sorted = sortMembers(c.members || [], memberSort);
    const host = $(".coll-members", view);
    if (!host) return;
    host.innerHTML = sorted.length
      ? sorted.map((m) =>
        `<div class="coll-member">${cardHTML(m)}<button class="coll-remove" data-id="${esc(m.id)}" title="Remove from this collection" aria-label="Remove from collection">${icon("x", { size: 14 })}</button></div>`
      ).join("")
      : `<div class="empty"><div class="big">${icon("archive", { size: 40 })}</div>No sessions in this collection yet.</div>`;
    wireMemberClicks(c);
  });

  wireMemberClicks(c);

  $("#collAskToggle").addEventListener("click", () => {
    const body = $("#collAskBody");
    const btn = $("#collAskToggle");
    body.hidden = !body.hidden;
    btn.classList.toggle("collapsed", body.hidden);
    btn.setAttribute("aria-expanded", body.hidden ? "false" : "true");
  });

  checkCollAskStatus();
  renderCollAskExamples(c, ov);
  const askForm = $("#collAskForm");
  askForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("#collAskInput").value.trim();
    if (!q) return;
    const limit = parseInt($("#collAskLimit").value, 10) || 8;
    $("#collAskExamples").hidden = true;
    streamAsk(
      `/api/collections/${encodeURIComponent(c.id)}/ask`,
      { question: q, limit },
      { answerEl: $("#collAskAnswer"), sourcesEl: $("#collAskSources"), sendBtn: $("#collAskSend") }
    );
  });
  $("#collAskInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); askForm.requestSubmit(); }
  });
}

function wireMemberClicks(c) {
  $$("#collectionView .coll-member .card").forEach((el) =>
    el.addEventListener("click", (e) => {
      if (e.target.closest(".coll-remove")) return;
      openSession(el.dataset.id);
    })
  );
  $$("#collectionView .coll-remove").forEach((b) =>
    b.addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        await api(`/api/collections/${encodeURIComponent(c.id)}/members/${encodeURIComponent(b.dataset.id)}`, { method: "DELETE" });
        toast("Removed from collection");
        openCollection(c.id, { fromHash: true });
      } catch (err) { toast(err.message, true); }
    })
  );
}

async function checkCollAskStatus() {
  const note = $("#collAskStatus");
  if (!note) return;
  try {
    const st = await api("/api/ask/status");
    if (!st.available) {
      note.hidden = false;
      note.innerHTML = `No local LLM detected. Install <a href="https://ollama.com" target="_blank" rel="noopener">Ollama</a> and run <code>ollama serve</code> to ask this collection. Everything stays on your machine.`;
      $("#collAskSend").disabled = true;
    } else {
      note.hidden = true;
      $("#collAskSend").disabled = false;
    }
  } catch (_) { /* leave as-is */ }
}

async function deleteCollection(c) {
  if (!window.confirm(`Delete the collection \u201C${c.name}\u201D? Your conversations are not deleted \u2014 only this grouping.`)) return;
  try {
    await api("/api/collections/" + encodeURIComponent(c.id), { method: "DELETE" });
    toast("Collection deleted");
    showCollections();
  } catch (e) { toast(e.message, true); }
}

// ----- create / edit dialog -----
function readRuleFromDialog() {
  const q = $("#collRuleQ").value.trim();
  const source = $("#collRuleSource").value || null;
  const repo = $("#collRuleRepo").value.trim() || null;
  const tagsRaw = $("#collRuleTags").value.trim();
  const tags = tagsRaw ? tagsRaw.split(",").map((t) => t.trim()).filter(Boolean) : [];
  const date_from = $("#collRuleFrom").value || null;
  const date_to = $("#collRuleTo").value || null;
  const modeBtn = $("#collRuleMode button.active");
  const mode = modeBtn ? modeBtn.dataset.mode : "hybrid";
  const rule = {};
  if (q) rule.q = q;
  if (source) rule.source = source;
  if (repo) rule.repo = repo;
  if (tags.length) rule.tags = tags;
  if (date_from) rule.date_from = date_from;
  if (date_to) rule.date_to = date_to;
  if (q && mode && mode !== "hybrid") rule.mode = mode;
  return ruleIsEmpty(rule) ? null : rule;
}

function fillRuleDialog(rule) {
  const r = rule || {};
  $("#collRuleQ").value = r.q || "";
  $("#collRuleSource").value = r.source || "";
  $("#collRuleRepo").value = r.repo || "";
  $("#collRuleTags").value = (r.tags || []).join(", ");
  $("#collRuleFrom").value = r.date_from || "";
  $("#collRuleTo").value = r.date_to || "";
  const mode = r.mode || "hybrid";
  $$("#collRuleMode button").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  syncRuleModeVisibility();
  updateRulePreview();
}

function syncRuleModeVisibility() {
  const modeEl = $("#collRuleMode");
  if (modeEl) modeEl.hidden = !$("#collRuleQ").value.trim();
}

function updateRulePreview() {
  const rule = readRuleFromDialog();
  const prev = $("#collRulePreview");
  if (!prev) return;
  if (rule && !ruleIsEmpty(rule)) {
    prev.hidden = false;
    prev.innerHTML = `<span class="crs-label">Preview</span> ${ruleSummary(rule)}`;
  } else {
    prev.hidden = true;
    prev.innerHTML = "";
  }
}

function wireDialogControls() {
  if (wireDialogControls.done) return;
  wireDialogControls.done = true;

  $$("#collColorPicker .coll-color-swatch").forEach((sw) =>
    sw.addEventListener("click", () => {
      $$("#collColorPicker .coll-color-swatch").forEach((s) => s.classList.remove("active"));
      sw.classList.add("active");
    })
  );

  $("#collRuleClear")?.addEventListener("click", () => {
    fillRuleDialog(null);
    collDialogState.rule = null;
  });

  ["collRuleQ", "collRuleRepo", "collRuleTags"].forEach((id) =>
    $(`#${id}`)?.addEventListener("input", () => { syncRuleModeVisibility(); updateRulePreview(); })
  );
  ["collRuleSource", "collRuleFrom", "collRuleTo"].forEach((id) =>
    $(`#${id}`)?.addEventListener("change", updateRulePreview)
  );
  $$("#collRuleMode button").forEach((b) =>
    b.addEventListener("click", () => {
      $$("#collRuleMode button").forEach((x) => x.classList.toggle("active", x === b));
      updateRulePreview();
    })
  );
}

function selectedDialogColor() {
  const active = $("#collColorPicker .coll-color-swatch.active");
  const color = active ? active.dataset.color : "";
  return color || null;
}

function setDialogColor(color) {
  $$("#collColorPicker .coll-color-swatch").forEach((s) => {
    s.classList.toggle("active", (s.dataset.color || "") === (color || ""));
  });
}

export function openCollectionDialog({ mode = "create", coll = null, rule = null, pendingSession = null, focusRule = false } = {}) {
  wireDialogControls();
  const initialRule = rule != null ? rule : (coll ? coll.rule : null);
  collDialogState = {
    mode,
    id: coll && coll.id ? coll.id : null,
    rule: initialRule,
    pendingSession,
  };
  $("#collectionDialogTitle").textContent = mode === "edit" ? "Edit collection" : "New collection";
  $("#collName").value = (coll && coll.name) || "";
  $("#collIcon").value = (coll && coll.icon) || "";
  $("#collDescription").value = (coll && coll.description) || "";
  $("#collPinned").checked = !!(coll && coll.pinned);
  setDialogColor(coll && coll.color);
  fillRuleDialog(initialRule);
  $("#collectionDialog").showModal();
  setTimeout(() => {
    if (focusRule) $("#collRuleQ")?.focus();
    else $("#collName").focus();
  }, 50);
}

export async function saveCollection() {
  const name = $("#collName").value.trim();
  if (!name) return toast("Give the collection a name", true);
  const rule = readRuleFromDialog();
  const payload = {
    name,
    icon: $("#collIcon").value.trim() || null,
    description: $("#collDescription").value.trim() || null,
    color: selectedDialogColor(),
    pinned: $("#collPinned").checked,
    rule,
  };
  try {
    let coll;
    if (collDialogState.mode === "edit" && collDialogState.id) {
      coll = await api("/api/collections/" + encodeURIComponent(collDialogState.id), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } else {
      coll = await api("/api/collections", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    }
    if (collDialogState.pendingSession && coll && coll.id) {
      try {
        await api(`/api/collections/${encodeURIComponent(coll.id)}/members`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: collDialogState.pendingSession }),
        });
      } catch (_) { /* non-fatal */ }
    }
    $("#collectionDialog").close();
    toast("Collection saved");
    if (state.view === "collection" && coll && coll.id) {
      openCollection(coll.id, { fromHash: true });
    } else if (state.view === "collections") {
      loadCollectionsGrid();
    } else if (coll && coll.id) {
      openCollection(coll.id);
    }
  } catch (e) { toast(e.message, true); }
}

export function saveCollectionFromFilters() {
  const rule = currentRule();
  if (ruleIsEmpty(rule)) return toast("Add a search or filter first", true);
  const name = rule.q || rule.repo || (rule.tags && rule.tags[0]) ||
    (rule.source ? srcMeta(rule.source).label : "") || "New collection";
  openCollectionDialog({ mode: "create", coll: { name }, rule });
}

function collMenuDot(c) {
  const cls = collAccentClass(c.color);
  return cls ? `<span class="cm-dot ${cls}" aria-hidden="true"></span>` : "";
}

// ----- add-to-collection menu (from a session) -----
export async function openCollMenu(anchor, sessionId) {
  const menu = $("#collMenu");
  let items;
  try { items = await api(`/api/sessions/${encodeURIComponent(sessionId)}/collections`); }
  catch (e) { return toast(e.message, true); }
  const rows = items.length
    ? items.map((c) =>
      `<button class="cm-item${c.member ? " on" : ""}" data-id="${esc(c.id)}">
        <span class="cm-mark">${c.member ? icon("check", { size: 14 }) : icon("plus", { size: 14 })}</span>
        ${collMenuDot(c)}<span class="cm-name">${c.pinned ? icon("star", { size: 12 }) : ""}${esc(collIconOf(c))} ${esc(c.name)}</span>
      </button>`
    ).join("")
    : `<div class="cm-empty muted">No collections yet</div>`;
  menu.innerHTML = rows + `<button class="cm-item cm-new" data-new="1"><span class="cm-mark">${icon("plus", { size: 14 })}</span> <span class="cm-name">New collection\u2026</span></button>`;
  const r = anchor.getBoundingClientRect();
  menu.style.top = (window.scrollY + r.bottom + 6) + "px";
  menu.style.left = (window.scrollX + Math.max(8, Math.min(r.left, window.innerWidth - 268))) + "px";
  menu.hidden = false;
  $$(".cm-item", menu).forEach((b) =>
    b.addEventListener("click", async () => {
      if (b.dataset.new) {
        menu.hidden = true;
        openCollectionDialog({ mode: "create", pendingSession: sessionId });
        return;
      }
      const cid = b.dataset.id;
      const on = b.classList.contains("on");
      try {
        if (on) {
          await api(`/api/collections/${encodeURIComponent(cid)}/members/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
          toast("Removed from collection");
        } else {
          await api(`/api/collections/${encodeURIComponent(cid)}/members`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ session_id: sessionId }),
          });
          toast("Added to collection");
        }
        openCollMenu(anchor, sessionId);
      } catch (e) { toast(e.message, true); }
    })
  );
}

export function hideCollMenu() {
  const menu = $("#collMenu");
  if (menu) menu.hidden = true;
}
