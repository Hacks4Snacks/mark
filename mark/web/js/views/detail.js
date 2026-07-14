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
let detailResizeHandler = null; // re-measures cached scroll metrics on resize
let detailStickyTimer = null; // pending sticky-header reveal (dwell debounce)
let detailGeneration = 0; // invalidates stale navigation/page/content responses

const fileRowHTML = (file) =>
  `<div class="aside-file" title="${esc(file.file_path)}">${esc(file.file_path)}</div>`;

const refRowHTML = (ref) =>
  `<a class="aside-file" href="${esc(ref.ref_value)}" target="_blank" rel="noopener" style="direction:ltr">${esc(ref.ref_value)}</a>`;

const attachmentDownloadHref = (session, attachment) =>
  (attachment.id != null && attachment.downloadable)
    ? `/api/sessions/${encodeURIComponent(session.id)}/attachments/${encodeURIComponent(attachment.id)}/download`
    : null;

function attachmentAsideHTML(session, attachment) {
  const attachmentIcon = attachment.category === "memory" ? "archive" : "paperclip";
  const downloadHref = attachmentDownloadHref(session, attachment);
  const downloadLink = downloadHref
    ? `<a class="att-dl" href="${downloadHref}" download="${esc(attachment.filename || "file")}" title="Download ${esc(attachment.filename || "file")}">${icon("download", { size: 14 })}</a>`
    : "";
  return `<div class="aside-file att-row" title="${esc(attachment.filename || "")}">`
    + `<a class="att-jump" data-att-doc="${attachment.id ?? ""}">${icon(attachmentIcon, { size: 13 })} ${esc(attachment.filename || "file")}</a>${downloadLink}</div>`;
}

function attachmentDetailHTML(session, attachment) {
  const attachmentIcon = attachment.category === "memory" ? "archive" : "paperclip";
  const meta = `${esc(attachment.filename || "file")} · ${fmtBytes(attachment.size_bytes)}`;
  const downloadHref = attachmentDownloadHref(session, attachment);
  const downloadLink = downloadHref
    ? `<a class="att-dl" href="${downloadHref}" download="${esc(attachment.filename || "file")}" title="Download ${esc(attachment.filename || "file")}">${icon("download", { size: 14 })} Download</a>`
    : "";
  const inner = attachment.content_available
    ? `<div class="attachment-body muted">Open to load content.</div>`
    : `<p class="muted">Content was not captured or is no longer available.</p>`;
  return `<details class="attachment" id="att-${attachment.id}" data-doc-id="${attachment.id ?? ""}"><summary>${icon(attachmentIcon, { size: 13 })} ${meta}${downloadLink}</summary>${inner}</details>`;
}

function attachmentGroupHTML(session, attachments, category) {
  if (!attachments.length) return "";
  const isMemory = category === "memory";
  return `<div class="attachments attachment-group" data-att-category="${category}">`
    + `<h3>${isMemory ? "Memory notes" : "Attachments created by the agent"}</h3>`
    + (isMemory ? `<p class="muted">Durable notes the agent saved with its memory tool while working on this conversation.</p>` : "")
    + `<div class="attachment-items">${attachments.map((attachment) => attachmentDetailHTML(session, attachment)).join("")}</div></div>`;
}

