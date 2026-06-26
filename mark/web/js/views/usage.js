"use strict";

// Usage & spend dashboard: headline tiles with trend sparklines, an interactive
// area chart (metric + granularity toggles and a hover read-out), auto-generated
// insight highlights, and ranked breakdown bars that drill into the session list.

import { api } from "../api.js";
import { showOnly, setLayoutDash, state } from "../state.js";
import { $, $$, esc, fmtCost, fmtTokens, fmtDuration, withTransition, srcMeta } from "../utils.js";
import { icon } from "../icons.js";
import { areaChart, sparkline, spendBarChart, PLOT } from "../charts.js";
import { teardownReading } from "./detail.js";
import { doSearch } from "./list.js";
import { syncFilterUI } from "../sidebar.js";

const HINTS = {
  cost: "Estimated from public model list prices and summed across every source. Sources that don't report token usage are approximated from text length, and local models count as free — treat this as a ballpark, not a bill.",
  sessions: "Conversations included in these totals, counted across all sources.",
  premium: "Premium (paid-model) requests counted against a plan's monthly allowance. Only sources that meter them contribute — see the badge for which; the rest report none.",
  aiu: "AI Units — GitHub Copilot's metering unit for premium models (each request counts as its multiplier, e.g. 1×/3×). Only Copilot reports AIU, so this total reflects Copilot usage alone.",
  tokens: "Input and output tokens summed across all sources that report usage; estimated where exact counts aren't available.",
  time: "Total session wall-clock time. Some sources don't record duration, so those sessions aren't counted here — see the badge.",
};

const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const WD_FULL = ["Sundays", "Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays"];

const METRICS = {
  cost: { label: "Spend", color: "var(--accent)", fmt: fmtCost, axis: shortCost },
  sessions: { label: "Sessions", color: "#59c2ff", fmt: (v) => v.toLocaleString(), axis: shortNum },
  premium: { label: "Premium", color: "#56d6a0", fmt: (v) => v.toLocaleString(), axis: shortNum },
};

const SRC_COLOR = {
  cli: "#8b8cff", vscode: "#59c2ff", cline: "#56d6a0", cursor: "#f0a45b",
  chatgpt: "#19c37d", zoocode: "#c98bff", roo: "#ff8bb0", kilocode: "#ffd166",
  copilot: "#9aa4b6", agent: "#7aa2ff", upload: "#8aa0c8",
};
const srcColor = (s) => SRC_COLOR[s] || "var(--accent)";

let _d = null;       // raw /api/usage payload
let _series = [];     // gap-filled daily series of { t, day, cost, sessions, premium }
let _metric = "cost";
let _gran = "day";    // day | week | month
let _buckets = [];    // current bucketed series, for the hover read-out

export async function showUsage(opts = {}) {
  const leaving = state.view !== "usage";
  state.view = "usage";
  state.currentId = null;
  teardownReading();
  const apply = () => { setLayoutDash(true); showOnly("#usageView"); };
  if (leaving) withTransition(apply);
  else apply();
  if (!opts.fromHash) location.hash = "#/usage";
  loadUsage();
}

export async function loadUsage() {
  const host = $("#usageBody");
  host.innerHTML = `<div class="lib-loading muted">Loading…</div>`;
  try {
    _d = await api("/api/usage");
    _series = buildSeries(_d.by_day || []);
    host.innerHTML = renderUsage(_d);
    wireUsage();
    renderTrend();
  } catch (e) {
    host.innerHTML = `<div class="empty"><div class="big">${icon("alert", { size: 40 })}</div>${esc(e.message)}</div>`;
  }
}

// ---- number / date formatting -------------------------------------------

