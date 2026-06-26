"use strict";

// Single-conversation detail view: transcript, metadata aside, topic editing,
// related sessions, and the reading-progress / sticky-header behaviour.

import { api } from "../api.js";
import { showOnly, state } from "../state.js";
import { loadFacets } from "../sidebar.js";
import {
  $, $$, esc, fmtBytes, fmtCost, fmtDate, fmtDuration, fmtTokens, srcMeta, toast, withTransition,
} from "../utils.js";
import { showList } from "./list.js";
import { openCollMenu } from "./collections.js";

let detailScrollHandler = null; // active reading-progress listener

export async function openSession(id, opts = {}) {
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
  const isCli = s.source === "cli";
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
      attachments.map((a, i) => `<a class="aside-file" href="#att-${i}" data-att="${i}" title="${esc(a.filename || "")}">📎 ${esc(a.filename || "file")}</a>`).join("")
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
  // Jump to (and expand) an attachment without touching the hash router.
  $$("#detailView .aside-file[data-att]").forEach((a) =>
    a.addEventListener("click", (e) => {
      e.preventDefault();
      const el = $("#att-" + a.dataset.att);
      if (el) { el.open = true; el.scrollIntoView({ behavior: "smooth", block: "start" }); }
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

export function teardownReading() {
  if (detailScrollHandler) {
    window.removeEventListener("scroll", detailScrollHandler);
    detailScrollHandler = null;
  }
  const prog = $("#readProgress");
  if (prog) { prog.hidden = true; prog.style.width = "0"; }
}
