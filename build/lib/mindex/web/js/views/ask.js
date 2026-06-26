"use strict";

// "Ask your history" view + the shared SSE streaming helper used by both the
// global Ask view and collection-scoped Ask.

import { api } from "../api.js";
import { showOnly, state } from "../state.js";
import { $, $$, esc, srcMeta, toast, withTransition } from "../utils.js";
import { openSession, teardownReading } from "./detail.js";

let askBusy = false;

export async function showAsk(opts = {}) {
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

export async function submitAsk() {
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
export async function streamAsk(url, body, els) {
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

export function renderAskSourcesInto(box, sources) {
  if (!box) return;
  if (!sources || !sources.length) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = `<h4>Sources</h4><div class="ask-src-list">${
    sources.map((s) => `<a class="ask-src" data-id="${esc(s.id)}" title="${esc(s.title || "")}"><b>[${s.n}]</b> <span class="ask-src-icon">${srcMeta(s.source).icon}</span> <span class="ask-src-title">${esc(s.title || "Untitled")}</span></a>`).join("")
  }</div>`;
  $$(".ask-src", box).forEach((a) => a.addEventListener("click", () => openSession(a.dataset.id)));
}
