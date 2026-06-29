"use strict";

// Search / browse list view: query execution, result cards, near-duplicate
// grouping, the active-filter summary bar, and keyboard navigation.

import { api } from "../api.js";
import { PAGE_SIZE, setLayoutWide, showOnly, state } from "../state.js";
import { loadFacets, loadStats, syncFilterUI } from "../sidebar.js";
import {
  $, $$, debounce, esc, fmtCost, fmtDate, fmtDuration, normTitle, srcMeta, toast, withTransition,
} from "../utils.js";
import { icon } from "../icons.js";
import { openSession, teardownReading } from "./detail.js";
import { toggleSaveCollectionBtn } from "./collections.js";

let kbdIndex = -1;        // keyboard-highlighted result card
let resultGroups = {};    // gid -> [representative, ...near-duplicates]

export async function doSearch(reset = true, opts = {}) {
  if (reset) state.limit = PAGE_SIZE;
  if (!opts.keepView) showList();
  const params = new URLSearchParams();
  if (state.q) params.set("q", state.q);
  params.set("mode", state.mode);
  params.set("sort", state.sort);
  if (state.source) params.set("source", state.source);
  if (state.repo) params.set("repo", state.repo);
  if (state.tags.size) params.set("tags", [...state.tags].join(","));
  if (state.dateFrom) params.set("date_from", state.dateFrom);
  if (state.dateTo) params.set("date_to", state.dateTo);
  if (state.showHidden) params.set("hidden", "1");
  params.set("limit", String(state.limit));

  renderActiveFilters();
  if (reset) {
    $("#results").innerHTML = Array(4).fill(
      '<div class="skeleton"><div class="sk-line lg"></div><div class="sk-line row"></div><div class="sk-line sm"></div></div>'
    ).join("");
  }
  try {
    const data = await api("/api/search?" + params); // empty q falls back to browse server-side
    renderResults(data, reset);
  } catch (e) {
    $("#results").innerHTML = `<div class="empty"><div class="big">${icon("alert", { size: 40 })}</div>${esc(e.message)}</div>`;
  }
}

export const run = debounce(() => doSearch(true), 180);

function loadMore() {
  state.limit += PAGE_SIZE;
  doSearch(false);
}

function renderResults(data, animate = true) {
  const results = data.results || [];
  const hasMore = results.length >= state.limit;
  $("#listTitle").textContent = state.showHidden
    ? (state.q ? `Hidden \u00b7 \u201c${state.q}\u201d` : "Hidden sessions")
    : (state.q ? `Results for \u201c${state.q}\u201d` : "Recent sessions");
  $("#listCount").textContent = results.length
    ? `${results.length}${hasMore ? "+" : ""} ${results.length === 1 ? "session" : "sessions"}`
    : "";
  kbdIndex = -1;
  renderActiveFilters();

  if (!results.length) {
    renderEmptyState();
    return;
  }

  // Group near-duplicate titles, preserving order of first appearance, so a
  // burst of identical threads collapses into one expandable card.
  resultGroups = {};
  const order = [];
  const seen = new Map();
  for (const r of results) {
    const key = normTitle(r.title);
    if (seen.has(key)) {
      resultGroups[seen.get(key)].push(r);
    } else {
      const gid = "g" + order.length;
      seen.set(key, gid);
      resultGroups[gid] = [r];
      order.push(gid);
    }
  }

  const resultsEl = $("#results");
  resultsEl.classList.toggle("no-anim", !animate);
  resultsEl.innerHTML =
    order.map((gid) => cardHTML(resultGroups[gid][0], gid, resultGroups[gid].length)).join("") +
    (hasMore ? `<button class="btn btn-block load-more" id="loadMore">Load ${PAGE_SIZE} more</button>` : "");
  wireCards();
  $("#loadMore")?.addEventListener("click", loadMore);
}

// The containerless Bookmark-M emblem, inline so empty states show the brand at scale.
const EMBLEM = `<svg class="em-mark" viewBox="0 0 32 32" aria-hidden="true" xmlns="http://www.w3.org/2000/svg">
  <defs><linearGradient id="emg" x1="0" y1="0" x2="1" y2="1"><stop stop-color="var(--accent)"/><stop offset="1" stop-color="var(--accent-2)"/></linearGradient></defs>
  <g transform="translate(1 1) scale(0.30)">
    <path d="M24 14 q0 -8 8 -8 h36 q8 0 8 8 v80 l-26 -17 l-26 17 z" fill="url(#emg)"/>
    <path d="M38 60 L38 32 L50 47 L62 32 L62 60" fill="none" stroke="#fff" stroke-width="9" stroke-linecap="round" stroke-linejoin="round"/>
  </g>
</svg>`;

const hasFilters = () =>
  !!(state.source || state.repo || state.tags.size || state.dateFrom || state.dateTo);

