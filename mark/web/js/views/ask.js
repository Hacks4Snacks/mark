"use strict";

// "Ask your history" view + the shared SSE streaming helper used by both the
// global Ask view and collection-scoped Ask.

import { api } from "../api.js";
import { showOnly, setLayoutWide, state } from "../state.js";
import { $, $$, esc, srcMeta, toast, withTransition } from "../utils.js";
import { icon } from "../icons.js";
import { openSession, teardownReading } from "./detail.js";

let askBusy = false;

const ASK_EXAMPLES = [
  "What did I work on this past week?",
  "Summarize my recent debugging sessions",
  "Which problems took me the longest to solve?",
  "Find conversations about authentication",
];

export async function showAsk(opts = {}) {
  // Feature-flagged off: never render the Ask view. Navigation paths (button,
  // palette, router) are also gated, so this is a defensive backstop.
  if (!state.askEnabled) return;
  const leaving = state.view !== "ask";
  state.view = "ask";
  state.currentId = null;
  teardownReading();
  const apply = () => { setLayoutWide(true); showOnly("#askView"); };
  if (leaving) withTransition(apply);
  else apply();
  if (!opts.fromHash) location.hash = "#/ask";
  checkAskStatus();
  renderAskExamples();
  setTimeout(() => $("#askInput")?.focus(), 60);
}

// Clickable starter prompts — shown until a question has been answered.
function renderAskExamples() {
  const host = $("#askExamples");
  if (!host) return;
  if ($("#askAnswer") && !$("#askAnswer").hidden) { host.hidden = true; return; }
  host.innerHTML =
    `<div class="ask-ex-label">${icon("sparkles", { size: 13 })} Try asking</div>` +
    `<div class="ask-ex-chips">${
      ASK_EXAMPLES.map((q) => `<button class="ask-ex" type="button">${esc(q)}</button>`).join("")
    }</div>`;
  host.hidden = false;
  $$(".ask-ex", host).forEach((b) =>
    b.addEventListener("click", () => {
      const input = $("#askInput");
      input.value = b.textContent;
      submitAsk();
    })
  );
}

async function checkAskStatus() {
  const note = $("#askStatus");
  try {
    const st = await api("/api/ask/status");
    if (!st.available) {
      note.hidden = false;
      note.innerHTML = `No local LLM detected. Conversation search and duration analysis still work. For narrative questions, install <a href="https://ollama.com" target="_blank" rel="noopener">Ollama</a>, run <code>ollama pull llama3.2</code>, and keep <code>ollama serve</code> running.`;
      $("#askModel").textContent = "analytics only";
      $("#askSend").disabled = false;
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
  const ex = $("#askExamples");
  if (ex) ex.hidden = true;
  askBusy = true;
  await streamAsk(
    "/api/ask",
    { question: q },
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
  let explicitCitations = null;
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
        if (ev.type === "sources") renderAskSourcesInto(sourcesEl, ev.sources, ev.retrieval);
        else if (ev.type === "token") { raw += ev.text; answerEl.textContent = raw; answerEl.scrollTop = answerEl.scrollHeight; }
        else if (ev.type === "citations") explicitCitations = ev.citations || [];
        else if (ev.type === "error") { toast(ev.error, true); raw += "\n\n" + ev.error; answerEl.textContent = raw; }
      }
    }
    if (raw.trim()) {
      let rendered = false;
      try {
        const r = await api("/api/render", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: raw }),
        });
        const safe = document.createElement("template");
        safe.innerHTML = r.html;
        safe.content.querySelectorAll("img, picture, video, audio, source, iframe, object, embed").forEach((node) => node.remove());
        answerEl.replaceChildren(safe.content.cloneNode(true));
        rendered = true;
      } catch (_) { /* keep plain text */ }
      markCitedSources(sourcesEl, answerEl, raw, rendered, explicitCitations);
    }
  } catch (e) {
    toast(e.message, true);
    answerEl.textContent = e.message;
  } finally {
    answerEl.classList.remove("streaming");
    if (sendBtn) sendBtn.disabled = false;
  }
}

function retrievalPlanHtml(retrieval) {
  if (!retrieval) return "";
  const labels = {
    lookup: "Evidence lookup",
    find: "Conversation search",
    summary: "Archive summary",
    duration: "Duration analysis",
  };
  const items = [["Mode", labels[retrieval.intent] || retrieval.intent]];
  if (retrieval.query) items.push(["Query", retrieval.query]);
  if (retrieval.repository) items.push(["Repository", retrieval.repository]);
  if (retrieval.date_from || retrieval.date_to) {
    const from = retrieval.date_from || "earliest";
    const to = retrieval.date_to || "today";
    items.push(["Dates", from === to ? from : `${from} to ${to}`]);
  }
  if (retrieval.recency && retrieval.recency !== "none") {
    items.push(["Ordering", retrieval.recency === "latest" ? "Latest first" : "Recent boost"]);
  }
  return `<div class="ask-plan" aria-label="Interpreted request">${items.map(([label, value]) =>
    `<span class="ask-plan-item"><b>${esc(label)}</b>${esc(value || "")}</span>`
  ).join("")}</div>`;
}

function evidenceLabel(passage) {
  if (passage.source_type === "session_summary") return "Session summary";
  if (passage.turn_index == null) return "Document";
  const turns = passage.context_turns || [];
  if (turns.length > 1) {
    const display = turns.map((turn) => turn + 1);
    const contiguous = display.every((turn, index) => index === 0 || turn === display[index - 1] + 1);
    return contiguous
      ? `Turns ${display[0]}–${display[display.length - 1]}`
      : `Turns ${display.join(", ")}`;
  }
  return `Turn ${passage.turn_index + 1}`;
}

