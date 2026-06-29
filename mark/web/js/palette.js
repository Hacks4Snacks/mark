"use strict";

// Command palette (Cmd/Ctrl-K). The signature recall surface: fuzzy across your
// conversations plus quick actions and navigation, all from the keyboard. It
// reuses the existing topbar buttons for actions (so wiring stays in one place)
// and calls view functions directly for navigation.

import { api } from "./api.js";
import { state } from "./state.js";
import { $, $$, debounce, esc, fmtDate, srcMeta } from "./utils.js";
import { icon } from "./icons.js";
import { openSession } from "./views/detail.js";
import { showList } from "./views/list.js";
import { showCollections } from "./views/collections.js";
import { showLibrary } from "./views/library.js";
import { showUsage } from "./views/usage.js";
import { showAsk } from "./views/ask.js";

// Static commands: navigation + actions. `run` does the work; actions click the
// already-wired topbar buttons so there is a single source of truth.
const COMMANDS = [
  { id: "nav-home", icon: "search", label: "Search & browse", hint: "Home", kind: "Go to", run: () => showList() },
  { id: "nav-collections", icon: "layers", label: "Collections", hint: "Saved, auto-updating groups", kind: "Go to", run: () => showCollections() },
  { id: "nav-library", icon: "code", label: "Snippet & command library", kind: "Go to", run: () => showLibrary() },
  { id: "nav-usage", icon: "pie", label: "Usage & spend", kind: "Go to", run: () => showUsage() },
  { id: "nav-ask", icon: "sparkles", label: "Ask your history", hint: "Local LLM", kind: "Go to", run: () => showAsk() },
  { id: "act-add", icon: "plus", label: "Add a note or file", kind: "Action", run: () => $("#addBtn")?.click() },
  { id: "act-rescan", icon: "sync", label: "Re-scan history now", kind: "Action", run: () => $("#reindexBtn")?.click() },
  { id: "act-theme", icon: "theme", label: "Toggle light / dark theme", kind: "Action", run: () => $("#themeBtn")?.click() },
];

let activeIdx = 0;
let items = [];          // current flat list of {type, ...} rendered as rows
let lastQueryToken = 0;  // guards out-of-order async search responses

const el = {};
function refs() {
  el.root = $("#cmdk");
  el.input = $("#cmdkInput");
  el.list = $("#cmdkList");
  el.backdrop = $("#cmdkBackdrop");
}

export function isPaletteOpen() {
  return el.root && !el.root.hidden;
}

export function openPalette() {
  if (!el.root) refs();
  if (isPaletteOpen()) return;
  el.root.hidden = false;
  document.body.classList.add("cmdk-open");
  el.input.value = "";
  requestAnimationFrame(() => el.input.focus());
  render("");
}

export function closePalette() {
  if (!el.root || el.root.hidden) return;
  el.root.hidden = true;
  document.body.classList.remove("cmdk-open");
}

// Lightweight subsequence fuzzy match for static commands.
function matches(q, text) {
  if (!q) return true;
  q = q.toLowerCase();
  text = text.toLowerCase();
  let i = 0;
  for (const ch of text) { if (ch === q[i]) i++; if (i === q.length) return true; }
  return text.includes(q);
}

function rowHTML(it, idx) {
  const sel = idx === activeIdx ? " is-active" : "";
  if (it.type === "session") {
    const m = srcMeta(it.source);
    const meta = [m.label, fmtDate(it.updated_at || it.created_at)].filter(Boolean).join(" · ");
    return `<button class="cmdk-row${sel}" role="option" data-idx="${idx}">
      <span class="cmdk-row-ic">${m.icon}</span>
      <span class="cmdk-row-main">
        <span class="cmdk-row-title">${esc(it.title || "Untitled")}</span>
        <span class="cmdk-row-sub">${esc(meta)}</span>
      </span>
      <span class="cmdk-row-kind">${esc(it.kindLabel || "Conversation")}</span>
    </button>`;
  }
  return `<button class="cmdk-row${sel}" role="option" data-idx="${idx}">
    <span class="cmdk-row-ic">${icon(it.icon, { size: 16 })}</span>
    <span class="cmdk-row-main">
      <span class="cmdk-row-title">${esc(it.label)}</span>
      ${it.hint ? `<span class="cmdk-row-sub">${esc(it.hint)}</span>` : ""}
    </span>
    <span class="cmdk-row-kind">${esc(it.kind || "")}</span>
  </button>`;
}

function paint() {
  if (!items.length) {
    el.list.innerHTML = `<div class="cmdk-empty">${icon("search", { size: 26 })}<span>No matches</span></div>`;
    return;
  }
  // group headers by `group`
  let html = "";
  let lastGroup = null;
  items.forEach((it, idx) => {
    if (it.group !== lastGroup) {
      html += `<div class="cmdk-group">${esc(it.group)}</div>`;
      lastGroup = it.group;
    }
    html += rowHTML(it, idx);
  });
  el.list.innerHTML = html;
  $$(".cmdk-row", el.list).forEach((row) => {
    row.addEventListener("click", () => choose(Number(row.dataset.idx)));
    row.addEventListener("mousemove", () => {
      const i = Number(row.dataset.idx);
      if (i !== activeIdx) { activeIdx = i; markActive(); }
    });
  });
  markActive();
}

function markActive() {
  $$(".cmdk-row", el.list).forEach((r, i) =>
    r.classList.toggle("is-active", i === activeIdx));
  const act = $$(".cmdk-row", el.list)[activeIdx];
  if (act) act.scrollIntoView({ block: "nearest" });
}

function buildStatic(q) {
  const cmds = COMMANDS.filter((c) => matches(q, c.label + " " + (c.hint || "") + " " + c.kind));
  return cmds.map((c) => ({ ...c, type: "command", group: c.kind }));
}

async function render(q) {
  // static commands first so the palette feels instant
  items = buildStatic(q);
  activeIdx = 0;
  paint();

  const token = ++lastQueryToken;
  // only hit the index when the user has typed something worth searching
  if (q.trim().length >= 1) {
    try {
      const params = new URLSearchParams({ q, mode: "hybrid", sort: "recent", limit: "6" });
      const data = await api("/api/search?" + params);
      if (token !== lastQueryToken) return; // a newer query superseded this one
      const sessions = (data.results || []).map((r) => ({
        type: "session", group: "Conversations",
        id: r.id, title: r.title, source: r.source,
        updated_at: r.updated_at, created_at: r.created_at,
      }));
      // conversations on top when the user is clearly searching
      items = [...sessions, ...buildStatic(q)];
      activeIdx = 0;
      paint();
    } catch (_) { /* keep static results on error */ }
  }
}

function choose(idx) {
  const it = items[idx];
  if (!it) return;
  closePalette();
  if (it.type === "session") openSession(it.id);
  else if (typeof it.run === "function") it.run();
}

function onKey(e) {
  if (!isPaletteOpen()) return;
  if (e.key === "ArrowDown") { e.preventDefault(); activeIdx = Math.min(activeIdx + 1, items.length - 1); markActive(); }
  else if (e.key === "ArrowUp") { e.preventDefault(); activeIdx = Math.max(activeIdx - 1, 0); markActive(); }
  else if (e.key === "Enter") { e.preventDefault(); choose(activeIdx); }
  else if (e.key === "Escape") { e.preventDefault(); closePalette(); }
}

export function setupPalette() {
  refs();
  if (!el.root) return;
  el.backdrop.addEventListener("click", closePalette);
  el.input.addEventListener("input", debounce((e) => render(e.target.value), 130));
  el.input.addEventListener("keydown", onKey);
}
