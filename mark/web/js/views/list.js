"use strict";

// Search / browse list view: query execution, result cards, near-duplicate
// grouping, the active-filter summary bar, and keyboard navigation.

import { api } from "../api.js";
import { PAGE_SIZE, showOnly, state } from "../state.js";
import { syncFilterUI } from "../sidebar.js";
import {
  $, $$, debounce, esc, fmtCost, fmtDate, fmtDuration, normTitle, srcMeta, withTransition,
} from "../utils.js";
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
    $("#results").innerHTML = `<div class="empty"><div class="big">⚠️</div>${esc(e.message)}</div>`;
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
  $("#listTitle").textContent = state.q ? `Results for “${state.q}”` : "Recent sessions";
  $("#listCount").textContent = results.length
    ? `${results.length}${hasMore ? "+" : ""} ${results.length === 1 ? "session" : "sessions"}`
    : "";
  kbdIndex = -1;
  renderActiveFilters();

  if (!results.length) {
    $("#results").innerHTML = `<div class="empty"><div class="big">${state.q ? "🔍" : "🗂️"}</div>${
      state.q ? "No conversations match. Try semantic mode or different words." : "Nothing here yet — re-scan or add a note."
    }</div>`;
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

export function cardHTML(r, gid = "", groupSize = 1) {
  const score = r.score != null ? `<div class="score" title="relevance"><i style="width:${Math.round(r.score * 100)}%"></i></div>` : "";
  const repo = r.repository ? `<span class="pill">📁 ${esc(r.repository)}</span>` : "";
  const src = `<span class="pill src-${r.source}">${srcMeta(r.source).icon} ${esc(srcMeta(r.source).label)}</span>`;
  const tags = (r.tags || []).slice(0, 4).map((t) => `<span class="t">${esc(t)}</span>`).join("");
  const dur = fmtDuration(r.duration_seconds);
  const cost = r.est_cost_usd ? fmtCost(r.est_cost_usd) : "";
  const dupe = groupSize > 1
    ? `<button class="dupe-badge" data-group="${gid}" title="Show ${groupSize - 1} more similar session${groupSize - 1 === 1 ? "" : "s"}">⧉ ${groupSize}</button>`
    : "";
  return `
    <div class="card" data-id="${esc(r.id)}"${gid ? ` data-group-rep="${gid}"` : ""}>
      <div class="card-top">
        <h3 class="card-title">${esc(r.title || "Untitled")}</h3>
        ${dupe}
        ${score}
      </div>
      <div class="card-snippet">${r.snippet || esc(r.summary || "")}</div>
      <div class="card-meta">
        ${src}${repo}
        <span class="pill">🕑 ${fmtDate(r.updated_at || r.created_at)}</span>
        ${r.turn_count ? `<span class="pill">💬 ${r.turn_count}</span>` : ""}
        ${dur ? `<span class="pill">⏱ ${dur}</span>` : ""}
        ${cost ? `<span class="pill cost">~${cost}</span>` : ""}
        <div class="card-tags">${tags}</div>
      </div>
    </div>`;
}

function wireCards() {
  $$("#results .card").forEach((el) =>
    el.addEventListener("click", (e) => {
      if (e.target.closest(".dupe-badge")) return;
      openSession(el.dataset.id);
    })
  );
  $$("#results .dupe-badge").forEach((b) =>
    b.addEventListener("click", (e) => { e.stopPropagation(); toggleGroup(b); })
  );
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
  if (state.source) chips.push({ type: "source", val: state.source, label: `${srcMeta(state.source).icon} ${srcMeta(state.source).label}` });
  if (state.repo) chips.push({ type: "repo", val: state.repo, label: `📁 ${state.repo}` });
  [...state.tags].forEach((t) => chips.push({ type: "tag", val: t, label: `# ${t}` }));
  if (state.dateFrom || state.dateTo) {
    chips.push({ type: "date", val: "", label: `🗓 ${state.dateFrom || "..."} → ${state.dateTo || "..."}` });
  }

  if (!chips.length) { el.hidden = true; el.innerHTML = ""; return; }
  el.hidden = false;
  el.innerHTML =
    `<span class="af-label">Filtered by</span>` +
    chips.map((c) => `<button class="af-chip" data-type="${c.type}" data-val="${esc(c.val)}">${esc(c.label)}<span class="x">×</span></button>`).join("") +
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