function shortNum(v) {
  if (v >= 1e6) return (v / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  if (v >= 1e3) return (v / 1e3).toFixed(1).replace(/\.0$/, "") + "k";
  return String(Math.round(v));
}
function shortCost(v) {
  if (v >= 1000) return "$" + (v / 1000).toFixed(1).replace(/\.0$/, "") + "k";
  if (v >= 1) return "$" + Math.round(v);
  return "$" + v.toFixed(1).replace(/\.0$/, "");
}
function bigNum(v) {
  v = v || 0;
  if (v >= 1e9) return (v / 1e9).toFixed(1).replace(/\.0$/, "") + "B";
  if (v >= 1e6) return Math.round(v / 1e6) + "M";
  if (v >= 1e3) return Math.round(v / 1e3) + "k";
  return String(Math.round(v));
}
const dayName = (t) => `${MON[new Date(t).getUTCMonth()]} ${new Date(t).getUTCDate()}`;
const monthName = (t) => `${MON[new Date(t).getUTCMonth()]} ’${String(new Date(t).getUTCFullYear()).slice(2)}`;
const fullDay = (t) => `${MON[new Date(t).getUTCMonth()]} ${new Date(t).getUTCDate()}, ${new Date(t).getUTCFullYear()}`;
const parseDay = (s) => { const [y, m, d] = s.split("-").map(Number); return Date.UTC(y, m - 1, d); };

// ---- series helpers ------------------------------------------------------

function buildSeries(byDay) {
  if (!byDay.length) return [];
  const key = (t) => new Date(t).toISOString().slice(0, 10);
  const map = new Map(byDay.map((r) => [r.day, r]));
  const start = parseDay(byDay[0].day);
  const end = parseDay(byDay[byDay.length - 1].day);
  const out = [];
  for (let t = start; t <= end; t += 86400000) {
    const r = map.get(key(t));
    out.push({ t, day: key(t), cost: r ? r.cost : 0, sessions: r ? r.sessions : 0, premium: r ? r.premium : 0 });
  }
  return out;
}

function bucketSeries(series, gran) {
  if (gran === "day") {
    return series.map((r) => ({ ...r, label: dayName(r.t), tip: fullDay(r.t) }));
  }
  const groups = new Map();
  for (const r of series) {
    const dt = new Date(r.t);
    let t0;
    if (gran === "week") {
      const dow = (dt.getUTCDay() + 6) % 7; // Monday = 0
      t0 = r.t - dow * 86400000;
    } else {
      t0 = Date.UTC(dt.getUTCFullYear(), dt.getUTCMonth(), 1);
    }
    let g = groups.get(t0);
    if (!g) { g = { t: t0, cost: 0, sessions: 0, premium: 0 }; groups.set(t0, g); }
    g.cost += r.cost; g.sessions += r.sessions; g.premium += r.premium;
  }
  return [...groups.values()].sort((a, b) => a.t - b.t).map((g) => ({
    ...g,
    label: gran === "month" ? monthName(g.t) : dayName(g.t),
    tip: gran === "month" ? `${MON[new Date(g.t).getUTCMonth()]} ${new Date(g.t).getUTCFullYear()}` : `Week of ${fullDay(g.t)}`,
  }));
}

const sum = (a) => a.reduce((s, v) => s + v, 0);
const lastN = (a, n) => a.slice(Math.max(0, a.length - n));
function maxBy(arr, f) {
  let best = null, bv = -Infinity;
  for (const r of arr) { const v = f(r); if (v > bv) { bv = v; best = r; } }
  return best;
}

// Percent change of the last window vs. the window before it (null if N/A).
function periodDelta(vals) {
  if (vals.length < 14) return null;
  const win = Math.min(30, Math.floor(vals.length / 2));
  const recent = sum(vals.slice(-win));
  const prev = sum(vals.slice(-2 * win, -win));
  if (prev <= 0) return null;
  return Math.round(((recent - prev) / prev) * 100);
}

// ---- top render ----------------------------------------------------------

function renderUsage(d) {
  const t = d.totals || {};
  if (!_series.length && !t.sessions) {
    return `<div class="empty"><div class="big">${icon("pie", { size: 40 })}</div>No usage yet — re-scan your history to populate the dashboard.</div>`;
  }
  return `
    ${tiles(t)}
    <p class="usage-caption">Spend and token figures are best-effort estimates — not every source reports usage, so totals can undercount. Hover ${icon("info", { size: 12 })} for details.</p>
    ${insights(d)}
    <div class="usage-card uc-trend">
      <div class="uc-head">
        <h4 id="uTrendTitle">${METRICS[_metric].label} over time</h4>
        <div class="uc-controls">
          <div class="segmented" id="uMetric" role="tablist" aria-label="Metric">
            ${Object.entries(METRICS).map(([k, m]) => `<button type="button" data-metric="${k}" class="${k === _metric ? "active" : ""}">${esc(m.label)}</button>`).join("")}
          </div>
          <div class="segmented" id="uGran" role="tablist" aria-label="Granularity">
            ${["day", "week", "month"].map((g) => `<button type="button" data-gran="${g}" class="${g === _gran ? "active" : ""}">${g[0].toUpperCase() + g.slice(1)}</button>`).join("")}
          </div>
        </div>
      </div>
      <div id="uChartWrap" class="uc-wrap"></div>
    </div>
    <div class="usage-grid">
      ${spendBarChart("By model", d.by_model || [], "model", { total: t.cost })}
      ${spendBarChart("By repository", d.by_repo || [], "repository", { drill: "repo", total: t.cost })}
      ${spendBarChart("By source", d.by_source || [], "source", { drill: "source", colorFor: (r) => srcColor(r.source), total: t.cost })}
    </div>`;
}

function infoSpan(hint) {
  return hint
    ? `<span class="us-info" tabindex="0" role="img" aria-label="${esc(hint)}" title="${esc(hint)}">${icon("info", { size: 12 })}</span>`
    : "";
}

// Badge showing which sources actually contribute a source-specific metric
// (premium / aiu / duration). Empty when every source reports it, or none do.
function scopeChip(key) {
  const all = _d?.by_source || [];
  if (!all.length) return "";
  const withIt = all.filter((s) => (s[key] || 0) > 0);
  if (!withIt.length || withIt.length === all.length) return "";
  const names = withIt.map((s) => srcMeta(s.source).label);
  let body;
  if (withIt.length <= 2) {
    const ics = withIt.map((s) => srcMeta(s.source).icon).join("");
    const txt = withIt.length === 1 ? `${names[0]} only` : names.join(" \u00b7 ");
    body = `${ics}<span class="us-scope-t">${esc(txt)}</span>`;
  } else {
    body = `<span class="us-scope-t">${withIt.length} of ${all.length} sources</span>`;
  }
  return `<div class="us-scope" title="Reported by ${esc(names.join(", "))}">${body}</div>`;
}

function tiles(t) {
  const live = [
    ["dollar", "Total spend", fmtCost(t.cost || 0), "cost", HINTS.cost, ""],
    ["message", "Sessions", (t.sessions || 0).toLocaleString(), "sessions", HINTS.sessions, ""],
    ["star", "Premium requests", (t.premium || 0).toLocaleString(), "premium", HINTS.premium, "premium"],
  ].map(([ic, key, val, metric, hint, scope]) => trendTile(ic, key, val, metric, hint, scope));
  const stat = [
    ["gauge", "AIU", (t.aiu || 0).toLocaleString(undefined, { maximumFractionDigits: 0 }), HINTS.aiu, "aiu"],
    ["cpu", "Tokens", `${bigNum(t.input_tokens)}<span class="us-sub">in</span> · ${bigNum(t.output_tokens)}<span class="us-sub">out</span>`, HINTS.tokens, ""],
    ["clock", "Time in sessions", fmtDuration(t.duration) || "0s", HINTS.time, "duration"],
  ].map(([ic, key, val, hint, scope]) => staticTile(ic, key, val, hint, scope));
  return `<div class="usage-stats">${live.join("")}${stat.join("")}</div>`;
}

function fmtDelta(dl) {
  const up = dl >= 0;
  const mag = Math.abs(dl);
  const text = mag >= 1000 ? `${Math.round(1 + mag / 100)}×` : `${mag}%`;
  return `<span class="us-delta ${up ? "up" : "down"}" title="vs. the previous 30 days">${icon(up ? "trend-up" : "trend-down", { size: 12 })}${text}</span>`;
}

function trendTile(ic, key, val, metric, hint, scope = "") {
  const vals = _series.map((r) => r[metric]);
  const spark = sparkline(lastN(vals, 45), { color: METRICS[metric].color });
  const dl = periodDelta(vals);
  const delta = dl == null ? "" : fmtDelta(dl);
  return `<div class="usage-stat has-spark">
    <div class="us-top"><span class="us-ic">${icon(ic, { size: 14 })}</span>${delta}</div>
    <div class="us-val">${val}</div>
    <div class="us-key">${esc(key)} ${infoSpan(hint)}</div>
    ${scope ? scopeChip(scope) : ""}
    <div class="us-spark">${spark}</div>
  </div>`;
}

function staticTile(ic, key, val, hint, scope = "") {
  return `<div class="usage-stat">
    <div class="us-top"><span class="us-ic">${icon(ic, { size: 14 })}</span></div>
    <div class="us-val">${val}</div>
    <div class="us-key">${esc(key)} ${infoSpan(hint)}</div>
    ${scope ? scopeChip(scope) : ""}
  </div>`;
}

function insight(ic, label, val, sub) {
  return `<div class="ins-card">
    <span class="ins-ic">${icon(ic, { size: 16 })}</span>
    <div class="ins-body">
      <div class="ins-label">${esc(label)}</div>
      <div class="ins-val" title="${esc(val)}">${esc(val)}</div>
      <div class="ins-sub">${esc(sub)}</div>
    </div>
  </div>`;
}

function insights(d) {
  const t = d.totals || {};
  const out = [];

  const topDay = maxBy(_d.by_day || [], (r) => r.cost);
  if (topDay && topDay.cost > 0) {
    out.push(insight("flame", "Priciest day", fmtCost(topDay.cost), fullDay(parseDay(topDay.day))));
  }
  const topModel = (d.by_model || [])[0];
  if (topModel) {
    out.push(insight("cpu", "Top model", topModel.model, `${fmtCost(topModel.cost)} · ${topModel.sessions} session${topModel.sessions === 1 ? "" : "s"}`));
  }
  const busyRepo = maxBy((d.by_repo || []).filter((r) => r.repository !== "(none)"), (r) => r.sessions);
  if (busyRepo) {
    out.push(insight("folder", "Busiest repo", busyRepo.repository, `${busyRepo.sessions} session${busyRepo.sessions === 1 ? "" : "s"} · ${fmtCost(busyRepo.cost)}`));
  }
  const wd = busiestWeekday(_series);
  if (wd) out.push(insight("calendar", "Most active", wd.name, `${wd.sessions.toLocaleString()} sessions in total`));

  if (t.sessions) {
    const avgTok = Math.round(((t.input_tokens || 0) + (t.output_tokens || 0)) / t.sessions);
    out.push(insight("gauge", "Avg / session", fmtCost(t.cost / t.sessions), `${fmtTokens(avgTok)} tokens each`));
  }
  const burn = recentDailyAvg(_series.map((r) => r.cost));
  if (burn != null) out.push(insight("activity", "Recent daily avg", fmtCost(burn), "spend · last 30 days"));

  return out.length ? `<div class="usage-insights">${out.join("")}</div>` : "";
}

function busiestWeekday(series) {
  if (!series.length) return null;
  const tot = [0, 0, 0, 0, 0, 0, 0];
  for (const r of series) tot[new Date(r.t).getUTCDay()] += r.sessions;
  let bi = 0;
  for (let i = 1; i < 7; i++) if (tot[i] > tot[bi]) bi = i;
  return tot[bi] > 0 ? { name: WD_FULL[bi], sessions: tot[bi] } : null;
}

function recentDailyAvg(costs) {
  if (costs.length < 7) return null;
  const win = Math.min(30, costs.length);
  return sum(lastN(costs, win)) / win;
}

// ---- trend chart + hover -------------------------------------------------

function renderTrend() {
  const wrap = $("#uChartWrap");
  if (!wrap) return;
  _buckets = bucketSeries(_series, _gran);
  if (!_buckets.length) { wrap.innerHTML = `<div class="muted uc-empty">No activity in range.</div>`; return; }
  const m = METRICS[_metric];
  wrap.innerHTML =
    areaChart({ values: _buckets.map((b) => b[_metric]), labels: _buckets.map((b) => b.label), fmt: m.axis, color: m.color }) +
    `<div class="uc-tip" hidden></div>`;
  wireHover(wrap);
}

function wireHover(wrap) {
  const svg = wrap.querySelector(".uc-svg");
  const tip = wrap.querySelector(".uc-tip");
  const cursor = svg && svg.querySelector(".uc-cursor");
  if (!svg || !tip || !cursor) return;
  const guide = cursor.querySelector(".uc-guide");
  const dot = cursor.querySelector(".uc-pt");
  const m = METRICS[_metric];
  const plotW = PLOT.W - PLOT.PL - PLOT.PR;
  const plotH = PLOT.H - PLOT.PT - PLOT.PB;
  const max = parseFloat(svg.dataset.max) || 1;
  const n = _buckets.length;

  const move = (e) => {
    const rect = svg.getBoundingClientRect();
    if (!rect.width) return;
    const frac = Math.max(0, Math.min(1, ((e.clientX - rect.left) / rect.width * PLOT.W - PLOT.PL) / plotW));
    const idx = n === 1 ? 0 : Math.round(frac * (n - 1));
    const b = _buckets[idx];
    if (!b) return;
    const px = n === 1 ? PLOT.PL + plotW / 2 : PLOT.PL + (idx / (n - 1)) * plotW;
    const py = PLOT.PT + (1 - b[_metric] / max) * plotH;
    guide.setAttribute("x1", px); guide.setAttribute("x2", px);
    dot.setAttribute("cx", px); dot.setAttribute("cy", py);
    cursor.setAttribute("opacity", "1");
    tip.hidden = false;
    tip.innerHTML =
      `<div class="uc-tip-d">${esc(b.tip)}</div>` +
      `<div class="uc-tip-v"><span class="uc-tip-dot" style="background:${m.color}"></span>${esc(m.label)} <b>${esc(m.fmt(b[_metric]))}</b></div>`;
    const left = (px / PLOT.W) * rect.width + (rect.left - wrap.getBoundingClientRect().left);
    const top = (py / PLOT.H) * rect.height + (rect.top - wrap.getBoundingClientRect().top);
    tip.style.left = `${left}px`;
    tip.style.top = `${top}px`;
  };
  const leave = () => { cursor.setAttribute("opacity", "0"); tip.hidden = true; };
  svg.addEventListener("pointermove", move);
  svg.addEventListener("pointerleave", leave);
}

// ---- interaction ---------------------------------------------------------

function wireUsage() {
  const body = $("#usageBody");
  if (!body) return;
  body.addEventListener("click", (e) => {
    const mb = e.target.closest("#uMetric [data-metric]");
    if (mb) return setMetric(mb.dataset.metric);
    const gb = e.target.closest("#uGran [data-gran]");
    if (gb) return setGran(gb.dataset.gran);
    const bar = e.target.closest(".bar-drill");
    if (bar) return drillTo(bar.dataset.drill, bar.dataset.val);
  });
  body.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    const bar = e.target.closest(".bar-drill");
    if (bar) { e.preventDefault(); drillTo(bar.dataset.drill, bar.dataset.val); }
  });
}

function setMetric(m) {
  if (!METRICS[m]) return;
  _metric = m;
  $$("#uMetric [data-metric]").forEach((b) => b.classList.toggle("active", b.dataset.metric === m));
  const title = $("#uTrendTitle");
  if (title) title.textContent = `${METRICS[m].label} over time`;
  renderTrend();
}

function setGran(g) {
  _gran = g;
  $$("#uGran [data-gran]").forEach((b) => b.classList.toggle("active", b.dataset.gran === g));
  renderTrend();
}

// Jump to the session list filtered by the clicked repository or source.
function drillTo(kind, val) {
  if (!val) return;
  state.q = "";
  state.tags.clear();
  state.dateFrom = "";
  state.dateTo = "";
  state.showHidden = false;
  if (kind === "repo") { state.repo = val; state.source = null; }
  else if (kind === "source") { state.source = val; state.repo = null; }
  else return;
  const sb = $("#search"); if (sb) sb.value = "";
  const df = $("#dateFrom"); if (df) df.value = "";
  const dt = $("#dateTo"); if (dt) dt.value = "";
  doSearch(true);
  syncFilterUI();
}
