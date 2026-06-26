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

const prefersReducedMotion = () =>
  window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// Run a DOM mutation inside a View Transition when supported (graceful fallback).
export function withTransition(fn) {
  if (document.startViewTransition && !prefersReducedMotion()) {
    document.startViewTransition(fn);
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
