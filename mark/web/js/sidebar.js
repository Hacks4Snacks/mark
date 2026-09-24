"use strict";

// Sidebar: headline stat cards, faceted filters (source/repo/topic/date), and
// keeping the filter chips' active state in sync with `state`.

import { api } from "./api.js";
import { state, showOnly, setLayoutDash } from "./state.js";
import { $, $$, esc, srcMeta, withTransition } from "./utils.js";
import { teardownReading } from "./views/detail.js";

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

// Source diagnostics use the existing app heartbeat. Only the visible health
// view requests detailed counts; no additional background scheduler is created.
let healthGeneration = 0;
let healthRequest = 0;
let healthInFlight = false;
let healthUpdatedAt = 0;
let latestCoordinator = null;
let lastCoordinatorFinished = null;
let healthActionBusy = false;
let coordinatorRevision = 0;

const timestamp = (value) => {
  if (!value) return "Not recorded";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Unknown" : date.toLocaleString();
};
const number = (value) => Number.isFinite(value) ? value.toLocaleString() : "Unknown";
const badge = (label, tone = "neutral") => `<span class="health-badge ${tone}">${esc(label)}</span>`;
const facts = (items) => `<dl class="health-facts">${items.map(([label, value]) =>
  `<div><dt>${esc(label)}</dt><dd>${esc(String(value))}</dd></div>`).join("")}</dl>`;

export function describeIndexHealth(index) {
  const coverage = index.coverage || {};
  if (index.error) return { label: "Index error", tone: "warning", hint: "Keyword search remains available for indexed chunks. Check the error below, then retry semantic indexing." };
  if (coverage.keyword_chunks < coverage.total_chunks) return { label: "Keyword coverage gap", tone: "warning", hint: "Some stored chunks are absent from the keyword index. A normal incremental retry may not rebuild unchanged source content; investigate before requesting a full rebuild." };
  if (!coverage.total_chunks) return { label: "Empty archive", tone: "neutral", hint: "No conversation chunks are stored yet. Check source paths or import an export using Add." };
  if (index.pending || !index.active || !coverage.identity || coverage.pending_chunks > 0) return { label: "Semantic indexing pending", tone: "warning", hint: "Keyword search is available. Compatible vectors may still be building, or startup verification is pending; stored vector counts alone do not imply readiness." };
  if (coverage.identity.backend === "builtin-hash") return { label: "Built-in fallback", tone: "warning", hint: "The built-in vectorizer is active, not a transformer model. The optional semantic package and a successful model load are needed for transformer-quality recall." };
  return { label: "Semantic index ready", tone: "good", hint: "The index is active. Coverage counts reflect the configured sampling cap, not a search-quality score." };
}

function indexHTML(index) {
  const coverage = index.coverage || {};
  const identity = coverage.identity;
  const status = describeIndexHealth(index);
  const eligible = coverage.eligible_chunks || 0;
  const embedded = coverage.embedded_chunks;
  const percent = eligible && Number.isFinite(embedded) ? Math.min(100, embedded / eligible * 100) : null;
  return `${badge(status.label, status.tone)}<p>${esc(status.hint)}</p>
    ${facts([
      [index.target_fingerprint ? "Building backend / model" : "Persisted backend / model", identity ? `${identity.backend} · ${identity.model}` : "Not initialized"],
      ["Configured preferred model", index.configured_model || "Unknown"],
      ["Keyword chunks / stored chunks", `${number(coverage.keyword_chunks)} / ${number(coverage.total_chunks)}`],
      ["Compatible vectors / eligible chunks", embedded == null ? `Unknown / ${number(eligible)}` : `${number(embedded)} / ${number(eligible)}`],
      ["Eligible chunks still missing vectors", number(coverage.pending_chunks)],
      ["Chunks excluded by sampling cap", number(coverage.excluded_by_cap)],
      ["Per-session embedding cap", number(index.per_session_cap)],
      ["Stored vector rows (all identities)", number(coverage.stored_vectors)],
      ["Generation", number(index.generation)],
    ])}
    ${percent == null ? "" : `<div class="health-coverage"><progress max="100" value="${percent}" aria-label="Compatible vector coverage of eligible chunks"></progress><span>${percent.toFixed(1)}% of eligible chunks</span></div>`}
    <p class="health-help">Coverage includes the entire local conversation index, including hidden/disabled-source sessions. Curated copies are separate. Counts do not load vector contents or run a model, and do not verify upstream completeness.</p>
    ${index.error ? `<pre class="health-error">${esc(index.error)}</pre>` : ""}`;
}