// Renders the right empty state for the situation: first-run onboarding, a
// no-results-for-query nudge, a filters-too-narrow prompt, or the hidden bucket.
function renderEmptyState() {
  const box = $("#results");
  if (state.showHidden) {
    box.innerHTML = `<div class="empty"><div class="big">${icon("eye-off", { size: 40 })}</div>No hidden sessions. Use Hide on a conversation to tuck it away here.</div>`;
    return;
  }
  if (state.q) {
    box.innerHTML = `<div class="empty empty-rich">
      <div class="big">${icon("search", { size: 40 })}</div>
      <p class="em-title">No matches for \u201c${esc(state.q)}\u201d</p>
      <p class="em-sub">Try ${state.mode !== "semantic" ? "<b>Semantic</b> mode to search by meaning, or " : ""}different words. Filters can also be narrowing your results.</p>
      <div class="em-actions">
        ${state.mode !== "semantic" ? `<button class="btn btn-primary" data-act="semantic">${icon("sparkles")} Search by meaning</button>` : ""}
        <button class="btn" data-act="ask">${icon("sparkles")} Ask your history instead</button>
        ${hasFilters() ? `<button class="btn btn-ghost" data-act="clear">Clear filters</button>` : ""}
      </div>
    </div>`;
    wireEmptyActions();
    return;
  }
  if (hasFilters()) {
    box.innerHTML = `<div class="empty empty-rich">
      <div class="big">${icon("folder", { size: 40 })}</div>
      <p class="em-title">No conversations match these filters</p>
      <p class="em-sub">Nothing falls inside the current source, repo, topic, or date filters.</p>
      <div class="em-actions"><button class="btn btn-primary" data-act="clear">Clear filters</button></div>
    </div>`;
    wireEmptyActions();
    return;
  }
  // first run — nothing indexed yet
  const srcs = ["vscode", "cli", "cursor", "cline", "chatgpt", "copilot"];
  box.innerHTML = `<div class="empty empty-rich">
    ${EMBLEM}
    <p class="em-title">Your AI memory starts here</p>
    <p class="em-sub">Mark builds a private, searchable archive of your AI coding conversations \u2014 everything stays on your machine. Scan your history to get started, or add a note by hand.</p>
    <div class="em-actions">
      <button class="btn btn-primary" data-act="scan">${icon("sync")} Scan my history</button>
      <button class="btn" data-act="add">${icon("plus")} Add a note</button>
      <button class="btn" data-act="ask">${icon("sparkles")} Ask your history</button>
    </div>
    <div class="em-sources">${srcs.map((s) => `<span class="em-src">${srcMeta(s).icon} ${esc(srcMeta(s).label)}</span>`).join("")}</div>
    <p class="em-hint">Tip: press <kbd>\u2318</kbd><kbd>K</kbd> anytime to search and jump around.</p>
  </div>`;
  wireEmptyActions();
}

function wireEmptyActions() {
  $$("#results [data-act]").forEach((b) =>
    b.addEventListener("click", () => {
      switch (b.dataset.act) {
        case "semantic": {
          state.mode = "semantic";
          $$("#modeToggle button").forEach((x) => x.classList.toggle("active", x.dataset.mode === "semantic"));
          run();
          break;
        }
        case "clear": clearAllFilters(); break;
        case "scan": $("#reindexBtn")?.click(); break;
        case "add": $("#addBtn")?.click(); break;
        case "ask": $("#askBtn")?.click(); break;
      }
    }));
}

export function cardHTML(r, gid = "", groupSize = 1) {
  const score = r.score != null ? `<div class="score" title="relevance"><i style="width:${Math.round(r.score * 100)}%"></i></div>` : "";
  const repo = r.repository ? `<span class="pill">${icon("folder")} ${esc(r.repository)}</span>` : "";
  const src = `<span class="pill src-${r.source}">${srcMeta(r.source).icon} ${esc(srcMeta(r.source).label)}</span>`;
  const tags = (r.tags || []).slice(0, 4).map((t) => `<span class="t">${esc(t)}</span>`).join("");
  const dur = fmtDuration(r.duration_seconds);
  const cost = r.est_cost_usd ? fmtCost(r.est_cost_usd) : "";
  const unhide = state.showHidden
    ? `<button class="card-unhide" data-unhide="${esc(r.id)}" title="Unhide this conversation">${icon("eye", { size: 13 })} Unhide</button>`
    : "";
  const dupe = groupSize > 1
    ? `<button class="dupe-badge" data-group="${gid}" title="Show ${groupSize - 1} more similar session${groupSize - 1 === 1 ? "" : "s"}">${icon("layers", { size: 13 })} ${groupSize}</button>`
    : "";
  return `
    <div class="card" data-id="${esc(r.id)}"${gid ? ` data-group-rep="${gid}"` : ""}>
      <div class="card-top">
        <h3 class="card-title">${esc(r.title || "Untitled")}</h3>
        ${unhide}
        ${dupe}
        ${score}
      </div>
      <div class="card-snippet">${r.snippet || esc(r.summary || "")}</div>
      <div class="card-meta">
        ${src}${repo}
        <span class="pill">${icon("clock")} ${fmtDate(r.updated_at || r.created_at)}</span>
        ${r.turn_count ? `<span class="pill">${icon("message")} ${r.turn_count}</span>` : ""}
        ${dur ? `<span class="pill">${icon("timer")} ${dur}</span>` : ""}
        ${cost ? `<span class="pill cost">~${cost}</span>` : ""}
        <div class="card-tags">${tags}</div>
      </div>
    </div>`;
}

