"use strict";

// Usage & spend dashboard: totals plus simple bar/column charts.

import { api } from "../api.js";
import { showOnly, state } from "../state.js";
import { $, esc, fmtCost, fmtTokens, srcMeta, withTransition } from "../utils.js";
import { teardownReading } from "./detail.js";

export async function showUsage(opts = {}) {
  const leaving = state.view !== "usage";
  state.view = "usage";
  state.currentId = null;
  teardownReading();
  const apply = () => showOnly("#usageView");
  if (leaving) withTransition(apply);
  else apply();
  if (!opts.fromHash) location.hash = "#/usage";
  loadUsage();
}

export async function loadUsage() {
  const host = $("#usageBody");
  host.innerHTML = `<div class="lib-loading muted">Loading…</div>`;
  try {
    const params = $("#usageAuto").checked ? "?include_automation=true" : "";
    const data = await api("/api/usage" + params);
    host.innerHTML = renderUsage(data);
  } catch (e) {
    host.innerHTML = `<div class="empty"><div class="big">⚠️</div>${esc(e.message)}</div>`;
  }
}

function renderUsage(d) {
  const t = d.totals || {};
  const cards = [
    ["Total spend", fmtCost(t.cost || 0)],
    ["Premium requests", (t.premium || 0).toLocaleString()],
    ["AIU", (t.aiu || 0).toLocaleString(undefined, { maximumFractionDigits: 0 })],
    ["Tokens", `${fmtTokens(t.input_tokens)} in · ${fmtTokens(t.output_tokens)} out`],
    ["Sessions", (t.sessions || 0).toLocaleString()],
  ];
  const stats = cards.map(([k, v]) =>
    `<div class="usage-stat"><div class="us-val">${v}</div><div class="us-key">${k}</div></div>`
  ).join("");
  return `
    <div class="usage-stats">${stats}</div>
    ${usageColChart(d.by_day || [])}
    <div class="usage-grid">
      ${usageBars("By model", d.by_model || [], "model")}
      ${usageBars("By repository", d.by_repo || [], "repository")}
      ${usageBars("By source", d.by_source || [], "source")}
    </div>`;
}

function usageColChart(byDay) {
  if (!byDay.length) return "";
  const max = Math.max(...byDay.map((d) => d.cost)) || 1;
  const bars = byDay.map((d) => {
    const h = Math.max(2, Math.round((d.cost / max) * 100));
    return `<div class="uc-col" title="${d.day} · ${fmtCost(d.cost)} · ${d.sessions} session${d.sessions === 1 ? "" : "s"}"><div class="uc-bar" style="height:${h}%"></div></div>`;
  }).join("");
  return `<div class="usage-card">
    <h4>Spend over time</h4>
    <div class="uc-chart">${bars}</div>
    <div class="uc-axis"><span>${esc(byDay[0].day)}</span><span>${esc(byDay[byDay.length - 1].day)}</span></div>
  </div>`;
}

function usageBars(title, rows, key) {
  if (!rows.length) return "";
  const max = Math.max(...rows.map((r) => r.cost)) || 1;
  const items = rows.map((r) => {
    const w = Math.max(2, Math.round((r.cost / max) * 100));
    const label = key === "source"
      ? `${srcMeta(r.source).icon} ${esc(srcMeta(r.source).label)}`
      : esc(String(r[key]));
    return `<div class="bar-row">
      <span class="bar-label" title="${esc(String(r[key]))}">${label}</span>
      <div class="bar-track"><div class="bar-fill" style="width:${w}%"></div></div>
      <span class="bar-val">${fmtCost(r.cost)} <em>${r.sessions}</em></span>
    </div>`;
  }).join("");
  return `<div class="usage-card"><h4>${esc(title)}</h4><div class="bars">${items}</div></div>`;
}