function sourceHTML(source) {
  const history = source.history || {};
  const names = { healthy: "Scanned", detected: "Detected / not yet scanned", missing: "Missing roots", disabled: "Disabled", degraded: "Partial roots", error: "Error", import: "Import only" };
  const tone = source.health === "healthy" ? "good" : ["error", "degraded", "missing"].includes(source.health) ? "warning" : "neutral";
  const results = Object.entries(history.result || {}).map(([key, count]) => `${number(count)} ${key}`).join(" · ");
  const oldConfig = source.configuration_changed ? " (previous configuration)" : "";
  return `<article class="health-source health-panel" data-source-key="${esc(source.key)}">
    <div class="health-panel-head"><h4>${esc(source.label)}</h4>${badge(names[source.health] || "Unknown", tone)}</div>
    ${facts([
      ["Adapter", source.key], ["Indexed sessions", number(source.indexed)],
      ["Last completed scan" + oldConfig, timestamp(history.last_success_at)],
      ["Last checked" + oldConfig, timestamp(history.last_checked_at)],
      ["Last outcome", history.status || "Not recorded"],
    ])}
    ${source.configuration_changed ? `<p class="health-help">Configuration changed; historical results do not describe the new paths/options.</p>` : ""}
    ${results ? `<p class="health-help">Last attempt counts: ${esc(results)}</p>` : ""}
    <ul class="health-roots">${(source.root_status || []).map(root => `<li><code>${esc(root.path)}</code>${badge(root.state, root.state === "present" ? "neutral" : "warning")}${root.error ? `<span>${esc(root.error)}</span>` : ""}</li>`).join("")}</ul>
    ${source.kind !== "import" && !source.roots.length ? `<p class="health-help">No roots found by discovery or configuration.</p>` : ""}
    ${source.error ? `<pre class="health-error">${esc(source.error)}</pre>` : ""}
    <p class="health-help">${esc(source.action)}</p>
    ${history.last_error ? `<details class="health-last-error"><summary>Last recorded error · ${esc(timestamp(history.last_error_at))}</summary><pre>${esc(history.last_error)}</pre><p class="health-help">Retained for diagnosis after recovery. The current state above is authoritative.</p></details>` : ""}
  </article>`;
}

function paintCoordinator(st) {
  if (state.view !== "sources") return;
  const label = st.stopping ? "Stopping" : st.running ? (st.queued ? "Running · queued follow-up" : "Running")
    : st.queued ? "Queued" : st.retry_required ? "Retry needed" : st.last_error || st.sync_error ? "Error" : st.finished_at ? "Completed" : "Idle";
  $("#healthRunState").textContent = label;
  $("#healthRunMessage").textContent = st.message || "idle";
  $("#healthCoordinatorDetails").innerHTML = facts([
    ["Automatic sync", st.auto_sync ? `Enabled · every ${st.sync_interval}s` : "Disabled · manual retry available"],
    ["Scan worker", st.ingest_worker_alive ? "Available" : "Not running"],
    ["Sync monitor", st.sync_worker_alive ? "Available" : st.auto_sync ? "Not running" : "Disabled"],
    ["Started", timestamp(st.started_at)], ["Finished", timestamp(st.finished_at)],
    ["Retry attempt", st.retry_attempt || 0],
    ["Next automatic retry", st.retry_at ? timestamp(st.retry_at) : st.retry_required ? "Not scheduled (manual retry or queued work)" : "Not needed"],
  ]) + (st.last_error ? `<pre class="health-error">${esc(st.last_error)}</pre>` : "")
    + (st.sync_error ? `<pre class="health-error">Sync monitor: ${esc(st.sync_error)}</pre>` : "");
  $("#healthRetry").disabled = healthActionBusy || !!st.stopping;
  $("#healthRepair").disabled = healthActionBusy || !!st.stopping;
}

