"use strict";

// DOM + formatting helpers shared across every view. Imports only the icon
// set (itself a leaf), so anything may import this without creating a cycle.

import { srcIcon } from "./icons.js";

export const $ = (sel, el = document) => el.querySelector(sel);
export const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

export const esc = (s) =>
  (s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export const SRC_LABEL = {
  vscode: "VS Code",
  cli: "Copilot CLI",
  cline: "Cline",
  zoocode: "Zoo Code",
  roo: "Roo Code",
  kilocode: "Kilo Code",
  cursor: "Cursor",
  chatgpt: "ChatGPT",
  agent: "Agent",
  upload: "Upload",
  copilot: "Copilot",
  copilot_memory: "Copilot memory",
};
// `icon` is an inline SVG string; `label` is the human name.
export const srcMeta = (s) => ({
  icon: srcIcon(s),
  label: SRC_LABEL[s] || s || "session",
});

export function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const days = Math.floor((Date.now() - d) / 86400000);
  if (days === 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 30) return `${days}d ago`;
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

export function fmtRelativeTime(iso, prefix = "Updated") {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const diff = Date.now() - d.getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return `${prefix} just now`;
  if (mins < 60) return `${prefix} ${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${prefix} ${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days === 1) return `${prefix} yesterday`;
  if (days < 30) return `${prefix} ${days}d ago`;
  return `${prefix} ${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })}`;
}

export const debounce = (fn, ms = 220) => {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
};

export function fmtDuration(s) {
  if (!s || s <= 0) return "";
  s = Math.round(s);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60), h = Math.floor(m / 60);
  if (h > 0) return `${h}h ${m % 60}m`;
  return `${m}m`;
}

export function fmtCost(c) {
  if (c == null) return "";
  if (c === 0) return "$0";
  if (c < 0.01) return "<$0.01";
  return "$" + c.toFixed(2);
}

export function fmtTokens(n) {
  if (!n) return "0";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return "" + n;
}

export function fmtBytes(n) {
  if (!n) return "0 B";
  if (n >= 1 << 20) return (n / (1 << 20)).toFixed(1) + " MB";
  if (n >= 1 << 10) return (n / (1 << 10)).toFixed(1) + " KB";
  return n + " B";
}

export const normTitle = (t) => (t || "Untitled").toLowerCase().replace(/\s+/g, " ").trim();

// URLs use human-facing (one-based) turn numbers; API and source data use zero.
export function sessionHash(id, { turnIndex = null, q = "" } = {}) {
  const params = new URLSearchParams();
  if (Number.isSafeInteger(turnIndex) && turnIndex >= 0 && turnIndex < Number.MAX_SAFE_INTEGER) {
    params.set("turn", String(turnIndex + 1));
  }
  if (q.trim()) params.set("q", q.trim().slice(0, 2000));
  return "#/session/" + encodeURIComponent(id) + (params.size ? "?" + params : "");
}

export function parseSessionHash(hash) {
  const match = hash.match(/^#\/session\/([^?]+)(?:\?(.*))?$/);
  if (!match) return null;
  let id;
  try { id = decodeURIComponent(match[1]); }
  catch (_) { return null; }
  const params = new URLSearchParams(match[2] || "");
  const rawTurn = params.get("turn");
  const number = rawTurn == null ? null : Number(rawTurn);
  const valid = rawTurn != null && /^\d+$/.test(rawTurn) && Number.isSafeInteger(number) && number > 0;
  return {
    id, turnIndex: valid ? number - 1 : null,
    q: (params.get("q") || "").trim().slice(0, 2000),
    invalidTarget: rawTurn != null && !valid,
  };
}

export function evidencePattern(query) {
  const escape = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const tokens = (value) => value.match(/[\p{L}\p{N}_]+/gu) || [];
  const quoted = [...query.matchAll(/"([^"]+)"/g)]
    .map((match) => tokens(match[1]).map(escape).join("[^\\p{L}\\p{N}_]+"))
    .filter(Boolean).map((phrase) => phrase + "(?![\\p{L}\\p{N}_])");
  const words = tokens(query.replace(/"[^"]+"/g, " ")).filter((word) => word.length > 1);
  const parts = [...quoted, ...words.map((word) => escape(word) + "[\\p{L}\\p{N}_]*")];
  return parts.length ? new RegExp("(?<![\\p{L}\\p{N}_])(?:" + parts.join("|") + ")", "giu") : null;
}

export function adjacentMatchPosition(page, index, delta) {
  if (!page?.total) return -1;
  return index < 0 ? (page.target_position || 0) + (delta < 0 ? -1 : 0)
    : page.offset + index + delta;
}

// Highlight text nodes, never HTML strings. A phrase may cross emphasis or
// syntax-highlighting spans without damaging the surrounding rendered markup.
export function highlightEvidence(root, query) {
  $$('mark[data-evidence]', root).forEach((mark) => {
    const parent = mark.parentNode;
    mark.replaceWith(document.createTextNode(mark.textContent));
    parent.normalize();
  });
  const pattern = evidencePattern(query);
  if (!pattern) return null;
  let first = null;
  let remaining = 1000; // bound DOM growth even after explicitly loading a huge turn
  const blocks = root.matches(".md") ? [root] : $$(".md", root);
  for (const block of blocks) {
    if (block.closest(".thinking")) continue; // display-only, not indexed
    const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT);
    const nodes = [];
    let text = "";
    while (walker.nextNode()) {
      const node = walker.currentNode;
      nodes.push({ node, start: text.length, end: text.length + node.length });
      text += node.data;
    }
    const matches = [];
    pattern.lastIndex = 0;
    let match;
    while (remaining > 0 && (match = pattern.exec(text))) {
      matches.push([match.index, match.index + match[0].length]);
      remaining -= 1;
    }
    let cursor = 0;
    for (const { node, start, end } of nodes) {
      while (cursor < matches.length && matches[cursor][1] <= start) cursor += 1;
      let index = cursor;
      let consumed = 0;
      const fragment = document.createDocumentFragment();
      while (index < matches.length && matches[index][0] < end) {
        const left = Math.max(start, matches[index][0]) - start;
        const right = Math.min(end, matches[index][1]) - start;
        fragment.append(document.createTextNode(node.data.slice(consumed, left)));
        const mark = document.createElement("mark");
        mark.dataset.evidence = "1";
        mark.textContent = node.data.slice(left, right);
        fragment.append(mark);
        first ||= mark;
        consumed = right;
        index += 1;
      }
      if (consumed) {
        fragment.append(document.createTextNode(node.data.slice(consumed)));
        node.replaceWith(fragment);
      }
    }
  }
  return first;
}

const prefersReducedMotion = () =>
  window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// Run a DOM mutation inside a View Transition when supported (graceful fallback).
export function withTransition(fn) {
  if (document.startViewTransition && document.visibilityState === "visible" && !prefersReducedMotion()) {
    // Switching tabs can skip the animation after the DOM update has begun.
    document.startViewTransition(fn).ready.catch(() => {});
  } else {
    fn();
  }
}

let toastTimer;
export function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast show" + (isError ? " error" : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = "toast"; }, 2600);
}
