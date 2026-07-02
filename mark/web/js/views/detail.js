"use strict";

// Single-conversation detail view: transcript, metadata aside, topic editing,
// related sessions, and the reading-progress / sticky-header behaviour.

import { api } from "../api.js";
import { showOnly, setLayoutWide, state } from "../state.js";
import { loadFacets, loadStats } from "../sidebar.js";
import {
  $, $$, esc, fmtBytes, fmtCost, fmtDate, fmtDuration, fmtTokens, srcMeta, toast, withTransition,
} from "../utils.js";
import { icon } from "../icons.js";
import { doSearch, showList } from "./list.js";
import { openCollMenu } from "./collections.js";

let detailScrollHandler = null; // active reading-progress listener
let detailStickyTimer = null; // pending sticky-header reveal (dwell debounce)

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
  setLayoutWide(false);
  showOnly("#detailView");
  const view = $("#detailView");
  window.scrollTo({ top: 0, behavior: "smooth" });

  const isHidden = !!s.hidden;
  const manualSet = new Set(s.manual_tags || []);
  const meta = [
    `<span class="pill src-${s.source}">${srcMeta(s.source).icon} ${esc(srcMeta(s.source).label)}</span>`,
    s.repository ? `<span class="pill">${icon("folder")} ${esc(s.repository)}</span>` : "",
    `<span class="pill">${icon("clock")} ${fmtDate(s.updated_at || s.created_at)}</span>`,
    s.turn_count ? `<span class="pill">${icon("message")} ${s.turn_count} turns</span>` : "",
    s.model ? `<span class="pill">${icon("cpu")} ${esc(s.model)}</span>` : "",
    fmtDuration(s.duration_seconds) ? `<span class="pill">${icon("timer")} ${fmtDuration(s.duration_seconds)}</span>` : "",
    s.est_cost_usd ? `<span class="pill cost">~${fmtCost(s.est_cost_usd)}${s.tokens_estimated ? " est." : ""}</span>` : "",
  ].join("");

  const files = (s.files || []);
  const refs = (s.refs || []).filter((r) => r.ref_type === "url");
  const asideBlocks = [];

  // Session id + resume
  const isCli = s.source === "cli";
  const resumeCmd = (state.resumeCmd || "copilot --resume {id}").replace("{id}", s.id);
  asideBlocks.push(`<div><h4>Session</h4>
    ${isCli ? `<div class="resume-hint">Resume in Copilot CLI</div>
      <div class="copy-row"><code>${esc(resumeCmd)}</code><button class="copy-btn" data-copy="${esc(resumeCmd)}" title="Copy">${icon("copy")}</button></div>`
      : `<div class="copy-row"><code title="${esc(s.id)}">${esc(s.id)}</code><button class="copy-btn" data-copy="${esc(s.id)}" title="Copy">${icon("copy")}</button></div>`}
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
    return `<span class="pill topic${m ? " manual" : ""}" data-tag="${esc(t)}">${esc(t)}${m ? `<button class="topic-x" data-del="${esc(t)}" title="Remove topic">${icon("x", { size: 12 })}</button>` : ""}</span>`;
  }).join("");
  asideBlocks.push(`<div><h4>Topics</h4>
    <div class="chips topics-edit">${topicPills || '<span class="muted">none yet</span>'}</div>
    <form class="topic-add" id="topicAdd">
      <input id="topicInput" placeholder="add a topic..." maxlength="40" autocomplete="off" spellcheck="false" />
      <button class="btn btn-ghost icon-only" type="submit" title="Add topic">${icon("plus")}</button>
    </form>
  </div>`);

  asideBlocks.push(`<div id="relatedBlock" class="related-block" hidden></div>`);

  const attachments = (s.attachments || []);
  const attDownloadHref = (a) =>
    (a.id != null && (a.content != null || a.stored_path))
      ? `/api/sessions/${encodeURIComponent(s.id)}/attachments/${encodeURIComponent(a.id)}/download`
      : null;
  // Memory-tool notes (kept where the agent wrote them) are shown apart from any
  // other agent-created files; they are told apart by their on-disk location.
  const isMemoryAtt = (a) =>
    (a.stored_path || "").replace(/\\/g, "/").includes("memory-tool/memories");
  const attIndexed = attachments.map((a, i) => ({ a, i }));
  const memAtts = attIndexed.filter(({ a }) => isMemoryAtt(a));
  const agentAtts = attIndexed.filter(({ a }) => !isMemoryAtt(a));
  const asideList = (items, ic) => `<div class="aside-files">${
    items.map(({ a, i }) => {
      const dl = attDownloadHref(a);
      const dlLink = dl
        ? `<a class="att-dl" href="${dl}" download="${esc(a.filename || "file")}" title="Download ${esc(a.filename || "file")}">${icon("download", { size: 14 })}</a>`
        : "";
      return `<div class="aside-file att-row" title="${esc(a.filename || "")}">`
        + `<a class="att-jump" data-att="${i}">${icon(ic, { size: 13 })} ${esc(a.filename || "file")}</a>${dlLink}</div>`;
    }).join("")
  }</div>`;
  if (memAtts.length) {
    asideBlocks.push(`<div><h4>Memory notes (${memAtts.length})</h4>${asideList(memAtts, "archive")}</div>`);
  }
  if (agentAtts.length) {
    asideBlocks.push(`<div><h4>Attachments (${agentAtts.length})</h4>${asideList(agentAtts, "paperclip")}</div>`);
  }

  let body;
  if (s.source === "upload" && s.document) {
    body = `<div class="md">${s.document.html || esc(s.document.content || "")}</div>`;
  } else {
    body = (s.turns || []).map(turnHTML).join("");
  }
  const attDetails = (items, ic) =>
    items.map(({ a, i }) => {
      const meta = `${esc(a.filename || "file")} · ${fmtBytes(a.size_bytes)}`;
      const dl = attDownloadHref(a);
      const dlLink = dl
        ? `<a class="att-dl" href="${dl}" download="${esc(a.filename || "file")}" title="Download ${esc(a.filename || "file")}">${icon("download", { size: 14 })} Download</a>`
        : "";
      const inner = a.html
        ? `<div class="md">${a.html}</div>`
        : a.content != null
          ? `<pre class="att-pre">${esc(a.content)}</pre>`
          : `<p class="muted">Not stored (binary or larger than the snapshot limit). Path: ${esc(a.stored_path || "")}</p>`;
      return `<details class="attachment" id="att-${i}"><summary>${icon(ic, { size: 13 })} ${meta}${dlLink}</summary>${inner}</details>`;
    }).join("");
  if (memAtts.length) {
    body += `<div class="attachments"><h3>Memory notes</h3>`
      + `<p class="muted">Durable notes the agent saved with its memory tool while working on this conversation.</p>`
      + `${attDetails(memAtts, "archive")}</div>`;
  }
  if (agentAtts.length) {
    body += `<div class="attachments"><h3>Attachments created by the agent</h3>${attDetails(agentAtts, "paperclip")}</div>`;
  }

  view.innerHTML = `
    <div class="detail-head">
      <div class="detail-top">
        <span class="back" id="backBtn">${icon("arrow-left", { size: 15 })} Back to results</span>
        <div class="detail-actions">
          <button class="btn btn-ghost" id="addToColl" title="Add this conversation to a collection">${icon("plus")} Collection</button>
          <button class="btn btn-ghost" id="copyLink" title="Copy a link to this conversation">${icon("link")} Link</button>
          <a class="btn btn-ghost" id="exportMd" href="/api/sessions/${encodeURIComponent(s.id)}/export.md" download title="Download as Markdown">${icon("download")} Markdown</a>
          <button class="btn btn-ghost${isHidden ? " is-hidden" : ""}" id="hideBtn" title="${isHidden ? "Unhide this conversation" : "Hide this conversation from listings"}">${icon(isHidden ? "eye" : "eye-off")} ${isHidden ? "Unhide" : "Hide"}</button>
          <button class="btn btn-ghost detail-delete" id="deleteBtn" title="Permanently delete this conversation">${icon("trash")} Delete</button>
        </div>
      </div>
      <h1>${esc(s.title || "Untitled")}</h1>
      ${s.summary ? `<p class="detail-summary">${esc(s.summary)}</p>` : ""}
      <div class="detail-meta">${meta}</div>
    </div>
    <div class="detail-sticky" id="detailSticky">
      <button type="button" class="ds-back" id="dsBack" title="Back to results">${icon("arrow-left", { size: 16 })}<span class="ds-title">${esc(s.title || "Untitled")}</span></button>
    </div>
    <div class="detail-body">
      <div class="transcript detail-scroll">${body || '<p class="muted">No content.</p>'}</div>
      <div class="detail-aside">${asideBlocks.join("") || '<span class="muted">No attachments.</span>'}</div>
    </div>`;
  const backToList = () => showList({ restoreId: s.id });
  $("#backBtn").addEventListener("click", backToList);
  $("#dsBack").addEventListener("click", backToList);
  $("#addToColl")?.addEventListener("click", (e) => {
    e.stopPropagation();
    openCollMenu(e.currentTarget, s.id);
  });
  $("#copyLink")?.addEventListener("click", async () => {
    const url = location.origin + location.pathname + "#/session/" + encodeURIComponent(s.id);
    try { await navigator.clipboard.writeText(url); toast("Link copied"); }
    catch (_) { toast("Copy failed", true); }
  });
  $("#hideBtn")?.addEventListener("click", async () => {
    const willHide = !s.hidden;
    try {
      await api(`/api/sessions/${encodeURIComponent(s.id)}/${willHide ? "hide" : "unhide"}`, { method: "POST" });
      s.hidden = willHide ? 1 : 0;
      toast(willHide ? "Session hidden" : "Session unhidden");
      // Hidden sessions stay reachable here, but counts/facets shift, so refresh.
      loadStats();
      loadFacets();
      renderDetail(s);
    } catch (e) { toast(e.message, true); }
  });
  $("#deleteBtn")?.addEventListener("click", async () => {
    const name = s.title || "this conversation";
    if (!window.confirm(`Permanently delete \u201C${name}\u201D? This removes it for good and keeps it from being re-imported on the next scan. This cannot be undone \u2014 use Hide if you only want it out of the way.`)) return;
    try {
      await api(`/api/sessions/${encodeURIComponent(s.id)}`, { method: "DELETE" });
      toast("Session permanently deleted");
      loadStats();
      loadFacets();
      showList();
      doSearch(true, { keepView: true });
    } catch (e) { toast(e.message, true); }
  });
  setupReading();
  $$("#detailView .copy-btn").forEach((b) =>
    b.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(b.dataset.copy); toast("Copied"); }
      catch (_) { toast("Copy failed", true); }
    })
  );
  // Jump to (and expand) an attachment without touching the hash router.
  $$("#detailView [data-att]").forEach((a) =>
    a.addEventListener("click", (e) => {
      e.preventDefault();
      const el = $("#att-" + a.dataset.att);
      if (el) { el.open = true; el.scrollIntoView({ behavior: "smooth", block: "start" }); }
    })
  );
  // Download links must not toggle/expand the attachment panel they sit in.
  $$("#detailView .att-dl").forEach((a) =>
    a.addEventListener("click", (e) => e.stopPropagation())
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
  // Model reasoning, collapsed by default — kept for auditable/forensic review.
  const thinking = t.thinking_html
    ? `<details class="thinking"><summary>Reasoning</summary><div class="md">${t.thinking_html}</div></details>`
    : "";
  const asst = (t.assistant_html || thinking)
    ? `<div class="role"><span class="who">Copilot</span></div>${tools}${thinking}`
      + (t.assistant_html ? `<div class="bubble assistant"><div class="md">${t.assistant_html}</div></div>` : "")
    : "";
  return `<div class="turn">${user}${asst}</div>`;
}

// ---------- reading mode (progress bar + sticky header) ----------
// The compact header is only meaningful once the full header (which carries its
// own "Back to results" control) has scrolled up under the fixed topbar — that's
// exactly when the sticky bar pins into place. Reveal it only after the reader
// dwells there, so a quick scroll that merely passes through on the way back to
// the top can't fade it in and then yank it away (the back-button flash).
// Hiding stays immediate.
const STICKY_DWELL = 160; // ms the header must stay tucked away before revealing

function clearStickyTimer() {
  if (detailStickyTimer != null) {
    clearTimeout(detailStickyTimer);
    detailStickyTimer = null;
  }
}

function setupReading() {
  // renderDetail() can run again (hide/unhide, topic edits) without leaving the
  // view, so drop any prior handler before wiring a fresh one.
  if (detailScrollHandler) window.removeEventListener("scroll", detailScrollHandler);
  clearStickyTimer();
  const prog = $("#readProgress");
  const sticky = $("#detailSticky");
  const head = $("#detailView .detail-head");
  const topbar = document.querySelector(".topbar");
  if (prog) prog.hidden = false;
  // The topbar's height varies with viewport width (its action row wraps), so
  // pin the compact header flush against its current bottom rather than a fixed
  // offset — otherwise the bar tucks behind a tall topbar or floats below it.
  const pinTop = () => (topbar ? topbar.getBoundingClientRect().height : 0);
  // True once the full header has scrolled above the sticky bar's pin line.
  const headerTuckedAway = () => !head || head.getBoundingClientRect().bottom <= pinTop();
  const onScroll = () => {
    const doc = document.documentElement;
    const max = doc.scrollHeight - doc.clientHeight;
    const pct = max > 0 ? Math.min(1, Math.max(0, doc.scrollTop / max)) : 0;
    if (prog) prog.style.width = (pct * 100).toFixed(1) + "%";
    if (!sticky) return;
    sticky.style.top = pinTop() + "px";
    if (headerTuckedAway()) {
      // Arm a delayed reveal; a transient pass-through never gets to fire it.
      if (!sticky.classList.contains("show") && detailStickyTimer == null) {
        detailStickyTimer = window.setTimeout(() => {
          detailStickyTimer = null;
          if (headerTuckedAway()) sticky.classList.add("show");
        }, STICKY_DWELL);
      }
    } else {
      clearStickyTimer();
      sticky.classList.remove("show");
    }
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
  clearStickyTimer();
  const prog = $("#readProgress");
  if (prog) { prog.hidden = true; prog.style.width = "0"; }
}