export function sourceHealthUnavailable(message) {
  if (state.view !== "sources") return;
  const host = $("#healthReadError");
  host.textContent = `Diagnostics unavailable; displayed values may be stale. ${message}`;
  host.hidden = false;
}

export async function loadSourceHealth() {
  if (state.view !== "sources") return;
  const generation = healthGeneration;
  const request = ++healthRequest;
  const statusRevision = coordinatorRevision;
  healthInFlight = true;
  $("#healthRefresh").disabled = true;
  try {
    const data = await api("/api/health");
    if (generation !== healthGeneration || request !== healthRequest || state.view !== "sources") return;
    healthUpdatedAt = Date.now();
    $("#healthChecked").textContent = `Coverage checked ${timestamp(data.checked_at)} · refreshes about every 10 seconds while this view is open`;
    $("#healthReadError").hidden = true;
    if (statusRevision === coordinatorRevision && !healthActionBusy) latestCoordinator = data.coordinator;
    paintCoordinator(latestCoordinator || data.coordinator);
    const indexMarkup = indexHTML(data.index);
    const sourceMarkup = data.sources.map(sourceHTML).join("") || `<p class="muted">No source adapters are registered.</p>`;
    // Do not reset expanded error details during identical background snapshots.
    if ($("#healthIndex").innerHTML !== indexMarkup) $("#healthIndex").innerHTML = indexMarkup;
    if ($("#healthSources").dataset.snapshot !== sourceMarkup) {
      const expanded = new Set($$("#healthSources .health-source:has(details[open])").map(card => card.dataset.sourceKey));
      $("#healthSources").innerHTML = sourceMarkup;
      $("#healthSources").dataset.snapshot = sourceMarkup;
      $$("#healthSources .health-source").forEach(card => {
        const details = $("details", card);
        if (details && expanded.has(card.dataset.sourceKey)) details.open = true;
      });
    }
  } catch (error) {
    if (generation === healthGeneration && request === healthRequest) sourceHealthUnavailable(error.message);
  } finally {
    if (request === healthRequest) {
      healthInFlight = false;
      $("#healthRefresh").disabled = false;
    }
  }
}

export function observeSourceHealth(st) {
  latestCoordinator = st;
  coordinatorRevision += 1;
  const finished = st.finished_at && st.finished_at !== lastCoordinatorFinished;
  lastCoordinatorFinished = st.finished_at;
  if (state.view !== "sources") return;
  paintCoordinator(st);
  if (!healthInFlight && (finished || Date.now() - healthUpdatedAt >= 10000)) loadSourceHealth();
}

export function showSources(opts = {}) {
  const generation = ++healthGeneration;
  state.view = "sources";
  state.currentId = null;
  teardownReading();
  if (!opts.fromHash && location.hash !== "#/sources") history.pushState(null, "", "#/sources");
  withTransition(() => {
    if (generation !== healthGeneration || state.view !== "sources") return;
    setLayoutDash(true);
    showOnly("#sourcesView");
  });
  if (latestCoordinator) paintCoordinator(latestCoordinator);
  loadSourceHealth();
}

export function setupSourceHealth(requestReindex) {
  const refresh = $("#healthRefresh");
  if (!refresh || refresh.dataset.wired) return;
  refresh.dataset.wired = "1";
  refresh.addEventListener("click", () => loadSourceHealth());
  for (const [selector, semantic] of [["#healthRetry", false], ["#healthRepair", true]]) {
    $(selector).addEventListener("click", async () => {
      if (healthActionBusy) return;
      healthActionBusy = true;
      coordinatorRevision += 1;
      $("#healthRetry").disabled = $("#healthRepair").disabled = true;
      const feedback = $("#healthActionFeedback");
      feedback.textContent = "Requesting a scan...";
      try {
        const st = await requestReindex(semantic);
        feedback.textContent = st.admission === "accepted" ? "Scan queued. Progress is shown above."
          : st.admission === "covered" ? "An existing scan already covers this request."
          : "Mark is stopping; retry after it restarts.";
        if (state.view === "sources") loadSourceHealth();
      } catch (error) { feedback.textContent = `Retry request failed: ${error.message}`; }
      finally {
        healthActionBusy = false;
        if (latestCoordinator) paintCoordinator(latestCoordinator);
      }
    });
  }
}
