"use strict";

// Hash-based deep-link routing. #/session/{id} opens a conversation,
// #/collection/{id} a collection, #/library|usage|ask|collections their views,
// and an empty hash shows the list.

import { state } from "./state.js";
import { parseSessionHash, toast } from "./utils.js";
import { showList } from "./views/list.js";
import { openSession, teardownReading } from "./views/detail.js";
import { showLibrary } from "./views/library.js";
import { showUsage } from "./views/usage.js";
import { showAsk } from "./views/ask.js";
import { openCollection, showCollections } from "./views/collections.js";

export function routeFromHash() {
  // In-page anchors (e.g. "#att-2" to jump to an attachment) are not app
  // routes; ignore them so they never fall through to the list view.
  if (location.hash && !location.hash.startsWith("#/")) return;
  // Back can return to an already-visible view before a detail GET finishes.
  // Invalidate that request even if this route needs no new view rendering.
  if (!location.hash.startsWith("#/session/")) teardownReading();
  if (location.hash === "#/library" || location.hash === "#/library/curated") {
    const mode = location.hash.endsWith("/curated") ? "curated" : "extracted";
    if (state.view !== "library" || (state.libraryMode || "extracted") !== mode) {
      showLibrary({ fromHash: true, mode });
    }
    return;
  }
  if (location.hash === "#/usage") {
    if (state.view !== "usage") showUsage({ fromHash: true });
    return;
  }
  if (location.hash === "#/ask" && state.askEnabled) {
    if (state.view !== "ask") showAsk({ fromHash: true });
    return;
  }
  if (location.hash === "#/collections") {
    if (state.view !== "collections") showCollections({ fromHash: true });
    return;
  }
  const mc = location.hash.match(/^#\/collection\/(.+)$/);
  if (mc) {
    const cid = decodeURIComponent(mc[1]);
    if (state.view === "collection" && state.currentCollectionId === cid) return;
    openCollection(cid, { fromHash: true });
    return;
  }
  if (location.hash.startsWith("#/session/")) {
    const target = parseSessionHash(location.hash);
    if (!target) {
      toast("Invalid conversation link", true);
      showList({ fromHash: true });
      return;
    }
    openSession(target.id, { ...target, fromHash: true });
  } else if (state.view !== "list") {
    showList({ fromHash: true });
  }
}

window.addEventListener("hashchange", routeFromHash);
