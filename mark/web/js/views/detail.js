"use strict";

// Single-conversation detail view: transcript, metadata aside, topic editing,
// related sessions, and the reading-progress / sticky-header behaviour.

import { api } from "../api.js";
import { showOnly, setLayoutWide, state } from "../state.js";
import { loadFacets, loadStats } from "../sidebar.js";
import {
  $, $$, adjacentMatchPosition, esc, fmtBytes, fmtCost, fmtDate, fmtDuration, fmtTokens, highlightEvidence,
  sessionHash, srcMeta, toast, withTransition,
} from "../utils.js";
import { icon } from "../icons.js";
import { doSearch, showList } from "./list.js";
import { openCollMenu } from "./collections.js";

let detailScrollHandler = null; // active reading-progress listener
let detailResizeHandler = null; // re-measures cached scroll metrics on resize
let detailStickyTimer = null; // pending sticky-header reveal (dwell debounce)
let detailGeneration = 0; // invalidates stale navigation/page/content responses
let detailRoute = null;
let activeSession = null;
let activeEvidence = { turnIndex: null, q: "" };
let evidenceRequest = 0;
let matchPage = null;
let matchIndex = -1;

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
  const target = {
    turnIndex: Number.isSafeInteger(opts.turnIndex) && opts.turnIndex >= 0 ? opts.turnIndex : null,
    q: (opts.q || "").trim().slice(0, 2000),
    invalidTarget: !!opts.invalidTarget,
  };
  const route = sessionHash(id, target);
  if (state.view === "detail" && detailRoute === route && !target.invalidTarget && !opts.refresh) return;
  const generation = ++detailGeneration;
  evidenceRequest += 1;
  detailRoute = route;
  try {
    const params = new URLSearchParams();
    if (target.turnIndex != null) params.set("turn_index", String(target.turnIndex));
    const s = await api("/api/sessions/" + encodeURIComponent(id) + (params.size ? "?" + params : ""));
    if (generation !== detailGeneration) return;
    activeEvidence = target;
    if (!opts.fromHash && location.hash !== route) history.pushState(null, "", route);
    await new Promise((resolve) => withTransition(() => {
      if (generation !== detailGeneration) { resolve(); return; }
      state.currentId = id;
      resolve(renderDetail(s));
    }));
  } catch (e) {
    if (generation !== detailGeneration) return;
    detailRoute = null;
    toast("Conversation unavailable: " + e.message, true);
  }
}

function renderDetail(s) {
  activeSession = s;
  state.view = "detail";
  setLayoutWide(false);
  showOnly("#detailView");
  const view = $("#detailView");
  window.scrollTo({ top: 0, behavior: "instant" });

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
    body = `<button class="btn btn-ghost detail-load-more" id="detailLoadPrevious" type="button"${s.turns_offset > 0 ? "" : " hidden"}>${icon("arrow-left")} Load previous turns</button>`
      + `<div id="detailTurns">${turnsBody}</div>`
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
    <div class="detail-sticky evidence-toolbar" id="detailSticky">
      <button type="button" class="ds-back" id="dsBack" title="Back to results">${icon("arrow-left", { size: 16 })}<span class="ds-title">${esc(s.title || "Untitled")}</span></button>
      ${s.turns_total ? `<form id="detailFind" class="detail-find" role="search" aria-label="Find in conversation">
        <input type="search" id="detailQuery" aria-label="Find in conversation" placeholder='Find in conversation; use "quotes" for phrases' maxlength="2000" value="${esc(activeEvidence.q)}" />
        <button class="btn btn-ghost" type="submit">Find</button>
        <div class="detail-match-controls">
          <button class="btn btn-ghost" type="button" id="detailMatchPrevious" aria-label="Previous matching turn" disabled>${icon("arrow-left")}</button>
          <output id="detailMatchCount" aria-live="polite">All indexed turns</output>
          <button class="btn btn-ghost" type="button" id="detailMatchNext" aria-label="Next matching turn" disabled>${icon("arrow-right")}</button>
        </div>
      </form>` : ""}
      <p id="evidenceNotice" class="evidence-notice" role="status" hidden></p>
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
    const url = location.origin + location.pathname + sessionHash(s.id, activeEvidence);
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
  $("#detailLoadPrevious")?.addEventListener("click", () => loadMoreTurns(s, true));
  $("#detailFind")?.addEventListener("submit", (event) => {
    event.preventDefault();
    openSession(s.id, { q: $("#detailQuery").value, refresh: true });
  });
  $("#detailMatchPrevious")?.addEventListener("click", () => moveMatch(s, -1));
  $("#detailMatchNext")?.addEventListener("click", () => moveMatch(s, 1));
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
  return initializeEvidence(s);
}

