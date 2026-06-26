"use strict";

// Snippet & command library: browse code blocks extracted from every session.

import { api } from "../api.js";
import { showOnly, state } from "../state.js";
import { $, $$, esc, srcMeta, toast, withTransition } from "../utils.js";
import { openSession, teardownReading } from "./detail.js";

export const libState = { q: "", language: "", commands: false };
let snippetData = [];

export async function showLibrary(opts = {}) {
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

export async function loadSnippets() {
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
