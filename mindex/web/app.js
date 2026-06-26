"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

const PAGE_SIZE = 50;

const state = {
  q: "",
  mode: "hybrid",
  sort: "recent",
  source: null,
  repo: null,
  tags: new Set(),
  dateFrom: "",
  dateTo: "",
  includeAutomation: false,
  view: "list",
  currentId: null,
  currentCollectionId: null,
  limit: PAGE_SIZE,
};

// Every primary view lives in a top-level container; switching simply toggles
// which one is visible. Listing them here keeps the show* helpers in sync.
const VIEW_IDS = [
  "#listView",
  "#detailView",
  "#collectionsView",
  "#collectionView",
  "#libraryView",
  "#usageView",
  "#askView",
];
function showOnly(visibleId) {
  for (const id of VIEW_IDS) {
    const el = $(id);
    if (el) el.hidden = id !== visibleId;
  }
}

const prefersReducedMotion = () =>
  window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// Run a DOM mutation inside a View Transition when supported (graceful fallback).
function withTransition(fn) {
  if (document.startViewTransition && !prefersReducedMotion()) {
    document.startViewTransition(fn);
  } else {
    fn();
  }
}

let kbdIndex = -1;             // keyboard-highlighted result card
let resultGroups = {};         // gid -> [representative, ...near-duplicates]
let detailScrollHandler = null; // active reading-progress listener

// ---------- helpers ----------
async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

const esc = (s) =>
  (s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const SRC = {
  vscode: { icon: "\uD83D\uDCAC", label: "VS Code" },
  cli: { icon: "\uD83E\uDD16", label: "Copilot CLI" },
  cline: { icon: "\uD83D\uDEE0\uFE0F", label: "Cline" },
  zoocode: { icon: "\uD83E\uDD93", label: "Zoo Code" },
  roo: { icon: "\uD83E\uDD98", label: "Roo Code" },
  kilocode: { icon: "\uD83D\uDD36", label: "Kilo Code" },
  chatgpt: { icon: "\u2728", label: "ChatGPT" },
  agent: { icon: "\uD83E\uDDE0", label: "Agent" },
  automation: { icon: "\u23F1\uFE0F", label: "Automation" },
  upload: { icon: "\uD83D\uDCCE", label: "Upload" },
  copilot: { icon: "\uD83D\uDCAC", label: "Copilot" },
};
const srcMeta = (s) => SRC[s] || { icon: "\uD83D\uDCAC", label: s || "session" };

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const days = Math.floor((Date.now() - d) / 86400000);
  if (days === 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 30) return `${days}d ago`;
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

const debounce = (fn, ms = 220) => {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
};

function fmtDuration(s) {
  if (!s || s <= 0) return "";
  s = Math.round(s);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60), h = Math.floor(m / 60);
  if (h > 0) return `${h}h ${m % 60}m`;
  return `${m}m`;
}
function fmtCost(c) {
  if (c == null) return "";
  if (c === 0) return "$0";
  if (c < 0.01) return "<$0.01";
  return "$" + c.toFixed(2);
}
function fmtTokens(n) {
  if (!n) return "0";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return "" + n;
}

function fmtBytes(n) {
  if (!n) return "0 B";
  if (n >= 1 << 20) return (n / (1 << 20)).toFixed(1) + " MB";
  if (n >= 1 << 10) return (n / (1 << 10)).toFixed(1) + " KB";
  return n + " B";
}

let toastTimer;
function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast show" + (isError ? " error" : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = "toast"; }, 2600);
}

// ---------- stats & facets ----------
async function loadStats() {
  const s = await api("/api/stats");
  const cards = [
    { n: s.sessions ?? 0, l: "sessions" },
    { n: s.turns ?? 0, l: "turns" },
    { n: s.files ?? 0, l: "files" },
    { n: s.tags ?? 0, l: "topics" },
  ];
  const cost = s.total_cost_usd != null
    ? `<div class="stat-card cost" style="grid-column:1/-1">
         <div class="n">~${fmtCost(s.total_cost_usd)}</div>
         <div class="l">est. spend · ${s.premium_requests || 0} premium reqs</div>
       </div>`
    : "";
  $("#statCards").innerHTML =
    cards.map((c) => `<div class="stat-card"><div class="n">${c.n}</div><div class="l">${c.l}</div></div>`).join("") +
    cost;
  return s;
}

