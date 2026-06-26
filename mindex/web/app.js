"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

const state = {
  q: "",
  mode: "hybrid",
  sort: "recent",
  source: null,
  repo: null,
  tags: new Set(),
  includeAutomation: false,
  view: "list",
};

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
  const semantic = s.embed_model && !s.embed_model.startsWith("builtin");
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
    cost +
    `<div class="stat-card" style="grid-column:1/-1">
       <div class="n">${semantic ? "Semantic" : "Lexical"}</div>
       <div class="l ${semantic ? "semantic-on" : ""}">${semantic ? esc(s.embed_model) : "keyword + vectors"}</div>
     </div>`;
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

  syncFilterUI();
}

function syncFilterUI() {
  $$("#sourceFilters .chip").forEach((c) => c.classList.toggle("active", c.dataset.source === state.source));
  $$("#repoFilters .facet").forEach((c) => c.classList.toggle("active", c.dataset.repo === state.repo));
  $$("#tagFilters .chip").forEach((c) => c.classList.toggle("active", state.tags.has(c.dataset.tag)));
}

// ---------- search / browse ----------
const run = debounce(async () => {
  showList();
  const params = new URLSearchParams();
  if (state.q) params.set("q", state.q);
  params.set("mode", state.mode);
  if (state.source) params.set("source", state.source);
  if (state.repo) params.set("repo", state.repo);
  if (state.tags.size) params.set("tags", [...state.tags].join(","));
  if (state.includeAutomation) params.set("include_automation", "true");
  params.set("limit", "40");

  $("#results").innerHTML = Array(4).fill('<div class="skeleton"></div>').join("");
  try {
    const data = state.q
      ? await api("/api/search?" + params)
      : await api("/api/search?" + params); // empty q falls back to browse server-side
    renderResults(data);
  } catch (e) {
    $("#results").innerHTML = `<div class="empty"><div class="big">⚠️</div>${esc(e.message)}</div>`;
  }
}, 180);

function renderResults(data) {
  const results = data.results || [];
  $("#listTitle").textContent = state.q ? `Results for “${state.q}”` : "Recent sessions";
  $("#listCount").textContent = results.length ? `${results.length} ${results.length === 1 ? "session" : "sessions"}` : "";

  if (!results.length) {
    $("#results").innerHTML = `<div class="empty"><div class="big">${state.q ? "🔍" : "🗂️"}</div>${
      state.q ? "No conversations match. Try semantic mode or different words." : "Nothing here yet — re-scan or add a note."
    }</div>`;
    return;
  }

  $("#results").innerHTML = results.map(cardHTML).join("");
  $$("#results .card").forEach((el) => el.addEventListener("click", () => openSession(el.dataset.id)));
}

function cardHTML(r) {
  const score = r.score != null ? `<div class="score" title="relevance"><i style="width:${Math.round(r.score * 100)}%"></i></div>` : "";
  const repo = r.repository ? `<span class="pill">📁 ${esc(r.repository)}</span>` : "";
  const src = `<span class="pill src-${r.source}">${srcMeta(r.source).icon} ${esc(srcMeta(r.source).label)}</span>`;
  const tags = (r.tags || []).slice(0, 4).map((t) => `<span class="t">${esc(t)}</span>`).join("");
  const dur = fmtDuration(r.duration_seconds);
  const cost = r.est_cost_usd ? fmtCost(r.est_cost_usd) : "";
  return `
    <div class="card" data-id="${esc(r.id)}">
      <div class="card-top">
        <h3 class="card-title">${esc(r.title || "Untitled")}</h3>
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

// ---------- detail ----------
async function openSession(id) {
  try {
    const s = await api("/api/sessions/" + encodeURIComponent(id));
    renderDetail(s);
  } catch (e) {
    toast(e.message, true);
  }
}

function renderDetail(s) {
  state.view = "detail";
  $("#listView").hidden = true;
  const view = $("#detailView");
  view.hidden = false;
  window.scrollTo({ top: 0, behavior: "smooth" });

  const tags = (s.tags || []).map((t) => `<span class="pill">${esc(t)}</span>`).join("");
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
  if (s.tags?.length) asideBlocks.push(`<div><h4>Topics</h4><div class="chips">${tags}</div></div>`);

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
      <span class="back" id="backBtn">← Back to results</span>
      <h1>${esc(s.title || "Untitled")}</h1>
      ${s.summary ? `<p class="detail-summary">${esc(s.summary)}</p>` : ""}
      <div class="detail-meta">${meta}</div>
    </div>
    <div class="detail-body">
      <div class="transcript detail-scroll">${body || '<p class="muted">No content.</p>'}</div>
      <div class="detail-aside">${asideBlocks.join("") || '<span class="muted">No attachments.</span>'}</div>
    </div>`;
  $("#backBtn").addEventListener("click", showList);
  $$("#detailView .copy-btn").forEach((b) =>
    b.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(b.dataset.copy); toast("Copied"); }
      catch (_) { toast("Copy failed", true); }
    })
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

function showList() {
  state.view = "list";
  $("#detailView").hidden = true;
  $("#listView").hidden = false;
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
    if (!gentle || idle) run();
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
        newId = (await api("/api/uploads", { method: "POST", body: fd })).id;
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

  $("#clearFilters").addEventListener("click", () => {
    state.source = null; state.repo = null; state.tags.clear();
    state.includeAutomation = false; $("#includeAutomation").checked = false;
    syncFilterUI(); run();
  });

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

  document.addEventListener("keydown", (e) => {
    if ((e.key === "/" || (e.metaKey && e.key === "k")) && document.activeElement !== $("#search")) {
      e.preventDefault(); $("#search").focus();
    }
    if (e.key === "Escape" && state.view === "detail") showList();
  });

  setupDialog();
}

async function init() {
  setup();
  await refreshAll();
  pollStatus(true);
}

init();
