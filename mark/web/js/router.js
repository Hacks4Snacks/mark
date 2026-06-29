"use strict";

// Hash-based deep-link routing. #/session/{id} opens a conversation,
// #/collection/{id} a collection, #/library|usage|ask|collections their views,
// and an empty hash shows the list.

import { state } from "./state.js";
import { showList } from "./views/list.js";
import { openSession } from "./views/detail.js";
import { showLibrary } from "./views/library.js";
import { showUsage } from "./views/usage.js";
import { showAsk } from "./views/ask.js";
import { openCollection, showCollections } from "./views/collections.js";

export function routeFromHash() {
  // In-page anchors (e.g. "#att-2" to jump to an attachment) are not app
  // routes; ignore them so they never fall through to the list view.
  if (location.hash && !location.hash.startsWith("#/")) return;
  if (location.hash === "#/library") {
    if (state.view !== "library") showLibrary({ fromHash: true });
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
  const m = location.hash.match(/^#\/session\/(.+)$/);
  if (m) {
    const id = decodeURIComponent(m[1]);
    if (state.view === "detail" && state.currentId === id) return;
    openSession(id, { fromHash: true });
  } else if (state.view !== "list") {
    showList({ fromHash: true });
  }
}

window.addEventListener("hashchange", routeFromHash);