async function loadFacets() {
  const f = await api("/api/facets");

  $("#sourceFilters").innerHTML = (f.sources || [])
    .map((s) => `<div class="chip" data-source="${esc(s.source)}">${srcMeta(s.source).icon} ${esc(srcMeta(s.source).label)} <span class="c">${s.count}</span></div>`)
    .join("") || `<span class="muted">—</span>`;

  const auto = (f.sources || []).find((s) => s.source === "automation");
  const row = $("#autoToggleRow");
  if (auto) {
    row.hidden = false;
    $("#autoCount").textContent = `(${auto.count})`;
  } else {
    row.hidden = true;
  }

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

function syncFilterUI() {
  $$("#sourceFilters .chip").forEach((c) => c.classList.toggle("active", c.dataset.source === state.source));
  $$("#repoFilters .facet").forEach((c) => c.classList.toggle("active", c.dataset.repo === state.repo));
  $$("#tagFilters .chip").forEach((c) => c.classList.toggle("active", state.tags.has(c.dataset.tag)));
}

// ---------- search / browse ----------
async function doSearch(reset = true, opts = {}) {
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
  if (state.includeAutomation) params.set("include_automation", "true");
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

const run = debounce(() => doSearch(true), 180);

function loadMore() {
  state.limit += PAGE_SIZE;
  doSearch(false);
}

const normTitle = (t) => (t || "Untitled").toLowerCase().replace(/\s+/g, " ").trim();

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

function cardHTML(r, gid = "", groupSize = 1) {
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
function renderActiveFilters() {
  toggleSaveCollectionBtn();
  const el = $("#activeFilters");
  if (!el) return;
  const chips = [];
  if (state.source) chips.push({ type: "source", val: state.source, label: `${srcMeta(state.source).icon} ${srcMeta(state.source).label}` });
  if (state.repo) chips.push({ type: "repo", val: state.repo, label: `📁 ${state.repo}` });
  [...state.tags].forEach((t) => chips.push({ type: "tag", val: t, label: `# ${t}` }));
  if (state.dateFrom || state.dateTo) {
    chips.push({ type: "date", val: "", label: `🗓 ${state.dateFrom || "…"} → ${state.dateTo || "…"}` });
  }
  if (state.includeAutomation) chips.push({ type: "auto", val: "", label: "⏱ automation runs" });

  if (!chips.length) { el.hidden = true; el.innerHTML = ""; return; }
  el.hidden = false;
  el.innerHTML =
    `<span class="af-label">Filtered by</span>` +
    chips.map((c) => `<button class="af-chip" data-type="${c.type}" data-val="${esc(c.val)}">${esc(c.label)}<span class="x">×</span></button>`).join("") +
    `<button class="af-clear" id="afClear">Clear all</button>`;
}

function clearAllFilters() {
  state.source = null; state.repo = null; state.tags.clear();
  state.dateFrom = ""; state.dateTo = "";
  $("#dateFrom").value = ""; $("#dateTo").value = "";
  state.includeAutomation = false; $("#includeAutomation").checked = false;
  syncFilterUI(); run();
}

// ---------- detail ----------
async function openSession(id, opts = {}) {
  try {
    const s = await api("/api/sessions/" + encodeURIComponent(id));
    state.currentId = id;
    withTransition(() => renderDetail(s));
    if (!opts.fromHash) location.hash = "#/session/" + encodeURIComponent(id);
  } catch (e) {
    toast(e.message, true);
  }
}

function renderDetail(s) {
  state.view = "detail";
  showOnly("#detailView");
  const view = $("#detailView");
  window.scrollTo({ top: 0, behavior: "smooth" });

  const manualSet = new Set(s.manual_tags || []);
  const meta = [
    `<span class="pill src-${s.source}">${srcMeta(s.source).icon} ${esc(srcMeta(s.source).label)}</span>`,
    s.repository ? `<span class="pill">📁 ${esc(s.repository)}</span>` : "",
    `<span class="pill">🕑 ${fmtDate(s.updated_at || s.created_at)}</span>`,
    s.turn_count ? `<span class="pill">💬 ${s.turn_count} turns</span>` : "",
    s.model ? `<span class="pill">🧠 ${esc(s.model)}</span>` : "",
    fmtDuration(s.duration_seconds) ? `<span class="pill">⏱ ${fmtDuration(s.duration_seconds)}</span>` : "",
    s.est_cost_usd ? `<span class="pill cost">~${fmtCost(s.est_cost_usd)}${s.tokens_estimated ? " est." : ""}</span>` : "",
  ].join("");

  const files = (s.files || []);
  const refs = (s.refs || []).filter((r) => r.ref_type === "url");
  const asideBlocks = [];

  // Session id + resume
  const isCli = s.source === "cli" || s.source === "automation";
  const resumeCmd = `copilot --resume ${s.id}`;
  asideBlocks.push(`<div><h4>Session</h4>
    ${isCli ? `<div class="resume-hint">Resume in Copilot CLI</div>
      <div class="copy-row"><code>${esc(resumeCmd)}</code><button class="copy-btn" data-copy="${esc(resumeCmd)}" title="Copy">⧉</button></div>`
      : `<div class="copy-row"><code title="${esc(s.id)}">${esc(s.id)}</code><button class="copy-btn" data-copy="${esc(s.id)}" title="Copy">⧉</button></div>`}
  </div>`);

  // Usage / cost
  const usageRows = [];
  if (s.model) usageRows.push(["Model", esc(s.model)]);
  if (s.duration_seconds) usageRows.push(["Duration", fmtDuration(s.duration_seconds)]);
  if (s.input_tokens || s.output_tokens) usageRows.push(["Tokens", `${fmtTokens(s.input_tokens)} in · ${fmtTokens(s.output_tokens)} out`]);
  if (s.premium_requests) usageRows.push(["Premium reqs", s.premium_requests]);
  if (s.aiu) usageRows.push(["AIU", s.aiu]);
  if (s.est_cost_usd != null) usageRows.push([`Est. cost${s.tokens_estimated ? " *" : ""}`, `~${fmtCost(s.est_cost_usd)}`]);
  if (usageRows.length) {
    asideBlocks.push(`<div><h4>Usage</h4><div class="usage">${
      usageRows.map(([k, v]) => `<div class="usage-row"><span>${k}</span><b>${v}</b></div>`).join("")
    }</div>${s.tokens_estimated ? '<div class="usage-note">* token counts estimated from text</div>' : ""}</div>`);
  }

  if (files.length) {
    asideBlocks.push(`<div><h4>Files (${files.length})</h4><div class="aside-files">${
      files.slice(0, 40).map((f) => `<div class="aside-file" title="${esc(f.file_path)}">${esc(f.file_path)}</div>`).join("")
    }</div></div>`);
  }
  if (refs.length) {
    asideBlocks.push(`<div><h4>Links (${refs.length})</h4><div class="aside-files">${
      refs.slice(0, 25).map((r) => `<a class="aside-file" href="${esc(r.ref_value)}" target="_blank" rel="noopener" style="direction:ltr">${esc(r.ref_value)}</a>`).join("")
    }</div></div>`);
  }
  const topicPills = (s.tags || []).map((t) => {
    const m = manualSet.has(t);
    return `<span class="pill topic${m ? " manual" : ""}" data-tag="${esc(t)}">${esc(t)}${m ? `<button class="topic-x" data-del="${esc(t)}" title="Remove topic">×</button>` : ""}</span>`;
  }).join("");
  asideBlocks.push(`<div><h4>Topics</h4>
    <div class="chips topics-edit">${topicPills || '<span class="muted">none yet</span>'}</div>
    <form class="topic-add" id="topicAdd">
      <input id="topicInput" placeholder="add a topic…" maxlength="40" autocomplete="off" spellcheck="false" />
      <button class="btn btn-ghost" type="submit" title="Add topic">＋</button>
    </form>
  </div>`);

  asideBlocks.push(`<div id="relatedBlock" class="related-block" hidden></div>`);

  const attachments = (s.attachments || []);
  if (attachments.length) {
    asideBlocks.push(`<div><h4>Attachments (${attachments.length})</h4><div class="aside-files">${
      attachments.map((a, i) => `<a class="aside-file" href="#att-${i}" title="${esc(a.filename || "")}">📎 ${esc(a.filename || "file")}</a>`).join("")
    }</div></div>`);
  }

  let body;
  if (s.source === "upload" && s.document) {
    body = `<div class="md">${s.document.html || esc(s.document.content || "")}</div>`;
  } else {
    body = (s.turns || []).map(turnHTML).join("");
  }
  if (attachments.length) {
    body += `<div class="attachments"><h3>Attachments created by the agent</h3>${
      attachments.map((a, i) => {
        const meta = `${esc(a.filename || "file")} · ${fmtBytes(a.size_bytes)}`;
        const inner = a.html
          ? `<div class="md">${a.html}</div>`
          : a.content != null
            ? `<pre class="att-pre">${esc(a.content)}</pre>`
            : `<p class="muted">Not stored (binary or larger than the snapshot limit). Path: ${esc(a.stored_path || "")}</p>`;
        return `<details class="attachment" id="att-${i}"><summary>📎 ${meta}</summary>${inner}</details>`;
      }).join("")
    }</div>`;
  }

  view.innerHTML = `
    <div class="detail-head">
      <div class="detail-top">
        <span class="back" id="backBtn">← Back to results</span>
        <div class="detail-actions">
          <button class="btn btn-ghost" id="addToColl" title="Add this conversation to a collection">＋ Collection</button>
          <button class="btn btn-ghost" id="copyLink" title="Copy a link to this conversation">🔗 Link</button>
          <a class="btn btn-ghost" id="exportMd" href="/api/sessions/${encodeURIComponent(s.id)}/export.md" download title="Download as Markdown">⤓ Markdown</a>
        </div>
      </div>
      <h1>${esc(s.title || "Untitled")}</h1>
      ${s.summary ? `<p class="detail-summary">${esc(s.summary)}</p>` : ""}
      <div class="detail-meta">${meta}</div>
    </div>
    <div class="detail-sticky" id="detailSticky">
      <span class="ds-back" id="dsBack" title="Back to results">←</span>
      <span class="ds-title">${esc(s.title || "Untitled")}</span>
    </div>
    <div class="detail-body">
      <div class="transcript detail-scroll">${body || '<p class="muted">No content.</p>'}</div>
      <div class="detail-aside">${asideBlocks.join("") || '<span class="muted">No attachments.</span>'}</div>
    </div>`;
  $("#backBtn").addEventListener("click", showList);
  $("#dsBack").addEventListener("click", showList);
  $("#addToColl")?.addEventListener("click", (e) => {
    e.stopPropagation();
    openCollMenu(e.currentTarget, s.id);
  });
  $("#copyLink")?.addEventListener("click", async () => {
    const url = location.origin + location.pathname + "#/session/" + encodeURIComponent(s.id);
    try { await navigator.clipboard.writeText(url); toast("Link copied"); }
    catch (_) { toast("Copy failed", true); }
  });
  setupReading();
  $$("#detailView .copy-btn").forEach((b) =>
    b.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(b.dataset.copy); toast("Copied"); }
      catch (_) { toast("Copy failed", true); }
    })
  );
  $("#topicAdd")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const tag = $("#topicInput").value.trim();
    if (!tag) return;
    try {
      await api(`/api/sessions/${encodeURIComponent(s.id)}/tags`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tag }),
      });
      renderDetail(await api("/api/sessions/" + encodeURIComponent(s.id)));
      loadFacets();
      toast("Topic added");
    } catch (err) { toast(err.message, true); }
  });
  $$("#detailView .topic-x").forEach((b) =>
    b.addEventListener("click", async () => {
      try {
        await api(`/api/sessions/${encodeURIComponent(s.id)}/tags/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" });
        renderDetail(await api("/api/sessions/" + encodeURIComponent(s.id)));
        loadFacets();
        toast("Topic removed");
      } catch (err) { toast(err.message, true); }
    })
  );
  loadRelated(s.id);
}

// Semantically nearest sessions, loaded after the detail view paints.
async function loadRelated(id) {
  const host = $("#relatedBlock");
  if (!host) return;
  let items;
  try { items = await api(`/api/sessions/${encodeURIComponent(id)}/related`); }
  catch (_) { return; }
  if (!items || !items.length) return;
  host.innerHTML = `<h4>Related</h4><div class="aside-files">${
    items.map((r) => `<a class="aside-file related" data-id="${esc(r.id)}" title="${esc(r.title || "")}">`
      + `<span class="rel-src">${srcMeta(r.source).icon}</span>`
      + `<span class="rel-title">${esc(r.title || "Untitled")}</span></a>`).join("")
  }</div>`;
  host.hidden = false;
  $$("#relatedBlock .related").forEach((a) =>
    a.addEventListener("click", () => openSession(a.dataset.id))
  );
}

function turnHTML(t) {
  const tools = (t.tools || []).length
    ? `<div class="tool-tags">${t.tools.map((x) => `<span class="tool">${esc(x)}</span>`).join("")}</div>`
    : "";
  const user = t.user_html ? `<div class="role"><span class="who">You</span></div><div class="bubble user"><div class="md">${t.user_html}</div></div>` : "";
  const asst = t.assistant_html ? `<div class="role"><span class="who">Copilot</span></div>${tools}<div class="bubble assistant"><div class="md">${t.assistant_html}</div></div>` : "";
  return `<div class="turn">${user}${asst}</div>`;
}

function highlightCard() {
  const cards = $$("#results .card");
  cards.forEach((c, i) => c.classList.toggle("kbd-active", i === kbdIndex));
  if (kbdIndex >= 0 && cards[kbdIndex]) cards[kbdIndex].scrollIntoView({ block: "nearest" });
}

function showList(opts) {
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

// ---------- snippet & command library ----------
const libState = { q: "", language: "", commands: false };
let snippetData = [];

async function showLibrary(opts = {}) {
  const leaving = state.view !== "library";
  state.view = "library";
  state.currentId = null;
  teardownReading();
  const apply = () => showOnly("#libraryView");
  if (leaving) withTransition(apply);
  else apply();
  if (!opts.fromHash) location.hash = "#/library";
  if (!$("#libLang").dataset.loaded) await loadSnippetLanguages();
  loadSnippets();
}

async function loadSnippetLanguages() {
  try {
    const langs = await api("/api/snippets/languages");
    const sel = $("#libLang");
    sel.innerHTML = `<option value="">All languages</option>` +
      langs.map((l) => `<option value="${esc(l.language)}">${esc(l.language)} (${l.count})</option>`).join("");
    sel.dataset.loaded = "1";
  } catch (_) { /* non-fatal */ }
}

async function loadSnippets() {
  const host = $("#libResults");
  const params = new URLSearchParams();
  if (libState.q) params.set("q", libState.q);
  if (libState.commands) params.set("commands", "true");
  else if (libState.language) params.set("language", libState.language);
  host.innerHTML = `<div class="lib-loading muted">Loading…</div>`;
  try {
    const data = await api("/api/snippets?" + params);
    snippetData = data.snippets || [];
    $("#libCount").textContent = snippetData.length
      ? `${snippetData.length}${snippetData.length >= 80 ? "+" : ""} snippet${snippetData.length === 1 ? "" : "s"}`
      : "";
    if (!snippetData.length) {
      host.innerHTML = `<div class="empty"><div class="big">⌗</div>No snippets match that filter.</div>`;
      return;
    }
    host.innerHTML = snippetData.map(snippetCardHTML).join("");
    $$("#libResults .snip-open").forEach((a) => a.addEventListener("click", () => openSession(a.dataset.id)));
    $$("#libResults .snip-copy").forEach((b) => b.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(snippetData[+b.dataset.idx].content); toast("Copied"); }
      catch (_) { toast("Copy failed", true); }
    }));
  } catch (e) {
    host.innerHTML = `<div class="empty"><div class="big">⚠️</div>${esc(e.message)}</div>`;
  }
}

function snippetCardHTML(s, i) {
  const lang = s.language || "text";
  const repo = s.repository ? ` · ${esc(s.repository)}` : "";
  return `<div class="snip-card">
    <div class="snip-head">
      <span class="snip-lang">${esc(lang)}</span>
      <a class="snip-open" data-id="${esc(s.session_id)}" title="Open conversation">${srcMeta(s.source).icon} ${esc(s.session_title || "Untitled")}${repo}</a>
      <button class="snip-copy" data-idx="${i}" title="Copy snippet">⧉</button>
    </div>
    <pre class="snip-code"><code>${esc(s.content)}</code></pre>
  </div>`;
}

// ---------- usage & spend dashboard ----------
async function showUsage(opts = {}) {
  const leaving = state.view !== "usage";
  state.view = "usage";
  state.currentId = null;
  teardownReading();
  const apply = () => showOnly("#usageView");
  if (leaving) withTransition(apply);
  else apply();
  if (!opts.fromHash) location.hash = "#/usage";
  loadUsage();
}

async function loadUsage() {
  const host = $("#usageBody");
  host.innerHTML = `<div class="lib-loading muted">Loading…</div>`;
  try {
    const params = $("#usageAuto").checked ? "?include_automation=true" : "";
    const data = await api("/api/usage" + params);
    host.innerHTML = renderUsage(data);
  } catch (e) {
    host.innerHTML = `<div class="empty"><div class="big">⚠️</div>${esc(e.message)}</div>`;
  }
}

function renderUsage(d) {
  const t = d.totals || {};
  const cards = [
    ["Total spend", fmtCost(t.cost || 0)],
    ["Premium requests", (t.premium || 0).toLocaleString()],
    ["AIU", (t.aiu || 0).toLocaleString(undefined, { maximumFractionDigits: 0 })],
    ["Tokens", `${fmtTokens(t.input_tokens)} in · ${fmtTokens(t.output_tokens)} out`],
    ["Sessions", (t.sessions || 0).toLocaleString()],
  ];
  const stats = cards.map(([k, v]) =>
    `<div class="usage-stat"><div class="us-val">${v}</div><div class="us-key">${k}</div></div>`
  ).join("");
  return `
    <div class="usage-stats">${stats}</div>
    ${usageColChart(d.by_day || [])}
    <div class="usage-grid">
      ${usageBars("By model", d.by_model || [], "model")}
      ${usageBars("By repository", d.by_repo || [], "repository")}
      ${usageBars("By source", d.by_source || [], "source")}
    </div>`;
}

function usageColChart(byDay) {
  if (!byDay.length) return "";
  const max = Math.max(...byDay.map((d) => d.cost)) || 1;
  const bars = byDay.map((d) => {
    const h = Math.max(2, Math.round((d.cost / max) * 100));
    return `<div class="uc-col" title="${d.day} · ${fmtCost(d.cost)} · ${d.sessions} session${d.sessions === 1 ? "" : "s"}"><div class="uc-bar" style="height:${h}%"></div></div>`;
  }).join("");
  return `<div class="usage-card">
    <h4>Spend over time</h4>
    <div class="uc-chart">${bars}</div>
    <div class="uc-axis"><span>${esc(byDay[0].day)}</span><span>${esc(byDay[byDay.length - 1].day)}</span></div>
  </div>`;
}

function usageBars(title, rows, key) {
  if (!rows.length) return "";
  const max = Math.max(...rows.map((r) => r.cost)) || 1;
  const items = rows.map((r) => {
    const w = Math.max(2, Math.round((r.cost / max) * 100));
    const label = key === "source"
      ? `${srcMeta(r.source).icon} ${esc(srcMeta(r.source).label)}`
      : esc(String(r[key]));
    return `<div class="bar-row">
      <span class="bar-label" title="${esc(String(r[key]))}">${label}</span>
      <div class="bar-track"><div class="bar-fill" style="width:${w}%"></div></div>
      <span class="bar-val">${fmtCost(r.cost)} <em>${r.sessions}</em></span>
    </div>`;
  }).join("");
  return `<div class="usage-card"><h4>${esc(title)}</h4><div class="bars">${items}</div></div>`;
}

// ---------- ask your history (optional local LLM) ----------
let askBusy = false;

async function showAsk(opts = {}) {
  const leaving = state.view !== "ask";
  state.view = "ask";
  state.currentId = null;
  teardownReading();
  const apply = () => showOnly("#askView");
  if (leaving) withTransition(apply);
  else apply();
  if (!opts.fromHash) location.hash = "#/ask";
  checkAskStatus();
  setTimeout(() => $("#askInput")?.focus(), 60);
}

async function checkAskStatus() {
  const note = $("#askStatus");
  try {
    const st = await api("/api/ask/status");
    if (!st.available) {
      note.hidden = false;
      note.innerHTML = `No local LLM detected. Install <a href="https://ollama.com" target="_blank" rel="noopener">Ollama</a>, then run <code>ollama pull llama3.2</code> and keep <code>ollama serve</code> running. Everything stays on your machine — no API keys.`;
      $("#askModel").textContent = "";
      $("#askSend").disabled = true;
    } else {
      note.hidden = true;
      $("#askModel").textContent = "via " + st.model;
      $("#askSend").disabled = false;
    }
  } catch (_) { /* leave as-is */ }
}

async function submitAsk() {
  if (askBusy) return;
  const q = $("#askInput").value.trim();
  if (!q) return;
  const limit = parseInt($("#askLimit")?.value, 10) || 8;
  askBusy = true;
  await streamAsk(
    "/api/ask",
    { question: q, limit },
    { answerEl: $("#askAnswer"), sourcesEl: $("#askSources"), sendBtn: $("#askSend") }
  );
  askBusy = false;
}

// Shared SSE streaming for both global Ask and collection-scoped Ask.
async function streamAsk(url, body, els) {
  const { answerEl, sourcesEl, sendBtn } = els;
  if (sendBtn) sendBtn.disabled = true;
  answerEl.hidden = false; answerEl.textContent = ""; answerEl.classList.add("streaming");
  if (sourcesEl) { sourcesEl.hidden = true; sourcesEl.innerHTML = ""; }
  let raw = "";
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok || !resp.body) throw new Error("Ask failed (" + resp.status + ")");
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split("\n\n");
      buf = parts.pop();
      for (const p of parts) {
        const line = p.trim();
        if (!line.startsWith("data:")) continue;
        const ev = JSON.parse(line.slice(5).trim());
        if (ev.type === "sources") renderAskSourcesInto(sourcesEl, ev.sources);
        else if (ev.type === "token") { raw += ev.text; answerEl.textContent = raw; answerEl.scrollTop = answerEl.scrollHeight; }
        else if (ev.type === "error") { toast(ev.error, true); raw += "\n\n" + ev.error; answerEl.textContent = raw; }
      }
    }
    if (raw.trim()) {
      try {
        const r = await api("/api/render", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: raw }),
        });
        answerEl.innerHTML = r.html;
      } catch (_) { /* keep plain text */ }
    }
  } catch (e) {
    toast(e.message, true);
    answerEl.textContent = e.message;
  } finally {
    answerEl.classList.remove("streaming");
    if (sendBtn) sendBtn.disabled = false;
  }
}

function renderAskSources(sources) {
  renderAskSourcesInto($("#askSources"), sources);
}

function renderAskSourcesInto(box, sources) {
  if (!box) return;
  if (!sources || !sources.length) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = `<h4>Sources</h4><div class="ask-src-list">${
    sources.map((s) => `<a class="ask-src" data-id="${esc(s.id)}" title="${esc(s.title || "")}"><b>[${s.n}]</b> <span class="ask-src-icon">${srcMeta(s.source).icon}</span> <span class="ask-src-title">${esc(s.title || "Untitled")}</span></a>`).join("")
  }</div>`;
  $$(".ask-src", box).forEach((a) => a.addEventListener("click", () => openSession(a.dataset.id)));
}

// ---------- collections ----------
let collDialogState = { mode: "create", id: null, rule: null, pendingSession: null };

const collIconOf = (c) => (c && c.icon) || "\u25A6";

function ruleIsEmpty(r) {
  if (!r) return true;
  return !(r.q || r.repo || r.source || (r.tags && r.tags.length) || r.date_from || r.date_to || r.include_automation);
}

function currentRule() {
  const rule = {};
  if (state.q) rule.q = state.q;
  if (state.mode && state.mode !== "hybrid") rule.mode = state.mode;
  if (state.source) rule.source = state.source;
  if (state.repo) rule.repo = state.repo;
  if (state.tags.size) rule.tags = [...state.tags];
  if (state.dateFrom) rule.date_from = state.dateFrom;
  if (state.dateTo) rule.date_to = state.dateTo;
  if (state.includeAutomation) rule.include_automation = true;
  return rule;
}

function ruleSummary(r) {
  if (ruleIsEmpty(r)) return `<span class="muted">manual selection only</span>`;
  const parts = [];
  if (r.q) parts.push(`matching \u201C${esc(r.q)}\u201D`);
  if (r.source) parts.push(`${srcMeta(r.source).icon} ${esc(srcMeta(r.source).label)}`);
  if (r.repo) parts.push(`\uD83D\uDCC1 ${esc(r.repo)}`);
  (r.tags || []).forEach((t) => parts.push(`# ${esc(t)}`));
  if (r.date_from || r.date_to) parts.push(`\uD83D\uDDD3 ${esc(r.date_from || "\u2026")} \u2192 ${esc(r.date_to || "\u2026")}`);
  if (r.include_automation) parts.push("incl. automation");
  return parts.map((p) => `<span class="crs-chip">${p}</span>`).join("");
}

function toggleSaveCollectionBtn() {
  const btn = $("#saveCollectionBtn");
  if (btn) btn.hidden = ruleIsEmpty(currentRule());
}

async function showCollections(opts = {}) {
  const leaving = state.view !== "collections";
  state.view = "collections";
  state.currentId = null;
  state.currentCollectionId = null;
  teardownReading();
  const apply = () => showOnly("#collectionsView");
  if (leaving) withTransition(apply); else apply();
  if (!opts.fromHash) location.hash = "#/collections";
  loadCollectionsGrid();
}

async function loadCollectionsGrid() {
  const host = $("#collectionsGrid");
  host.innerHTML = `<div class="lib-loading muted">Loading\u2026</div>`;
  try {
    const cols = await api("/api/collections");
    $("#collectionsCount").textContent = cols.length
      ? `${cols.length} collection${cols.length === 1 ? "" : "s"}`
      : "";
    if (!cols.length) {
      host.innerHTML = `<div class="empty"><div class="big">\u25A6</div>No collections yet.<br/>Run a search or pick filters, then <b>Save as collection</b> \u2014 or create one here.</div>`;
      return;
    }
    host.innerHTML = cols.map(collectionCardHTML).join("");
    $$("#collectionsGrid .coll-card").forEach((el) =>
      el.addEventListener("click", () => openCollection(el.dataset.id))
    );
  } catch (e) {
    host.innerHTML = `<div class="empty"><div class="big">\u26A0\uFE0F</div>${esc(e.message)}</div>`;
  }
}

function collectionCardHTML(c) {
  const n = c.count || 0;
  const kind = ruleIsEmpty(c.rule) ? "manual" : "auto-updating";
  return `<div class="coll-card" data-id="${esc(c.id)}">
    <div class="coll-card-icon">${esc(collIconOf(c))}</div>
    <div class="coll-card-body">
      <h3>${esc(c.name)}</h3>
      ${c.description ? `<p class="coll-card-desc">${esc(c.description)}</p>` : ""}
      <div class="coll-card-meta">
        <span class="pill">${n} session${n === 1 ? "" : "s"}</span>
        <span class="pill coll-kind">${kind}</span>
      </div>
    </div>
  </div>`;
}

async function openCollection(id, opts = {}) {
  state.currentCollectionId = id;
  state.currentId = null;
  teardownReading();
  try {
    const c = await api("/api/collections/" + encodeURIComponent(id));
    const leaving = state.view !== "collection";
    state.view = "collection";
    const apply = () => { showOnly("#collectionView"); renderCollection(c); };
    if (leaving) withTransition(apply); else apply();
    if (!opts.fromHash) location.hash = "#/collection/" + encodeURIComponent(id);
    window.scrollTo({ top: 0, behavior: "smooth" });
  } catch (e) {
    toast(e.message, true);
    showCollections();
  }
}

function renderCollection(c) {
  const ov = c.overview || {};
  const t = ov.totals || {};
  const view = $("#collectionView");

  const stats = [
    [`${t.sessions || 0}`, "sessions"],
    [fmtCost(t.cost || 0), "est. spend"],
    [`${t.files || 0}`, "files touched"],
    [fmtDuration(t.duration) || "0s", "time"],
    [`${t.premium || 0}`, "premium reqs"],
  ].map(([v, k]) => `<div class="usage-stat"><div class="us-val">${v}</div><div class="us-key">${k}</div></div>`).join("");

  const span = (ov.date_min || ov.date_max)
    ? `<span class="pill">\uD83D\uDDD3 ${fmtDate(ov.date_min)} \u2013 ${fmtDate(ov.date_max)}</span>` : "";
  const topics = (ov.topics || []).slice(0, 10)
    .map((tp) => `<span class="t">${esc(tp.tag)}</span>`).join("");

  const members = c.members || [];
  const membersHTML = members.length
    ? members.map((m) => `<div class="coll-member">${cardHTML(m)}<button class="coll-remove" data-id="${esc(m.id)}" title="Remove from this collection">\u00D7</button></div>`).join("")
    : `<div class="empty"><div class="big">\uD83D\uDDC2\uFE0F</div>No sessions in this collection yet.${ruleIsEmpty(c.rule) ? " Open a conversation and use \u201C\uFF0B Collection\u201D to add it." : ""}</div>`;

  view.innerHTML = `
    <div class="detail-head">
      <div class="detail-top">
        <span class="back" id="collBack">\u2190 All collections</span>
        <div class="detail-actions">
          <button class="btn btn-ghost" id="collEdit" title="Edit name, icon, description">\u270E Edit</button>
          <button class="btn btn-ghost" id="collDelete" title="Delete this collection">\uD83D\uDDD1 Delete</button>
        </div>
      </div>
      <h1><span class="coll-title-icon">${esc(collIconOf(c))}</span> ${esc(c.name)}</h1>
      ${c.description ? `<p class="detail-summary">${esc(c.description)}</p>` : ""}
      <div class="coll-rule-line"><span class="crs-label">Auto-includes</span> ${ruleSummary(c.rule)} ${span}</div>
    </div>

    <div class="usage-stats coll-stats">${stats}</div>
    ${topics ? `<div class="coll-topics"><h4>Topics</h4><div class="card-tags">${topics}</div></div>` : ""}

    <div class="coll-ask">
      <h4>\u2726 Ask this collection</h4>
      <form class="ask-form" id="collAskForm">
        <textarea id="collAskInput" rows="2" placeholder="Ask a question answered only from these conversations\u2026"></textarea>
        <button class="btn btn-primary" id="collAskSend" type="submit">Ask</button>
      </form>
      <div id="collAskStatus" class="ask-status" hidden></div>
      <div id="collAskSources" class="ask-sources" hidden></div>
      <div id="collAskAnswer" class="ask-answer md" hidden></div>
    </div>

    <div class="list-head coll-members-head"><h2>Sessions</h2><span class="muted">${members.length}${members.length >= 1 ? "" : ""} ${members.length === 1 ? "session" : "sessions"}</span></div>
    <div class="results coll-members">${membersHTML}</div>`;

  $("#collBack").addEventListener("click", () => showCollections());
  $("#collEdit").addEventListener("click", () => openCollectionDialog({ mode: "edit", coll: c }));
  $("#collDelete").addEventListener("click", () => deleteCollection(c));

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

  checkCollAskStatus();
  const askForm = $("#collAskForm");
  askForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("#collAskInput").value.trim();
    if (!q) return;
    streamAsk(
      `/api/collections/${encodeURIComponent(c.id)}/ask`,
      { question: q, limit: 8 },
      { answerEl: $("#collAskAnswer"), sourcesEl: $("#collAskSources"), sendBtn: $("#collAskSend") }
    );
  });
  $("#collAskInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); askForm.requestSubmit(); }
  });
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
function openCollectionDialog({ mode = "create", coll = null, rule = null, pendingSession = null } = {}) {
  collDialogState = {
    mode,
    id: coll && coll.id ? coll.id : null,
    rule: rule != null ? rule : (coll ? coll.rule : null),
    pendingSession,
  };
  $("#collectionDialogTitle").textContent = mode === "edit" ? "Edit collection" : "New collection";
  $("#collName").value = (coll && coll.name) || "";
  $("#collIcon").value = (coll && coll.icon) || "";
  $("#collDescription").value = (coll && coll.description) || "";
  const sum = $("#collRuleSummary");
  if (collDialogState.rule && !ruleIsEmpty(collDialogState.rule)) {
    sum.hidden = false;
    sum.innerHTML = `<span class="crs-label">Auto-includes</span> ${ruleSummary(collDialogState.rule)}`;
  } else {
    sum.hidden = true;
    sum.innerHTML = "";
  }
  $("#collectionDialog").showModal();
  setTimeout(() => $("#collName").focus(), 50);
}

async function saveCollection() {
  const name = $("#collName").value.trim();
  if (!name) return toast("Give the collection a name", true);
  const rule = (collDialogState.rule && !ruleIsEmpty(collDialogState.rule)) ? collDialogState.rule : null;
  const payload = {
    name,
    icon: $("#collIcon").value.trim() || null,
    description: $("#collDescription").value.trim() || null,
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
    if (coll && coll.id) openCollection(coll.id);
  } catch (e) { toast(e.message, true); }
}

function saveCollectionFromFilters() {
  const rule = currentRule();
  if (ruleIsEmpty(rule)) return toast("Add a search or filter first", true);
  const name = rule.q || rule.repo || (rule.tags && rule.tags[0]) ||
    (rule.source ? srcMeta(rule.source).label : "") || "New collection";
  openCollectionDialog({ mode: "create", coll: { name }, rule });
}

// ----- add-to-collection menu (from a session) -----
async function openCollMenu(anchor, sessionId) {
  const menu = $("#collMenu");
  let items;
  try { items = await api(`/api/sessions/${encodeURIComponent(sessionId)}/collections`); }
  catch (e) { return toast(e.message, true); }
  const rows = items.length
    ? items.map((c) => `<button class="cm-item${c.member ? " on" : ""}" data-id="${esc(c.id)}"><span class="cm-mark">${c.member ? "\u2713" : "\uFF0B"}</span> <span class="cm-name">${esc(collIconOf(c))} ${esc(c.name)}</span></button>`).join("")
    : `<div class="cm-empty muted">No collections yet</div>`;
  menu.innerHTML = rows + `<button class="cm-item cm-new" data-new="1"><span class="cm-mark">\uFF0B</span> <span class="cm-name">New collection\u2026</span></button>`;
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

function hideCollMenu() {
  const menu = $("#collMenu");
  if (menu) menu.hidden = true;
}

// ---------- reading mode (progress bar + sticky header) ----------
function setupReading() {
  const prog = $("#readProgress");
  const sticky = $("#detailSticky");
  if (prog) prog.hidden = false;
  const onScroll = () => {
    const doc = document.documentElement;
    const max = doc.scrollHeight - doc.clientHeight;
    const pct = max > 0 ? Math.min(1, Math.max(0, doc.scrollTop / max)) : 0;
    if (prog) prog.style.width = (pct * 100).toFixed(1) + "%";
    if (sticky) sticky.classList.toggle("show", doc.scrollTop > 150);
  };
  window.addEventListener("scroll", onScroll, { passive: true });
  detailScrollHandler = onScroll;
  onScroll();
}

function teardownReading() {
  if (detailScrollHandler) {
    window.removeEventListener("scroll", detailScrollHandler);
    detailScrollHandler = null;
  }
  const prog = $("#readProgress");
  if (prog) { prog.hidden = true; prog.style.width = "0"; }
}

// ---------- status, reindex & live auto-sync ----------
// A single self-scheduling loop polls /api/status: fast while an import runs,
// then a gentle heartbeat while idle so background auto-syncs (a session ending)
// show up on their own. `last_ingest` advancing means the index changed.
const HEARTBEAT_MS = 10000;
let statusTimer;
let lastSeenIngest;        // undefined until first observation
let lastSessionCount;      // visible-session count at last refresh

async function pollStatus(initial = false) {
  let running = false;
  try {
    const st = await api("/api/status");
    const banner = $("#statusBanner");
    running = !!st.running;
    if (running) {
      banner.hidden = false;
      banner.innerHTML = `<span class="dot"></span> ${esc(st.message || "Indexing…")}`;
      $("#reindexBtn").classList.add("spin");
    } else {
      banner.hidden = true;
      $("#reindexBtn").classList.remove("spin");
    }
    // The index changed (manual reindex or background auto-sync completed).
    if (st.last_ingest !== lastSeenIngest) {
      const first = lastSeenIngest === undefined;
      lastSeenIngest = st.last_ingest;
      if (!first) await refreshAll(true);
    }
  } catch (_) {}
  clearTimeout(statusTimer);
  statusTimer = setTimeout(() => pollStatus(), running ? 1100 : HEARTBEAT_MS);
}

// gentle: only re-run the result list when the user is idle at the top of the
// list view, so an auto-sync never yanks the page while they're reading.
async function refreshAll(gentle = false) {
  const [s] = await Promise.all([loadStats(), loadFacets()]);
  const count = s ? s.sessions ?? 0 : undefined;
  if (gentle && count != null && lastSessionCount != null && count > lastSessionCount) {
    const n = count - lastSessionCount;
    toast(`Synced ${n} new session${n === 1 ? "" : "s"}`);
  }
  if (count != null) lastSessionCount = count;
  if (state.view === "list") {
    const idle =
      document.activeElement !== $("#search") && window.scrollY < 40;
    if (!gentle || idle) doSearch(true, { keepView: true });
  }
}

// ---------- add dialog ----------
function setupDialog() {
  const dlg = $("#addDialog");
  let addMode = "note";
  let pickedFile = null;

  $("#addBtn").addEventListener("click", () => { pickedFile = null; $("#fileName").textContent = ""; dlg.showModal(); });
  $("#addClose").addEventListener("click", () => dlg.close());
  $("#addCancel").addEventListener("click", () => dlg.close());

  $$("#addModeToggle button").forEach((b) =>
    b.addEventListener("click", () => {
      addMode = b.dataset.add;
      $$("#addModeToggle button").forEach((x) => x.classList.toggle("active", x === b));
      $("#notePane").hidden = addMode !== "note";
      $("#filePane").hidden = addMode !== "file";
    })
  );

  const fileInput = $("#fileInput");
  const dz = $("#dropzone");
  fileInput.addEventListener("change", () => { pickedFile = fileInput.files[0]; $("#fileName").textContent = pickedFile ? pickedFile.name : ""; });
  ["dragover", "dragenter"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, () => dz.classList.remove("drag")));
  dz.addEventListener("drop", (e) => { e.preventDefault(); pickedFile = e.dataTransfer.files[0]; $("#fileName").textContent = pickedFile ? pickedFile.name : ""; });

  $("#addSave").addEventListener("click", async () => {
    try {
      let newId;
      if (addMode === "note") {
        const title = $("#noteTitle").value.trim();
        const text = $("#noteText").value.trim();
        if (!title && !text) return toast("Add a title or some text", true);
        newId = (await api("/api/notes", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title: title || "Untitled note", text }),
        })).id;
      } else {
        if (!pickedFile) return toast("Choose a file first", true);
        const fd = new FormData();
        fd.append("file", pickedFile);
        const res = await api("/api/uploads", { method: "POST", body: fd });
        if (res.matched) {
          dlg.close();
          const lbl = srcMeta(res.matched).label;
          const n = res.imported;
          toast(`Imported ${n} ${lbl} conversation${n === 1 ? "" : "s"}` +
            (res.skipped ? ` · ${res.skipped} unchanged` : ""));
          await refreshAll();
          return;
        }
        newId = res.id;
      }
      dlg.close();
      $("#noteTitle").value = ""; $("#noteText").value = "";
      toast("Saved to mindex");
      await refreshAll();
      if (newId) openSession(newId);
    } catch (e) {
      toast(e.message, true);
    }
  });
}