function durationLabel(seconds) {
  if (!Number.isFinite(seconds)) return "";
  const total = Math.max(0, Math.round(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const parts = [];
  if (hours) parts.push(`${hours}h`);
  if (minutes) parts.push(`${minutes}m`);
  if (secs || !parts.length) parts.push(`${secs}s`);
  return parts.join(" ");
}

function sourceMetrics(source) {
  const parts = [];
  if (Number.isFinite(source.duration_seconds)) parts.push(durationLabel(source.duration_seconds));
  if (Number.isFinite(source.turn_count)) parts.push(`${source.turn_count} turn${source.turn_count === 1 ? "" : "s"}`);
  return parts.length ? ` · ${parts.join(" · ")}` : "";
}

function indentationColumns(line) {
  let columns = 0;
  for (const char of line) {
    if (char === " ") columns += 1;
    else if (char === "\t") columns += 4 - (columns % 4);
    else break;
  }
  return columns;
}

function stripInlineCode(line) {
  let output = "";
  let index = 0;
  while (index < line.length) {
    if (line[index] !== "`") {
      output += line[index++];
      continue;
    }
    let runEnd = index;
    while (line[runEnd] === "`") runEnd += 1;
    const delimiter = "`".repeat(runEnd - index);
    let searchFrom = runEnd;
    let closing = -1;
    while (searchFrom < line.length) {
      const found = line.indexOf(delimiter, searchFrom);
      if (found < 0) break;
      if (line[found - 1] !== "`" && line[found + delimiter.length] !== "`") {
        closing = found;
        break;
      }
      searchFrom = found + 1;
    }
    if (closing < 0) {
      output += line.slice(index);
      break;
    }
    output += " ";
    index = closing + delimiter.length;
  }
  return output;
}

function markdownWithoutCode(text) {
  const prose = [];
  let fence = null;
  for (const line of text.split("\n")) {
    if (fence) {
      const closing = line.match(/^ {0,3}(`+|~+)[ \t]*$/);
      if (closing && closing[1][0] === fence[0] && closing[1].length >= fence.length) fence = null;
      continue;
    }
    const opening = line.match(/^ {0,3}(`{3,}|~{3,})/);
    if (opening) {
      fence = opening[1];
      continue;
    }
    if (indentationColumns(line) >= 4) continue;
    prose.push(stripInlineCode(line));
  }
  return prose.join("\n");
}

function citationNumbers(text, maximum) {
  const cited = new Set();
  for (const match of text.matchAll(/\[([0-9][0-9,\s\-–—]*)\]/g)) {
    for (const part of match[1].split(",")) {
      const value = part.trim().replace(/[–—]/g, "-");
      if (/^\d+$/.test(value)) {
        const number = Number(value);
        if (Number.isSafeInteger(number) && number >= 1 && number <= maximum) cited.add(String(number));
        continue;
      }
      const range = value.match(/^(\d+)\s*-\s*(\d+)$/);
      if (!range) continue;
      const start = Number(range[1]);
      const end = Number(range[2]);
      if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end)) continue;
      if (start < 1 || end > maximum || end < start || end - start > 100) continue;
      for (let number = start; number <= end; number += 1) cited.add(String(number));
    }
  }
  return cited;
}

function markCitedSources(box, answerEl, raw, rendered, explicitCitations) {
  if (!box) return;
  const maximum = $$(".ask-src", box).length;
  let text;
  if (rendered) {
    const prose = answerEl.cloneNode(true);
    prose.querySelectorAll("code, pre").forEach((node) => node.replaceWith(" "));
    text = prose.textContent || "";
  } else {
    text = markdownWithoutCode(raw);
  }
  const cited = explicitCitations == null
    ? citationNumbers(text, maximum)
    : new Set(explicitCitations.filter((number) => Number.isSafeInteger(number) && number >= 1 && number <= maximum).map(String));
  $$(".ask-src", box).forEach((source) => {
    const used = cited.has(source.dataset.sourceN);
    source.classList.toggle("cited", used);
    if (used) source.title = "Cited in the generated answer";
  });
}

export function renderAskSourcesInto(box, sources, retrieval = null) {
  if (!box) return;
  if ((!sources || !sources.length) && !retrieval) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = `${retrievalPlanHtml(retrieval)}<h4>Context considered</h4><div class="ask-src-list">${
    (sources || []).map((s) => `<details class="ask-src" data-source-n="${s.n}">
      <summary><b>[${s.n}]</b> <span class="ask-src-icon">${srcMeta(s.source).icon}</span> <span class="ask-src-title">${esc(s.title || "Untitled")}</span></summary>
      <div class="ask-src-evidence">
        ${(s.passages || []).map((p) => `<div class="ask-src-passage">
          <span class="muted">${evidenceLabel(p)}${p.timestamp ? ` · ${esc(String(p.timestamp).slice(0, 10))}` : ""}${sourceMetrics(s)}</span>
          <p>${esc(p.prompt_excerpt || p.excerpt || "")}</p>
        </div>`).join("")}
        <button type="button" class="btn btn-ghost ask-src-open" data-id="${esc(s.id)}">Open conversation</button>
      </div>
    </details>`).join("")
  }</div>`;
  $$(".ask-src-open", box).forEach((button) =>
    button.addEventListener("click", () => openSession(button.dataset.id))
  );
}
