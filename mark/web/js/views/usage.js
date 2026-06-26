"use strict";

// Usage & spend dashboard: totals plus simple bar/column charts.

import { api } from "../api.js";
import { showOnly, setLayoutWide, state } from "../state.js";
import { $, esc, fmtCost, fmtTokens, withTransition } from "../utils.js";
import { icon } from "../icons.js";
import { spendColChart, spendBarChart } from "../charts.js";
import { teardownReading } from "./detail.js";

export async function showUsage(opts = {}) {
  const leaving = state.view !== "usage";
  state.view = "usage";
  state.currentId = null;
  teardownReading();
  const apply = () => { setLayoutWide(false); showOnly("#usageView"); };
  if (leaving) withTransition(apply);
  else apply();
  if (!opts.fromHash) location.hash = "#/usage";
  loadUsage();
}

export async function loadUsage() {
  const host = $("#usageBody");
  host.innerHTML = `<div class="lib-loading muted">Loading...</div>`;
  try {
    const data = await api("/api/usage");
    host.innerHTML = renderUsage(data);
  } catch (e) {
    host.innerHTML = `<div class="empty"><div class="big">${icon("alert", { size: 40 })}</div>${esc(e.message)}</div>`;
  }
}

function renderUsage(d) {
  const t = d.totals || {};
  const cards = [
    ["Total spend", fmtCost(t.cost || 0),
      "Estimated from public model list prices. Sources that don't report token usage are approximated from text length, and local models count as free — treat this as a ballpark, not a bill."],
    ["Premium requests", (t.premium || 0).toLocaleString(),
      "Premium (paid-model) requests counted against a GitHub Copilot plan's monthly allowance. Sources that don't report this are omitted."],
    ["AIU", (t.aiu || 0).toLocaleString(undefined, { maximumFractionDigits: 0 }),
      "AI Units — GitHub Copilot's metering unit for premium models: each request counts as the model's multiplier (e.g. 1× or 3×) in AIU."],
    ["Tokens", `${fmtTokens(t.input_tokens)} in · ${fmtTokens(t.output_tokens)} out`,
      "Input and output tokens summed across sessions that report usage. Estimated counts are included where exact numbers aren't available."],
    ["Sessions", (t.sessions || 0).toLocaleString(),
      "Conversations included in these totals (automation runs are excluded unless you toggle them on)."],
  ];
  const stats = cards.map(([k, v, hint]) =>
    `<div class="usage-stat"><div class="us-val">${v}</div><div class="us-key">${esc(k)}${
      hint ? ` <span class="us-info" tabindex="0" role="img" aria-label="${esc(hint)}" title="${esc(hint)}">${icon("info", { size: 13 })}</span>` : ""
    }</div></div>`
  ).join("");
  return `
    <div class="usage-stats">${stats}</div>
    <p class="usage-caption">Spend and token figures are best-effort estimates — not every source reports usage, so totals can undercount. Hover ${icon("info", { size: 12 })} for details.</p>
    ${spendColChart(d.by_day || [], { title: "Spend over time" })}
    <div class="usage-grid">
      ${spendBarChart("By model", d.by_model || [], "model")}
      ${spendBarChart("By repository", d.by_repo || [], "repository")}
      ${spendBarChart("By source", d.by_source || [], "source")}
    </div>`;
}