// ---------- wire up ----------
function setup() {
  // theme
  const saved = localStorage.getItem("mindex-theme");
  if (saved) document.documentElement.dataset.theme = saved;
  $("#themeBtn").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("mindex-theme", next);
  });

  $("#search").addEventListener("input", (e) => { state.q = e.target.value.trim(); run(); });

  $$("#modeToggle button").forEach((b) =>
    b.addEventListener("click", () => {
      state.mode = b.dataset.mode;
      $$("#modeToggle button").forEach((x) => x.classList.toggle("active", x === b));
      run();
    })
  );

  $("#sortSelect").addEventListener("change", (e) => { state.sort = e.target.value; run(); });

  $("#sourceFilters").addEventListener("click", (e) => {
    const chip = e.target.closest(".chip"); if (!chip) return;
    state.source = state.source === chip.dataset.source ? null : chip.dataset.source;
    syncFilterUI(); run();
  });
  $("#includeAutomation").addEventListener("change", (e) => {
    state.includeAutomation = e.target.checked;
    run();
  });
  $("#repoFilters").addEventListener("click", (e) => {
    const f = e.target.closest(".facet"); if (!f) return;
    state.repo = state.repo === f.dataset.repo ? null : f.dataset.repo;
    syncFilterUI(); run();
  });
  $("#tagFilters").addEventListener("click", (e) => {
    const chip = e.target.closest(".chip"); if (!chip) return;
    const t = chip.dataset.tag;
    state.tags.has(t) ? state.tags.delete(t) : state.tags.add(t);
    syncFilterUI(); run();
  });

  $("#clearFilters").addEventListener("click", clearAllFilters);

  $("#activeFilters").addEventListener("click", (e) => {
    if (e.target.closest("#afClear")) { clearAllFilters(); return; }
    const chip = e.target.closest(".af-chip"); if (!chip) return;
    const { type, val } = chip.dataset;
    if (type === "source") state.source = null;
    else if (type === "repo") state.repo = null;
    else if (type === "tag") state.tags.delete(val);
    else if (type === "date") { state.dateFrom = ""; state.dateTo = ""; $("#dateFrom").value = ""; $("#dateTo").value = ""; }
    else if (type === "auto") { state.includeAutomation = false; $("#includeAutomation").checked = false; }
    syncFilterUI(); run();
  });

  $("#dateFrom").addEventListener("change", (e) => { state.dateFrom = e.target.value; run(); });
  $("#dateTo").addEventListener("change", (e) => { state.dateTo = e.target.value; run(); });

  $("#brandHome").addEventListener("click", () => {
    state.q = ""; $("#search").value = "";
    state.source = null; state.repo = null; state.tags.clear();
    state.includeAutomation = false; $("#includeAutomation").checked = false;
    syncFilterUI(); showList(); run();
  });

  $("#reindexBtn").addEventListener("click", async () => {
    try { await api("/api/reindex", { method: "POST" }); toast("Re-scanning Copilot history…"); pollStatus(); }
    catch (e) { toast(e.message, true); }
  });

  $("#collectionsBtn").addEventListener("click", () => showCollections());
  $("#newCollectionBtn").addEventListener("click", () => openCollectionDialog({ mode: "create" }));
  $("#saveCollectionBtn").addEventListener("click", saveCollectionFromFilters);
  $("#collClose").addEventListener("click", () => $("#collectionDialog").close());
  $("#collCancel").addEventListener("click", () => $("#collectionDialog").close());
  $("#collSave").addEventListener("click", saveCollection);
  $("#collName").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); saveCollection(); }
  });
  document.addEventListener("click", (e) => {
    const menu = $("#collMenu");
    if (menu && !menu.hidden && !menu.contains(e.target) && !e.target.closest("#addToColl")) {
      hideCollMenu();
    }
  });

  $("#libraryBtn").addEventListener("click", () => showLibrary());
  $("#libSearch").addEventListener("input", debounce(() => {
    libState.q = $("#libSearch").value.trim(); loadSnippets();
  }, 200));
  $("#libLang").addEventListener("change", () => { libState.language = $("#libLang").value; loadSnippets(); });
  $("#libCommands").addEventListener("change", () => {
    libState.commands = $("#libCommands").checked;
    $("#libLang").disabled = libState.commands;
    loadSnippets();
  });

  $("#usageBtn").addEventListener("click", () => showUsage());
  $("#usageAuto").addEventListener("change", loadUsage);

  $("#askBtn").addEventListener("click", () => showAsk());
  $("#askForm").addEventListener("submit", (e) => { e.preventDefault(); submitAsk(); });
  $("#askInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submitAsk(); }
  });

  document.addEventListener("keydown", (e) => {
    const inSearch = document.activeElement === $("#search");
    if ((e.key === "/" && !inSearch) || (e.metaKey && e.key === "k")) {
      e.preventDefault(); $("#search").focus(); return;
    }
    if (e.key === "Escape") {
      const menu = $("#collMenu");
      if (menu && !menu.hidden) { hideCollMenu(); return; }
      if (state.view === "detail") { showList(); return; }
    }

    // arrow-key navigation through results (works even while typing a query)
    if (state.view === "list") {
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
  });

  setupDialog();
}

