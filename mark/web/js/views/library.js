"use strict";

// Extracted snippets and independently saved, user-curated solutions.

import { api } from "../api.js";
import { showOnly, setLayoutWide, state } from "../state.js";
import { $, $$, debounce, esc, fmtDate, sessionHash, srcMeta, toast, withTransition } from "../utils.js";
import { icon } from "../icons.js";
import { openSession, teardownReading } from "./detail.js";

export const libState = { q: "", language: "", commands: false };
let snippetData = [];
let libraryGeneration = 0;
let snippetRequest = 0;
let solutionRequest = 0;
const solutionState = { q: "", tag: "", status: "", favorite: false, offset: 0 };
const STATUS_LABELS = { needs_review: "Needs review", verified: "Verified", outdated: "Outdated" };
const jsonRequest = (method, body) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

export async function showLibrary(opts = {}) {
  const generation = ++libraryGeneration;
  snippetRequest += 1;
  solutionRequest += 1;
  const mode = opts.mode || state.libraryMode || "extracted";
  const leaving = state.view !== "library";
  state.view = "library";
  state.libraryMode = mode;
  state.currentId = null;
  teardownReading();
  const apply = () => {
    if (generation !== libraryGeneration || state.view !== "library") return;
    setLayoutWide(false);
    showOnly("#libraryView");
    $("#libExtractedPanel").hidden = mode !== "extracted";
    $("#libCuratedPanel").hidden = mode !== "curated";
    $$("#libModeToggle [role=tab]").forEach((button) => {
      const selected = button.dataset.libmode === mode;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
    });
  };
  if (leaving) withTransition(apply);
  else apply();
  const hash = mode === "curated" ? "#/library/curated" : "#/library";
  if (!opts.fromHash && location.hash !== hash) history.pushState(null, "", hash);
  if (mode === "curated") { loadCuratedSolutions(); return; }
  if (!$("#libLang").dataset.loaded) await loadSnippetLanguages();
  if (generation === libraryGeneration && state.view === "library") loadSnippets();
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

export async function loadSnippets() {
  const request = ++snippetRequest;
  const host = $("#libResults");
  const params = new URLSearchParams();
  if (libState.q) params.set("q", libState.q);
  if (libState.commands) params.set("commands", "true");
  else if (libState.language) params.set("language", libState.language);
  host.innerHTML = `<div class="lib-loading muted">Loading...</div>`;
  try {
    const data = await api("/api/snippets?" + params);
    if (request !== snippetRequest || state.view !== "library" || state.libraryMode !== "extracted") return;
    snippetData = data.snippets || [];
    $("#libCount").textContent = snippetData.length
      ? `${snippetData.length}${snippetData.length >= 80 ? "+" : ""} snippet${snippetData.length === 1 ? "" : "s"}`
      : "";
    if (!snippetData.length) {
      host.innerHTML = `<div class="empty"><div class="big">${icon("code", { size: 40 })}</div>No snippets match that filter.</div>`;
      return;
    }
    host.innerHTML = snippetData.map(snippetCardHTML).join("");
    $$("#libResults .snip-open").forEach((a) => a.addEventListener("click", (event) => {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      openSession(a.dataset.id, {
        turnIndex: a.dataset.turn === "" ? null : Number(a.dataset.turn), q: libState.q,
      });
    }));
    $$("#libResults .snip-copy").forEach((b) => b.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(snippetData[+b.dataset.idx].content); toast("Copied"); }
      catch (_) { toast("Copy failed", true); }
    }));
    $$("#libResults .snip-save").forEach((button) => button.addEventListener("click", () => {
      const snippet = snippetData[Number(button.dataset.idx)];
      openSolutionDialog({ source: { kind: "snippet", session_id: snippet.session_id, snippet_id: snippet.id } });
    }));
  } catch (e) {
    if (request !== snippetRequest || state.view !== "library" || state.libraryMode !== "extracted") return;
    host.innerHTML = `<div class="empty"><div class="big">${icon("alert", { size: 40 })}</div>${esc(e.message)}</div>`;
  }
}