function evidenceNotice(message = "") {
  const host = $("#evidenceNotice");
  if (!host) return;
  host.textContent = message;
  host.hidden = !message;
}

function evidenceCurrent(s, request, generation) {
  return request === evidenceRequest && generation === detailGeneration
    && activeSession === s && state.view === "detail";
}

function updateMatchControls() {
  const total = matchPage?.total || 0;
  const position = matchIndex < 0 ? -1 : matchPage.offset + matchIndex;
  const output = $("#detailMatchCount");
  if (output) output.textContent = !activeEvidence.q ? "All indexed turns"
    : position >= 0 ? `${position + 1} / ${total} matching turns` : `${total} matching turns`;
  const previous = $("#detailMatchPrevious");
  const next = $("#detailMatchNext");
  if (previous) previous.disabled = !total || adjacentMatchPosition(matchPage, matchIndex, -1) < 0;
  if (next) next.disabled = !total || adjacentMatchPosition(matchPage, matchIndex, 1) >= total;
}

function rememberEvidence(s, replace = false) {
  const route = sessionHash(s.id, activeEvidence);
  detailRoute = route;
  if (location.hash !== route) history[replace ? "replaceState" : "pushState"](null, "", route);
}

async function matchingTurns(s, params) {
  const query = new URLSearchParams({ q: activeEvidence.q, ...params });
  return api(`/api/sessions/${encodeURIComponent(s.id)}/matches?${query}`);
}

async function initializeEvidence(s) {
  const request = ++evidenceRequest;
  const generation = detailGeneration;
  matchPage = null;
  matchIndex = -1;
  const missing = activeEvidence.invalidTarget || s.target_turn_found === false;
  if (missing) {
    activeEvidence.turnIndex = null;
    evidenceNotice("This turn is no longer available or the link is invalid. Showing the beginning of the conversation.");
  }
  try {
    if (activeEvidence.q && s.turns_total) {
      const params = activeEvidence.turnIndex == null ? {} : { turn_index: activeEvidence.turnIndex };
      const page = await matchingTurns(s, params);
      if (!evidenceCurrent(s, request, generation)) return;
      matchPage = page;
      if (activeEvidence.turnIndex == null && !missing) activeEvidence.turnIndex = page.turn_indices[0] ?? null;
      matchIndex = page.turn_indices.indexOf(activeEvidence.turnIndex);
      if (!page.total && !missing) evidenceNotice("No matching indexed messages. Matches use keyword/phrase search, not semantic similarity or session metadata.");
    }
    updateMatchControls();
    if (activeEvidence.turnIndex != null) {
      await focusEvidenceTurn(s, activeEvidence.turnIndex, request, generation);
    } else if (!s.turns_total && activeEvidence.q) {
      const document = $(".transcript > .md");
      if (document) highlightEvidence(document, activeEvidence.q);
    }
    if (evidenceCurrent(s, request, generation) && !missing) rememberEvidence(s, true);
  } catch (error) {
    if (evidenceCurrent(s, request, generation)) evidenceNotice("Evidence lookup failed: " + error.message);
  }
}