async function init() {
  setup();
  await refreshAll();
  routeFromHash();
  pollStatus(true);
}

// Deep-link routing: #/session/{id} opens a conversation; #/library opens the library; empty hash shows the list.
function routeFromHash() {
  if (location.hash === "#/library") {
    if (state.view !== "library") showLibrary({ fromHash: true });
    return;
  }
  if (location.hash === "#/usage") {
    if (state.view !== "usage") showUsage({ fromHash: true });
    return;
  }
  if (location.hash === "#/ask") {
    if (state.view !== "ask") showAsk({ fromHash: true });
    return;
  }
  if (location.hash === "#/collections") {
    if (state.view !== "collections") showCollections({ fromHash: true });
    return;
  }
  const mc = location.hash.match(/^#\/collection\/(.+)$/);
  if (mc) {
    const cid = decodeURIComponent(mc[1]);
    if (state.view === "collection" && state.currentCollectionId === cid) return;
    openCollection(cid, { fromHash: true });
    return;
  }
  const m = location.hash.match(/^#\/session\/(.+)$/);
  if (m) {
    const id = decodeURIComponent(m[1]);
    if (state.view === "detail" && state.currentId === id) return;
    openSession(id, { fromHash: true });
  } else if (state.view !== "list") {
    showList({ fromHash: true });
  }
}
window.addEventListener("hashchange", routeFromHash);

init();