function snippetCardHTML(s, i) {
  const lang = s.language || "text";
  const repo = s.repository ? ` · ${esc(s.repository)}` : "";
  return `<div class="snip-card">
    <div class="snip-head">
      <span class="snip-lang">${esc(lang)}</span>
      <a class="snip-open" data-id="${esc(s.session_id)}" data-turn="${s.turn_index ?? ""}" href="${esc(sessionHash(s.session_id, { turnIndex: s.turn_index, q: libState.q }))}" title="Open matching turn">${srcMeta(s.source).icon} ${esc(s.session_title || "Untitled")}${repo}</a>
      <button class="snip-copy" data-idx="${i}" title="Copy snippet">${icon("copy", { size: 14 })}</button>
      <button class="btn btn-ghost snip-save" data-idx="${i}" type="button">${icon("plus", { size: 14 })} Save solution</button>
    </div>
    <pre class="snip-code"><code>${esc(s.content)}</code></pre>
  </div>`;
}

function sourceStatusText(solution) {
  const status = solution.source_status || "available";
  const labels = {
    available: "Source content still matches this saved copy.",
    changed: "Source content changed. This saved copy has not been modified.",
    missing: "Source is missing. This saved copy and your annotations are preserved.",
  };
  return labels[status] + (solution.source_hidden ? " Source is hidden or disabled." : "");
}

function sourceSummary(solution) {
  const turn = Number.isSafeInteger(solution.source_turn_index) ? `Turn ${solution.source_turn_index + 1}` : "";
  return [solution.source_title, turn, solution.repository, srcMeta(solution.source).label].filter(Boolean).join(" · ");
}

function solutionCardHTML(solution) {
  const status = STATUS_LABELS[solution.status];
  return `<article class="solution-card" data-solution-id="${esc(solution.id)}">
    <div class="solution-card-head">
      <div><span class="solution-status ${esc(solution.status)}">${esc(status)}</span>
        <h3><button class="solution-open" type="button">${esc(solution.title)}</button></h3></div>
      <button class="btn btn-ghost solution-star${solution.favorite ? " is-favorite" : ""}" type="button" aria-pressed="${solution.favorite}" aria-label="${solution.favorite ? "Remove from" : "Add to"} favorites">${icon("star")}</button>
    </div>
    <p class="solution-excerpt">${esc(solution.content_preview || "")}</p>
    ${solution.notes ? `<p class="solution-card-notes"><b>Notes:</b> ${esc(solution.notes.slice(0, 240))}</p>` : ""}
    ${solution.prerequisites ? `<p class="solution-card-notes"><b>Requires:</b> ${esc(solution.prerequisites.slice(0, 160))}</p>` : ""}
    <div class="chips solution-tags">${solution.tags.map(tag => `<span class="pill">${esc(tag)}</span>`).join("")}</div>
    <p class="solution-origin">${esc(sourceSummary(solution))}</p>
    <p class="solution-health${solution.source_status !== "available" || solution.source_hidden ? " needs-attention" : ""}">${esc(sourceStatusText(solution))}</p>
    <div class="solution-card-foot"><span class="muted">Saved ${fmtDate(solution.created_at)} · ${solution.source_kind === "answer" ? "Answer" : esc(solution.language || "Snippet")}</span>
      <button class="btn btn-ghost solution-open" type="button">${icon("pencil", { size: 14 })} Open / edit</button>
    </div>
  </article>`;
}