async function moveMatch(s, delta) {
  if (!matchPage?.total) return;
  const request = ++evidenceRequest;
  const generation = detailGeneration;
  const position = adjacentMatchPosition(matchPage, matchIndex, delta);
  if (position < 0 || position >= matchPage.total) return;
  $("#detailMatchPrevious").disabled = true;
  $("#detailMatchNext").disabled = true;
  try {
    if (position < matchPage.offset || position >= matchPage.offset + matchPage.turn_indices.length) {
      const page = await matchingTurns(s, { offset: Math.floor(position / 100) * 100 });
      if (!evidenceCurrent(s, request, generation)) return;
      matchPage = page;
    }
    matchIndex = position - matchPage.offset;
    const turn = matchPage.turn_indices[matchIndex];
    if (turn == null) { evidenceNotice("Matches changed during indexing. Run Find again."); return; }
    if (await focusEvidenceTurn(s, turn, request, generation)) {
      activeEvidence.turnIndex = turn;
      rememberEvidence(s);
    }
  } catch (error) {
    if (evidenceCurrent(s, request, generation)) evidenceNotice(error.message);
  } finally {
    if (evidenceCurrent(s, request, generation)) updateMatchControls();
  }
}

function updateTurnPaging(s) {
  const previous = $("#detailLoadPrevious");
  const next = $("#detailLoadMore");
  if (previous) previous.hidden = !(s.turns_offset > 0);
  if (next) next.hidden = !s.has_more_turns;
}

async function focusEvidenceTurn(s, index, request, generation) {
  let turn = $("#turn-" + index);
  if (!turn) {
    const page = await api(`/api/sessions/${encodeURIComponent(s.id)}?turn_index=${index}`);
    if (!evidenceCurrent(s, request, generation)) return false;
    if (!page.target_turn_found) {
      evidenceNotice("This turn is no longer available. Open the conversation again to refresh it.");
      return false;
    }
    s.turns = page.turns;
    s.turns_offset = page.turns_offset;
    s.has_more_turns = page.has_more_turns;
    $("#detailTurns").innerHTML = page.turns.map(turnHTML).join("");
    updateTurnPaging(s);
    wireDeferredTurns(s);
    turn = $("#turn-" + index);
  }
  if (turn?.classList.contains("deferred-turn") || turn?.classList.contains("turn-preview")) {
    const params = new URLSearchParams({ preview: "true", q: activeEvidence.q });
    const preview = await api(`/api/sessions/${encodeURIComponent(s.id)}/turns/${index}?${params}`);
    if (!evidenceCurrent(s, request, generation) || !turn.isConnected) return false;
    const replacement = htmlElement(turnHTML(preview));
    turn.replaceWith(replacement);
    turn = replacement;
    wireDeferredTurns(s);
  }
  if (!turn || !evidenceCurrent(s, request, generation)) return false;
  $$("#detailTurns .evidence-target").forEach((previous) => {
    previous.classList.remove("evidence-target");
    highlightEvidence(previous, "");
  });
  turn.classList.add("evidence-target");
  const highlighted = highlightEvidence(turn, activeEvidence.q);
  if (activeEvidence.q && !highlighted) evidenceNotice("Showing the selected turn; no literal highlight was found. The match may use stemming or semantic similarity.");
  else evidenceNotice();
  setupReading();
  turn.focus({ preventScroll: true });
  (highlighted || turn).scrollIntoView({ block: highlighted ? "center" : "start", behavior: "instant" });
  return true;
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
      highlightEvidence(host, activeEvidence.q);
      setupReading();
    }
  } catch (error) {
    if (generation !== detailGeneration || !button.isConnected) return;
    button.disabled = false;
    toast(error.message, true);
  }
}

