"use strict";

// App shell: status polling, the add note/file dialog, all DOM event wiring,
// and bootstrapping. Views live in ./views/*; this module just connects them.

import { api } from "./api.js";
import { state } from "./state.js";
import { loadFacets, loadStats, syncFilterUI } from "./sidebar.js";
import { $, $$, debounce, esc, srcMeta, toast } from "./utils.js";
import { routeFromHash } from "./router.js";
import {
  clearAllFilters, doSearch, handleListKey, run, showList,
} from "./views/list.js";
import { openSession } from "./views/detail.js";
import {
  hideCollMenu, openCollectionDialog, saveCollection, saveCollectionFromFilters, showCollections,
} from "./views/collections.js";
import { libState, loadSnippets, showLibrary } from "./views/library.js";
import { loadUsage, showUsage } from "./views/usage.js";
import { showAsk, submitAsk } from "./views/ask.js";

// ---------- status, reindex & live auto-sync ----------
// A single self-scheduling loop polls /api/status: fast while an import runs,
// then a gentle heartbeat while idle so background auto-syncs (a session ending)
// show up on their own. `last_ingest` advancing means the index changed.
const HEARTBEAT_MS = 10000;
let statusTimer;
let lastSeenIngest;        // undefined until first observation
let lastSessionCount;      // visible-session count at last refresh

async function pollStatus(initial = false) {
  let running = false;
  try {
    const st = await api("/api/status");
    const banner = $("#statusBanner");
    running = !!st.running;
    if (running) {
      banner.hidden = false;
      banner.innerHTML = `<span class="dot"></span> ${esc(st.message || "Indexing...")}`;
      $("#reindexBtn").classList.add("spin");
    } else {
      banner.hidden = true;
      $("#reindexBtn").classList.remove("spin");
    }
    // The index changed (manual reindex or background auto-sync completed).
    if (st.last_ingest !== lastSeenIngest) {
      const first = lastSeenIngest === undefined;
      lastSeenIngest = st.last_ingest;
      if (!first) await refreshAll(true);
    }
  } catch (_) {}
  clearTimeout(statusTimer);
  statusTimer = setTimeout(() => pollStatus(), running ? 1100 : HEARTBEAT_MS);
}

// gentle: only re-run the result list when the user is idle at the top of the
// list view, so an auto-sync never yanks the page while they're reading.
async function refreshAll(gentle = false) {
  const [s] = await Promise.all([loadStats(), loadFacets()]);
  const count = s ? s.sessions ?? 0 : undefined;
  if (gentle && count != null && lastSessionCount != null && count > lastSessionCount) {
    const n = count - lastSessionCount;
    toast(`Synced ${n} new session${n === 1 ? "" : "s"}`);
  }
  if (count != null) lastSessionCount = count;
  if (state.view === "list") {
    const idle =
      document.activeElement !== $("#search") && window.scrollY < 40;
    if (!gentle || idle) doSearch(true, { keepView: true });
  }
}

// ---------- add dialog ----------
function setupDialog() {
  const dlg = $("#addDialog");
  let addMode = "note";
  let pickedFile = null;

  $("#addBtn").addEventListener("click", () => { pickedFile = null; $("#fileName").textContent = ""; dlg.showModal(); });
  $("#addClose").addEventListener("click", () => dlg.close());
  $("#addCancel").addEventListener("click", () => dlg.close());

  $$("#addModeToggle button").forEach((b) =>
    b.addEventListener("click", () => {
      addMode = b.dataset.add;
      $$("#addModeToggle button").forEach((x) => x.classList.toggle("active", x === b));
      $("#notePane").hidden = addMode !== "note";
      $("#filePane").hidden = addMode !== "file";
    })
  );

  const fileInput = $("#fileInput");
  const dz = $("#dropzone");
  fileInput.addEventListener("change", () => { pickedFile = fileInput.files[0]; $("#fileName").textContent = pickedFile ? pickedFile.name : ""; });
  ["dragover", "dragenter"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, () => dz.classList.remove("drag")));
  dz.addEventListener("drop", (e) => { e.preventDefault(); pickedFile = e.dataTransfer.files[0]; $("#fileName").textContent = pickedFile ? pickedFile.name : ""; });

  $("#addSave").addEventListener("click", async () => {
    try {
      let newId;
      if (addMode === "note") {
        const title = $("#noteTitle").value.trim();
        const text = $("#noteText").value.trim();
        if (!title && !text) return toast("Add a title or some text", true);
        newId = (await api("/api/notes", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title: title || "Untitled note", text }),
        })).id;
      } else {
        if (!pickedFile) return toast("Choose a file first", true);
        const fd = new FormData();
        fd.append("file", pickedFile);
        const res = await api("/api/uploads", { method: "POST", body: fd });
        if (res.matched) {
          dlg.close();
          const lbl = srcMeta(res.matched).label;
          const n = res.imported;
          toast(`Imported ${n} ${lbl} conversation${n === 1 ? "" : "s"}` +
            (res.skipped ? ` · ${res.skipped} unchanged` : ""));
          await refreshAll();
          return;
        }
        newId = res.id;
      }
      dlg.close();
      $("#noteTitle").value = ""; $("#noteText").value = "";
      toast("Saved to mindex");
      await refreshAll();
      if (newId) openSession(newId);
    } catch (e) {
      toast(e.message, true);
    }
  });
}