async function loadCuratedSolutions(reset = true) {
  const request = ++solutionRequest;
  if (reset) solutionState.offset = 0;
  const params = new URLSearchParams({ ...solutionState, limit: 25 });
  const host = $("#solutionResults");
  host.innerHTML = `<p class="lib-loading muted">Loading saved solutions...</p>`;
  try {
    const page = await api("/api/solutions?" + params);
    if (request !== solutionRequest || state.view !== "library" || state.libraryMode !== "curated") return;
    $("#libCount").textContent = `${page.total} saved solution${page.total === 1 ? "" : "s"}`;
    if (!page.solutions.length && page.total && page.offset) {
      solutionState.offset = Math.floor((page.total - 1) / 25) * 25;
      return loadCuratedSolutions(false);
    }
    if (!page.solutions.length) {
      host.innerHTML = `<div class="empty"><div class="big">${icon("archive", { size: 36 })}</div><p>No saved solutions match these filters.</p><p>Use <b>Save answer</b> on a conversation or <b>Save solution</b> on an extracted snippet to start your collection.</p></div>`;
      return;
    }
    host.innerHTML = page.solutions.map(solutionCardHTML).join("")
      + `<div class="solution-paging"><button id="solutionsPrevious" class="btn btn-ghost" type="button"${page.offset ? "" : " disabled"}>Previous</button><span>${page.offset + 1}–${page.offset + page.solutions.length} of ${page.total}</span><button id="solutionsNext" class="btn btn-ghost" type="button"${page.has_more ? "" : " disabled"}>Next</button></div>`;
    $$(".solution-card", host).forEach((card, index) => {
      const solution = page.solutions[index];
      $$(".solution-open", card).forEach(button => button.addEventListener("click", () => openSolutionDialog({ solutionId: solution.id })));
      $(".solution-star", card).addEventListener("click", async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
          await api("/api/solutions/" + encodeURIComponent(solution.id), jsonRequest("PUT", editableSolutionFields(solution, { favorite: !solution.favorite })));
          // Every successful mutation refreshes the current query, even if a
          // different card's mutation already replaced the originating list.
          if (state.view === "library" && state.libraryMode === "curated") await loadCuratedSolutions();
        } catch (error) {
          if (button.isConnected) { button.disabled = false; toast(error.message, true); }
          if (state.view === "library" && state.libraryMode === "curated") loadCuratedSolutions();
        }
      });
    });
    $("#solutionsPrevious").addEventListener("click", () => { solutionState.offset = Math.max(0, page.offset - 25); loadCuratedSolutions(false); });
    $("#solutionsNext").addEventListener("click", () => { solutionState.offset = page.offset + 25; loadCuratedSolutions(false); });
  } catch (error) {
    if (request !== solutionRequest || state.view !== "library" || state.libraryMode !== "curated") return;
    host.innerHTML = `<div class="empty">${esc(error.message)}</div>`;
  }
}

export function editableSolutionFields(solution, overrides = {}) {
  const result = {};
  for (const key of ["title", "notes", "tags", "favorite", "status", "prerequisites", "revision"]) {
    result[key] = Object.hasOwn(overrides, key) ? overrides[key] : solution[key];
  }
  return result;
}

let dialogRequest = 0;
let dialogSource = null;
let dialogData = null;
let dialogFocus = null;
let dialogBusy = false;

function setSolutionBusy(busy) {
  dialogBusy = busy;
  $("#solutionFields").disabled = busy;
  $("#solutionSave").disabled = busy;
  $("#solutionDelete").disabled = busy;
  $("#solutionCopy").disabled = busy;
}

function solutionError(message = "") {
  const host = $("#solutionError");
  host.textContent = message;
  host.hidden = !message;
}

async function renderSolutionContent(data, request) {
  const host = $("#solutionContent");
  const pre = document.createElement("pre");
  pre.className = "solution-code";
  pre.textContent = data.content;
  host.replaceChildren(pre); // safe plain-text fallback; never inject raw source HTML
  if (data.source_kind !== "answer") return;
  try {
    const rendered = await api("/api/render", jsonRequest("POST", { text: data.content }));
    if (request !== dialogRequest || !$("#solutionDialog").open) return;
    const template = document.createElement("template");
    template.innerHTML = rendered.html;
    template.content.querySelectorAll("img, picture, video, audio, source, iframe, object, embed").forEach(node => node.remove());
    template.content.querySelectorAll("a").forEach(link => { link.target = "_blank"; link.rel = "noopener noreferrer"; });
    const prose = document.createElement("div");
    prose.className = "md";
    prose.append(template.content);
    host.replaceChildren(prose);
  } catch (_) { /* the exact saved text remains readable if rendering fails */ }
}

