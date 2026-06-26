"use strict";

// Collections: the grid, a single collection (overview + members + scoped Ask),
// the create/edit dialog, the add-to-collection menu, and saved-search rules.

import { api } from "../api.js";
import { showOnly, state } from "../state.js";
import {
  $, $$, esc, fmtCost, fmtDate, fmtDuration, srcMeta, toast, withTransition,
} from "../utils.js";
import { cardHTML } from "./list.js";
import { openSession, teardownReading } from "./detail.js";
import { streamAsk } from "./ask.js";

let collDialogState = { mode: "create", id: null, rule: null, pendingSession: null };

const collIconOf = (c) => (c && c.icon) || "\u25A6";

export function ruleIsEmpty(r) {
  if (!r) return true;
  return !(r.q || r.repo || r.source || (r.tags && r.tags.length) || r.date_from || r.date_to);
}

export function currentRule() {
  const rule = {};
  if (state.q) rule.q = state.q;
  if (state.mode && state.mode !== "hybrid") rule.mode = state.mode;
  if (state.source) rule.source = state.source;
  if (state.repo) rule.repo = state.repo;
  if (state.tags.size) rule.tags = [...state.tags];
  if (state.dateFrom) rule.date_from = state.dateFrom;
  if (state.dateTo) rule.date_to = state.dateTo;
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
  return parts.map((p) => `<span class="crs-chip">${p}</span>`).join("");
}

export function toggleSaveCollectionBtn() {
  const btn = $("#saveCollectionBtn");
  if (btn) btn.hidden = ruleIsEmpty(currentRule());
}

export async function showCollections(opts = {}) {
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

export async function openCollection(id, opts = {}) {
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
export function openCollectionDialog({ mode = "create", coll = null, rule = null, pendingSession = null } = {}) {
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

export async function saveCollection() {
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

export function saveCollectionFromFilters() {
  const rule = currentRule();
  if (ruleIsEmpty(rule)) return toast("Add a search or filter first", true);
  const name = rule.q || rule.repo || (rule.tags && rule.tags[0]) ||
    (rule.source ? srcMeta(rule.source).label : "") || "New collection";
  openCollectionDialog({ mode: "create", coll: { name }, rule });
}

// ----- add-to-collection menu (from a session) -----
export async function openCollMenu(anchor, sessionId) {
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

export function hideCollMenu() {
  const menu = $("#collMenu");
  if (menu) menu.hidden = true;
}