async function loadMoreTurns(s, previous = false) {
  const button = $(previous ? "#detailLoadPrevious" : "#detailLoadMore");
  if (!button || button.disabled) return;
  const generation = detailGeneration;
  const transcript = $("#detailTurns");
  button.disabled = true;
  try {
    const start = s.turns_offset || 0;
    const size = s.turns_limit || 20;
    const offset = previous ? Math.max(0, start - size) : start + (s.turns || []).length;
    const limit = previous ? start - offset : size;
    const page = await api(`/api/sessions/${encodeURIComponent(s.id)}/turns?offset=${offset}&limit=${limit}`);
    if (generation !== detailGeneration || state.currentId !== s.id || !transcript?.isConnected) return;
    if ((s.turns_offset || 0) !== start) return;
    transcript.insertAdjacentHTML(previous ? "afterbegin" : "beforeend", page.turns.map(turnHTML).join(""));
    s.turns = previous ? page.turns.concat(s.turns || []) : (s.turns || []).concat(page.turns);
    if (previous) s.turns_offset = offset;
    else s.has_more_turns = page.has_more;
    updateTurnPaging(s);
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
  $$("#detailView .copy-turn-link").forEach((button) => {
    if (button.dataset.wired) return;
    button.dataset.wired = "1";
    button.addEventListener("click", async () => {
      const hash = sessionHash(s.id, { turnIndex: Number(button.dataset.turn), q: activeEvidence.q });
      try { await navigator.clipboard.writeText(location.origin + location.pathname + hash); toast("Turn link copied"); }
      catch (_) { toast("Copy failed", true); }
    });
  });
  $$("#detailView .deferred-turn-load").forEach((button) => {
    if (button.dataset.wired) return;
    button.dataset.wired = "1";
    button.addEventListener("click", async () => {
      const generation = detailGeneration;
      button.disabled = true;
      try {
        const turn = await api(`/api/sessions/${encodeURIComponent(s.id)}/turns/${encodeURIComponent(button.dataset.turn)}`);
        if (generation !== detailGeneration || state.currentId !== s.id || !button.isConnected) return;
        const replacement = htmlElement(turnHTML(turn));
        button.closest(".turn")?.replaceWith(replacement);
        wireDeferredTurns(s);
        if (activeEvidence.turnIndex === turn.turn_index) {
          replacement.classList.add("evidence-target");
          highlightEvidence(replacement, activeEvidence.q);
        }
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
  const number = Number(t.turn_index);
  const heading = `<div class="turn-heading"><a href="${esc(sessionHash(activeSession?.id || state.currentId, { turnIndex: number, q: activeEvidence.q }))}">Turn ${number + 1}</a><button class="btn btn-ghost copy-turn-link" type="button" data-turn="${number}" aria-label="Copy link to turn ${number + 1}">${icon("link", { size: 13 })} Copy turn link</button></div>`;
  if (t.deferred) {
    return `<section class="turn deferred-turn" id="turn-${number}" data-turn-index="${number}" tabindex="-1" aria-label="Turn ${number + 1}">${heading}
      <div class="deferred-message"><div><strong>Large turn</strong><span>${Number(t.content_chars || 0).toLocaleString()} characters</span></div>
      <button class="btn btn-ghost deferred-turn-load" type="button" data-turn="${t.turn_index}">${icon("download")} Load turn</button>
      </div></section>`;
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
  const preview = t.preview ? `<div class="turn-preview-note">Showing excerpts from a large turn (${Number(t.content_chars || 0).toLocaleString()} characters).<button class="btn btn-ghost deferred-turn-load" type="button" data-turn="${number}">Load full turn</button></div>` : "";
  return `<section class="turn${t.preview ? " turn-preview" : ""}" id="turn-${number}" data-turn-index="${number}" tabindex="-1" aria-label="Turn ${number + 1}">${heading}${preview}${user}${asst}</section>`;
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
    const offset = pinTop + (sticky?.offsetHeight || 0) + 16;
    if (sticky) sticky.style.top = pinTop + "px";
    $("#detailView")?.style.setProperty("--evidence-scroll-top", offset + "px");
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
  evidenceRequest += 1;
  detailRoute = null;
  activeSession = null;
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
