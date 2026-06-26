"use strict";

// Shared CSS/SVG charts used by the Usage and Collections dashboards.

import { esc, fmtCost, srcMeta } from "./utils.js";

// Geometry of the large trend chart's viewBox, shared with the Usage view's
// hover handler so it can map a cursor position back onto a data point.
export const PLOT = { W: 1000, H: 260, PL: 54, PR: 16, PT: 18, PB: 30 };

// Round a max value up to a "nice" axis bound (1/2/2.5/5/10 × 10ⁿ).
function niceMax(x) {
  if (!(x > 0)) return 1;
  const pow = Math.pow(10, Math.floor(Math.log10(x)));
  const n = x / pow;
  const step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10;
  return step * pow;
}

// Large area/line trend chart with y-axis gridlines and x-axis ticks. The
// caller supplies already-bucketed { values, labels } plus an axis formatter;
// hover (cursor + tooltip) is wired by the caller against `data-max`.
export function areaChart({ values, labels, fmt = String, color = "var(--accent)", id = "uChart" }) {
  const { W, H, PL, PR, PT, PB } = PLOT;
  const n = values.length;
  if (!n) return "";
  const plotW = W - PL - PR;
  const plotH = H - PT - PB;
  const max = niceMax(Math.max(...values, 0));
  const x = (i) => (n === 1 ? PL + plotW / 2 : PL + (i / (n - 1)) * plotW);
  const y = (v) => PT + (1 - v / max) * plotH;

  const ticks = [0, 0.25, 0.5, 0.75, 1];
  const grid = ticks.map((t) => {
    const gy = y(max * t);
    return (
      `<line x1="${PL}" y1="${gy.toFixed(1)}" x2="${W - PR}" y2="${gy.toFixed(1)}" class="uc-grid"/>` +
      `<text x="${PL - 8}" y="${(gy + 3.5).toFixed(1)}" class="uc-ylab" text-anchor="end">${esc(fmt(max * t))}</text>`
    );
  }).join("");

  const pts = values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`);
  const line = `M${pts.join(" L")}`;
  const base = y(0).toFixed(1);
  const area = `M${x(0).toFixed(1)},${base} L${pts.join(" L")} L${x(n - 1).toFixed(1)},${base} Z`;

  const want = Math.min(6, n);
  const xlabs = [];
  for (let k = 0; k < want; k++) {
    const i = want === 1 ? 0 : Math.round((k / (want - 1)) * (n - 1));
    const anchor = k === 0 ? "start" : k === want - 1 ? "end" : "middle";
    xlabs.push(`<text x="${x(i).toFixed(1)}" y="${H - 9}" class="uc-xlab" text-anchor="${anchor}">${esc(labels[i] ?? "")}</text>`);
  }

  return `<svg id="${id}" class="uc-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" role="img" data-max="${max}">
    <defs><linearGradient id="${id}Fill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="${color}" stop-opacity=".34"/>
      <stop offset="100%" stop-color="${color}" stop-opacity="0"/>
    </linearGradient></defs>
    ${grid}
    <path d="${area}" fill="url(#${id}Fill)" class="uc-area"/>
    <path d="${line}" fill="none" stroke="${color}" class="uc-line"/>
    <g class="uc-cursor" opacity="0">
      <line x1="0" y1="${PT}" x2="0" y2="${H - PB}" class="uc-guide"/>
      <circle cx="0" cy="0" r="4" class="uc-pt" fill="${color}"/>
    </g>
    ${xlabs.join("")}
  </svg>`;
}

// Tiny inline sparkline for the stat tiles (no axes, stretches to its box).
export function sparkline(values, { color = "var(--accent)" } = {}) {
  const n = values.length;
  if (n < 2) return "";
  const max = Math.max(...values, 0);
  const min = Math.min(...values, 0);
  const span = max - min || 1;
  const W = 100, H = 28, P = 3;
  const x = (i) => P + (i / (n - 1)) * (W - 2 * P);
  const y = (v) => P + (1 - (v - min) / span) * (H - 2 * P);
  const pts = values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`);
  const line = `M${pts.join(" L")}`;
  const area = `M${x(0).toFixed(1)},${H - P} L${pts.join(" L")} L${x(n - 1).toFixed(1)},${H - P} Z`;
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">
    <path d="${area}" fill="${color}" fill-opacity=".14"/>
    <path d="${line}" fill="none" stroke="${color}" stroke-width="2" vector-effect="non-scaling-stroke"/>
  </svg>`;
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

// Horizontal ranked spend bars. opts:
//   colorFor(row) -> css color for the fill (else the accent gradient)
//   drill          -> 'repo' | 'source': make rows click/keyboard navigable
//   total          -> grand total for the share-of-spend percentage
export function spendBarChart(title, rows, key, opts = {}) {
  if (!rows.length) return "";
  const { colorFor, drill, total } = opts;
  const max = Math.max(...rows.map((r) => r.cost)) || 1;
  const grand = total || rows.reduce((s, r) => s + r.cost, 0) || 1;
  const items = rows.map((r) => {
    const w = Math.max(2, Math.round((r.cost / max) * 100));
    const pct = Math.round((r.cost / grand) * 100);
    const raw = String(r[key]);
    const label = key === "source"
      ? `${srcMeta(r.source).icon} ${esc(srcMeta(r.source).label)}`
      : esc(raw);
    const fill = colorFor ? colorFor(r) : "";
    const fillStyle = ` style="--w:${w}%${fill ? `;background:${fill}` : ""}"`;
    const canDrill = drill && raw !== "(none)" && raw !== "(unknown)";
    const drillAttr = canDrill
      ? ` data-drill="${drill}" data-val="${esc(raw)}" tabindex="0" role="button"`
      : "";
    const tip = `${raw} · ${fmtCost(r.cost)} · ${r.sessions} session${r.sessions === 1 ? "" : "s"}`;
    return `<div class="bar-row${canDrill ? " bar-drill" : ""}"${drillAttr} title="${esc(tip)}">
      <span class="bar-label">${label}</span>
      <div class="bar-track"><div class="bar-fill"${fillStyle}></div></div>
      <span class="bar-val">${fmtCost(r.cost)} <em>${pct}%</em></span>
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
      <div class="bar-track"><div class="bar-fill" style="--w:${w}%"></div></div>
      <span class="bar-val">${r.sessions}</span>
    </div>`;
  }).join("");
  return `<div class="usage-card"><h4>${esc(title)}</h4><div class="bars">${items}</div></div>`;
}