function wireCards() {
  $$("#results .card").forEach((el) =>
    el.addEventListener("click", (e) => {
      if (e.target.closest(".dupe-badge")) return;
      if (e.target.closest(".card-unhide")) return;
      openSession(el.dataset.id);
    })
  );
  $$("#results .dupe-badge").forEach((b) =>
    b.addEventListener("click", (e) => { e.stopPropagation(); toggleGroup(b); })
  );
  $$("#results .card-unhide").forEach((b) =>
    b.addEventListener("click", (e) => { e.stopPropagation(); unhideFromList(b.dataset.unhide); })
  );
}

// Unhide straight from the hidden-sessions list, then refresh counts + list.
async function unhideFromList(id) {
  try {
    await api(`/api/sessions/${encodeURIComponent(id)}/unhide`, { method: "POST" });
    toast("Session unhidden");
    loadStats();
    loadFacets();
    doSearch(true, { keepView: true });
  } catch (e) {
    toast(e.message, true);
  }
}

// Expand/collapse the near-duplicate sessions that sit behind a representative card.
function toggleGroup(badge) {
  const gid = badge.dataset.group;
  const rep = badge.closest(".card");
  const extras = (resultGroups[gid] || []).slice(1);
  if (rep.dataset.expanded === "1") {
    let n = rep.nextElementSibling;
    while (n && n.classList.contains("group-extra")) { const nx = n.nextElementSibling; n.remove(); n = nx; }
    rep.dataset.expanded = "0";
    badge.classList.remove("open");
  } else {
    rep.insertAdjacentHTML("afterend", extras.map((r) => cardHTML(r)).join(""));
    let n = rep.nextElementSibling;
    for (let i = 0; i < extras.length && n; i++) {
      n.classList.add("group-extra");
      const id = n.dataset.id;
      n.addEventListener("click", () => openSession(id));
      n = n.nextElementSibling;
    }
    rep.dataset.expanded = "1";
    badge.classList.add("open");
  }
}

// ---------- active-filter summary ----------
export function renderActiveFilters() {
  toggleSaveCollectionBtn();
  const el = $("#activeFilters");
  if (!el) return;
  const chips = [];
  if (state.source) chips.push({ type: "source", val: state.source, label: `${srcMeta(state.source).icon} ${esc(srcMeta(state.source).label)}`, raw: true });
  if (state.repo) chips.push({ type: "repo", val: state.repo, label: `${icon("folder")} ${esc(state.repo)}`, raw: true });
  [...state.tags].forEach((t) => chips.push({ type: "tag", val: t, label: `# ${t}` }));
  if (state.dateFrom || state.dateTo) {
    chips.push({ type: "date", val: "", label: `${icon("calendar")} ${esc(state.dateFrom || "...")} ${icon("arrow-right", { size: 13 })} ${esc(state.dateTo || "...")}`, raw: true });
  }

  if (!chips.length) { el.hidden = true; el.innerHTML = ""; return; }
  el.hidden = false;
  el.innerHTML =
    `<span class="af-label">Filtered by</span>` +
    chips.map((c) => `<button class="af-chip" data-type="${c.type}" data-val="${esc(c.val)}">${c.raw ? c.label : esc(c.label)}<span class="x">${icon("x", { size: 12 })}</span></button>`).join("") +
    `<button class="af-clear" id="afClear">Clear all</button>`;
}

export function clearAllFilters() {
  state.source = null; state.repo = null; state.tags.clear();
  state.dateFrom = ""; state.dateTo = "";
  $("#dateFrom").value = ""; $("#dateTo").value = "";
  syncFilterUI(); run();
}

export function showList(opts) {
  const leaving = state.view !== "list";
  teardownReading();
  state.currentId = null;
  const apply = () => {
    state.view = "list";
    setLayoutWide(false);
    showOnly("#listView");
  };
  if (leaving) withTransition(apply);
  else apply();
  if (leaving && !(opts && opts.fromHash) && location.hash) {
    history.pushState("", document.title, location.pathname + location.search);
  }
}

function highlightCard() {
  const cards = $$("#results .card");
  cards.forEach((c, i) => c.classList.toggle("kbd-active", i === kbdIndex));
  if (kbdIndex >= 0 && cards[kbdIndex]) cards[kbdIndex].scrollIntoView({ block: "nearest" });
}

// Arrow-key navigation through results (works even while typing a query).
export function handleListKey(e) {
  const cards = $$("#results .card");
  if (!cards.length) return;
  if (e.key === "ArrowDown") {
    e.preventDefault(); kbdIndex = Math.min(kbdIndex + 1, cards.length - 1); highlightCard();
  } else if (e.key === "ArrowUp") {
    e.preventDefault(); kbdIndex = Math.max(kbdIndex - 1, 0); highlightCard();
  } else if (e.key === "Enter" && kbdIndex >= 0 && cards[kbdIndex]) {
    e.preventDefault(); openSession(cards[kbdIndex].dataset.id);
  }
}
