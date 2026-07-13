"use strict";

// Sidebar: headline stat cards, faceted filters (source/repo/topic/date), and
// keeping the filter chips' active state in sync with `state`.

import { api } from "./api.js";
import { state } from "./state.js";
import { $, $$, esc, srcMeta } from "./utils.js";

export async function loadStats() {
  const s = await api("/api/stats");
  const cards = [
    { n: s.sessions ?? 0, l: "sessions" },
    { n: s.turns ?? 0, l: "turns" },
    { n: s.files ?? 0, l: "files" },
    { n: s.tags ?? 0, l: "topics" },
  ];
  // Spend now lives on the dedicated Usage page, so the sidebar stays a clean
  // set of count cards.
  $("#statCards").innerHTML =
    cards.map((c) => `<div class="stat-card"><div class="n">${c.n}</div><div class="l">${c.l}</div></div>`).join("");
  return s;
}

export async function loadFacets() {
  const f = await api("/api/facets");
  state.facets = f;

  $("#sourceFilters").innerHTML = (f.sources || [])
    .map((s) => `<div class="chip" data-source="${esc(s.source)}">${srcMeta(s.source).icon} ${esc(srcMeta(s.source).label)} <span class="c">${s.count}</span></div>`)
    .join("") || `<span class="muted">—</span>`;

  $("#repoFilters").innerHTML = (f.repositories || [])
    .map((r) => `<div class="facet" data-repo="${esc(r.name)}"><span class="name">${esc(r.name)}</span><span class="count">${r.count}</span></div>`)
    .join("") || `<span class="muted">No repositories</span>`;

  $("#tagFilters").innerHTML = (f.tags || [])
    .map((t) => `<div class="chip" data-tag="${esc(t.tag)}">${esc(t.tag)}<span class="c">${t.count}</span></div>`)
    .join("") || `<span class="muted">No topics yet</span>`;

  const dmin = (f.date_min || "").slice(0, 10);
  const dmax = (f.date_max || "").slice(0, 10);
  for (const id of ["#dateFrom", "#dateTo"]) {
    if (dmin) $(id).min = dmin;
    if (dmax) $(id).max = dmax;
  }

  syncFilterUI();
}

export function syncFilterUI() {
  $$("#sourceFilters .chip").forEach((c) => c.classList.toggle("active", c.dataset.source === state.source));
  $$("#repoFilters .facet").forEach((c) => c.classList.toggle("active", c.dataset.repo === state.repo));
  $$("#tagFilters .chip").forEach((c) => c.classList.toggle("active", state.tags.has(c.dataset.tag)));
}