export async function openSession(id, opts = {}) {
  const generation = ++detailGeneration;
  try {
    const s = await api("/api/sessions/" + encodeURIComponent(id));
    if (generation !== detailGeneration) return;
    withTransition(() => {
      if (generation !== detailGeneration) return;
      state.currentId = id;
      renderDetail(s);
    });
    if (!opts.fromHash) location.hash = "#/session/" + encodeURIComponent(id);
  } catch (e) {
    if (generation !== detailGeneration) return;
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
  const refs = (s.refs || []);
  const filesTotal = Number(s.files_total ?? files.length);
  const refsTotal = Number(s.refs_total ?? refs.length);
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

  if (filesTotal) {
    asideBlocks.push(`<div><h4>Files (<span id="detailFilesLoaded">${files.length}</span> of ${filesTotal})</h4><div class="aside-files" id="detailFiles">${
      files.map(fileRowHTML).join("")
    }</div><button class="btn btn-ghost detail-metadata-more" id="detailFilesMore" type="button"${files.length < filesTotal ? "" : " hidden"}>${icon("plus")} Load more</button></div>`);
  }
  if (refsTotal) {
    asideBlocks.push(`<div><h4>Links (<span id="detailRefsLoaded">${refs.length}</span> of ${refsTotal})</h4><div class="aside-files" id="detailRefs">${
      refs.map(refRowHTML).join("")
    }</div><button class="btn btn-ghost detail-metadata-more" id="detailRefsMore" type="button"${refs.length < refsTotal ? "" : " hidden"}>${icon("plus")} Load more</button></div>`);
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
  const attachmentsTotal = Number(s.attachments_total ?? attachments.length);
  const memAtts = attachments.filter((attachment) => attachment.category === "memory");
  const agentAtts = attachments.filter((attachment) => attachment.category !== "memory");
  if (attachmentsTotal) {
    asideBlocks.push(`<div><h4>Attachments (<span id="detailAttachmentsLoaded">${attachments.length}</span> of ${attachmentsTotal})</h4><div class="aside-files" id="detailAttachments">${
      attachments.map((attachment) => attachmentAsideHTML(s, attachment)).join("")
    }</div><button class="btn btn-ghost detail-metadata-more" id="detailAttachmentsMore" type="button"${attachments.length < attachmentsTotal ? "" : " hidden"}>${icon("plus")} Load more</button></div>`);
  }

  let body;
  let turnsBody = "";
  if (s.source === "upload" && s.document) {
    body = s.document.deferred
      ? `<div class="deferred-document"><div><strong>Large document</strong><span>${Number(s.document.content_chars || 0).toLocaleString()} characters</span></div><button class="btn btn-ghost" id="documentLoad" type="button">${icon("download")} Load document</button></div>`
      : `<div class="md">${s.document.html || ""}</div>`;
  } else {
    turnsBody = (s.turns || []).map(turnHTML).join("");
    body = `<div id="detailTurns">${turnsBody}</div>`
      + `<button class="btn btn-ghost detail-load-more" id="detailLoadMore" type="button"${s.has_more_turns ? "" : " hidden"}>${icon("plus")} Load more</button>`;
  }
  if (attachmentsTotal) {
    body += `<div id="detailAttachmentGroups">${attachmentGroupHTML(s, memAtts, "memory")}${attachmentGroupHTML(s, agentAtts, "agent")}</div>`;
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
    const generation = detailGeneration;
    const willHide = !s.hidden;
    try {
      await api(`/api/sessions/${encodeURIComponent(s.id)}/${willHide ? "hide" : "unhide"}`, { method: "POST" });
      if (generation !== detailGeneration || state.currentId !== s.id) return;
      s.hidden = willHide ? 1 : 0;
      toast(willHide ? "Session hidden" : "Session unhidden");
      // Hidden sessions stay reachable here, but counts/facets shift, so refresh.
      loadStats();
      loadFacets();
      renderDetail(s);
    } catch (e) { toast(e.message, true); }
  });
  $("#deleteBtn")?.addEventListener("click", async () => {
    const generation = detailGeneration;
    const name = s.title || "this conversation";
    if (!window.confirm(`Permanently delete \u201C${name}\u201D? This removes it for good and keeps it from being re-imported on the next scan. This cannot be undone \u2014 use Hide if you only want it out of the way.`)) return;
    try {
      await api(`/api/sessions/${encodeURIComponent(s.id)}`, { method: "DELETE" });
      if (generation !== detailGeneration || state.currentId !== s.id) return;
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
  wireAttachmentJumps();
  wireDeferredTurns(s);
  wireAttachments(s);
  wireMetadataPaging(s);
  $("#documentLoad")?.addEventListener("click", (event) => loadDocument(s, event.currentTarget));
  $("#detailLoadMore")?.addEventListener("click", () => loadMoreTurns(s));
  $("#topicAdd")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const generation = detailGeneration;
    const tag = $("#topicInput").value.trim();
    if (!tag) return;
    try {
      await api(`/api/sessions/${encodeURIComponent(s.id)}/tags`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tag }),
      });
      const refreshed = await api("/api/sessions/" + encodeURIComponent(s.id));
      if (generation !== detailGeneration || state.currentId !== s.id) return;
      renderDetail(refreshed);
      loadFacets();
      toast("Topic added");
    } catch (err) { toast(err.message, true); }
  });
  $$("#detailView .topic-x").forEach((b) =>
    b.addEventListener("click", async () => {
      const generation = detailGeneration;
      try {
        await api(`/api/sessions/${encodeURIComponent(s.id)}/tags/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" });
        const refreshed = await api("/api/sessions/" + encodeURIComponent(s.id));
        if (generation !== detailGeneration || state.currentId !== s.id) return;
        renderDetail(refreshed);
        loadFacets();
        toast("Topic removed");
      } catch (err) { toast(err.message, true); }
    })
  );
  loadRelated(s.id);
}

async function loadDocument(s, button) {
  const generation = detailGeneration;
  button.disabled = true;
  try {
    const document = await api(`/api/sessions/${encodeURIComponent(s.id)}/document`);
    if (generation !== detailGeneration || state.currentId !== s.id || !button.isConnected) return;
    const host = button.closest(".deferred-document");
    if (host) {
      host.className = "md";
      host.innerHTML = document.html;
      setupReading();
    }
  } catch (error) {
    if (generation !== detailGeneration || !button.isConnected) return;
    button.disabled = false;
    toast(error.message, true);
  }
}

async function loadMoreTurns(s) {
  const button = $("#detailLoadMore");
  if (!button || button.disabled) return;
  const generation = detailGeneration;
  const transcript = $("#detailTurns");
  button.disabled = true;
  try {
    const offset = (s.turns || []).length;
    const page = await api(`/api/sessions/${encodeURIComponent(s.id)}/turns?offset=${offset}`);
    if (generation !== detailGeneration || state.currentId !== s.id || !transcript?.isConnected) return;
    transcript.insertAdjacentHTML("beforeend", page.turns.map(turnHTML).join(""));
    s.turns = (s.turns || []).concat(page.turns);
    s.has_more_turns = page.has_more;
    button.hidden = !page.has_more;
    wireDeferredTurns(s);
    setupReading();
  } catch (error) {
    if (generation !== detailGeneration) return;
    toast(error.message, true);
  } finally {
    if (generation === detailGeneration && button.isConnected) button.disabled = false;
  }
}

function wireDeferredTurns(s) {
  $$("#detailView .deferred-turn-load").forEach((button) => {
    if (button.dataset.wired) return;
    button.dataset.wired = "1";
    button.addEventListener("click", async () => {
      const generation = detailGeneration;
      button.disabled = true;
      try {
        const turn = await api(`/api/sessions/${encodeURIComponent(s.id)}/turns/${encodeURIComponent(button.dataset.turn)}`);
        if (generation !== detailGeneration || state.currentId !== s.id || !button.isConnected) return;
        button.closest(".turn")?.replaceWith(htmlElement(turnHTML(turn)));
        setupReading();
      } catch (error) {
        if (generation !== detailGeneration) return;
        button.disabled = false;
        toast(error.message, true);
      }
    });
  });
}

function wireAttachments(s) {
  $$("#detailView .attachment[data-doc-id]").forEach((panel) => {
    if (panel.dataset.wired) return;
    panel.dataset.wired = "1";
    panel.addEventListener("toggle", async () => {
      if (!panel.open || panel.dataset.loaded) return;
      const generation = detailGeneration;
      const body = $(".attachment-body", panel);
      if (!body) return;
      panel.dataset.loaded = "1";
      body.textContent = "Loading...";
      try {
        const content = await api(`/api/sessions/${encodeURIComponent(s.id)}/attachments/${encodeURIComponent(panel.dataset.docId)}`);
        if (generation !== detailGeneration || state.currentId !== s.id || !panel.isConnected) return;
        body.className = "attachment-body md";
        body.innerHTML = content.html;
        setupReading();
      } catch (error) {
        if (generation !== detailGeneration || !panel.isConnected) return;
        delete panel.dataset.loaded;
        body.textContent = error.message;
        toast(error.message, true);
      }
    });
  });
}

function wireAttachmentJumps() {
  $$("#detailView [data-att-doc]").forEach((link) => {
    if (link.dataset.wired) return;
    link.dataset.wired = "1";
    link.addEventListener("click", (event) => {
      event.preventDefault();
      const panel = $("#att-" + link.dataset.attDoc);
      if (panel) {
        panel.open = true;
        panel.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });
  });
  $$("#detailView .att-dl").forEach((link) => {
    if (link.dataset.wired) return;
    link.dataset.wired = "1";
    link.addEventListener("click", (event) => event.stopPropagation());
  });
}

function appendAttachmentDetails(s, attachments) {
  const groups = $("#detailAttachmentGroups");
  if (!groups) return;
  for (const category of ["memory", "agent"]) {
    const items = attachments.filter((attachment) =>
      category === "memory"
        ? attachment.category === "memory"
        : attachment.category !== "memory"
    );
    if (!items.length) continue;
    let group = $(`#detailAttachmentGroups [data-att-category="${category}"]`);
    if (!group) {
      groups.insertAdjacentHTML("beforeend", attachmentGroupHTML(s, items, category));
      continue;
    }
    $(".attachment-items", group)?.insertAdjacentHTML(
      "beforeend",
      items.map((attachment) => attachmentDetailHTML(s, attachment)).join("")
    );
  }
}

function wireMetadataPaging(s) {
  const resources = [
    {
      button: $("#detailFilesMore"),
      host: $("#detailFiles"),
      loaded: $("#detailFilesLoaded"),
      endpoint: "files",
      stateKey: "files",
      rowHTML: fileRowHTML,
    },
    {
      button: $("#detailRefsMore"),
      host: $("#detailRefs"),
      loaded: $("#detailRefsLoaded"),
      endpoint: "refs",
      stateKey: "refs",
      rowHTML: refRowHTML,
    },
    {
      button: $("#detailAttachmentsMore"),
      host: $("#detailAttachments"),
      loaded: $("#detailAttachmentsLoaded"),
      endpoint: "attachments",
      stateKey: "attachments",
      rowHTML: (attachment) => attachmentAsideHTML(s, attachment),
      afterAppend: (items) => {
        appendAttachmentDetails(s, items);
        wireAttachmentJumps();
        wireAttachments(s);
        setupReading();
      },
    },
  ];
  for (const resource of resources) {
    if (!resource.button || resource.button.dataset.wired) continue;
    resource.button.dataset.wired = "1";
    resource.button.addEventListener("click", async () => {
      if (resource.button.disabled) return;
      const generation = detailGeneration;
      const offset = (s[resource.stateKey] || []).length;
      resource.button.disabled = true;
      try {
        const page = await api(`/api/sessions/${encodeURIComponent(s.id)}/${resource.endpoint}?offset=${offset}`);
        if (generation !== detailGeneration || state.currentId !== s.id || !resource.host?.isConnected) return;
        resource.host.insertAdjacentHTML(
          "beforeend",
          page.items.map(resource.rowHTML).join("")
        );
        s[resource.stateKey] = (s[resource.stateKey] || []).concat(page.items);
        if (resource.loaded) resource.loaded.textContent = String(s[resource.stateKey].length);
        resource.button.hidden = !page.has_more;
        resource.afterAppend?.(page.items);
      } catch (error) {
        if (generation !== detailGeneration) return;
        toast(error.message, true);
      } finally {
        if (generation === detailGeneration && resource.button.isConnected) {
          resource.button.disabled = false;
        }
      }
    });
  }
}

function htmlElement(markup) {
  const template = document.createElement("template");
  template.innerHTML = markup.trim();
  return template.content.firstElementChild;
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
  if (t.deferred) {
    return `<div class="turn deferred-turn" data-turn-index="${t.turn_index}">
      <div><strong>Large turn</strong><span>${Number(t.content_chars || 0).toLocaleString()} characters</span></div>
      <button class="btn btn-ghost deferred-turn-load" type="button" data-turn="${t.turn_index}">${icon("download")} Load turn</button>
    </div>`;
  }
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
  // view, so drop any prior handlers before wiring fresh ones.
  if (detailScrollHandler) window.removeEventListener("scroll", detailScrollHandler);
  if (detailResizeHandler) window.removeEventListener("resize", detailResizeHandler);
  clearStickyTimer();
  const prog = $("#readProgress");
  const sticky = $("#detailSticky");
  const head = $("#detailView .detail-head");
  const topbar = document.querySelector(".topbar");
  if (prog) prog.hidden = false;

  // A single agent turn can carry a megabyte of rendered markdown, so a long
  // transcript is a very large DOM. Reading layout (getBoundingClientRect,
  // scrollHeight) right after writing a style forces a synchronous reflow, and
  // doing that on every scroll event made scrolling crawl — the reflow cost
  // scales with the whole document. So cache the only two metrics the handler
  // needs and refresh them only when layout can actually change (resize):
  //   pinTop    — the topbar height, where the compact header pins. It varies
  //               with viewport width (the action row wraps), hence measured.
  //   headBottom — the full header's bottom in document space, so "has the
  //               header scrolled away" is pure arithmetic against scrollTop.
  let pinTop = 0;
  let headBottom = 0;
  const measure = () => {
    pinTop = topbar ? topbar.getBoundingClientRect().height : 0;
    headBottom = head ? head.getBoundingClientRect().bottom + window.scrollY : 0;
    if (sticky) sticky.style.top = pinTop + "px";
  };

  // Coalesce bursts of scroll events to one update per animation frame, and do
  // every read before any write so nothing forces a reflow.
  let ticking = false;
  const update = () => {
    ticking = false;
    const doc = document.documentElement;
    const scrollTop = doc.scrollTop;
    const max = doc.scrollHeight - doc.clientHeight;
    const pct = max > 0 ? Math.min(1, Math.max(0, scrollTop / max)) : 0;
    if (prog) prog.style.width = (pct * 100).toFixed(1) + "%";
    if (!sticky) return;
    // True once the full header has scrolled above the compact bar's pin line.
    const tuckedAway = scrollTop + pinTop >= headBottom;
    if (tuckedAway) {
      // Arm a delayed reveal; a transient pass-through never gets to fire it.
      if (!sticky.classList.contains("show") && detailStickyTimer == null) {
        detailStickyTimer = window.setTimeout(() => {
          detailStickyTimer = null;
          if (document.documentElement.scrollTop + pinTop >= headBottom) {
            sticky.classList.add("show");
          }
        }, STICKY_DWELL);
      }
    } else {
      clearStickyTimer();
      sticky.classList.remove("show");
    }
  };
  const onScroll = () => {
    if (!ticking) {
      ticking = true;
      window.requestAnimationFrame(update);
    }
  };
  const onResize = () => { measure(); onScroll(); };
  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("resize", onResize, { passive: true });
  detailScrollHandler = onScroll;
  detailResizeHandler = onResize;
  measure();
  update();
}

export function teardownReading() {
  detailGeneration += 1;
  if (detailScrollHandler) {
    window.removeEventListener("scroll", detailScrollHandler);
    detailScrollHandler = null;
  }
  if (detailResizeHandler) {
    window.removeEventListener("resize", detailResizeHandler);
    detailResizeHandler = null;
  }
  clearStickyTimer();
  const prog = $("#readProgress");
  if (prog) { prog.hidden = true; prog.style.width = "0"; }
}