// ---------- wire up ----------
function setup() {
  // theme
  const saved = localStorage.getItem("mindex-theme");
  if (saved) document.documentElement.dataset.theme = saved;
  $("#themeBtn").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("mindex-theme", next);
  });

  $("#search").addEventListener("input", (e) => { state.q = e.target.value.trim(); run(); });

  $$("#modeToggle button").forEach((b) =>
    b.addEventListener("click", () => {
      state.mode = b.dataset.mode;
      $$("#modeToggle button").forEach((x) => x.classList.toggle("active", x === b));
      run();
    })
  );

  $("#sortSelect").addEventListener("change", (e) => { state.sort = e.target.value; run(); });

  $("#sourceFilters").addEventListener("click", (e) => {
    const chip = e.target.closest(".chip"); if (!chip) return;
    state.source = state.source === chip.dataset.source ? null : chip.dataset.source;
    syncFilterUI(); run();
  });
  $("#includeAutomation").addEventListener("change", (e) => {
    state.includeAutomation = e.target.checked;
    run();
  });
  $("#repoFilters").addEventListener("click", (e) => {
    const f = e.target.closest(".facet"); if (!f) return;
    state.repo = state.repo === f.dataset.repo ? null : f.dataset.repo;
    syncFilterUI(); run();
  });
  $("#tagFilters").addEventListener("click", (e) => {
    const chip = e.target.closest(".chip"); if (!chip) return;
    const t = chip.dataset.tag;
    state.tags.has(t) ? state.tags.delete(t) : state.tags.add(t);
    syncFilterUI(); run();
  });

  $("#clearFilters").addEventListener("click", clearAllFilters);

  $("#activeFilters").addEventListener("click", (e) => {
    if (e.target.closest("#afClear")) { clearAllFilters(); return; }
    const chip = e.target.closest(".af-chip"); if (!chip) return;
    const { type, val } = chip.dataset;
    if (type === "source") state.source = null;
    else if (type === "repo") state.repo = null;
    else if (type === "tag") state.tags.delete(val);
    else if (type === "date") { state.dateFrom = ""; state.dateTo = ""; $("#dateFrom").value = ""; $("#dateTo").value = ""; }
    else if (type === "auto") { state.includeAutomation = false; $("#includeAutomation").checked = false; }
    syncFilterUI(); run();
  });

  $("#dateFrom").addEventListener("change", (e) => { state.dateFrom = e.target.value; run(); });
  $("#dateTo").addEventListener("change", (e) => { state.dateTo = e.target.value; run(); });

  $("#brandHome").addEventListener("click", () => {
    state.q = ""; $("#search").value = "";
    state.source = null; state.repo = null; state.tags.clear();
    state.includeAutomation = false; $("#includeAutomation").checked = false;
    syncFilterUI(); showList(); run();
  });

  $("#reindexBtn").addEventListener("click", async () => {
    try { await api("/api/reindex", { method: "POST" }); toast("Re-scanning Copilot history..."); pollStatus(); }
    catch (e) { toast(e.message, true); }
  });

  $("#collectionsBtn").addEventListener("click", () => showCollections());
  $("#newCollectionBtn").addEventListener("click", () => openCollectionDialog({ mode: "create" }));
  $("#saveCollectionBtn").addEventListener("click", saveCollectionFromFilters);
  $("#collClose").addEventListener("click", () => $("#collectionDialog").close());
  $("#collCancel").addEventListener("click", () => $("#collectionDialog").close());
  $("#collSave").addEventListener("click", saveCollection);
  $("#collName").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); saveCollection(); }
  });
  document.addEventListener("click", (e) => {
    const menu = $("#collMenu");
    if (menu && !menu.hidden && !menu.contains(e.target) && !e.target.closest("#addToColl")) {
      hideCollMenu();
    }
  });

  $("#libraryBtn").addEventListener("click", () => showLibrary());
  $("#libSearch").addEventListener("input", debounce(() => {
    libState.q = $("#libSearch").value.trim(); loadSnippets();
  }, 200));
  $("#libLang").addEventListener("change", () => { libState.language = $("#libLang").value; loadSnippets(); });
  $("#libCommands").addEventListener("change", () => {
    libState.commands = $("#libCommands").checked;
    $("#libLang").disabled = libState.commands;
    loadSnippets();
  });

  $("#usageBtn").addEventListener("click", () => showUsage());
  $("#usageAuto").addEventListener("change", loadUsage);

  $("#askBtn").addEventListener("click", () => showAsk());
  $("#askForm").addEventListener("submit", (e) => { e.preventDefault(); submitAsk(); });
  $("#askInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submitAsk(); }
  });

  document.addEventListener("keydown", (e) => {
    const inSearch = document.activeElement === $("#search");
    if ((e.key === "/" && !inSearch) || (e.metaKey && e.key === "k")) {
      e.preventDefault(); $("#search").focus(); return;
    }
    if (e.key === "Escape") {
      const menu = $("#collMenu");
      if (menu && !menu.hidden) { hideCollMenu(); return; }
      if (state.view === "detail") { showList(); return; }
    }

    // arrow-key navigation through results (works even while typing a query)
    if (state.view === "list") handleListKey(e);
  });

  setupDialog();
}

async function init() {
  setup();
  await refreshAll();
  routeFromHash();
  pollStatus(true);
}

init();
