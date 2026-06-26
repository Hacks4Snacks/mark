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
  dateFrom: "",
  dateTo: "",
  view: "list",
  currentId: null,
  currentCollectionId: null,
  limit: PAGE_SIZE,
  // Copilot CLI "resume" command template (from /api/status; {id} is substituted).
  resumeCmd: "copilot --resume {id}",
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
  if (layout) layout.classList.toggle("layout--wide", on);
}
