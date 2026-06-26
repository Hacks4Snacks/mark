"use strict";

// Shared CSS bar/column charts (used by Usage and Collections dashboards).

import { esc, fmtCost, srcMeta } from "./utils.js";

export function spendColChart(byDay, { title = "Spend over time" } = {}) {
  if (!byDay.length) return "";
  const max = Math.max(...byDay.map((d) => d.cost)) || 1;
  const bars = byDay.map((d) => {
    const h = Math.max(2, Math.round((d.cost / max) * 100));
    return `<div class="uc-col" title="${esc(d.day)} · ${fmtCost(d.cost)} · ${d.sessions} session${d.sessions === 1 ? "" : "s"}"><div class="uc-bar" style="height:${h}%"></div></div>`;
  }).join("");
  return `<div class="usage-card">
    <h4>${esc(title)}</h4>
    <div class="uc-chart">${bars}</div>
    <div class="uc-axis"><span>${esc(byDay[0].day)}</span><span>${esc(byDay[byDay.length - 1].day)}</span></div>
  </div>`;
}

export function activityColChart(byDay, { title = "Activity" } = {}) {
  if (!byDay.length) return "";
  const max = Math.max(...byDay.map((d) => d.sessions)) || 1;
  const bars = byDay.map((d) => {
    const h = Math.max(2, Math.round((d.sessions / max) * 100));
    const tip = `${d.day} · ${d.sessions} session${d.sessions === 1 ? "" : "s"}`;
    return `<div class="uc-col" title="${esc(tip)}"><div class="uc-bar" style="height:${h}%"></div></div>`;
  }).join("");
  return `<div class="usage-card">
    <h4>${esc(title)}</h4>
    <div class="uc-chart">${bars}</div>
    <div class="uc-axis"><span>${esc(byDay[0].day)}</span><span>${esc(byDay[byDay.length - 1].day)}</span></div>
  </div>`;
}

export function spendBarChart(title, rows, key) {
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

export function sessionBarChart(title, rows, key) {
  if (!rows.length) return "";
  const max = Math.max(...rows.map((r) => r.sessions)) || 1;
  const items = rows.map((r) => {
    const w = Math.max(2, Math.round((r.sessions / max) * 100));
    const label = key === "source"
      ? `${srcMeta(r.source).icon} ${esc(srcMeta(r.source).label)}`
      : esc(String(r[key]));
    return `<div class="bar-row">
      <span class="bar-label" title="${esc(String(r[key]))}">${label}</span>
      <div class="bar-track"><div class="bar-fill" style="width:${w}%"></div></div>
      <span class="bar-val">${r.sessions}</span>
    </div>`;
  }).join("");
  return `<div class="usage-card"><h4>${esc(title)}</h4><div class="bars">${items}</div></div>`;
}