export async function openSolutionDialog({ source = null, solutionId = null } = {}) {
  const request = ++dialogRequest;
  const dialog = $("#solutionDialog");
  const originView = state.view;
  const originSession = state.currentId;
  dialogFocus = document.activeElement;
  dialogSource = source;
  dialogData = null;
  $("#solutionForm").reset();
  solutionError();
  $("#solutionLoading").hidden = false;
  $("#solutionDelete").hidden = true;
  $("#solutionSourceLink").hidden = true;
  $("#solutionContent").replaceChildren();
  $("#solutionContentPanel").open = !solutionId;
  $("#solutionOrigin").textContent = "";
  $("#solutionSourceStatus").textContent = "";
  $("#solutionDialogTitle").textContent = solutionId ? "Curated solution" : "Save solution";
  $("#solutionSave").textContent = solutionId ? "Save changes" : "Save solution";
  setSolutionBusy(true);
  if (!dialog.open) dialog.showModal();
  try {
    const data = solutionId
      ? await api("/api/solutions/" + encodeURIComponent(solutionId))
      : await api("/api/solutions/preview", jsonRequest("POST", source));
    if (request !== dialogRequest || !dialog.open) return;
    if (state.view !== originView || state.currentId !== originSession) { dialog.close(); return; }
    dialogData = data;
    $("#solutionTitle").value = data.title || `${data.source_title} — ${data.source_kind === "answer" ? "answer" : "snippet"}`.slice(0, 160);
    $("#solutionNotes").value = data.notes || "";
    $("#solutionTags").value = (data.tags || []).join(", ");
    $("#solutionFavorite").checked = !!data.favorite;
    $("#solutionStatus").value = data.status || "needs_review";
    $("#solutionPrerequisites").value = data.prerequisites || "";
    $("#solutionOrigin").textContent = sourceSummary(data) + ` · Session ${data.source_session_id}`;
    $("#solutionSourceStatus").textContent = sourceStatusText(data);
    const sourceLink = $("#solutionSourceLink");
    sourceLink.hidden = data.source_status === "missing";
    sourceLink.href = sessionHash(data.source_session_id, { turnIndex: data.source_turn_index });
    sourceLink.textContent = Number.isSafeInteger(data.source_turn_index) ? `Open source turn ${data.source_turn_index + 1}` : "Open source conversation";
    $("#solutionContentLabel").textContent = `${data.source_kind === "answer" ? "Answer" : "Snippet"} · ${data.content.length.toLocaleString()} characters (read-only copy)`;
    $("#solutionDelete").hidden = !solutionId;
    setSolutionBusy(false);
    $("#solutionTitle").focus();
    await renderSolutionContent(data, request);
  } catch (error) {
    if (request === dialogRequest && dialog.open) solutionError(error.message);
  } finally {
    if (request === dialogRequest) $("#solutionLoading").hidden = true;
  }
}

async function saveSolution(event) {
  event.preventDefault();
  if (dialogBusy || !dialogData) return;
  const request = dialogRequest;
  const data = dialogData;
  const fields = {
    title: $("#solutionTitle").value.trim(), notes: $("#solutionNotes").value,
    tags: $("#solutionTags").value.split(",").map(tag => tag.trim()).filter(Boolean),
    favorite: $("#solutionFavorite").checked, status: $("#solutionStatus").value,
    prerequisites: $("#solutionPrerequisites").value,
  };
  if (!fields.title) { solutionError("Enter a title for this solution."); return; }
  setSolutionBusy(true);
  solutionError();
  try {
    const result = data.id
      ? await api("/api/solutions/" + encodeURIComponent(data.id), jsonRequest("PUT", { ...fields, revision: data.revision }))
      : await api("/api/solutions", jsonRequest("POST", { ...fields, source: dialogSource, source_sha256: data.source_sha256 }));
    // Closing the dialog does not cancel an already-submitted write. Refresh
    // committed state independently of whether its original editor still exists.
    if (state.view === "library" && state.libraryMode === "curated") loadCuratedSolutions();
    if (request !== dialogRequest || !$("#solutionDialog").open) return;
    $("#solutionDialog").close();
    toast(data.id ? "Solution updated" : result.created ? "Saved to Curated solutions" : "Already saved. Edit it in Curated solutions.");
  } catch (error) {
    if (request === dialogRequest) solutionError(error.message);
  } finally {
    if (request === dialogRequest) setSolutionBusy(false);
  }
}

