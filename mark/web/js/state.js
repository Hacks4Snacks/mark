"use strict";

import { $ } from "./utils.js";

export const PAGE_SIZE = 50;

export const state = {
  q: "",
  mode: "hybrid",
  sort: "recent",
  source: null,
  repo: null,
  tags: new Set(),
  facets: { repositories: [], tags: [], sources: [] },
  dateFrom: "",
  dateTo: "",
  view: "list",
  // When on, the list shows only manually hidden sessions so they can be
  // reviewed and unhidden.
  showHidden: false,
  currentId: null,
  currentCollectionId: null,
  limit: PAGE_SIZE,
  // Copilot CLI "resume" command template (from /api/status; {id} is substituted).
  resumeCmd: "copilot --resume {id}",
  // Whether the Ask (local RAG) feature is enabled (from /api/status). Off until
  // the backend reports it on, so the Ask UI stays dormant by default.
  askEnabled: false,
};

// Every primary view lives in a top-level container; switching simply toggles
// which one is visible. Listing them here keeps the show* helpers in sync.
export const VIEW_IDS = [
  "#listView",
  "#detailView",
  "#collectionsView",
  "#collectionView",
  "#libraryView",
  "#usageView",
  "#askView",
];

export function showOnly(visibleId) {
  for (const id of VIEW_IDS) {
    const el = $(id);
    if (el) el.hidden = id !== visibleId;
  }
}

/** Hide the search sidebar and use a narrower content column (Collections views). */
export function setLayoutWide(on) {
  const layout = $(".layout");
  if (!layout) return;
  layout.classList.toggle("layout--wide", on);
  layout.classList.remove("layout--dash");
}

/** Hide the sidebar and use the full page width (the Usage dashboard). */
export function setLayoutDash(on) {
  const layout = $(".layout");
  if (!layout) return;
  layout.classList.toggle("layout--dash", on);
  if (on) layout.classList.remove("layout--wide");
}