export function setupLibrary() {
  const dialog = $("#solutionDialog");
  if (dialog.dataset.wired) return;
  dialog.dataset.wired = "1";
  $$("#libModeToggle [role=tab]").forEach(button => {
    button.addEventListener("click", () => showLibrary({ mode: button.dataset.libmode }));
    button.addEventListener("keydown", event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const tabs = $$("#libModeToggle [role=tab]");
      const index = event.key === "Home" ? 0 : event.key === "End" ? 1 : 1 - tabs.indexOf(button);
      tabs[index].focus();
      tabs[index].click();
    });
  });
  $("#libSearch").addEventListener("input", debounce(() => { libState.q = $("#libSearch").value.trim(); loadSnippets(); }, 200));
  $("#libLang").addEventListener("change", () => { libState.language = $("#libLang").value; loadSnippets(); });
  $("#libCommands").addEventListener("change", () => {
    libState.commands = $("#libCommands").checked;
    $("#libLang").disabled = libState.commands;
    loadSnippets();
  });
  for (const [selector, key] of [["#solutionSearch", "q"], ["#solutionTagFilter", "tag"]]) {
    $(selector).addEventListener("input", debounce(() => { solutionState[key] = $(selector).value.trim(); loadCuratedSolutions(); }, 200));
  }
  $("#solutionStatusFilter").addEventListener("change", () => { solutionState.status = $("#solutionStatusFilter").value; loadCuratedSolutions(); });
  $("#solutionFavoritesOnly").addEventListener("change", () => { solutionState.favorite = $("#solutionFavoritesOnly").checked; loadCuratedSolutions(); });
  $("#solutionForm").addEventListener("submit", saveSolution);
  $("#solutionClose").addEventListener("click", () => dialog.close());
  $("#solutionCancel").addEventListener("click", () => dialog.close());
  dialog.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      dialog.close();
    }
  });
  dialog.addEventListener("close", () => {
    dialogRequest += 1;
    dialogData = null;
    dialogSource = null;
    if (dialogFocus?.isConnected) dialogFocus.focus({ preventScroll: true });
  });
  window.addEventListener("hashchange", () => { if (dialog.open) dialog.close(); });
  $("#solutionSourceLink").addEventListener("click", event => {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    const data = dialogData;
    if (!data || data.source_status === "missing") return;
    dialog.close();
    openSession(data.source_session_id, { turnIndex: data.source_turn_index });
  });
  $("#solutionCopy").addEventListener("click", async () => {
    if (!dialogData) return;
    try { await navigator.clipboard.writeText(dialogData.content); toast("Saved content copied"); }
    catch (_) { solutionError("Copy failed. Select the saved content to copy it manually."); }
  });
  $("#solutionDelete").addEventListener("click", async () => {
    if (dialogBusy || !dialogData?.id) return;
    if (!window.confirm("Delete this saved copy and its annotations? The original conversation is not changed.")) return;
    const data = dialogData;
    const request = dialogRequest;
    setSolutionBusy(true);
    try {
      await api(`/api/solutions/${encodeURIComponent(data.id)}?revision=${data.revision}`, { method: "DELETE" });
      if (state.view === "library" && state.libraryMode === "curated") loadCuratedSolutions();
      if (request !== dialogRequest) return;
      dialog.close();
      toast("Saved copy deleted; source unchanged");
    } catch (error) {
      if (request === dialogRequest) solutionError(error.message);
    } finally {
      if (request === dialogRequest) setSolutionBusy(false);
    }
  });
}
