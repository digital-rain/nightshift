"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  view: "now",
  queue: [],
  sortMode: "manual",    // active queue's sort mode: "manual" (drag order) or "priority"
  playPriorities: [],    // play-priority filter (0-5 levels allowed to play); [] = all
  runs: [],
  playlists: [],         // [{name, task_count}]
  libraryCount: 0,       // main `.tasks` queue's task count (the "library" row)
  activePlaylist: null,  // focused queue: null = main `.tasks`, else playlist name
  player: { state: "idle", mode: "auto", now_playing: null, cursor: null, run_id: null, active_playlist: null, running_playlist: null },
  players: {},           // per-queue runner state keyed by queue ("main" | playlist name), fed by multiplexed SSE
  logCache: {},          // "runId/task" -> full text
  expandedLogs: new Set(),
  expandedFm: new Set(),
  currentRunId: null,
  selectedTask: null,    // selection anchor (cursor): the last task clicked, drives keyboard nav + detail target
  selectedTasks: new Set(), // multi-selection: every selected task id (shift-click extends it)
  detailScreenTask: null, // task open in the full-area editable detail pane (view "detail")
  detailHistory: null,   // { run, rec } when the detail pane shows a read-only history task
  detailCreate: false,   // true when the detail pane is creating a new task (no file yet)
  createBrief: null,     // cached brief-shaped defaults (model options + flags) for create
  detailReturn: "now",   // view to return to when the detail pane's back is pressed
  detailDraft: null,     // buffered, unsaved edits for the open detail pane (discarded on back)
  libraryTasks: null,    // cached main-queue tasks for the Add-from "library" source
  repos: null,           // /api/repos payload (workspace, known repos, per-queue bindings, warnings)
};

// Transport glyphs for the Now box (borderless triangle / pause bars).
const PLAY_GLYPH = "\u25B6";          // ▶
const PAUSE_GLYPH = "\u275A\u275A";   // ❚❚

// Bottom-bar transport icons as SVGs sharing one 24×24 viewBox so play, pause,
// skip, and stop all center on the same axis (text glyphs don't align).
const PLAY_SVG = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M7 4v16l13-8z"/></svg>';
const PAUSE_SVG = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="4" width="3" height="16" rx="1"/><rect x="15" y="4" width="3" height="16" rx="1"/></svg>';

// Expando-panel disclosure chevron: points right when collapsed, rotates 90° to
// point down when open (the rotation lives in CSS, 150ms). Mirrors longitude's
// CollapsibleSection from the style guide, built on Nightshift's vanilla stack.
const CHEVRON_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>';

// Build an expando panel: a section whose head is the whole toggle (chevron +
// UPPERCASE caption, with an optional right-aligned collapsed-state preview).
// Returns { panel, body } so callers fill the body; clicking the head shows or
// hides it. `open` sets the initial state. `accessory`, when given, is an
// interactive element pinned to the right of the head bar (outside the toggle
// button, so clicking it never opens/closes the panel); it is shown only while
// the panel is open, since it acts on the body's contents.
function expando(caption, { open = true, subtitle = "", accessory = null } = {}) {
  const panel = document.createElement("section");
  panel.className = "xpanel" + (open ? " open" : "");

  const head = document.createElement("button");
  head.type = "button";
  head.className = "xpanel-head";
  head.setAttribute("aria-expanded", open ? "true" : "false");

  const chev = document.createElement("span");
  chev.className = "xpanel-chev";
  chev.innerHTML = CHEVRON_SVG;
  const cap = document.createElement("span");
  cap.className = "xpanel-cap";
  cap.textContent = caption;
  const sub = document.createElement("span");
  sub.className = "xpanel-sub";
  sub.textContent = subtitle;
  sub.hidden = open || !subtitle;
  head.append(chev, cap, sub);

  const body = document.createElement("div");
  body.className = "xpanel-body";
  body.hidden = !open;

  // With an accessory, the head bar is a flex row holding the toggle button and
  // the accessory as siblings; without one, the button is the bar itself.
  let headHost = head;
  if (accessory) {
    accessory.classList.add("xpanel-accessory");
    accessory.hidden = !open;
    headHost = document.createElement("div");
    headHost.className = "xpanel-headrow";
    headHost.append(head, accessory);
  }

  head.addEventListener("click", () => {
    const isOpen = panel.classList.toggle("open");
    head.setAttribute("aria-expanded", isOpen ? "true" : "false");
    body.hidden = !isOpen;
    sub.hidden = isOpen || !subtitle;
    if (accessory) accessory.hidden = !isOpen;
  });

  panel.append(headHost, body);
  return { panel, body };
}

// --------------------------------------------------------------------------
// Execution-state vocabulary (one consistent set across every screen)
// --------------------------------------------------------------------------
// Maps the engine's raw task status to the media-player vocabulary and the CSS
// class that colours its pill.
const STATE_LABELS = {
  pending: "Queued",
  running: "Running",
  paused: "Paused",
  // A task whose resolved target repo isn't present in the workspace is paused
  // (auto-resumable), never failed — it reads as "Paused" with the warn pill.
  repo_unavailable: "Paused",
  completed: "Completed",
  error: "Failed",
  stopped: "Cancelled",
  skipped: "Skipped",
  aborted: "Aborted",
};
function stateLabel(status) {
  return STATE_LABELS[status] || (status ? status[0].toUpperCase() + status.slice(1) : "—");
}
// The CSS class that colours a status pill. Most statuses map to a same-named
// class, but a few synonyms collapse onto a shared visual: `repo_unavailable`
// (a paused, auto-resumable task) reuses the `.status.paused` warn treatment.
function statusClass(status) {
  if (status === "repo_unavailable") return "paused";
  return status || "running";
}
function statusPill(status) {
  const span = document.createElement("span");
  span.className = "status " + statusClass(status);
  span.textContent = stateLabel(status);
  return span;
}

// Classified failure reasons (engine `failure_kind`) → short operator-facing
// labels, so History shows *why* a task failed without opening the log.
const FAILURE_LABELS = {
  merge_conflict: "merge conflict",
  merge_rejected: "merge rejected",
  validation_error: "validation",
  worker_error: "worker error",
  worker_launch: "worker launch",
  timeout: "timeout",
  aborted: "aborted",
  no_changes: "no changes",
};

// A small labelled chip for a failed task's classified reason. Returns null
// when there's nothing to show (succeeded, or no classified kind).
function failureBadge(kind) {
  const label = FAILURE_LABELS[kind];
  if (!label) return null;
  const span = document.createElement("span");
  span.className = "fail-badge fail-" + kind;
  span.textContent = label;
  return span;
}

// A task's validated work is preserved on a branch (so Resolve can land it)
// only when the squash-merge itself failed.
function isResolvable(task) {
  return (
    task.status === "error" &&
    (task.recoverable ||
      task.failure_kind === "merge_conflict" ||
      task.failure_kind === "merge_rejected")
  );
}

// --------------------------------------------------------------------------
// API helpers
// --------------------------------------------------------------------------
async function getJSON(url) {
  const r = await fetch(url);
  return r.json();
}
async function sendJSON(url, method, body) {
  const r = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  return { ok: r.ok, status: r.status, data };
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Inline Markdown on an already-escaped string: code spans, links, bold, italic.
// The input is HTML-escaped (so `<`/`>`/`"` are entities already), which keeps
// this safe — we only ever re-introduce a fixed set of tags. Links are limited
// to http(s)/mailto/relative targets so a `javascript:` URL can't sneak in.
function inlineMd(s) {
  // Split on code spans so emphasis/link rules never touch code contents: with a
  // capturing split the odd-indexed parts are the code, the even parts are prose.
  return s.split(/`([^`]+)`/).map((part, idx) => {
    if (idx % 2 === 1) return `<code>${part}</code>`;
    return part
      .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, text, url) =>
        /^(https?:|mailto:)/i.test(url) || /^[/#]/.test(url)
          ? `<a href="${url}" target="_blank" rel="noopener noreferrer">${text}</a>`
          : m)
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/__([^_]+)__/g, "<strong>$1</strong>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>")
      .replace(/(^|[^\w])_([^_]+)_(?=[^\w]|$)/g, "$1<em>$2</em>");
  }).join("");
}

// Minimal, dependency-free Markdown → HTML for the brief preview. The whole
// source is HTML-escaped up front, so no author-supplied markup survives; the
// block parser then emits a safe, fixed tag set (headings, lists, blockquotes,
// fenced code, rules, paragraphs) with inline formatting via inlineMd.
function renderMarkdown(src) {
  const lines = escapeHtml(src || "").split("\n");
  const out = [];
  let para = [];
  let list = null;   // { type: "ul" | "ol", items: [] }
  let quote = [];

  const closePara = () => {
    if (para.length) { out.push(`<p>${inlineMd(para.join("<br>"))}</p>`); para = []; }
  };
  const closeList = () => {
    if (list) {
      const items = list.items.map((it) => `<li>${inlineMd(it)}</li>`).join("");
      out.push(`<${list.type}>${items}</${list.type}>`);
      list = null;
    }
  };
  const closeQuote = () => {
    if (quote.length) { out.push(`<blockquote>${inlineMd(quote.join("<br>"))}</blockquote>`); quote = []; }
  };
  const closeAll = () => { closePara(); closeList(); closeQuote(); };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    // Fenced code block — copy verbatim until the closing fence (or EOF).
    const fence = line.match(/^\s*(`{3,}|~{3,})/);
    if (fence) {
      closeAll();
      const marker = fence[1][0];
      const buf = [];
      const close = new RegExp(`^\\s*\\${marker}{3,}\\s*$`);
      for (i++; i < lines.length && !close.test(lines[i]); i++) buf.push(lines[i]);
      out.push(`<pre><code>${buf.join("\n")}</code></pre>`);
      continue;
    }
    if (/^\s*$/.test(line)) { closeAll(); continue; }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { closeAll(); out.push("<hr>"); continue; }

    const h = line.match(/^\s*(#{1,6})\s+(.*?)\s*#*\s*$/);
    if (h) { closeAll(); out.push(`<h${h[1].length}>${inlineMd(h[2])}</h${h[1].length}>`); continue; }

    const bq = line.match(/^\s*&gt;\s?(.*)$/);  // `>` is escaped to `&gt;`
    if (bq) { closePara(); closeList(); quote.push(bq[1]); continue; }
    closeQuote();

    const ul = line.match(/^\s*[-*+]\s+(.*)$/);
    const ol = ul ? null : line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (ul || ol) {
      closePara();
      const type = ul ? "ul" : "ol";
      if (!list || list.type !== type) { closeList(); list = { type, items: [] }; }
      list.items.push((ul || ol)[1]);
      continue;
    }
    closeList();
    para.push(line.trim());
  }
  closeAll();
  return out.join("\n");
}

// --------------------------------------------------------------------------
// Theme
// --------------------------------------------------------------------------
const THEME_KEY = "nightshift-theme";
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
}
function initTheme(defaultTheme) {
  let theme = localStorage.getItem(THEME_KEY);
  if (!theme) {
    theme = defaultTheme
      || (window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  }
  applyTheme(theme);
}

// --------------------------------------------------------------------------
// Navigation (Home / Now / Queue / Playlists / History)
// --------------------------------------------------------------------------
function setView(view) {
  // Home is an action, not a screen: it returns to the main `.tasks` queue and
  // shows Now.
  if (view === "home") {
    activatePlaylist(null);
    return;
  }
  state.view = view;
  document.body.setAttribute("data-view", view);
  // The full-area detail takeover (used for both editing and creating a task)
  // is a sub-view of Now: keep Now/Home lit while it's open (it has no nav tab).
  // The Stats page is likewise a sub-view of History — keep History lit.
  let navView = view;
  if (view === "detail") navView = "now";
  else if (view === "stats") navView = "history";
  for (const b of document.querySelectorAll("#bottomnav .navbtn")) {
    let on = b.dataset.view === navView;
    if (b.dataset.view === "home") on = state.activePlaylist === null && navView === "now";
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
  }
  if (view === "now") renderNow();
  else if (view === "queue") renderQueue();
  else if (view === "playlists") renderPlaylists();
  else if (view === "history") renderHistory();
  else if (view === "stats") renderStats();
  else if (view === "repos") { renderRepos(); loadRepos(); }
  else if (view === "detail") renderDetailScreen();
}

// --------------------------------------------------------------------------
// Transport
// --------------------------------------------------------------------------
function getMode() {
  const active = document.querySelector("#mode .mode-opt.active");
  return active ? active.dataset.mode : "auto";
}
function setMode(mode) {
  const group = $("mode");
  if (!group) return;
  for (const b of group.querySelectorAll(".mode-opt")) {
    const on = b.dataset.mode === mode;
    b.classList.toggle("active", on);
    b.setAttribute("aria-pressed", on ? "true" : "false");
  }
}

async function transport(action, extra = {}) {
  // Drive the *focused* queue's runner by default (null → server falls back to
  // the focused queue); callers may target another queue via `extra.queue`.
  const body = { action, mode: getMode(), queue: state.activePlaylist, ...extra };
  const { data } = await sendJSON("/api/transport", "POST", body);
  if (data && data.state) ingestState(data);
  // Hitting play follows the run to the Now view, wherever it was triggered
  // from (bottom-bar, idle hero, a queue-row double-click). A selected task has
  // already moved the server cursor, so the run starts from there; this just
  // brings the focus to what's now playing. Pause/select/stop/skip stay put.
  if (action === "play" && data && data.state === "playing") setView("now");
}

// The single play/pause control: pause while playing, otherwise play/resume.
function togglePlayPause() {
  transport(state.player.state === "playing" ? "pause" : "play");
}

// The map key for a queue: "main" for the main `.tasks` queue, else its name.
// Mirrors the server's queue keying so SSE frames and the per-queue map align.
function focusedQueueKey() {
  return state.activePlaylist || "main";
}

// Fold one queue's runner state into the per-queue map (used for the Playlists
// "running" badges and the multi-queue running indicator).
function ingestQueueState(key, st) {
  if (!key) return;
  state.players[key] = { ...(state.players[key] || {}), ...st };
}

// Ingest a state payload from either /api/state (aggregate, carries a `queues`
// map) or a multiplexed SSE frame (single queue, carries a `queue` key). The
// focused queue's state drives the single-context view (Now/transport); other
// queues only update their per-queue card/badge.
function ingestState(data) {
  if (!data) return;
  if (data.queues && typeof data.queues === "object") {
    for (const [key, st] of Object.entries(data.queues)) ingestQueueState(key, st);
  }
  if (data.queue) {
    // A multiplexed SSE frame for one queue.
    ingestQueueState(data.queue, data);
    if (data.queue === focusedQueueKey()) updatePlayerState(data);
    else if (state.view === "playlists") renderPlaylists();
  } else if (data.queues && state.players[focusedQueueKey()]) {
    // An aggregate payload (/api/state, transport response): its flat fields
    // follow whichever queue is running, so drive the focused view from the
    // per-queue map instead (seeded just above) to stay queue-correct.
    applyFocusedState();
  } else {
    updatePlayerState(data);
  }
  renderRunningIndicator();
}

// Keys of every queue currently running (playing or paused).
function runningQueueKeys() {
  return Object.entries(state.players)
    .filter(([, p]) => p && (p.state === "playing" || p.state === "paused"))
    .map(([key]) => key);
}

// The "Now" nav button doubles as a cycler across the running queues' Now
// Playing pages. Off the Now screen it just lands there for the queue in focus.
// On the Now screen it advances to the next queue with a live run (wrapping);
// with no other running queue to move to, it stays put.
function cycleNow() {
  if (state.view !== "now") {
    setView("now");
    return;
  }
  const keys = runningQueueKeys();
  if (keys.length === 0) return;
  const current = focusedQueueKey();
  const idx = keys.indexOf(current);
  // Focused queue isn't running: step into the first running queue. Otherwise
  // advance to the next running queue, wrapping — but if it's the only one
  // running, there's nowhere else to go.
  let next;
  if (idx === -1) {
    next = keys[0];
  } else {
    next = keys[(idx + 1) % keys.length];
    if (next === current) return;
  }
  // Map the queue key back to the activate-by-name form (main → null).
  activatePlaylist(next === "main" ? null : next, "now");
}

// Surface concurrent runs in the brand sub-line: when more than one queue is
// running, append a "· N running" hint so it's clear work is happening on
// queues other than the one in focus.
function renderRunningIndicator() {
  const tag = $("brand-tag");
  if (!tag) return;
  const n = runningQueueKeys().length;
  let hint = tag.querySelector(".brand-running");
  if (n > 1) {
    if (!hint) {
      hint = document.createElement("span");
      hint.className = "brand-running";
      tag.append(hint);
    }
    hint.textContent = ` \u00b7 ${n} running`;
  } else if (hint) {
    hint.remove();
  }
}

function updatePlayerState(player) {
  state.player = { ...state.player, ...player };
  if (player.run_id) state.currentRunId = player.run_id;
  // Seed the per-queue map from an aggregate payload so badges/indicator are
  // correct even before the next SSE frame for each queue arrives.
  if (player.queues && typeof player.queues === "object") {
    for (const [key, st] of Object.entries(player.queues)) ingestQueueState(key, st);
  }
  // Keep the active-playlist view in sync with authoritative server state.
  if ("active_playlist" in player) syncActivePlaylist(player.active_playlist);
  if (player.mode && !$("mode").contains(document.activeElement)) setMode(player.mode);
  // One play/pause toggle: shows pause while playing, play otherwise. Always
  // actionable (play starts/resumes; pause pauses).
  const playing = player.state === "playing";
  const playBtn = $("btn-play");
  playBtn.innerHTML = playing ? PAUSE_SVG : PLAY_SVG;
  playBtn.title = playing ? "Pause" : (player.state === "paused" ? "Resume" : "Play");
  playBtn.disabled = false;
  // Stop is honoured whenever something is running or paused — never gated.
  $("btn-stop").disabled = player.state === "idle";
  $("btn-skip").disabled = player.state === "idle";
  renderNow();
  renderQueue();
  // Keep the playlists screen's per-queue "running" badges live as runs start
  // and stop on queues other than the focused one.
  if (state.view === "playlists") renderPlaylists();
  if (state.detailScreenTask && state.view === "detail") renderDetailScreen();
  // syncActivePlaylist may have reset the brand-tag text; re-apply the hint.
  renderRunningIndicator();
}

// Reflect the active queue in the brand sub-line so it's always clear which
// playlist the controls/queue/history operate on.
function syncActivePlaylist(name) {
  const changed = state.activePlaylist !== (name ?? null);
  state.activePlaylist = name ?? null;
  const tag = $("brand-tag");
  if (tag) {
    if (state.activePlaylist) {
      tag.textContent = `Playlist \u00b7 ${state.activePlaylist}`;
      tag.classList.add("playlist");
    } else {
      tag.textContent = "agent task runner";
      tag.classList.remove("playlist");
    }
  }
  if (changed && state.view === "playlists") renderPlaylists();
}

// Refresh the per-queue state map from the aggregate /api/state `queues` map,
// without disturbing the focused single-context view.
async function refreshQueues() {
  try {
    const agg = await getJSON("/api/state");
    if (agg && agg.queues) {
      for (const [k, st] of Object.entries(agg.queues)) ingestQueueState(k, st);
    }
  } catch { /* keep the last known per-queue states on a transient error */ }
}

// Drive the single-context view (Now/transport) from the *focused* queue's own
// runner state — not the aggregate flat state, which follows whichever queue is
// running. Falls back to idle when the focused queue has no state yet.
function applyFocusedState() {
  const st = state.players[focusedQueueKey()] || {
    state: "idle", now_playing: null, cursor: null, run_id: null,
    active_playlist: state.activePlaylist, running_playlist: null,
  };
  updatePlayerState(st);
}

// Switch the active queue server-side, then refresh everything and land on a
// target view (Now by default; selecting a playlist row lands on the Queue).
async function activatePlaylist(name, targetView = "now") {
  const { ok, data } = await sendJSON("/api/active", "POST", { playlist: name });
  if (!ok) {
    alert((data && data.error) || "could not switch queue");
    return;
  }
  syncActivePlaylist(name);
  state.selectedTask = null;
  state.selectedTasks = new Set();
  document.body.classList.remove("has-detail");
  await Promise.all([loadQueue(), loadRuns(), refreshQueues()]);
  // Reflect the queue we just focused (it may be idle while another runs).
  applyFocusedState();
  setView(targetView);
}

// --------------------------------------------------------------------------
// Data loading
// --------------------------------------------------------------------------
async function loadQueue() {
  state.queue = await getJSON("/api/queue");
  // The sort mode is per-queue (persisted in config.json) and drives both this
  // display and the engine's play order, so load it alongside the queue.
  try {
    const sort = await getJSON("/api/queue/sort");
    state.sortMode = (sort && sort.sort) || "manual";
  } catch { state.sortMode = "manual"; }
  // The play-priority filter is also per-queue (persisted in config.json) and
  // restricts which tasks play, so load it alongside the queue.
  try {
    const pp = await getJSON("/api/queue/play-priorities");
    state.playPriorities = (pp && Array.isArray(pp.priorities)) ? pp.priorities : [];
  } catch { state.playPriorities = []; }
  renderPlayFilter();
  // When the main queue is active it *is* the library, so keep that row's count
  // in step with the queue we just loaded.
  if (state.activePlaylist === null) {
    state.libraryCount = state.queue.length;
    if (state.view === "playlists") renderPlaylists();
  }
  renderQueue();
  renderNow();
}

// Toggle the active queue between manual (drag) order and priority sort,
// persist it, and reload so the display and the engine's play order agree.
async function toggleSortMode() {
  const next = state.sortMode === "priority" ? "manual" : "priority";
  const { ok, data } = await sendJSON("/api/queue/sort", "PUT", { sort: next });
  if (!ok) {
    alert((data && data.error) || "could not change sort mode");
    return;
  }
  state.sortMode = (data && data.sort) || next;
  await loadQueue();
}

// Persist the play-priority filter for the active queue, then reload so the Now
// list/count and the engine's play set agree. ``next`` is an array of 0-5
// levels; [] clears the filter (play all priorities).
async function setPlayPriorities(next) {
  const { ok, data } = await sendJSON(
    "/api/queue/play-priorities", "PUT", { priorities: next });
  if (!ok) {
    alert((data && data.error) || "could not change play filter");
    return;
  }
  state.playPriorities =
    data && Array.isArray(data.priorities) ? data.priorities : [];
  renderPlayFilter();
  await loadQueue();
}

// Multi-select toggle for one priority level. Adding/removing a level from the
// current set; emptying the set falls back to "all".
function togglePlayPriority(level) {
  const set = new Set(state.playPriorities || []);
  if (set.has(level)) set.delete(level);
  else set.add(level);
  setPlayPriorities([...set].sort((a, b) => a - b));
}

// Render the header's [ALL|P0..P5] segmented multi-select to match the active
// filter. ALL is "on" only when no specific levels are selected.
function renderPlayFilter() {
  const wrap = $("play-filter");
  if (!wrap) return;
  const selected = new Set(state.playPriorities || []);
  const all = selected.size === 0;
  for (const opt of wrap.querySelectorAll(".pf-opt")) {
    const raw = opt.dataset.priority;
    const on = raw === "all" ? all : selected.has(Number(raw));
    opt.classList.toggle("on", on);
    opt.setAttribute("aria-pressed", on ? "true" : "false");
  }
}

async function loadRuns() {
  state.runs = await getJSON("/api/runs");
  renderHistory();
  if (state.view === "stats") renderStats();
  renderNow();
}

async function loadPlaylists() {
  state.playlists = await getJSON("/api/playlists");
  // The main `.tasks/` queue shows up as the "library" row on this screen. When
  // it's the active queue its tasks are already in `state.queue`; otherwise ask
  // the server for its count so the row stays accurate from any playlist.
  if (state.activePlaylist === null) {
    state.libraryCount = state.queue.length;
  } else {
    try { state.libraryCount = (await getJSON("/api/main/tasks")).length; }
    catch { /* keep the last known count on a transient error */ }
  }
  renderPlaylists();
}

// --------------------------------------------------------------------------
// Repos screen (multi-repo workspace)
// --------------------------------------------------------------------------
// The Repos page is a thin view over `GET /api/repos`: the read-only workspace
// path, the known-repos set (workspace children with a `.git`), and each
// queue's default-repo binding + availability. A Rescan button re-scans the
// workspace and auto-resumes any task paused because its repo was absent.

// Pull the repos payload into state, then refresh the Repos screen if it's the
// one on display. Kept resilient: a transient error leaves the last snapshot.
async function loadRepos() {
  try {
    state.repos = await getJSON("/api/repos");
  } catch { /* keep the last known repos on a transient error */ }
  if (state.view === "repos") renderRepos();
}

// A <select> of known workspace repos for binding a queue/task to a target
// repo. `current` is the bound value ("" / null = the empty option, named by
// `emptyLabel`: "— none —" clears a queue default; "Inherit" for a per-task
// override). A `current` value that isn't among the known repos (an absent
// repo a binding still points at) is preserved as an extra option so the
// selector round-trips it instead of silently dropping it.
function repoSelect(current, emptyLabel) {
  const select = document.createElement("select");
  select.className = "ctl-select repo-select";
  const cur = current || "";
  const known = (state.repos && Array.isArray(state.repos.repos))
    ? state.repos.repos.map((r) => r.name)
    : [];
  const values = [""];
  for (const name of known) values.push(name);
  if (cur && !values.includes(cur)) values.push(cur);
  for (const v of values) {
    const opt = document.createElement("option");
    opt.value = v;
    opt.textContent = v === ""
      ? emptyLabel
      : (known.includes(v) ? v : `${v} (absent)`);
    select.append(opt);
  }
  select.value = cur;
  return select;
}

// A repo-availability pill, reusing the shared status vocabulary: present repos
// read as the green "completed" treatment; absent ones reuse the warn
// `.status.paused` look (matching the paused tasks they cause).
function availabilityBadge(available) {
  const span = document.createElement("span");
  span.className = "status " + (available ? "completed" : "paused");
  span.textContent = available ? "Available" : "Absent";
  return span;
}

function renderRepos() {
  const wsEl = $("repos-workspace");
  if (!wsEl) return;  // screen markup not present in this build
  const data = state.repos || {};

  wsEl.textContent = data.workspace || "—";

  // Warnings — one per queue whose bound repo is absent (deduped server-side).
  const warn = $("repos-warnings");
  if (warn) {
    warn.innerHTML = "";
    const warnings = data.warnings || [];
    warn.hidden = warnings.length === 0;
    for (const w of warnings) {
      const row = document.createElement("div");
      row.className = "repos-warning";
      row.textContent =
        `Queue \u201c${w.queue}\u201d is bound to \u201c${w.repo}\u201d, which `
        + "isn\u2019t present in the workspace \u2014 its tasks are paused until "
        + "you clone it and rescan.";
      warn.append(row);
    }
  }

  // Known repos (workspace children with a `.git`); the tasks store is tagged.
  const list = $("repos-list");
  if (list) {
    list.innerHTML = "";
    const repos = data.repos || [];
    const count = $("repos-count");
    if (count) count.textContent = repos.length ? `(${repos.length})` : "";
    const empty = $("repos-empty");
    if (empty) empty.hidden = repos.length > 0;
    for (const r of repos) list.append(repoRow(r, data.tasks_repo));
  }

  // Per-queue default-repo bindings + selectors.
  const queues = $("repos-queues");
  if (queues) {
    queues.innerHTML = "";
    for (const q of data.queues || []) queues.append(repoQueueRow(q));
  }
}

function repoRow(r, tasksRepo) {
  const li = document.createElement("li");
  li.className = "repo-item";
  const main = document.createElement("div");
  main.className = "repo-main";
  const name = document.createElement("span");
  name.className = "repo-name";
  name.textContent = r.name;
  main.append(name);
  if (tasksRepo && r.name === tasksRepo) {
    const tag = document.createElement("span");
    tag.className = "repo-tag";
    tag.textContent = "tasks store";
    main.append(tag);
  }
  li.append(main, availabilityBadge(r.available));
  return li;
}

// One per-queue binding row: the queue label + its bound repo's availability,
// and a default-repo selector that persists via PUT /api/queue/repo. The 400
// (malformed name) is surfaced inline without tearing down the row.
function repoQueueRow(q) {
  const li = document.createElement("li");
  li.className = "repo-queue-item";

  const head = document.createElement("div");
  head.className = "repo-queue-head";
  const label = document.createElement("span");
  label.className = "repo-queue-name";
  label.textContent = q.queue;
  head.append(label);
  if (q.repo) head.append(availabilityBadge(q.available));
  li.append(head);

  const ctl = document.createElement("label");
  ctl.className = "repo-queue-ctl";
  const span = document.createElement("span");
  span.className = "repo-queue-ctl-label";
  span.textContent = "Default repo";
  const select = repoSelect(q.repo, "\u2014 none \u2014");
  const err = document.createElement("p");
  err.className = "error repo-queue-error";
  err.hidden = true;
  select.addEventListener("change", () => setQueueRepo(q.queue, select.value, err));
  ctl.append(span, select);
  li.append(ctl, err);
  return li;
}

// Persist a queue's default target repo. The default queue's label is "main";
// an empty selection clears the binding (the queue then has no default and
// tasks must set their own). Threads the queue label explicitly so any queue
// is editable from this page, not just the focused one.
async function setQueueRepo(label, value, errEl) {
  const repo = value ? value : null;
  const url = `/api/queue/repo?queue=${encodeURIComponent(label)}`;
  const { ok, data } = await sendJSON(url, "PUT", { repo });
  if (!ok) {
    if (errEl) {
      errEl.textContent = (data && data.error) || "could not set queue repo";
      errEl.hidden = false;
    }
    return;
  }
  if (errEl) errEl.hidden = true;
  // The binding (and thus availability/warnings) changed; re-pull repos and
  // refresh the queue/run views so resumed/paused tasks reflect the new repo.
  await loadRepos();
  await Promise.all([loadQueue(), loadRuns()]);
}

// Re-scan the workspace for repos (auto-resuming any task paused on a now
// present repo), then refresh this page and the queue/history views.
async function rescanRepos(btn) {
  if (btn) btn.disabled = true;
  try {
    const { ok, data } = await sendJSON("/api/repos/rescan", "POST", {});
    if (ok && data) state.repos = data;
    renderRepos();
    await Promise.all([loadQueue(), loadRuns(), loadPlaylists()]);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// --------------------------------------------------------------------------
// Now screen
// --------------------------------------------------------------------------
// Execution phases reported by the engine, in order. `commit` is implicit on a
// successful finish (the worktree squash/land step).
const PHASES = [
  { key: "worker", label: "Worker" },
  { key: "validate", label: "Validate" },
  { key: "commit", label: "Commit" },
];

// True when the focused queue is the one a run is actively draining. Live-run
// affordances (the "now playing" marker, the detail-pane edit lock, the Now
// stepper) only apply when you're viewing the running queue; focus another
// queue and they fall away so it stays fully browsable and editable.
function runningHere() {
  return (state.player.running_playlist ?? null) === (state.activePlaylist ?? null);
}

// True when `name` (null = main) is a queue with a live run. Reads the
// per-queue map (fed by multiplexed SSE) so *every* running queue lights up,
// not just the focused/primary one — two queues can run at once.
function isQueueRunning(name) {
  const p = state.players[name || "main"];
  if (p) return p.state === "playing" || p.state === "paused";
  // Fall back to the single-context fields for the focused queue before its
  // first per-queue frame lands.
  const live = state.player.state === "playing" || state.player.state === "paused";
  return live && (state.player.running_playlist ?? null) === (name ?? null);
}

function currentTaskRecord() {
  const rid = state.player.run_id || state.currentRunId;
  if (!rid || !state.player.now_playing || !runningHere()) return null;
  const run = state.runs.find((r) => r.id === rid);
  if (!run) return null;
  return run.tasks.find((t) => t.task === state.player.now_playing) || null;
}

function phaseIndex(record) {
  if (!record) return 0;
  if (record.status === "completed") return PHASES.length;     // all done
  if (record.phase === "commit") return 2;
  if (record.phase === "validate") return 1;
  return 0;                                              // worker / resolve / unknown
}

// A resolve job runs resolve → validate → commit; relabel the first step so the
// stepper and clock read "Resolve" instead of "Worker" for that flow.
function phaseLabels(rec) {
  if (rec && rec.phase === "resolve") {
    return [{ key: "resolve", label: "Resolve" }, PHASES[1], PHASES[2]];
  }
  return PHASES;
}

// True when the active play-priority filter allows a task of this priority to
// play. An empty filter ([]) means "all priorities".
function playPriorityAllows(priority) {
  if (!state.playPriorities || state.playPriorities.length === 0) return true;
  const p = typeof priority === "number" ? priority : 5;
  return state.playPriorities.includes(p);
}

// "Up next" = enabled, not-now-playing tasks that the play-priority filter lets
// through. This mirrors the engine's play set so the count and Now list reflect
// exactly what will run.
function upNextItems() {
  return state.queue.filter(
    (i) =>
      !i.disabled &&
      i.task !== state.player.now_playing &&
      playPriorityAllows(i.priority)
  );
}

function renderNow() {
  const wrap = $("now-body");
  if (!wrap) return;
  wrap.innerHTML = "";

  // Column 1, ordered: Now executing (or idle hero) → spacer → Up Next. The
  // full queue lives on its own page; the Now screen stays focused on what's
  // playing and what's next.
  const active = state.player.state === "playing" || state.player.state === "paused";
  wrap.append(active ? executionCard() : idleHero());
  wrap.append(spacer());
  wrap.append(upNextCard());

  renderNowDetail();
}

function spacer() {
  const s = document.createElement("div");
  s.className = "now-spacer";
  return s;
}

// The task detail is now a full-area editable pane (opened from a queue/Now
// selection), so the Now screen no longer grows an inline detail column. This
// keeps the legacy slot empty at every width.
function renderNowDetail() {
  const detail = $("now-detail");
  if (!detail) return;
  document.body.classList.remove("has-detail");
  detail.innerHTML = "";
}

function executionCard() {
  const card = document.createElement("div");
  card.className = "exec-card";

  const rec = currentTaskRecord();
  const now = state.player.now_playing;
  // Click the card (but not the play/pause toggle) to open the full-area detail.
  if (now) {
    card.classList.add("exec-clickable");
    card.addEventListener("click", () => openDetailScreen(now));
  }
  const queueItem = state.queue.find((i) => i.task === now);
  const title = (rec && rec.title) || (queueItem && queueItem.title) || now || "Starting\u2026";
  const status = state.player.state === "paused" ? "paused" : (rec ? rec.status : "running");

  const head = document.createElement("div");
  head.className = "exec-head";
  const eyebrow = document.createElement("div");
  eyebrow.className = "exec-eyebrow";
  eyebrow.textContent = "Now executing";
  const h = document.createElement("div");
  h.className = "exec-title";
  h.textContent = title;
  const file = document.createElement("div");
  file.className = "exec-file";
  const model = rec && rec.frontmatter && rec.frontmatter.model;
  file.textContent = now ? `${now}.md` + (model ? ` \u00b7 ${model}` : "") : "";
  head.append(eyebrow, h, file);

  // Play/pause the now-executing item directly from the Now box. The transport
  // state already conveys running vs paused, so no status pill is shown here.
  const playing = state.player.state === "playing";
  const toggle = document.createElement("button");
  toggle.className = "exec-toggle";
  toggle.innerHTML = playing ? PAUSE_GLYPH : PLAY_GLYPH;
  toggle.title = playing ? "Pause" : "Resume";
  toggle.addEventListener("click", (e) => { e.stopPropagation(); togglePlayPause(); });
  const actions = document.createElement("div");
  actions.className = "exec-actions";
  actions.append(toggle);
  head.append(actions);

  // Live "<phase> · 14m 03s" clock, driven by a 1s ticker (tickElapsed).
  if (rec && rec.phase_started_at && status === "running" && state.player.state !== "paused") {
    const labels = phaseLabels(rec);
    const phaseLabel = (labels[phaseIndex(rec)] || labels[0]).label;
    const el = document.createElement("div");
    el.className = "exec-elapsed";
    el.id = "now-elapsed";
    el.dataset.since = rec.phase_started_at;
    el.dataset.phase = phaseLabel;
    el.textContent = `${phaseLabel} \u00b7 ${formatElapsed(Date.now() - Date.parse(rec.phase_started_at))}`;
    head.append(el);
  }
  card.append(head);

  card.append(phaseStepper(rec, status));

  // Live log tail for the current task (once one is actually executing).
  const log = document.createElement("pre");
  log.className = "exec-log";
  log.id = "now-log";
  if (!now) {
    log.textContent = "starting\u2026";
  } else {
    const rid = state.player.run_id || state.currentRunId;
    const key = `${rid}/${now}`;
    const text = state.logCache[key];
    if (text === undefined) {
      log.textContent = "waiting for output\u2026";
      if (rid) fetchLog(rid, now, key, renderNow);
    } else {
      log.textContent = logTail(text, 12);
    }
  }
  card.append(log);
  return card;
}

function phaseStepper(rec, status) {
  const failed = status === "error" || status === "aborted" || status === "stopped";
  const current = phaseIndex(rec);
  const row = document.createElement("div");
  row.className = "stepper";
  const phases = phaseLabels(rec);
  phases.forEach((p, i) => {
    const step = document.createElement("div");
    step.className = "step";
    if (i < current) step.classList.add("done");
    else if (i === current && !failed) step.classList.add("active");
    else if (i === current && failed) step.classList.add("failed");
    const dot = document.createElement("span");
    dot.className = "step-dot";
    const label = document.createElement("span");
    label.className = "step-label";
    label.textContent = p.label;
    step.append(dot, label);
    row.append(step);
    if (i < phases.length - 1) {
      const bar = document.createElement("span");
      bar.className = "step-bar" + (i < current ? " done" : "");
      row.append(bar);
    }
  });
  return row;
}

function idleHero() {
  const hero = document.createElement("div");
  hero.className = "idle-hero";
  const next = upNextItems()[0];
  const btn = document.createElement("button");
  btn.className = "idle-play";
  btn.innerHTML = PLAY_GLYPH;
  btn.title = next ? `Play \u201c${next.title || next.task}\u201d` : "Play";
  btn.disabled = !next;
  btn.addEventListener("click", () => transport("play"));
  const big = document.createElement("div");
  big.className = "idle-big";
  big.textContent = "Idle";
  const sub = document.createElement("div");
  sub.className = "idle-sub";
  sub.textContent = next ? `Press play to start \u201c${next.title || next.task}\u201d` : "Add a task to get started";
  hero.append(btn, big, sub);
  return hero;
}

function upNextCard() {
  const items = upNextItems();
  const card = document.createElement("button");
  card.className = "upnext";
  card.type = "button";
  card.addEventListener("click", () => setView("queue"));
  const left = document.createElement("div");
  left.className = "upnext-left";
  const label = document.createElement("div");
  label.className = "upnext-label";
  label.textContent = `Up next \u00b7 ${items.length} queued`;
  const next = document.createElement("div");
  next.className = "upnext-next";
  next.textContent = items.length ? (items[0].title || items[0].task) : "Queue is empty";
  left.append(label, next);
  const chev = document.createElement("span");
  chev.className = "upnext-chev";
  chev.innerHTML = "&#8250;";
  card.append(left, chev);
  return card;
}

function logTail(text, n) {
  const lines = text.split("\n").filter((l, i, arr) => l.length || i >= arr.length - 1);
  return lines.slice(-n).join("\n");
}

// --------------------------------------------------------------------------
// Queue screen
// --------------------------------------------------------------------------
function renderQueue() {
  const ul = $("queue");
  if (!ul) return;
  // A re-render replaces the row buttons the floating menu is anchored to, so
  // close it first rather than leave it pointing at a detached trigger.
  closeRowMenu();
  ul.innerHTML = "";
  // Chrome: the active queue's name (playlist or main) above the list.
  const plLabel = $("queue-playlist");
  if (plLabel) plLabel.textContent = state.activePlaylist || "Main queue";
  // Count the same "up next" set the Now screen shows (pending = not the live
  // track, not disabled) so the two queue counts always agree.
  const pending = upNextItems().length;
  $("queue-count").textContent = pending ? `(${pending})` : "";
  // Reflect the active sort mode on the header toggle.
  const sortBtn = $("queue-sort");
  if (sortBtn) {
    const byPriority = state.sortMode === "priority";
    sortBtn.classList.toggle("active", byPriority);
    sortBtn.setAttribute("aria-pressed", byPriority ? "true" : "false");
    sortBtn.title = byPriority
      ? "Sorted by priority — click for manual (drag) order"
      : "Manual order — click to sort by priority";
  }
  $("queue-empty").hidden = state.queue.length > 0;
  for (const item of state.queue) ul.append(queueItemRow(item));
}

// The right-hand block of a queue row: a status display sat just left of the
// timing. Both columns are fixed-width (see .q-status / .q-timing in the CSS)
// so that across every row the status displays share a right edge and the
// timings share a left edge, while the titles keep their shared left edge.
//   [ ……… status ][ timing ]
// The status mirrors the shared player vocabulary: the now-playing row reflects
// the live transport (running/paused); other rows show their latest run's
// outcome, or "Queued" when they've never run. The timing is the live phase
// clock while running, the last run's duration once finished, else blank.
function queueRowAside(item, isNow) {
  const aside = document.createElement("div");
  aside.className = "q-aside";

  const found = latestRecordFor(item.task);
  const rec = found ? found.rec : null;
  let status;
  if (isNow) status = state.player.state === "paused" ? "paused" : "running";
  else if (rec) status = rec.status;
  else status = "pending";

  const status_box = document.createElement("div");
  status_box.className = "q-status";
  status_box.append(statusPill(status));
  aside.append(status_box);

  const timing = document.createElement("div");
  timing.className = "q-timing";
  if (isNow) {
    timing.textContent = formatWhen(found ? found.run.started_at : null) || "now";
  } else if (rec) {
    timing.textContent = taskDuration(rec) || formatWhen(rec.finished_at || rec.started_at);
  }
  aside.append(timing);
  return aside;
}

// One queue row, shared by the Queue screen and the Now screen's embedded list.
function queueItemRow(item) {
  const li = document.createElement("li");
  li.className = "q-item";
  li.dataset.task = item.task;
  li.title = "Click to select · shift-click to multi-select · double-click to play/pause · Delete to remove";
  const isNow = runningHere() && item.task === state.player.now_playing;
  if (isNow) li.classList.add("now-playing");
  else if (item.task === state.selectedTask || item.task === state.player.cursor) li.classList.add("cursor");
  if (state.selectedTasks.has(item.task)) li.classList.add("selected");
  // Dim rows the active play-priority filter excludes: they stay visible (and
  // editable) but won't run until ALL or their level is reselected.
  if (!item.disabled && !isNow && !playPriorityAllows(item.priority)) {
    li.classList.add("out-of-scope");
  }

  // Drag handle — grab to reorder the execution queue. Dragging is meaningless
  // while the queue is sorted by priority (the order is computed), so the grip
  // is inert in that mode; the manual order is preserved underneath as the
  // tiebreak for equal priorities and is restored when priority sort is off.
  const byPriority = state.sortMode === "priority";
  const handle = document.createElement("span");
  handle.className = byPriority ? "q-grip disabled" : "q-grip";
  handle.title = byPriority ? "Manual reorder is off while sorting by priority" : "Drag to reorder";
  handle.setAttribute("aria-hidden", "true");
  handle.innerHTML = "&#8942;&#8942;";
  handle.draggable = !byPriority;
  handle.addEventListener("click", (e) => e.stopPropagation());
  if (!byPriority) wireDragHandlers(li, handle, item.task);

  // Spinner slot — every row reserves this space so titles never shift; only the
  // now-playing row animates it (the style-guide "refreshing" spin-ring).
  const spinner = document.createElement("span");
  spinner.className = isNow ? "q-spinner spinning" : "q-spinner";
  spinner.setAttribute("aria-hidden", "true");
  if (isNow) spinner.title = "Running";

  const main = document.createElement("div");
  main.className = "q-main";
  const title = document.createElement("div");
  title.className = "q-title";
  title.textContent = item.title || item.task;
  const file = document.createElement("div");
  file.className = "q-file";
  file.textContent = `${item.task}.md`;
  main.append(title, file);

  li.append(handle, spinner, main, queueRowAside(item, isNow));
  if (typeof item.priority === "number") {
    const chip = document.createElement("span");
    chip.className = "badge priority p" + item.priority;
    chip.textContent = "P" + item.priority;
    chip.title = `Priority ${item.priority} (0 = highest, 5 = lowest)`;
    li.append(chip);
  }
  if (item.evergreen) {
    const badge = document.createElement("span");
    badge.className = "badge evergreen";
    badge.textContent = "evergreen";
    li.append(badge);
  }
  if (item.disabled) {
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = "disabled";
    li.append(badge);
  }
  // Per-row "…" menu: the row's actions (add to playlist, get info, play
  // next/last, enable/disable, remove from playlist). Opening it selects the
  // row (so the menu acts on a known selection) without opening anything.
  li.append(queueRowMenuButton(item));

  // Queue-row gestures, per the media-player model:
  //   click                 → select only (move the cursor); never opens a pane
  //   shift-click           → extend the multi-selection
  //   option-click / Enter  → "get info": open the editable detail pane
  //   double-click          → invoke the transport play/pause toggle (play when
  //                           paused or stopped, pause when playing)
  li.addEventListener("click", (e) => {
    if (e.altKey) {
      openTaskDetail(item.task);
      return;
    }
    selectQueueTask(item.task, { extend: e.shiftKey });
  });
  li.addEventListener("dblclick", (e) => {
    if (e.altKey) return; // alt double-click is just two detail opens
    togglePlayPause();
  });
  return li;
}

// Select a queue task: move the server-side play cursor and update the
// multi-selection. A plain click selects just this task; a shift-click toggles
// it into (or out of) the existing selection so several tasks can be acted on
// at once from the "…" menu. Selecting never opens the detail pane — that's the
// menu's "Get info" (or option-click / Enter).
function selectQueueTask(task, { extend = false } = {}) {
  if (extend) {
    if (state.selectedTasks.has(task)) state.selectedTasks.delete(task);
    else state.selectedTasks.add(task);
  } else {
    state.selectedTasks = new Set([task]);
  }
  state.selectedTask = task;
  transport("select", { task });
  renderQueue();
  renderNow();
}

// The tasks the Delete key acts on: the whole multi-selection in queue order,
// falling back to the single cursor row. Empty when nothing is selected.
function deleteTargets() {
  if (state.selectedTasks.size) {
    return state.queue.map((i) => i.task).filter((t) => state.selectedTasks.has(t));
  }
  return state.selectedTask ? [state.selectedTask] : [];
}

// The tasks the "…" menu acts on: the whole multi-selection when the clicked
// row is part of it, otherwise just the clicked row (so the menu always has a
// sensible target even when nothing was pre-selected).
function menuTargets(task) {
  if (state.selectedTasks.has(task) && state.selectedTasks.size > 1) {
    // Preserve queue order so reorders (play next/last) are deterministic.
    return state.queue.map((i) => i.task).filter((t) => state.selectedTasks.has(t));
  }
  return [task];
}

// Glyph for the per-row actions menu trigger (three vertical dots).
const ELLIPSIS_GLYPH = "\u22EF"; // ⋯

// Build a row's "…" trigger. Clicking it selects the row (without extending an
// existing multi-selection unless the row is already in it) and pops the menu.
function queueRowMenuButton(item) {
  const btn = document.createElement("button");
  btn.className = "q-menu-btn";
  btn.title = "Actions";
  btn.setAttribute("aria-haspopup", "true");
  btn.setAttribute("aria-label", "Task actions");
  btn.textContent = ELLIPSIS_GLYPH;
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    // Capture the trigger's screen position before any re-render detaches it,
    // so the floating menu anchors to where the button actually is.
    const anchor = btn.getBoundingClientRect();
    // Acting on a row that isn't part of the current selection selects just it,
    // so the menu's target is unambiguous; a row already in the selection keeps
    // the whole set (the menu then acts on all of them).
    if (!state.selectedTasks.has(item.task)) {
      selectQueueTask(item.task);
    }
    openRowMenu(anchor, item);
  });
  return btn;
}

// The single floating row-actions menu. One element is reused across rows; it
// closes on outside click, Escape, scroll, or resize.
let rowMenuEl = null;
// The single nested flyout (e.g. "Priority ›"). Tracked separately so it can be
// opened/closed independently of its parent menu.
let rowSubmenuEl = null;

function closeRowSubmenu() {
  if (rowSubmenuEl) {
    rowSubmenuEl.remove();
    rowSubmenuEl = null;
  }
}

function closeRowMenu() {
  closeRowSubmenu();
  if (rowMenuEl) {
    rowMenuEl.remove();
    rowMenuEl = null;
  }
}

// Build the priority flyout's leaf items: P0..P5. When every target already
// shares a priority that level is marked current (a check). Choosing a level
// PATCHes each target (skipping the running task) and closes the menu.
function buildPrioritySubmenu(targets) {
  const current = sharedPriority(targets);
  const items = [];
  for (let level = 0; level <= 5; level++) {
    const tag = level === 0 ? " — highest" : level === 5 ? " — lowest" : "";
    items.push({
      label: `P${level}${tag}`,
      current: current === level,
      act: () => rowSetPriority(targets, level),
    });
  }
  return items;
}

// The shared priority across targets, or null when they differ / are unknown.
function sharedPriority(targets) {
  let seen = null;
  for (const t of targets) {
    const found = state.queue.find((i) => i.task === t);
    const p = found && typeof found.priority === "number" ? found.priority : null;
    if (p === null) return null;
    if (seen === null) seen = p;
    else if (seen !== p) return null;
  }
  return seen;
}

// Open the nested flyout to the right of its parent item (flipping left when it
// would overflow). Reuses the `.row-menu` look with a `.row-submenu` modifier.
function openRowSubmenu(anchorBtn, items) {
  closeRowSubmenu();
  const menu = document.createElement("div");
  menu.className = "row-menu row-submenu";
  menu.setAttribute("role", "menu");
  for (const spec of items) {
    const b = document.createElement("button");
    b.className = "row-menu-item";
    b.setAttribute("role", "menuitemradio");
    b.setAttribute("aria-checked", spec.current ? "true" : "false");
    if (spec.current) b.classList.add("checked");
    const lab = document.createElement("span");
    lab.textContent = spec.label;
    const mark = document.createElement("span");
    mark.className = "row-menu-check";
    mark.textContent = spec.current ? "\u2713" : "";
    b.append(mark, lab);
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      closeRowMenu();
      spec.act();
    });
    menu.append(b);
  }
  document.body.append(menu);
  rowSubmenuEl = menu;
  const r = anchorBtn.getBoundingClientRect();
  const mw = menu.offsetWidth || 180;
  let left = r.right - 2;
  if (left + mw > window.innerWidth - 8) left = Math.max(8, r.left - mw + 2);
  let top = r.top;
  const mh = menu.offsetHeight || 0;
  if (top + mh > window.innerHeight - 8) top = Math.max(8, window.innerHeight - mh - 8);
  menu.style.left = `${Math.round(left)}px`;
  menu.style.top = `${Math.round(top)}px`;
}

// Open the row-actions menu anchored to a screen rect (the trigger's position,
// captured before any re-render detaches the button). `item` is the row the
// menu was opened from; the actions apply to `menuTargets(item.task)` (the
// multi-selection when the row is part of it, else just the row).
function openRowMenu(anchor, item) {
  closeRowMenu();
  const targets = menuTargets(item.task);
  const many = targets.length > 1;
  const suffix = many ? ` (${targets.length})` : "";

  const menu = document.createElement("div");
  menu.className = "row-menu";
  menu.setAttribute("role", "menu");

  // Toggle wording follows the selection: when every target is already
  // disabled the action enables; otherwise it disables.
  const allDisabled = targets.every((t) => {
    const found = state.queue.find((i) => i.task === t);
    return found && found.disabled;
  });

  const items = [
    { label: "Add to playlist" + suffix, act: () => rowAddToPlaylist(targets) },
    { label: "Get info", act: () => openTaskDetail(item.task), disabled: many },
    { divider: true },
    { label: "Play next" + suffix, act: () => rowPlayNext(targets) },
    { label: "Play last" + suffix, act: () => rowPlayLast(targets) },
    { label: "Priority", submenu: () => buildPrioritySubmenu(targets) },
    { divider: true },
    {
      label: (allDisabled ? "Enable" : "Disable") + suffix,
      act: () => rowSetDisabled(targets, !allDisabled),
    },
    { label: "Remove from playlist" + suffix, act: () => rowRemove(targets) },
  ];

  for (const spec of items) {
    if (spec.divider) {
      const hr = document.createElement("div");
      hr.className = "row-menu-sep";
      menu.append(hr);
      continue;
    }
    const b = document.createElement("button");
    b.className = "row-menu-item";
    b.setAttribute("role", "menuitem");
    if (spec.submenu) {
      // A drill-in item: a label plus a "›" chevron. Hover or click opens the
      // nested flyout; the submenu itself owns the leaf actions.
      b.classList.add("has-submenu");
      b.setAttribute("aria-haspopup", "true");
      const lab = document.createElement("span");
      lab.textContent = spec.label;
      const chev = document.createElement("span");
      chev.className = "row-menu-chevron";
      chev.textContent = "\u203a";
      b.append(lab, chev);
      const open = (e) => {
        e.stopPropagation();
        openRowSubmenu(b, spec.submenu());
      };
      b.addEventListener("click", open);
      b.addEventListener("mouseenter", open);
    } else if (spec.disabled) {
      b.textContent = spec.label;
      b.disabled = true;
      // "Get info" opens one task's pane, so it's only offered for a single
      // target — disable it (with a hint) when several are selected.
      b.title = "Select a single task to view its info";
    } else {
      b.textContent = spec.label;
      // Moving onto a non-submenu item dismisses any open flyout so two menus
      // are never visible at once.
      b.addEventListener("mouseenter", closeRowSubmenu);
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        closeRowMenu();
        spec.act();
      });
    }
    menu.append(b);
  }

  document.body.append(menu);
  rowMenuEl = menu;
  positionRowMenu(menu, anchor);
}

// Position the floating menu under its anchor rect, nudged left so it stays
// within the viewport (the trigger sits at the right edge of a row).
function positionRowMenu(menu, r) {
  const mw = menu.offsetWidth || 200;
  let left = r.right - mw;
  if (left < 8) left = 8;
  let top = r.bottom + 6;
  const mh = menu.offsetHeight || 0;
  if (top + mh > window.innerHeight - 8) {
    top = Math.max(8, r.top - mh - 6); // flip above when it would overflow
  }
  menu.style.left = `${Math.round(left)}px`;
  menu.style.top = `${Math.round(top)}px`;
}

// ----- row-menu actions --------------------------------------------------- #

// Add to playlist → open the same playlist menu the chrome's add surface
// offers (the Add-from-playlist picker), so playlist membership is managed from
// one place rather than a second, divergent dialog.
function rowAddToPlaylist(_targets) {
  openAddFrom();
}

// Play next → move the target(s) to just after the now-playing track (or to the
// front when nothing is playing), preserving their relative order.
async function rowPlayNext(targets) {
  const order = state.queue.map((i) => i.task);
  const set = new Set(targets);
  const rest = order.filter((t) => !set.has(t));
  const now = state.player.now_playing;
  const at = now ? rest.indexOf(now) + 1 : 0;
  const next = [...rest.slice(0, at), ...targets, ...rest.slice(at)];
  await applyQueueOrder(next);
}

// Play last → move the target(s) to the end of the queue, preserving order.
async function rowPlayLast(targets) {
  const order = state.queue.map((i) => i.task);
  const set = new Set(targets);
  const next = [...order.filter((t) => !set.has(t)), ...targets];
  await applyQueueOrder(next);
}

// Persist a new queue order locally + server-side, reusing the drag-reorder
// path so the optimistic render and the PUT confirmation stay in one place.
async function applyQueueOrder(order) {
  const byTask = Object.fromEntries(state.queue.map((i) => [i.task, i]));
  state.queue = order.map((t) => byTask[t]).filter(Boolean);
  renderQueue();
  renderNow();
  await persistQueueOrder();
}

// Enable/disable → flip the `disabled` frontmatter for the target(s) via PATCH.
// The now-playing task can't be edited (its spec mustn't change under a live
// worker), so it's skipped with a note.
async function rowSetDisabled(targets, disabled) {
  let skipped = false;
  for (const task of targets) {
    if (task === state.player.now_playing) { skipped = true; continue; }
    const { ok, data } = await sendJSON(
      `/api/tasks/${encodeURIComponent(task)}`, "PATCH", { disabled });
    if (!ok) { alert((data && data.error) || "could not update task"); break; }
  }
  if (skipped) alert("The running task can't be changed — stop it first.");
  await loadQueue();
}

// Set priority → PATCH the `priority` frontmatter (0-5) for the target(s). The
// now-playing task is skipped (its spec mustn't change under a live worker);
// priority only affects ordering, so skipping it is harmless.
async function rowSetPriority(targets, level) {
  let skipped = false;
  for (const task of targets) {
    if (task === state.player.now_playing) { skipped = true; continue; }
    const { ok, data } = await sendJSON(
      `/api/tasks/${encodeURIComponent(task)}`, "PATCH", { priority: level });
    if (!ok) { alert((data && data.error) || "could not update task"); break; }
  }
  if (skipped) alert("The running task can't be changed — stop it first.");
  await loadQueue();
}

// Remove from playlist → delete the task file from the active queue only. A
// copy living in another playlist is a separate file and is untouched.
async function rowRemove(targets) {
  const many = targets.length > 1;
  const where = state.activePlaylist || "the main queue";
  const msg = many
    ? `Remove ${targets.length} tasks from ${where}? This deletes their files from this queue.`
    : `Remove "${targets[0]}.md" from ${where}? This deletes the file from this queue.`;
  if (!confirm(msg)) return;
  for (const task of targets) {
    if (task === state.player.now_playing) {
      alert("The running task can't be removed — stop it first.");
      continue;
    }
    const { ok, data } = await sendJSON(
      `/api/tasks/${encodeURIComponent(task)}`, "DELETE");
    if (!ok) { alert((data && data.error) || "could not remove task"); break; }
    state.selectedTasks.delete(task);
  }
  await loadQueue();
}

function cssEscape(s) {
  return window.CSS && CSS.escape ? CSS.escape(s) : String(s).replace(/"/g, '\\"');
}

// Arrow-key navigation over the queue, shared by the Queue and Now screens. The
// queue list already contains the now-executing item and the up-next items, so
// stepping through it covers all three. Unlike a click this never opens the
// detail pane — it just moves the selection cursor and keeps it in view.
function moveQueueSelection(delta) {
  const tasks = state.queue.map((i) => i.task);
  if (!tasks.length) return;
  let idx = tasks.indexOf(state.selectedTask);
  if (idx === -1) idx = delta > 0 ? -1 : 0;
  const task = tasks[Math.max(0, Math.min(tasks.length - 1, idx + delta))];
  state.selectedTask = task;
  state.selectedTasks = new Set([task]);
  transport("select", { task });
  renderNow();
  renderQueue();
  const row = document.querySelector(`.q-item[data-task="${cssEscape(task)}"]`);
  if (row && row.scrollIntoView) row.scrollIntoView({ block: "nearest" });
}

function anyModalOpen() {
  return [...document.querySelectorAll(".modal")].some((m) => !m.hidden);
}

// Up/Down move the queue selection on the Now and Queue screens; Enter opens the
// selected task's detail. Ignored while typing or with a modal open.
function onGlobalKeydown(e) {
  // Escape first dismisses an open row-actions menu (before anything else); a
  // nested flyout closes one level at a time.
  if (e.key === "Escape" && rowSubmenuEl) {
    e.preventDefault();
    closeRowSubmenu();
    return;
  }
  if (e.key === "Escape" && rowMenuEl) {
    e.preventDefault();
    closeRowMenu();
    return;
  }
  // Escape exits the detail pane back to where it was opened from (discards
  // unsaved edits / the new-task draft).
  if (e.key === "Escape" && state.view === "detail" && !anyModalOpen()) {
    e.preventDefault();
    closeDetailScreen();
    return;
  }
  const isDelete = e.key === "Delete" || e.key === "Backspace";
  if (e.key !== "ArrowUp" && e.key !== "ArrowDown" && e.key !== "Enter" && !isDelete) return;
  const tag = ((e.target && e.target.tagName) || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select" ||
      (e.target && e.target.isContentEditable)) return;
  if (anyModalOpen()) return;
  if (state.view !== "queue" && state.view !== "now") return;
  if (isDelete) {
    // Delete removes the selected task(s) from the active queue after a
    // simple ok/cancel confirmation (the row "x" icon is gone).
    const targets = deleteTargets();
    if (!targets.length) return;
    e.preventDefault();
    rowRemove(targets);
    return;
  }
  if (e.key === "Enter") {
    if (state.selectedTask) { e.preventDefault(); openTaskDetail(state.selectedTask); }
    return;
  }
  e.preventDefault();
  moveQueueSelection(e.key === "ArrowDown" ? 1 : -1);
}

// Drag-to-reorder: the grip starts the drag; the <li> is the drop target. We
// reorder state.queue locally for instant feedback, re-render, then persist the
// new order to .tasks/config.json via PUT /api/queue/order.
let dragTask = null;

function wireDragHandlers(li, handle, task) {
  handle.addEventListener("dragstart", (e) => {
    dragTask = task;
    li.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    try { e.dataTransfer.setData("text/plain", task); } catch { /* ignore */ }
  });
  handle.addEventListener("dragend", () => {
    li.classList.remove("dragging");
    for (const el of document.querySelectorAll(".q-item.drop-target")) {
      el.classList.remove("drop-target");
    }
    dragTask = null;
  });
  li.addEventListener("dragover", (e) => {
    if (dragTask === null || dragTask === task) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    li.classList.add("drop-target");
  });
  li.addEventListener("dragleave", () => li.classList.remove("drop-target"));
  li.addEventListener("drop", (e) => {
    e.preventDefault();
    li.classList.remove("drop-target");
    if (dragTask === null || dragTask === task) return;
    moveQueueItem(dragTask, task);
  });
}

function moveQueueItem(fromTask, toTask) {
  const items = state.queue.slice();
  const from = items.findIndex((i) => i.task === fromTask);
  const to = items.findIndex((i) => i.task === toTask);
  if (from === -1 || to === -1) return;
  const [moved] = items.splice(from, 1);
  items.splice(to, 0, moved);
  state.queue = items;
  renderQueue();
  renderNow();
  persistQueueOrder();
}

async function persistQueueOrder() {
  const order = state.queue.map((i) => i.task);
  const { ok, data } = await sendJSON("/api/queue/order", "PUT", { order });
  if (!ok) {
    alert((data && data.error) || "could not save queue order");
    loadQueue();
    return;
  }
  // The server echoes the persisted order as { order: [...stems] }; re-sort the
  // local queue to match so the rendered order is authoritative.
  if (data && Array.isArray(data.order)) {
    const byTask = Object.fromEntries(state.queue.map((i) => [i.task, i]));
    state.queue = data.order.map((t) => byTask[t]).filter(Boolean);
    renderQueue();
    renderNow();
  }
}

// --------------------------------------------------------------------------
// History screen ("recently played")
// --------------------------------------------------------------------------
function renderHistory() {
  const wrap = $("history");
  if (!wrap) return;
  wrap.innerHTML = "";
  const rows = [];
  for (const run of state.runs) {
    for (const task of run.tasks) rows.push({ run, task });
  }
  $("history-empty").hidden = rows.length > 0;
  for (const { run, task } of rows.slice(0, 200)) {
    wrap.append(historyRow(run, task));
  }
}

// Open / close the Statistics sub-view (a takeover of the History tab).
function openStats() {
  setView("stats");
}
function closeStats() {
  setView("history");
}

// --------------------------------------------------------------------------
// Statistics screen — graphical summary of the active queue's run history.
// --------------------------------------------------------------------------
// Aggregate the loaded run records into the headline figures the Stats page
// graphs: total tasks, average duration, success rate, and a per-failure-mode
// breakdown. Terminal (finished) tasks only — a still-running task has no
// outcome or duration to summarise.
function computeStats() {
  const tasks = [];
  for (const run of state.runs) {
    for (const task of run.tasks) tasks.push(task);
  }
  const terminal = tasks.filter((t) => t.status && t.status !== "running");
  const completed = terminal.filter((t) => t.status === "completed");

  const durations = [];
  for (const t of terminal) {
    const secs = taskDurationSecs(t);
    if (secs !== null) durations.push(secs);
  }
  const avgSecs = durations.length
    ? durations.reduce((a, b) => a + b, 0) / durations.length
    : null;

  // Failure modes: classified `failure_kind` for every non-completed terminal
  // task, falling back to a generic bucket when the engine left it unset.
  const failures = {};
  for (const t of terminal) {
    if (t.status === "completed") continue;
    const kind = t.failure_kind || (t.status === "stopped" ? "stopped" : "unknown");
    failures[kind] = (failures[kind] || 0) + 1;
  }
  const failureRows = Object.entries(failures)
    .map(([kind, count]) => ({ kind, count }))
    .sort((a, b) => b.count - a.count);

  // A completed task lands at least one squash commit (its `commit_sha`); a task
  // that landed more than once (an initial run plus a later resolve/recovery)
  // carries a comma-separated list of shas, so count each one. Lines of code is
  // the churn those commits recorded (`loc`), already excluding comments, build
  // files, and docs at the engine. Tasks that landed nothing (no changes,
  // failures) carry no sha and don't count.
  let commits = 0;
  let loc = 0;
  for (const t of completed) {
    commits += commitShaList(t.commit_sha).length;
    if (typeof t.loc === "number" && Number.isFinite(t.loc)) loc += t.loc;
  }

  return {
    total: terminal.length,
    completed: completed.length,
    avgSecs,
    successRate: terminal.length ? completed.length / terminal.length : null,
    failureRows,
    failureTotal: terminal.length - completed.length,
    commits,
    loc,
  };
}

// A task's duration in seconds (engine-recorded total preferred), or null when
// it can't be determined — mirrors taskDuration() but returns a number to average.
function taskDurationSecs(task) {
  if (!task) return null;
  const total = task.timings && task.timings.total;
  if (total !== null && total !== undefined && !Number.isNaN(Number(total))) {
    return Number(total);
  }
  if (task.started_at && task.finished_at) {
    const ms = Date.parse(task.finished_at) - Date.parse(task.started_at);
    if (ms >= 0) return ms / 1000;
  }
  return null;
}

function renderStats() {
  const titleEl = $("stats-title");
  if (titleEl) {
    titleEl.textContent = state.activePlaylist
      ? `Statistics \u00b7 ${state.activePlaylist}`
      : "Statistics \u00b7 main queue";
  }
  const body = $("stats-body");
  if (!body) return;
  body.innerHTML = "";

  const s = computeStats();
  const empty = $("stats-empty");
  if (empty) empty.hidden = s.total > 0;
  if (!s.total) return;

  // Headline figures as a stat-card row.
  const cards = document.createElement("div");
  cards.className = "stat-cards";
  cards.append(
    statCard("Tasks", String(s.total), "completed runs in history"),
    statCard("Avg time", s.avgSecs !== null ? (formatSecs(s.avgSecs) || "—") : "—", "per task"),
    statCard(
      "Success",
      s.successRate !== null ? `${Math.round(s.successRate * 100)}%` : "—",
      `${s.completed} of ${s.total}`,
    ),
    statCard("Commits", String(s.commits), "landed on main"),
    statCard("Lines of code", formatCount(s.loc), "code churned (ex. comments, build, docs)"),
  );
  body.append(cards);

  // Success-rate bar: completed vs failed, as a proportional graph.
  const failed = s.total - s.completed;
  const outcome = statSection("Outcomes");
  outcome.append(
    proportionBar([
      { label: "Completed", count: s.completed, cls: "stat-fill-ok" },
      { label: "Failed", count: failed, cls: "stat-fill-err" },
    ], s.total),
  );
  body.append(outcome);

  // Breakdown by failure mode: one horizontal bar per classified kind.
  const breakdown = statSection("Failure modes");
  if (!s.failureRows.length) {
    const none = document.createElement("p");
    none.className = "stat-none";
    none.textContent = "No failures recorded.";
    breakdown.append(none);
  } else {
    const max = Math.max(...s.failureRows.map((r) => r.count));
    const list = document.createElement("div");
    list.className = "stat-bars";
    for (const row of s.failureRows) {
      list.append(failureBar(row.kind, row.count, max));
    }
    breakdown.append(list);
  }
  body.append(breakdown);
}

function statCard(label, value, sub) {
  const card = document.createElement("div");
  card.className = "stat-card";
  const v = document.createElement("div");
  v.className = "stat-card-value";
  v.textContent = value;
  const l = document.createElement("div");
  l.className = "stat-card-label";
  l.textContent = label;
  card.append(v, l);
  if (sub) {
    const s = document.createElement("div");
    s.className = "stat-card-sub";
    s.textContent = sub;
    card.append(s);
  }
  return card;
}

function statSection(title) {
  const sec = document.createElement("div");
  sec.className = "stat-section";
  const h = document.createElement("h3");
  h.className = "stat-section-title";
  h.textContent = title;
  sec.append(h);
  return sec;
}

// A single 100%-wide bar split into proportional, labelled segments.
function proportionBar(segments, total) {
  const wrap = document.createElement("div");
  wrap.className = "stat-proportion";
  const track = document.createElement("div");
  track.className = "stat-proportion-track";
  for (const seg of segments) {
    if (!seg.count) continue;
    const part = document.createElement("div");
    part.className = `stat-proportion-part ${seg.cls}`;
    part.style.flexGrow = String(seg.count);
    part.title = `${seg.label}: ${seg.count}`;
    track.append(part);
  }
  const legend = document.createElement("div");
  legend.className = "stat-legend";
  for (const seg of segments) {
    const item = document.createElement("span");
    item.className = "stat-legend-item";
    const dot = document.createElement("span");
    dot.className = `stat-dot ${seg.cls}`;
    const pct = total ? Math.round((seg.count / total) * 100) : 0;
    item.append(dot, document.createTextNode(`${seg.label} ${seg.count} (${pct}%)`));
    legend.append(item);
  }
  wrap.append(track, legend);
  return wrap;
}

// One labelled horizontal bar for a failure mode, scaled against the busiest.
function failureBar(kind, count, max) {
  const row = document.createElement("div");
  row.className = "stat-bar-row";
  const label = document.createElement("span");
  label.className = "stat-bar-label";
  label.textContent = FAILURE_LABELS[kind] || kind;
  const track = document.createElement("div");
  track.className = "stat-bar-track";
  const fill = document.createElement("div");
  fill.className = `stat-bar-fill fail-${kind}`;
  fill.style.width = `${max ? (count / max) * 100 : 0}%`;
  track.append(fill);
  const value = document.createElement("span");
  value.className = "stat-bar-value";
  value.textContent = String(count);
  row.append(label, track, value);
  return row;
}

function historyRow(run, task) {
  const row = document.createElement("button");
  row.className = "hrow";
  row.type = "button";
  row.addEventListener("click", () => openHistoryDetail(run, task));

  const pill = statusPill(task.status || "running");
  pill.classList.add("hrow-pill");

  const main = document.createElement("div");
  main.className = "hrow-main";
  const title = document.createElement("div");
  title.className = "hrow-title";
  title.textContent = task.title || task.task;
  if (run.playlist) {
    const tag = document.createElement("span");
    tag.className = "hrow-tag";
    tag.textContent = run.playlist;
    title.append(tag);
  }
  // The target repo this run ran against (workspace-relative child name), shown
  // as a neutral tag beside the title; absent on runs that predate the column.
  const repo = task.repo || run.repo;
  if (repo) {
    const rtag = document.createElement("span");
    rtag.className = "hrow-tag hrow-repo";
    rtag.textContent = repo;
    rtag.title = `Target repo: ${repo}`;
    title.append(rtag);
  }
  const meta = document.createElement("div");
  meta.className = "hrow-meta";
  const badge = task.status === "error" ? failureBadge(task.failure_kind) : null;
  if (badge) meta.append(badge);
  const metaText = document.createElement("span");
  metaText.className = "hrow-meta-text";
  metaText.textContent = task.result_line || `${task.task}.md`;
  meta.append(metaText);
  main.append(title, meta);

  const aside = document.createElement("div");
  aside.className = "hrow-aside";
  const dur = document.createElement("div");
  dur.className = "hrow-dur";
  // Per-task duration/"when" — not the whole run's window (a 9-task run would
  // otherwise show the same 1h+ duration and finish time on every row).
  dur.textContent = taskDuration(task) || formatDuration(run.started_at, run.finished_at);
  const when = document.createElement("div");
  when.className = "hrow-when";
  when.textContent = formatWhen(task.finished_at || task.started_at || run.finished_at || run.started_at);
  aside.append(dur, when);

  row.append(pill, main, aside);
  return row;
}

function formatDuration(start, end) {
  if (!start || !end) return "";
  const ms = Date.parse(end) - Date.parse(start);
  if (!(ms >= 0)) return "";
  return formatElapsed(ms);
}

// The history commit field is a comma-separated list of every sha a task landed
// (one for a single land, more when a resolve/recovery landed it again). Split it
// into individual shas, tolerating null/empty.
function commitShaList(commitSha) {
  if (!commitSha) return [];
  return String(commitSha)
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

// A single task's wall-clock duration: prefer the engine-recorded total
// (worker + validate + commit + overhead), else its own start→finish span.
function taskDuration(task) {
  if (!task) return "";
  const total = task.timings && task.timings.total;
  if (total !== null && total !== undefined && !Number.isNaN(Number(total))) {
    return formatSecs(total) || "";
  }
  return formatDuration(task.started_at, task.finished_at);
}

// Human-readable elapsed from milliseconds: "12s" / "14m 03s" / "1h 04m".
function formatElapsed(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${String(m % 60).padStart(2, "0")}m`;
}

// Format a per-phase timing (seconds, possibly fractional) for the detail sheet.
function formatSecs(value) {
  if (value === null || value === undefined) return null;
  const n = Number(value);
  if (Number.isNaN(n)) return null;
  if (n < 60) return `${n.toFixed(n < 10 ? 1 : 0)}s`;
  return formatElapsed(n * 1000);
}

// A whole-number count with thousands separators (e.g. 12345 → "12,345").
function formatCount(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "0";
  return Math.round(n).toLocaleString("en-US");
}

// Update the live "<phase> · elapsed" clock in place without re-rendering.
function tickElapsed() {
  const el = document.getElementById("now-elapsed");
  if (!el || !el.dataset.since) return;
  const ms = Date.now() - Date.parse(el.dataset.since);
  if (ms >= 0) el.textContent = `${el.dataset.phase} \u00b7 ${formatElapsed(ms)}`;
}

function formatWhen(iso) {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const diff = Date.now() - t;
  const min = Math.round(diff / 60000);
  if (min < 1) return "just now";
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  return new Date(t).toLocaleDateString();
}

async function clearCompleted() {
  if (!state.runs.length) return;
  if (!confirm(
    "Clear all run records? This deletes their logs from disk, and each task's "
    + "preserved branch/worktree when no surviving run still needs it."
  )) return;
  const { ok, data } = await sendJSON("/api/runs", "DELETE");
  if (!ok) {
    alert(data.error || "could not clear runs");
    return;
  }
  state.logCache = {};
  loadRuns();
}

async function resolveTask(runId, task, btn) {
  if (btn) { btn.disabled = true; btn.textContent = "Resolving\u2026"; }
  const { ok, data } = await sendJSON(
    `/api/runs/${encodeURIComponent(runId)}/${encodeURIComponent(task)}/resolve`,
    "POST",
  );
  if (!ok) {
    alert((data && data.error) || "could not resolve task");
    if (btn) { btn.disabled = false; btn.innerHTML = resolveButtonInner(); }
    return;
  }
  // The resolve runs as a tracked job — jump to Now to watch it land.
  clearDetailState();
  loadRuns();
  setView("now");
}

// Inline bot glyph + label for the Resolve button (shared by the markup and the
// re-enable-on-error path).
function resolveButtonInner() {
  return (
    '<svg class="bot-icon" viewBox="0 0 24 24" width="15" height="15" ' +
    'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
    'stroke-linejoin="round" aria-hidden="true">' +
    '<rect x="4" y="8" width="16" height="11" rx="2"></rect>' +
    '<path d="M12 8V4"></path><circle cx="12" cy="3" r="1"></circle>' +
    '<path d="M2 13v3"></path><path d="M22 13v3"></path>' +
    '<circle cx="9" cy="13" r="1"></circle><circle cx="15" cy="13" r="1"></circle>' +
    '</svg><span>Resolve</span>'
  );
}

async function deleteRun(runId) {
  const { ok, data } = await sendJSON(`/api/runs/${encodeURIComponent(runId)}`, "DELETE");
  if (!ok) {
    alert(data.error || "could not delete run");
    return false;
  }
  for (const k of Object.keys(state.logCache)) {
    if (k.startsWith(runId + "/")) delete state.logCache[k];
  }
  loadRuns();
  return true;
}

// --------------------------------------------------------------------------
// Playlists screen (alternate, self-contained queues)
// --------------------------------------------------------------------------
// A playlist is a directory-backed queue under .tasks/<name>. Selecting one
// makes it the active queue: all controls, the Queue, and History operate on
// it. The main `.tasks/` queue is shown first as the "library" row and is
// selectable exactly like any other playlist (it just can't be deleted). The
// "Home" bottom tab is a shortcut to the same thing.
// The play-state spinner for a playlist row. Mirrors the queue view's
// now-playing spinner exactly (same .q-spinner style, same leading slot): every
// row reserves the space so names never shift, and the slot only animates when
// that queue has a live run — so you can see which playlists are playing from
// the Playlists screen.
function playlistSpinner(running) {
  const spinner = document.createElement("span");
  spinner.className = running ? "q-spinner spinning" : "q-spinner";
  spinner.setAttribute("aria-hidden", "true");
  if (running) spinner.title = "Running";
  return spinner;
}

function renderPlaylists() {
  const ul = $("playlists");
  if (!ul) return;
  ul.innerHTML = "";
  const items = state.playlists;
  // The library always counts as one selectable queue alongside the playlists.
  const total = items.length + 1;
  $("playlists-count").textContent = `(${total})`;
  // The library row is always present, so the list is never empty.
  $("playlists-empty").hidden = true;

  ul.append(libraryRow());
  for (const pl of items) ul.append(playlistRow(pl));
}

// The "library" row: the main `.tasks/` queue, rendered as a playlist. Selecting
// it activates the main queue (playlist = null), just like the Home tab.
function libraryRow() {
  const li = document.createElement("li");
  li.className = "pl-item pl-library";
  const active = state.activePlaylist === null;
  if (active) li.classList.add("active");
  li.addEventListener("click", () => activatePlaylist(null, "queue"));

  li.append(playlistSpinner(isQueueRunning(null)));

  const main = document.createElement("div");
  main.className = "pl-main";
  const name = document.createElement("div");
  name.className = "pl-name";
  name.textContent = "library";
  const tag = document.createElement("span");
  tag.className = "pl-tag";
  tag.textContent = "main queue";
  name.append(tag);
  const meta = document.createElement("div");
  meta.className = "pl-meta";
  const n = state.libraryCount;
  const count = `${n} ${n === 1 ? "task" : "tasks"}`;
  meta.textContent = active ? `${count} \u00b7 active queue` : count;
  main.append(name, meta);
  li.append(main);

  // The chevron switches to the main queue and drops straight into its Queue.
  const chev = document.createElement("button");
  chev.className = "upnext-chev pl-chev";
  chev.innerHTML = "&#8250;";
  chev.title = "Open the main queue";
  chev.addEventListener("click", (e) => {
    e.stopPropagation();
    activatePlaylist(null, "queue");
  });
  li.append(chev);
  return li;
}

function playlistRow(pl) {
  const li = document.createElement("li");
  li.className = "pl-item";
  const active = state.activePlaylist === pl.name;
  if (active) li.classList.add("active");
  li.addEventListener("click", () => activatePlaylist(pl.name, "queue"));

  li.append(playlistSpinner(isQueueRunning(pl.name)));

  const main = document.createElement("div");
  main.className = "pl-main";
  const name = document.createElement("div");
  name.className = "pl-name";
  name.textContent = pl.name;
  const meta = document.createElement("div");
  meta.className = "pl-meta";
  const count = `${pl.task_count} ${pl.task_count === 1 ? "task" : "tasks"}`;
  meta.textContent = active ? `${count} \u00b7 active queue` : count;
  main.append(name, meta);
  li.append(main);

  const del = document.createElement("button");
  del.className = "pl-del";
  del.title = "Delete this playlist (and its tasks)";
  del.innerHTML = "&#10005;";
  del.addEventListener("click", (e) => {
    e.stopPropagation();
    deletePlaylist(pl.name);
  });
  li.append(del);

  // The chevron switches to this playlist and drops straight into its Queue.
  const chev = document.createElement("button");
  chev.className = "upnext-chev pl-chev";
  chev.innerHTML = "&#8250;";
  chev.title = "Open this playlist's queue";
  chev.addEventListener("click", (e) => {
    e.stopPropagation();
    activatePlaylist(pl.name, "queue");
  });
  li.append(chev);
  return li;
}

// Fill the add-queue repo dropdown from the known-repos snapshot, keeping the
// leading "inherit / none" option. Best-effort: an empty snapshot just leaves
// the single inherit option.
function fillPlaylistRepoOptions() {
  const select = $("playlist-repo");
  if (!select) return;
  const known = (state.repos && Array.isArray(state.repos.repos))
    ? state.repos.repos.map((r) => r.name)
    : [];
  select.innerHTML = "";
  const blank = document.createElement("option");
  blank.value = "";
  blank.textContent = "— inherit / none —";
  select.append(blank);
  for (const name of known) {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    select.append(opt);
  }
  select.value = "";
}

function openPlaylistCreate() {
  $("playlist-name").value = "";
  if ($("playlist-validate")) $("playlist-validate").value = "";
  if ($("playlist-auto-resolve")) $("playlist-auto-resolve").value = "";
  fillPlaylistRepoOptions();
  $("playlist-error").hidden = true;
  $("playlist-modal").hidden = false;
  $("playlist-name").focus();
}

// Collect the queue-specific options from the add-queue form. Only fields the
// operator actually set are returned, so unset fields inherit the main queue's
// value rather than being written as blanks.
function collectPlaylistConfig() {
  const out = {};
  const repo = $("playlist-repo") ? $("playlist-repo").value : "";
  if (repo) out.repo = repo;
  // Validate is intentionally not trimmed away: a single space is the explicit
  // "disable validation" sentinel the backend understands.
  if ($("playlist-validate")) {
    const validate = $("playlist-validate").value;
    if (validate !== "") out.validate = validate;
  }
  const ar = $("playlist-auto-resolve") ? $("playlist-auto-resolve").value : "";
  if (ar) out.auto_resolve = ar;
  return out;
}

async function savePlaylist() {
  const name = $("playlist-name").value.trim();
  if (!name) {
    $("playlist-error").textContent = "name is required";
    $("playlist-error").hidden = false;
    return;
  }
  const { ok, data } = await sendJSON("/api/playlists", "POST", { name });
  if (!ok) {
    $("playlist-error").textContent = (data && data.error) || "could not create playlist";
    $("playlist-error").hidden = false;
    return;
  }
  // Persist the queue-specific options against the freshly-created queue. The
  // endpoint is server-only, so a manager backend (which lacks it) simply skips
  // this step — the queue is still created with its defaults.
  const config = collectPlaylistConfig();
  if (data && data.name && Object.keys(config).length) {
    const res = await sendJSON(
      `/api/queue/config?queue=${encodeURIComponent(data.name)}`, "PUT", config,
    );
    if (!res.ok && res.data && res.data.error) {
      $("playlist-error").textContent = res.data.error;
      $("playlist-error").hidden = false;
      return;
    }
  }
  $("playlist-modal").hidden = true;
  await loadPlaylists();
  // Activating a freshly-created playlist drops you straight into its queue.
  if (data && data.name) activatePlaylist(data.name, "queue");
}

async function deletePlaylist(name) {
  if (!confirm(`Delete playlist "${name}"? This removes .tasks/${name}/ and all its tasks and runs.`)) return;
  const { ok, data } = await sendJSON(`/api/playlists/${encodeURIComponent(name)}`, "DELETE");
  if (!ok) {
    alert((data && data.error) || "could not delete playlist");
    return;
  }
  await loadPlaylists();
  // Deleting the active playlist drops back to main; resync focus + per-queue
  // states from the server, then reflect the (now focused) queue.
  try {
    const agg = await getJSON("/api/state");
    if (agg && agg.queues) {
      for (const [k, st] of Object.entries(agg.queues)) ingestQueueState(k, st);
    }
    syncActivePlaylist(agg && agg.active_playlist);
  } catch { /* keep current focus on a transient error */ }
  applyFocusedState();
  if (state.activePlaylist === null) await Promise.all([loadQueue(), loadRuns()]);
}

// The editable settings inside the detail pane's SETTINGS panel: a single
// wrapping row holding the on/off switches (Enabled, Evergreen, Auto-merge,
// Draft) as one segmented control, the model dropdown, and the priority
// segmented control (P0–P5) — all in line. Toggling a switch or changing the
// model/priority buffers the change into `state.detailDraft` — nothing is
// persisted until Save. When the pane is too narrow for one line the controls
// wrap into a left-aligned two-column flow (switches / model / priority).
// Edits are blocked while the task is the live track (its spec mustn't change
// under a running worker).
function settingsControls(brief, draft, rerender, locked) {
  const wrap = document.createElement("div");

  const row = document.createElement("div");
  row.className = "settings-row";

  const seg = document.createElement("div");
  seg.className = "segmented";
  seg.setAttribute("role", "group");
  seg.setAttribute("aria-label", "Task switches");
  const switches = [
    ["Enabled", () => !draft.disabled, (on) => { draft.disabled = !on; }],
    ["Evergreen", () => !!draft.evergreen, (on) => { draft.evergreen = on; }],
    ["Auto-merge", () => !!draft.automerge, (on) => { draft.automerge = on; }],
    ["Draft", () => !!draft.draft, (on) => { draft.draft = on; }],
  ];
  for (const [label, getOn, setOn] of switches) {
    const seg_btn = document.createElement("button");
    seg_btn.type = "button";
    seg_btn.className = "seg-opt";
    seg_btn.textContent = label;
    const on = getOn();
    seg_btn.classList.toggle("on", on);
    seg_btn.setAttribute("aria-pressed", on ? "true" : "false");
    seg_btn.disabled = locked;
    seg_btn.addEventListener("click", () => {
      setOn(!getOn());
      if (typeof rerender === "function") rerender();
    });
    seg.append(seg_btn);
  }
  row.append(
    seg,
    modelSelect(brief, draft, locked),
    prioritySegment(draft, rerender, locked),
    repoOverride(draft, locked),
  );
  wrap.append(row);

  if (locked) {
    const note = document.createElement("div");
    note.className = "ctl-note";
    note.textContent = "Stop the run to edit the playing task.";
    wrap.append(note);
  }
  return wrap;
}

// Priority picker: a P0–P5 segmented control (P0 = highest, P5 = lowest) reusing
// the same `.segmented` / `.seg-opt` pattern as the switches, sitting in line
// with them and the model dropdown. The chosen level is buffered into
// `draft.priority`; it drives the queue's priority sort. The per-button title
// carries the highest/lowest hint, so no separate caption or legend is shown.
function prioritySegment(draft, rerender, locked) {
  const seg = document.createElement("div");
  seg.className = "segmented";
  seg.setAttribute("role", "group");
  seg.setAttribute("aria-label", "Task priority (P0 highest, P5 lowest)");
  const current = typeof draft.priority === "number" ? draft.priority : 5;
  for (let level = 0; level <= 5; level++) {
    const seg_btn = document.createElement("button");
    seg_btn.type = "button";
    seg_btn.className = "seg-opt";
    seg_btn.textContent = "P" + level;
    seg_btn.title = level === 0 ? "P0 — highest" : level === 5 ? "P5 — lowest" : "P" + level;
    const on = current === level;
    seg_btn.classList.toggle("on", on);
    seg_btn.setAttribute("aria-pressed", on ? "true" : "false");
    seg_btn.disabled = locked;
    seg_btn.addEventListener("click", () => {
      draft.priority = level;
      if (typeof rerender === "function") rerender();
    });
    seg.append(seg_btn);
  }
  return seg;
}

// The model dropdown, right-aligned in line with the segmented switches. Its
// value is buffered into the draft; "Default" clears the pinned model so the
// task inherits the config default.
function modelSelect(brief, draft, locked) {
  const options = brief.model_options || [];
  const effectiveModel = (brief.frontmatter && brief.frontmatter.model) || "";
  const wrap = document.createElement("label");
  wrap.className = "model-select";
  const span = document.createElement("span");
  span.className = "model-label";
  span.textContent = "Model";
  const select = document.createElement("select");
  select.className = "ctl-select";
  select.disabled = locked;
  const current = draft.model || "default";
  const values = ["default", ...options];
  if (current !== "default" && !values.includes(current)) values.push(current);
  for (const v of values) {
    const opt = document.createElement("option");
    opt.value = v;
    opt.textContent = v === "default"
      ? (current === "default" && effectiveModel ? `Default (${effectiveModel})` : "Default")
      : v;
    select.append(opt);
  }
  select.value = current;
  select.addEventListener("change", () => { draft.model = select.value; });
  wrap.append(span, select);
  return wrap;
}

// The optional per-task target-repo override, in line with the model dropdown
// and populated from `/api/repos`. The empty "Inherit" choice clears the
// override so the task uses its queue's default repo; a value pins this task to
// that repo regardless of the queue default.
function repoOverride(draft, locked) {
  const wrap = document.createElement("label");
  wrap.className = "model-select repo-override";
  const span = document.createElement("span");
  span.className = "model-label";
  span.textContent = "Repo";
  const select = repoSelect(draft.repo, "Inherit");
  select.disabled = locked;
  select.addEventListener("change", () => { draft.repo = select.value; });
  wrap.append(span, select);
  return wrap;
}

// Seed a fresh edit buffer from a freshly-read brief. `model` carries the raw,
// file-only pin ("default" when the file inherits the config default) so the
// dropdown round-trips without forcing a model onto an inheriting task.
function draftFromBrief(brief) {
  const raw = brief.frontmatter_raw || {};
  return {
    // A new task has no file behind it (brief.task is null) and no title yet, so
    // its draft title must start as an empty string — never `undefined`/`null`,
    // which an <input>.value would render as the literal text "undefined".
    title: brief.title || brief.task || "",
    body: brief.body || "",
    disabled: !!brief.disabled,
    evergreen: !!brief.evergreen,
    automerge: !!(brief.frontmatter && brief.frontmatter.automerge),
    draft: !!(brief.frontmatter && brief.frontmatter.draft),
    model: raw.model || "default",
    priority: (brief.frontmatter && typeof brief.frontmatter.priority === "number")
      ? brief.frontmatter.priority
      : 5,
    // The per-task target-repo override, file-only (empty = inherit the queue
    // default). Read from the raw frontmatter so an inheriting task stays empty.
    repo: raw.repo || "",
  };
}

// --------------------------------------------------------------------------
// Task-brief detail view (deep-linkable)
// --------------------------------------------------------------------------
// The brief detail reflects the *selected* task, falling back to the *running*
// task when nothing is selected. It shows the task file's brief (title, body,
// frontmatter) plus live status/log when that task is the one playing. Opened by
// option-clicking a queue row, and deep-linked via the `#task=<id>` URL hash.

// Resolve which task the brief should show, honouring the selected→running
// fallback. Returns a task id or null.
function detailTarget() {
  return state.player.cursor || state.player.now_playing || null;
}

// Latest run record for a task across history (most recent first), or null.
function latestRecordFor(task) {
  for (const run of state.runs) {
    const rec = run.tasks.find((t) => t.task === task);
    if (rec) return { run, rec };
  }
  return null;
}

// Open the task-detail pane for a task. Resolves the selected/running fallback
// when no task is given. A task that has left the queue (completed and removed)
// has no editable spec, so it falls back to the read-only history run sheet.
async function openTaskDetail(task) {
  const target = task || detailTarget();
  if (!target) {
    alert("Nothing selected. Click a queue item first, or start a task.");
    return;
  }
  let brief;
  try {
    brief = await getJSON(`/api/tasks/${encodeURIComponent(target)}`);
  } catch {
    brief = null;
  }
  if (!brief || brief.error) {
    const found = latestRecordFor(target);
    if (found) { openHistoryDetail(found.run, found.rec); return; }
    alert((brief && brief.error) || "task not found");
    return;
  }
  openDetailScreen(target);
}

// ----- shared detail-pane building blocks (editable + history) -----------

// The FILE / STATUS header that opens both flavours of the detail pane.
function detailStatusHead(task, status) {
  return metaGrid([["File", `${task}.md`], ["Status", stateLabel(status)]]);
}

// The RUN DETAILS metaGrid pairs for a run + its task record: per-task window,
// per-phase timings (when instrumented), commit landing, and who launched it.
function runDetailPairs(run, rec) {
  const pairs = [
    ["Started", rec.started_at || run.started_at ? new Date(rec.started_at || run.started_at).toLocaleString() : "—"],
    ["Finished", rec.finished_at || run.finished_at ? new Date(rec.finished_at || run.finished_at).toLocaleString() : "—"],
    ["Duration", taskDuration(rec) || formatDuration(run.started_at, run.finished_at) || "—"],
  ];
  // The target repo the run ran against (workspace-relative child name). Carried
  // on the task record or the run; older runs that predate the column omit it.
  const repo = rec.repo || run.repo;
  if (repo) pairs.push(["Repo", repo]);
  const t = rec.timings;
  if (t && typeof t === "object") {
    for (const [key, label] of [["worker", "Worker"], ["validate", "Validate"], ["commit", "Commit"]]) {
      const v = formatSecs(t[key]);
      if (v !== null) pairs.push([label + " time", v]);
    }
  }
  const shas = commitShaList(rec.commit_sha);
  pairs.push(
    [
      shas.length > 1 ? "Commits" : "Commit",
      shas.length ? `landed (${shas.join(", ")})` : (rec.status === "error" ? "not landed" : "—"),
    ],
    ["Launched by", run.launched_by || "—"],
  );
  return pairs;
}

// A read-only <pre> that lazy-loads a run's captured log (live tail when the
// task is the playing track). `isActive` guards the async fill against a stale
// pane. Shares the `detail-screen-log` id so SSE tailing finds it.
function logPre(runId, task, { live = false, logId = "detail-screen-log", isActive = () => true } = {}) {
  const log = document.createElement("pre");
  log.className = "log full";
  log.id = logId;
  const key = `${runId}/${task}`;
  const text = state.logCache[key];
  if (text === undefined) {
    log.textContent = live ? "waiting for output\u2026" : "loading\u2026";
    if (runId) {
      fetchLog(runId, task, key, () => { if (isActive()) log.textContent = state.logCache[key] || "(no output)"; });
    }
  } else {
    log.textContent = text || "(no output)";
  }
  return log;
}

// Build the editable task-detail content as a fragment of expando panels: BRIEF
// (title + spec prose) and SETTINGS (segmented switches + model) are always
// shown; RUN DETAILS / LOG appear only once the task has run (the now-playing
// task gets a live LOG, with RUN DETAILS held back until final timings exist).
// Edits write to `state.detailDraft`; nothing persists until Save. `opts.rerender`
// re-renders the host after a switch toggles; `opts.isActive` guards async fills.
function taskDetailContent(brief, draft, opts = {}) {
  const { rerender = () => {}, isActive = () => true, logId = "detail-screen-log" } = opts;
  // A create pane has no file yet (brief.task is null): it never locks, has no
  // run history or log, and its Save creates the task instead of patching it.
  const creating = !!opts.creating;
  const task = brief.task;
  const locked = !creating && runningHere() && task === state.player.now_playing;
  const frag = document.createDocumentFragment();

  const found = creating ? null : latestRecordFor(task);
  const rec = found ? found.rec : null;
  const run = found ? found.run : null;
  const isNow = locked;
  let status;
  if (creating) status = "pending";
  else if (isNow) status = state.player.state === "paused" ? "paused" : "running";
  else if (rec) status = rec.status;
  else status = "pending";

  // The new-task pane leads with a "new task" line in place of the File/Status
  // grid (there is no file path or run status yet).
  if (creating) {
    frag.append(metaGrid([["File", "new task"], ["Status", "Not yet created"]]));
  } else {
    frag.append(detailStatusHead(task, status));
  }

  // TITLE — stands on its own (not inside an expando) and is always editable
  // except while the task is the running track (its spec mustn't change under a
  // live worker). Completed tasks have no editable spec, so they show the title
  // only as the pane headline (the read-only history flavour).
  const titleField = document.createElement("label");
  titleField.className = "detail-field detail-title-field";
  const titleLabel = document.createElement("span");
  titleLabel.className = "detail-field-label";
  titleLabel.textContent = "Title";
  const titleInput = document.createElement("input");
  titleInput.type = "text";
  // Guard against a nullish draft title: `input.value = undefined` renders the
  // literal text "undefined", so coerce to an empty string (placeholder shows).
  titleInput.value = draft.title || "";
  titleInput.disabled = locked;
  if (creating) titleInput.placeholder = "short descriptive title";
  titleInput.addEventListener("input", () => { draft.title = titleInput.value; });
  titleField.append(titleLabel, titleInput);
  frag.append(titleField);

  // BRIEF — the spec prose, with a MARKDOWN | PREVIEW segmented control pinned to
  // the right of the expando bar. MARKDOWN shows the editable textarea; PREVIEW
  // renders the buffered markdown read-only.
  const briefArea = document.createElement("textarea");
  briefArea.rows = 10;
  briefArea.value = draft.body;
  briefArea.disabled = locked;
  if (creating) briefArea.placeholder = "what should the worker do?";
  briefArea.addEventListener("input", () => { draft.body = briefArea.value; });
  briefArea.className = "detail-brief-edit";

  const preview = document.createElement("div");
  preview.className = "detail-brief markdown-body";

  // The view choice rides on the draft so it survives a settings-driven rerender;
  // it is never persisted (saveDetail/createDetail send an explicit field list).
  const seg = document.createElement("div");
  seg.className = "segmented brief-view-seg";
  seg.setAttribute("role", "group");
  seg.setAttribute("aria-label", "Brief view");
  const setView = (view) => {
    const previewing = view === "preview";
    draft.briefView = view;
    briefArea.hidden = previewing;
    preview.hidden = !previewing;
    if (previewing) preview.innerHTML = renderMarkdown(draft.body);
    for (const b of seg.children) {
      const on = b.dataset.view === view;
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    }
  };
  for (const [label, view] of [["Markdown", "markdown"], ["Preview", "preview"]]) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "seg-opt";
    b.textContent = label;
    b.dataset.view = view;
    b.addEventListener("click", () => setView(view));
    seg.append(b);
  }
  const brf = expando("Brief", { open: true, accessory: seg });
  brf.body.append(briefArea, preview);
  setView(draft.briefView === "preview" ? "preview" : "markdown");
  frag.append(brf.panel);

  // RUN DETAILS / LOG — only when the task has run. A completed record with
  // timings gets RUN DETAILS; a live or historical record gets a LOG.
  if (rec && rec.timings) {
    const rd = expando("Run details", { open: false });
    rd.body.append(metaGrid(runDetailPairs(run, rec)));
    frag.append(rd.panel);
  }
  if (isNow || rec) {
    const runId = isNow ? (state.player.run_id || state.currentRunId) : (run && run.id);
    const lg = expando(isNow ? "Live log" : "Log", { open: isNow });
    lg.body.append(logPre(runId, task, { live: isNow, logId, isActive }));
    frag.append(lg.panel);
  }

  // Settings — segmented switches (left) + model dropdown (right). A plain
  // (always-open, unlabelled) panel below the log, not an expando.
  const set = document.createElement("section");
  set.className = "xpanel";
  const setBody = document.createElement("div");
  setBody.className = "xpanel-body";
  setBody.append(settingsControls(brief, draft, rerender, locked));
  set.append(setBody);
  frag.append(set);

  // Save — bottom-right. Persists the buffered edits in one PATCH, then returns
  // to Now. (Back without Save discards the draft — there is no Cancel button.)
  const actions = document.createElement("div");
  actions.className = "detail-actions";
  const err = document.createElement("p");
  err.className = "error detail-save-error";
  err.hidden = true;
  const saveBtn = document.createElement("button");
  saveBtn.className = "btn primary";
  saveBtn.textContent = creating ? "Create" : "Save";
  saveBtn.disabled = locked;
  saveBtn.addEventListener("click", () =>
    creating ? createDetail(draft, err) : saveDetail(brief, draft, err));
  actions.append(err, saveBtn);
  frag.append(actions);
  return frag;
}

// Persist the buffered detail-pane edits, then leave the pane back to Now. The
// payload only sends the model as a raw pin ("default" clears it); the backend
// guards against editing a running task's spec.
async function saveDetail(brief, draft, errEl) {
  const payload = {
    title: draft.title.trim(),
    body: draft.body,
    disabled: !!draft.disabled,
    evergreen: !!draft.evergreen,
    automerge: !!draft.automerge,
    draft: !!draft.draft,
    model: draft.model || "default",
    priority: typeof draft.priority === "number" ? draft.priority : 5,
  };
  // The per-task repo override is only sent when the user actually changed it
  // (PATCH semantics — unsent fields are left untouched). A new value pins the
  // task to that repo; clearing it sends null so the override is removed and the
  // task inherits its queue's default repo again.
  const repoNow = (draft.repo || "").trim();
  const repoWas = ((brief.frontmatter_raw && brief.frontmatter_raw.repo) || "").trim();
  if (repoNow !== repoWas) payload.repo = repoNow || null;
  if (!payload.title) {
    if (errEl) { errEl.textContent = "title is required"; errEl.hidden = false; }
    return;
  }
  const { ok, data } = await sendJSON(
    `/api/tasks/${encodeURIComponent(brief.task)}`, "PATCH", payload);
  if (!ok) {
    if (errEl) { errEl.textContent = (data && data.error) || "could not save task"; errEl.hidden = false; }
    return;
  }
  await loadQueue();  // title/enable/evergreen changes alter the queue rows
  closeDetailScreen();
}

// Create a new task from the buffered create-pane edits, then leave back to
// where the pane was opened from. The whole brief (title, prose, toggles, and
// model) is sent in one POST so the detail view is the single add surface.
async function createDetail(draft, errEl) {
  const title = draft.title.trim();
  if (!title) {
    if (errEl) { errEl.textContent = "title is required"; errEl.hidden = false; }
    return;
  }
  const payload = {
    title,
    text: draft.body,
    disabled: !!draft.disabled,
    evergreen: !!draft.evergreen,
    automerge: !!draft.automerge,
    draft: !!draft.draft,
    model: draft.model || "default",
    priority: typeof draft.priority === "number" ? draft.priority : 5,
  };
  // Per-task repo override: included only when set (empty ⇒ inherit the queue
  // default). Omitted entirely otherwise, matching the create contract.
  if (draft.repo && draft.repo.trim()) payload.repo = draft.repo.trim();
  const { ok, data } = await sendJSON("/api/tasks", "POST", payload);
  if (!ok) {
    if (errEl) { errEl.textContent = (data && data.error) || "could not create task"; errEl.hidden = false; }
    return;
  }
  await loadQueue();
  closeDetailScreen();
}

// ----- full-area detail pane (view "detail") -----------------------------
// One pane serves two flavours: an *editable* brief (opened from a queue/Now
// task) and a *read-only history* record (opened from a History row). Both take
// over the content region with a `<` back control that returns to the view they
// were opened from. An editable pane seeds a fresh draft on open; navigating
// back without saving discards it.
function openDetailScreen(task) {
  if (!task) return;
  state.detailScreenTask = task;
  state.detailHistory = null;
  state.detailCreate = false;
  state.detailDraft = null;  // seeded once the brief loads in renderDetailScreen
  state.detailReturn = state.view === "queue" ? "queue" : "now";
  setTaskHash(task);
  setView("detail");
}

// Open the detail pane in its create flavour: the same editable surface (title,
// brief, settings, model) but with no file behind it yet. Save creates the
// task. Reachable from the top-bar "+" and the Queue's "New task…" menu item.
function openCreateTask() {
  state.detailScreenTask = null;
  state.detailHistory = null;
  state.detailCreate = true;
  state.detailDraft = null;  // seeded once the defaults load in renderDetailScreen
  state.detailReturn = state.view === "queue" ? "queue" : "now";
  clearTaskHash();
  setView("detail");
}

// Open the read-only history flavour for a completed run's task record.
function openHistoryDetail(run, rec) {
  if (!run || !rec) return;
  state.detailHistory = { run, rec };
  state.detailScreenTask = null;
  state.detailDraft = null;
  state.detailReturn = state.view === "now" ? "now" : "history";
  clearTaskHash();
  setView("detail");
}

// Tear down the open pane's state without navigating (so callers that want a
// specific destination — e.g. Resolve/Run-now jumping to Now — can set it).
function clearDetailState() {
  state.detailScreenTask = null;
  state.detailHistory = null;
  state.detailCreate = false;
  state.createBrief = null;
  state.detailDraft = null;
  clearTaskHash();
}

function closeDetailScreen() {
  const back = state.detailReturn || "now";
  clearDetailState();
  setView(back);
}

function renderDetailScreen() {
  if (state.detailHistory) { renderHistoryDetail(); return; }
  if (state.detailCreate) { renderCreateScreen(); return; }
  const task = state.detailScreenTask;
  const body = $("detail-screen-body");
  if (!task || !body) return;
  getJSON(`/api/tasks/${encodeURIComponent(task)}`)
    .then((brief) => {
      if (state.detailScreenTask !== task) return;
      $("detail-screen-title").textContent = (brief && brief.title) || task;
      body.innerHTML = "";
      if (!brief || brief.error) {
        const p = document.createElement("p");
        p.className = "empty";
        p.textContent = "This task is no longer in the queue (it may have finished).";
        body.append(p);
        return;
      }
      // Seed the edit buffer once, the first time the brief loads for this task,
      // so a live re-render (status flip, new log line) never clobbers in-flight
      // typing.
      if (!state.detailDraft) state.detailDraft = draftFromBrief(brief);
      body.append(taskDetailContent(brief, state.detailDraft, {
        rerender: () => renderDetailScreenLocal(brief),
        isActive: () => state.detailScreenTask === task && state.view === "detail",
        logId: "detail-screen-log",
      }));
    })
    .catch(() => { /* keep the current render on a transient error */ });
}

// Render the create flavour: fetch the active queue's brief-shaped defaults
// (effective model/flags + model options) once, seed an empty draft, then build
// the same editable content with Save wired to create.
function renderCreateScreen() {
  const body = $("detail-screen-body");
  if (!body) return;
  $("detail-screen-title").textContent = "New task";
  // The defaults don't change while the pane is open, so a draft already in hand
  // means we only need to re-render (e.g. after a switch toggle).
  if (state.detailDraft && state.createBrief) {
    renderCreateContent(state.createBrief);
    return;
  }
  getJSON("/api/task-defaults")
    .then((brief) => {
      if (!state.detailCreate) return;
      state.createBrief = brief || {};
      if (!state.detailDraft) state.detailDraft = draftFromBrief(state.createBrief);
      renderCreateContent(state.createBrief);
    })
    .catch(() => { /* keep the current render on a transient error */ });
}

function renderCreateContent(brief) {
  const body = $("detail-screen-body");
  if (!body || !state.detailCreate || !state.detailDraft) return;
  body.innerHTML = "";
  body.append(taskDetailContent(brief, state.detailDraft, {
    creating: true,
    rerender: () => renderCreateContent(brief),
    isActive: () => state.detailCreate && state.view === "detail",
  }));
}

// Re-render the pane from the brief already in hand + the live draft, without a
// round-trip — used when a switch toggles so its pressed state updates in place.
function renderDetailScreenLocal(brief) {
  const body = $("detail-screen-body");
  if (!body || state.detailScreenTask !== brief.task || !state.detailDraft) return;
  body.innerHTML = "";
  body.append(taskDetailContent(brief, state.detailDraft, {
    rerender: () => renderDetailScreenLocal(brief),
    isActive: () => state.detailScreenTask === brief.task && state.view === "detail",
    logId: "detail-screen-log",
  }));
}

// Render the read-only history flavour: the run outcome up top, then BRIEF
// (original prose), RUN DETAILS, FRONTMATTER (segmented displays + model, none
// editable), and LOG as expando panels, with the run actions at the bottom.
function renderHistoryDetail() {
  const { run, rec } = state.detailHistory || {};
  const body = $("detail-screen-body");
  if (!rec || !body) return;
  $("detail-screen-title").textContent = rec.title || rec.task;
  body.innerHTML = "";

  body.append(detailStatusHead(rec.task, rec.status));

  // Run outcome — the worker's result line + any classified failure reason.
  if (rec.result_line) {
    const res = document.createElement("div");
    res.className = "detail-result";
    res.textContent = rec.result_line;
    const badge = rec.status === "error" ? failureBadge(rec.failure_kind) : null;
    if (badge) res.append(badge);
    body.append(res);
  }
  if (rec.error && (rec.status === "error" || rec.status === "aborted")) {
    const err = document.createElement("pre");
    err.className = "detail-error";
    err.textContent = rec.error;
    body.append(err);
  }

  // BRIEF — the original prose captured at task start (read-only). Older runs
  // predate brief capture, so fall back to an explicit note.
  const brf = expando("Brief", { open: true });
  const brief = document.createElement("div");
  brief.className = "detail-brief";
  if (rec.body) {
    brief.textContent = rec.body;
  } else {
    brief.classList.add("muted");
    brief.textContent = "(brief not retained for this run)";
  }
  brf.body.append(brief);
  body.append(brf.panel);

  // RUN DETAILS — only meaningful for tasks that have run, so it's history-only.
  const rd = expando("Run details", { open: true });
  rd.body.append(metaGrid(runDetailPairs(run, rec)));
  body.append(rd.panel);

  // LOG — the run's captured output (history-only).
  const lg = expando("Log", { open: false });
  lg.body.append(logPre(run.id, rec.task, {
    live: false,
    isActive: () => state.detailHistory && state.detailHistory.rec.task === rec.task,
  }));
  body.append(lg.panel);

  // Frontmatter — recorded flags as read-only segmented displays + locked model
  // dropdown. A plain panel below the log, mirroring the queue's settings.
  body.append(historyFrontmatter(rec));

  body.append(historyActions(run, rec));
}

// The history frontmatter panel: draft / automerge / split as a read-only
// segmented display (mirroring the editable switches) and the model as a locked
// dropdown — the recorded spec, shown but not changeable. A plain (unlabelled,
// non-expando) panel, matching the queue's settings panel.
function historyFrontmatter(rec) {
  const panel = document.createElement("section");
  panel.className = "xpanel";
  const body = document.createElement("div");
  body.className = "xpanel-body";
  panel.append(body);
  const fm = rec.frontmatter || {};

  const row = document.createElement("div");
  row.className = "settings-row";

  const seg = document.createElement("div");
  seg.className = "segmented display";
  seg.setAttribute("role", "group");
  seg.setAttribute("aria-label", "Task flags");
  for (const [label, key] of [["Draft", "draft"], ["Auto-merge", "automerge"], ["Split", "split"]]) {
    const opt = document.createElement("span");
    opt.className = "seg-opt";
    opt.classList.toggle("on", !!fm[key]);
    opt.textContent = label;
    seg.append(opt);
  }

  const wrap = document.createElement("label");
  wrap.className = "model-select";
  const span = document.createElement("span");
  span.className = "model-label";
  span.textContent = "Model";
  const select = document.createElement("select");
  select.className = "ctl-select";
  select.disabled = true;
  const opt = document.createElement("option");
  opt.textContent = fm.model || "default";
  select.append(opt);
  wrap.append(span, select);

  row.append(seg, wrap);
  body.append(row);
  return panel;
}

// The history run actions: Resolve (when the task validated but didn't land),
// Run now (when it's back in the active queue), and Delete run.
function historyActions(run, rec) {
  const actions = document.createElement("div");
  actions.className = "detail-actions";
  if (isResolvable(rec)) {
    const resolve = document.createElement("button");
    resolve.className = "btn primary btn-resolve";
    resolve.innerHTML = resolveButtonInner();
    resolve.addEventListener("click", () => resolveTask(run.id, rec.task, resolve));
    actions.append(resolve);
  }
  if (state.queue.some((i) => i.task === rec.task)) {
    const runNow = document.createElement("button");
    runNow.className = "btn primary";
    runNow.textContent = "Run now";
    runNow.addEventListener("click", () => {
      transport("play", { mode: "oneshot", task: rec.task });
      clearDetailState();
      setView("now");
    });
    actions.append(runNow);
  }
  const del = document.createElement("button");
  del.className = "btn danger";
  del.textContent = "Delete run";
  del.addEventListener("click", async () => {
    if (!confirm(
      "Remove this run record? This deletes its logs from disk, and the task's "
      + "preserved branch/worktree when no other run still needs it."
    )) return;
    if (await deleteRun(run.id)) closeDetailScreen();
  });
  actions.append(del);
  return actions;
}

// ----- deep-link (URL hash) ----------------------------------------------
// The detail pane round-trips through `#task=<id>` so it can be linked and
// restored. We touch only the task key, leaving any other hash content intact.
function setTaskHash(task) {
  const next = `#task=${encodeURIComponent(task)}`;
  if (location.hash !== next) history.replaceState(null, "", next);
}
function clearTaskHash() {
  if (location.hash.startsWith("#task=")) history.replaceState(null, "", location.pathname + location.search);
}
function taskFromHash() {
  const m = location.hash.match(/^#task=([^&]+)/);
  return m ? decodeURIComponent(m[1]) : null;
}
function applyHash() {
  const task = taskFromHash();
  if (task) {
    if (state.detailScreenTask !== task) openTaskDetail(task);
  } else if (state.detailScreenTask && state.view === "detail") {
    closeDetailScreen();
  }
}

function metaGrid(pairs) {
  const grid = document.createElement("div");
  grid.className = "detail-meta";
  for (const [k, v] of pairs) {
    const key = document.createElement("div");
    key.className = "meta-k";
    key.textContent = k;
    const val = document.createElement("div");
    val.className = "meta-v";
    val.textContent = v;
    grid.append(key, val);
  }
  return grid;
}

async function fetchLog(runId, task, key, after) {
  const data = await getJSON(`/api/runs/${encodeURIComponent(runId)}/${encodeURIComponent(task)}/log`);
  state.logCache[key] = data.text || "";
  if (after) after();
}

// --------------------------------------------------------------------------
// SSE + auto refresh
// --------------------------------------------------------------------------
let refreshTimer = null;
function scheduleRefresh() {
  if (refreshTimer) return;
  refreshTimer = setTimeout(() => {
    refreshTimer = null;
    loadRuns();
    loadQueue();
    loadPlaylists();
  }, 250);
}

function connectEvents() {
  const es = new EventSource("/api/events");
  es.onmessage = (msg) => {
    let data;
    try { data = JSON.parse(msg.data); } catch { return; }
    if (data.kind === "state") {
      ingestState(data);
    } else if (data.kind === "event") {
      handleEngineEvent(data);
    }
  };
  es.onerror = () => { /* EventSource auto-reconnects */ };
}

function handleEngineEvent(ev) {
  // Frames are tagged with their queue; only the *focused* queue's events drive
  // the single-context Now log/cursor. A background queue's run still triggers a
  // refresh (so its history/badges update), but never hijacks the live view.
  const focused = (ev.queue || "main") === focusedQueueKey();
  if (focused && ev.run_id) state.currentRunId = ev.run_id;
  if (focused && ev.type === "task_log" && ev.task) {
    const key = `${state.currentRunId}/${ev.task}`;
    state.logCache[key] = (state.logCache[key] || "") + (ev.line || "");
    // Snappy live tail on the Now screen without waiting for the debounce.
    if (state.view === "now" && ev.task === state.player.now_playing) {
      const log = $("now-log");
      if (log) log.textContent = logTail(state.logCache[key], 12);
    }
    // Keep the editable detail pane's read-only live log current too.
    if (state.detailScreenTask && ev.task === state.detailScreenTask &&
        state.detailScreenTask === state.player.now_playing) {
      const slog = $("detail-screen-log");
      if (slog) slog.textContent = state.logCache[key];
    }
  }
  if (["task_started", "task_status", "task_result", "run_finished", "run_started"].includes(ev.type)) {
    scheduleRefresh();
  } else if (ev.type === "task_log") {
    scheduleRefresh();
  }
}

// --------------------------------------------------------------------------
// Add task (reuses the full-area detail pane in its create flavour)
// --------------------------------------------------------------------------
// Adding a task opens the same editable detail surface used to edit one (title,
// brief, settings, model) with no file behind it yet; Save creates the task.
// `<` (or Escape) navigates back, discarding the draft (back *is* cancel).
function openAdd() {
  openCreateTask();
}

// ----- Add dropdown (New task / Add from playlist) -----------------------
function toggleAddMenu(force) {
  const menu = $("add-menu");
  const btn = $("btn-add-menu");
  if (!menu || !btn) return;
  const show = force !== undefined ? force : menu.hidden;
  menu.hidden = !show;
  btn.setAttribute("aria-expanded", show ? "true" : "false");
}
function closeAddMenu() { toggleAddMenu(false); }

// ----- Gear dropdown (Settings / Workers / Repos) ------------------------
// The gear no longer opens Settings directly: it fronts a small menu that also
// reaches the Workers and Repos screens (formerly bottom-bar tabs).
function toggleSettingsMenu(force) {
  const menu = $("settings-menu");
  const btn = $("btn-settings");
  if (!menu || !btn) return;
  const show = force !== undefined ? force : menu.hidden;
  menu.hidden = !show;
  btn.setAttribute("aria-expanded", show ? "true" : "false");
}
function closeSettingsMenu() { toggleSettingsMenu(false); }

// ----- Add from another playlist -----------------------------------------
async function openAddFrom() {
  await loadPlaylists();
  // The main `.tasks/` queue shows up as the special "library" playlist; we
  // only offer it as a source when it isn't already the active queue. Prefetch
  // its tasks so the row can show a count and expand without a round-trip.
  state.libraryTasks = null;
  if (state.activePlaylist !== null) {
    try { state.libraryTasks = await getJSON("/api/main/tasks"); }
    catch { state.libraryTasks = []; }
  }
  $("addfrom-status").hidden = true;
  renderAddFrom();
  $("addfrom-modal").hidden = false;
}

function renderAddFrom() {
  const ul = $("addfrom-list");
  if (!ul) return;
  ul.innerHTML = "";

  // Source queues offered to the picker: the special "library" (main queue,
  // only when it isn't the active queue) followed by every other playlist.
  const sources = [];
  if (state.activePlaylist !== null) {
    sources.push({ name: "library", library: true, task_count: (state.libraryTasks || []).length });
  }
  for (const pl of state.playlists) {
    if (pl.name !== state.activePlaylist) sources.push(pl);
  }

  $("addfrom-empty").hidden = sources.length > 0;
  if (sources.length) ul.append(addFromGroup(sources));
}

// Top-level "Playlists" group — collapsed until clicked, then lists every
// source queue (the library + playlists).
function addFromGroup(sources) {
  const li = document.createElement("li");
  li.className = "addfrom-item addfrom-group";

  const head = document.createElement("div");
  head.className = "addfrom-head addfrom-group-head";
  const chev = document.createElement("button");
  chev.className = "addfrom-chev";
  chev.innerHTML = "&#8250;";
  chev.title = "Show playlists";
  const name = document.createElement("span");
  name.className = "addfrom-name";
  name.textContent = "Playlists";
  const count = document.createElement("span");
  count.className = "addfrom-count";
  count.textContent = `${sources.length}`;
  head.append(chev, name, count);

  const sub = document.createElement("ul");
  sub.className = "addfrom-sub";
  sub.hidden = true;
  for (const s of sources) sub.append(addFromSourceRow(s));

  head.addEventListener("click", (e) => {
    e.stopPropagation();
    const open = !sub.hidden;
    sub.hidden = open;
    chev.classList.toggle("open", !open);
  });

  li.append(head, sub);
  return li;
}

// A single source queue (a playlist, or the special "library" = main queue).
// Clicking the row expands its tasks; the + copies the whole queue across.
function addFromSourceRow(s) {
  const source = s.library ? null : s.name;
  const li = document.createElement("li");
  li.className = "addfrom-item";

  const head = document.createElement("div");
  head.className = "addfrom-head";

  const add = document.createElement("button");
  add.className = "addfrom-add";
  add.innerHTML = "&#43;";
  add.title = `Add all ${s.task_count} task(s) from “${s.name}”`;
  add.addEventListener("click", (e) => { e.stopPropagation(); importFrom(source, null); });

  const name = document.createElement("span");
  name.className = "addfrom-name";
  name.textContent = s.name;
  if (s.library) {
    name.classList.add("addfrom-library");
    const tag = document.createElement("span");
    tag.className = "addfrom-tag";
    tag.textContent = "main queue";
    name.append(tag);
  }

  const count = document.createElement("span");
  count.className = "addfrom-count";
  count.textContent = `${s.task_count}`;

  const chev = document.createElement("button");
  chev.className = "addfrom-chev";
  chev.innerHTML = "&#8250;";
  chev.title = "Show tasks";

  const sub = document.createElement("ul");
  sub.className = "addfrom-sub";
  sub.hidden = true;

  const expand = async (e) => {
    e.stopPropagation();
    if (!sub.hidden) { sub.hidden = true; chev.classList.remove("open"); return; }
    if (!sub.dataset.loaded) {
      let tasks = [];
      if (s.library) {
        tasks = state.libraryTasks || [];
      } else {
        try { tasks = await getJSON(`/api/playlists/${encodeURIComponent(s.name)}/tasks`); }
        catch { tasks = []; }
      }
      sub.innerHTML = "";
      if (!tasks.length) {
        const empty = document.createElement("li");
        empty.className = "empty";
        empty.textContent = "No tasks.";
        sub.append(empty);
      } else {
        for (const t of tasks) sub.append(addFromTaskRow(source, t));
      }
      sub.dataset.loaded = "1";
    }
    sub.hidden = false;
    chev.classList.add("open");
  };
  head.addEventListener("click", expand);
  chev.addEventListener("click", expand);

  head.append(add, name, count, chev);
  li.append(head, sub);
  return li;
}

function addFromTaskRow(plName, t) {
  const li = document.createElement("li");
  li.className = "addfrom-task";
  const plus = document.createElement("button");
  plus.className = "addfrom-add";
  plus.innerHTML = "&#43;";
  plus.title = "Add this task";
  const title = document.createElement("span");
  title.className = "addfrom-task-title";
  title.textContent = t.title || t.task;
  const add = (e) => { e.stopPropagation(); importFrom(plName, [t.task]); };
  plus.addEventListener("click", add);
  li.addEventListener("click", add);
  li.append(plus, title);
  return li;
}

async function importFrom(source, tasks) {
  const { ok, data } = await sendJSON("/api/queue/import", "POST", { source, tasks });
  if (!ok) { alert((data && data.error) || "could not add tasks"); return; }
  await loadPlaylists();   // (counts unchanged, but keep the picker fresh)
  await loadQueue();
  const n = (data && data.imported && data.imported.length) || 0;
  const status = $("addfrom-status");
  if (status) {
    status.textContent = `Added ${n} task${n === 1 ? "" : "s"} to ${state.activePlaylist || "the main queue"}.`;
    status.hidden = false;
  }
}

// --------------------------------------------------------------------------
// Settings — full-page multi-tier editor
// --------------------------------------------------------------------------
const settingsState = {
  tiers: [],
  activeSurface: null,
  activeCategory: null,
  dirty: {},
  errors: {},
  searchQuery: "",
};

async function openSettings() {
  const data = await getJSON("/api/settings");
  settingsState.tiers = data.tiers || [];
  settingsState.dirty = {};
  settingsState.errors = {};
  settingsState.searchQuery = "";

  if (settingsState.tiers.length && !settingsState.activeSurface) {
    const first = settingsState.tiers[0];
    settingsState.activeSurface = first.surface;
    if (first.categories.length) {
      settingsState.activeCategory = first.categories[0].name;
    }
  }

  setView("settings");
  renderSettingsSidebar();
  renderSettingsFields();
  updateSaveBar();
  $("settings-search").value = "";
}

function renderSettingsSidebar() {
  const tree = $("settings-tree");
  tree.innerHTML = "";
  for (const tier of settingsState.tiers) {
    const div = document.createElement("div");
    div.className = "st-tier";
    const label = document.createElement("div");
    label.className = "st-tier-label";
    label.textContent = tier.surface.charAt(0).toUpperCase() + tier.surface.slice(1);
    div.append(label);
    for (const cat of tier.categories) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "st-cat";
      if (tier.surface === settingsState.activeSurface && cat.name === settingsState.activeCategory) {
        btn.classList.add("active");
      }
      btn.textContent = cat.name;
      btn.setAttribute("role", "treeitem");
      btn.addEventListener("click", () => {
        settingsState.activeSurface = tier.surface;
        settingsState.activeCategory = cat.name;
        settingsState.searchQuery = "";
        $("settings-search").value = "";
        renderSettingsSidebar();
        renderSettingsFields();
      });
      div.append(btn);
    }
    tree.append(div);
  }
}

function renderSettingsFields() {
  const pane = $("settings-fields-pane");
  pane.innerHTML = "";

  const query = settingsState.searchQuery.toLowerCase().trim();
  if (query) {
    renderSearchResults(pane, query);
    return;
  }

  const tier = settingsState.tiers.find(t => t.surface === settingsState.activeSurface);
  if (!tier) return;
  const cat = tier.categories.find(c => c.name === settingsState.activeCategory);
  if (!cat) return;

  const title = document.createElement("h2");
  title.className = "settings-cat-title";
  title.textContent = cat.name;
  pane.append(title);

  for (const field of cat.fields) {
    pane.append(buildFieldRow(field, tier.surface));
  }

  const rawDetails = document.createElement("details");
  rawDetails.className = "sf-raw-json";
  const summary = document.createElement("summary");
  summary.textContent = "Raw JSON";
  const textarea = document.createElement("textarea");
  textarea.rows = 8;
  textarea.spellcheck = false;
  const rawData = {};
  for (const f of cat.fields) {
    if (!f.secret) rawData[f.key] = f.stored;
  }
  textarea.value = JSON.stringify(rawData, null, 2);
  textarea.dataset.surface = tier.surface;
  textarea.dataset.category = cat.name;
  rawDetails.append(summary, textarea);
  pane.append(rawDetails);
}

function renderSearchResults(pane, query) {
  const title = document.createElement("h2");
  title.className = "settings-cat-title";
  title.textContent = `Search: "${query}"`;
  pane.append(title);

  let count = 0;
  for (const tier of settingsState.tiers) {
    for (const cat of tier.categories) {
      for (const field of cat.fields) {
        const searchable = `${field.label} ${field.key} ${field.desc}`.toLowerCase();
        if (searchable.includes(query)) {
          pane.append(buildFieldRow(field, tier.surface));
          count++;
        }
      }
    }
  }
  if (!count) {
    const empty = document.createElement("div");
    empty.className = "sf-empty-search";
    empty.textContent = "No settings match your search.";
    pane.append(empty);
  }
}

function buildFieldRow(field, surface) {
  const row = document.createElement("div");
  row.className = "sf-row";
  const fullKey = `${surface}.${field.key}`;
  if (fullKey in settingsState.dirty) row.classList.add("sf-dirty");
  if (fullKey in settingsState.errors) row.classList.add("sf-error");

  const head = document.createElement("div");
  head.className = "sf-head";
  const label = document.createElement("span");
  label.className = "sf-label";
  label.textContent = field.label;
  const key = document.createElement("code");
  key.className = "sf-key";
  key.textContent = field.key;
  head.append(label, key);

  const badges = document.createElement("span");
  badges.className = "sf-badges";
  if (field.apply === "restart") {
    const b = document.createElement("span");
    b.className = "sf-badge sf-badge-restart";
    b.textContent = "restart";
    badges.append(b);
  } else if (field.apply === "live") {
    const b = document.createElement("span");
    b.className = "sf-badge sf-badge-live";
    b.textContent = "live";
    badges.append(b);
  } else if (field.apply === "next-task") {
    const b = document.createElement("span");
    b.className = "sf-badge sf-badge-next-task";
    b.textContent = "next-task";
    badges.append(b);
  }
  if (field.secret) {
    const b = document.createElement("span");
    b.className = "sf-badge sf-badge-secret";
    b.textContent = "secret";
    badges.append(b);
  }
  head.append(badges);
  row.append(head);

  const desc = document.createElement("div");
  desc.className = "sf-desc";
  desc.textContent = field.desc;
  desc.id = `sf-desc-${fullKey}`;
  row.append(desc);

  if (field.env_shadowed) {
    const warn = document.createElement("div");
    warn.className = "sf-env-warn";
    warn.textContent = `Overridden by \`${field.env}\`; editing the file won't change the running value until the env var is unset.`;
    row.append(warn);
  }

  const control = document.createElement("div");
  control.className = "sf-control";
  control.setAttribute("aria-describedby", `sf-desc-${fullKey}`);
  control.append(buildControl(field, surface, fullKey));
  row.append(control);

  if (fullKey in settingsState.errors) {
    const errMsg = document.createElement("div");
    errMsg.className = "sf-error-msg";
    errMsg.textContent = settingsState.errors[fullKey];
    row.append(errMsg);
  }

  return row;
}

function buildControl(field, surface, fullKey) {
  const currentValue = fullKey in settingsState.dirty
    ? settingsState.dirty[fullKey]
    : field.stored;

  switch (field.type) {
    case "bool": return buildToggle(field, surface, fullKey, currentValue);
    case "enum": return buildSelect(field, surface, fullKey, currentValue);
    case "int":
    case "float": return buildNumber(field, surface, fullKey, currentValue);
    case "duration": return buildDuration(field, surface, fullKey, currentValue);
    case "string": return field.secret
      ? buildSecret(field, surface, fullKey)
      : buildText(field, surface, fullKey, currentValue);
    case "string_list":
    case "int_list":
    case "regex_list": return buildChipEditor(field, surface, fullKey, currentValue);
    case "str_map": return buildMapEditor(field, surface, fullKey, currentValue);
    default: return buildText(field, surface, fullKey, currentValue);
  }
}

function markDirty(surface, field, fullKey, value) {
  const original = field.stored;
  if (JSON.stringify(value) === JSON.stringify(original)) {
    delete settingsState.dirty[fullKey];
  } else {
    settingsState.dirty[fullKey] = value;
  }
  delete settingsState.errors[fullKey];
  updateSaveBar();
  const row = document.querySelector(`[data-sf-key="${fullKey}"]`)?.closest(".sf-row");
  if (row) {
    row.classList.toggle("sf-dirty", fullKey in settingsState.dirty);
    row.classList.remove("sf-error");
    const errEl = row.querySelector(".sf-error-msg");
    if (errEl) errEl.remove();
  }
}

function buildToggle(field, surface, fullKey, value) {
  const wrap = document.createElement("div");
  wrap.className = "sf-toggle";
  const track = document.createElement("div");
  track.className = "sf-toggle-track" + (value ? " on" : "");
  track.dataset.sfKey = fullKey;
  const thumb = document.createElement("div");
  thumb.className = "sf-toggle-thumb";
  track.append(thumb);
  const lbl = document.createElement("span");
  lbl.className = "sf-toggle-label";
  lbl.textContent = value ? "On" : "Off";
  track.addEventListener("click", () => {
    const newVal = !track.classList.contains("on");
    track.classList.toggle("on", newVal);
    lbl.textContent = newVal ? "On" : "Off";
    markDirty(surface, field, fullKey, newVal);
  });
  wrap.append(track, lbl);
  return wrap;
}

function buildSelect(field, surface, fullKey, value) {
  const select = document.createElement("select");
  for (const opt of (field.options || [])) {
    const o = document.createElement("option");
    o.value = opt;
    o.textContent = opt;
    if (opt === value) o.selected = true;
    select.append(o);
  }
  select.dataset.sfKey = fullKey;
  select.addEventListener("change", () => {
    markDirty(surface, field, fullKey, select.value);
  });
  return select;
}

function buildNumber(field, surface, fullKey, value) {
  const input = document.createElement("input");
  input.type = "number";
  input.value = value != null ? value : "";
  input.dataset.sfKey = fullKey;
  if (field.type === "float") input.step = "any";
  input.addEventListener("input", () => {
    const v = field.type === "int" ? parseInt(input.value, 10) : parseFloat(input.value);
    if (!isNaN(v)) markDirty(surface, field, fullKey, v);
  });
  return input;
}

function buildDuration(field, surface, fullKey, value) {
  const wrap = document.createElement("div");
  const input = document.createElement("input");
  input.type = "text";
  input.value = value || "";
  input.placeholder = "e.g. 45s, 30m, 1h30m";
  input.dataset.sfKey = fullKey;
  const hint = document.createElement("div");
  hint.className = "sf-duration-hint";
  const validate = () => {
    const v = input.value.trim();
    if (!v) { hint.textContent = ""; hint.className = "sf-duration-hint"; return; }
    if (/^(\d+[smh]\s*)+$/i.test(v)) {
      hint.textContent = "Valid duration";
      hint.className = "sf-duration-hint valid";
      markDirty(surface, field, fullKey, v);
    } else {
      hint.textContent = "Invalid format (use 45s, 30m, 1h30m)";
      hint.className = "sf-duration-hint invalid";
    }
  };
  input.addEventListener("input", validate);
  validate();
  wrap.append(input, hint);
  return wrap;
}

function buildText(field, surface, fullKey, value) {
  const input = document.createElement("input");
  input.type = "text";
  input.value = value != null ? value : "";
  input.dataset.sfKey = fullKey;
  input.addEventListener("input", () => {
    const v = input.value;
    markDirty(surface, field, fullKey, v || null);
  });
  return input;
}

function buildSecret(field, surface, fullKey) {
  const wrap = document.createElement("div");
  const status = document.createElement("div");
  status.className = "sf-secret-status" + (field.is_set ? " is-set" : "");
  status.textContent = field.is_set ? "Currently set" : "Not set";
  const input = document.createElement("input");
  input.type = "password";
  input.placeholder = "Leave blank to keep current value";
  input.dataset.sfKey = fullKey;
  input.addEventListener("input", () => {
    if (input.value) {
      markDirty(surface, field, fullKey, input.value);
    } else {
      delete settingsState.dirty[fullKey];
      updateSaveBar();
    }
  });
  wrap.append(status, input);
  return wrap;
}

function buildChipEditor(field, surface, fullKey, value) {
  const items = Array.isArray(value) ? [...value] : [];
  const wrap = document.createElement("div");
  wrap.className = "sf-chips";
  wrap.dataset.sfKey = fullKey;

  const rerender = () => {
    wrap.innerHTML = "";
    items.forEach((item, i) => {
      const row = document.createElement("div");
      row.className = "sf-chip-row";
      const input = document.createElement("input");
      input.className = "sf-chip-input";
      input.type = field.type === "int_list" ? "number" : "text";
      input.value = item;
      input.addEventListener("input", () => {
        const v = field.type === "int_list" ? parseInt(input.value, 10) : input.value;
        items[i] = v;
        if (field.type === "regex_list") {
          try { new RegExp(input.value); input.classList.remove("invalid"); }
          catch { input.classList.add("invalid"); }
        }
        markDirty(surface, field, fullKey, [...items]);
      });
      if (field.type === "regex_list") {
        try { new RegExp(item); } catch { input.classList.add("invalid"); }
      }
      const del = document.createElement("button");
      del.type = "button";
      del.className = "sf-chip-del";
      del.textContent = "\u00d7";
      del.addEventListener("click", () => {
        items.splice(i, 1);
        markDirty(surface, field, fullKey, [...items]);
        rerender();
      });
      row.append(input, del);
      wrap.append(row);
    });
    const addBtn = document.createElement("button");
    addBtn.type = "button";
    addBtn.className = "sf-chip-add";
    addBtn.textContent = "+ Add";
    addBtn.addEventListener("click", () => {
      items.push(field.type === "int_list" ? 0 : "");
      markDirty(surface, field, fullKey, [...items]);
      rerender();
    });
    wrap.append(addBtn);
  };
  rerender();
  return wrap;
}

function buildMapEditor(field, surface, fullKey, value) {
  const entries = Object.entries(value || {}).map(([k, v]) => ({ k, v }));
  const wrap = document.createElement("div");
  wrap.className = "sf-map";
  wrap.dataset.sfKey = fullKey;

  const collectMap = () => {
    const obj = {};
    for (const e of entries) { if (e.k) obj[e.k] = e.v; }
    return obj;
  };

  const rerender = () => {
    wrap.innerHTML = "";
    entries.forEach((entry, i) => {
      const row = document.createElement("div");
      row.className = "sf-map-row";
      const ki = document.createElement("input");
      ki.className = "sf-map-key";
      ki.placeholder = "key";
      ki.value = entry.k;
      ki.addEventListener("input", () => {
        entries[i].k = ki.value;
        markDirty(surface, field, fullKey, collectMap());
      });
      const sep = document.createElement("span");
      sep.className = "sf-map-sep";
      sep.textContent = "\u2192";
      const vi = document.createElement("input");
      vi.className = "sf-map-val";
      vi.placeholder = "value";
      vi.value = entry.v;
      vi.addEventListener("input", () => {
        entries[i].v = vi.value;
        markDirty(surface, field, fullKey, collectMap());
      });
      const del = document.createElement("button");
      del.type = "button";
      del.className = "sf-chip-del";
      del.textContent = "\u00d7";
      del.addEventListener("click", () => {
        entries.splice(i, 1);
        markDirty(surface, field, fullKey, collectMap());
        rerender();
      });
      row.append(ki, sep, vi, del);
      wrap.append(row);
    });
    const addBtn = document.createElement("button");
    addBtn.type = "button";
    addBtn.className = "sf-chip-add";
    addBtn.textContent = "+ Add entry";
    addBtn.addEventListener("click", () => {
      entries.push({ k: "", v: "" });
      rerender();
    });
    wrap.append(addBtn);
  };
  rerender();
  return wrap;
}

function updateSaveBar() {
  const bar = $("settings-savebar");
  const count = Object.keys(settingsState.dirty).length;
  if (count === 0) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;
  $("settings-dirty-count").textContent =
    `${count} unsaved change${count === 1 ? "" : "s"}`;
}

function discardSettings() {
  settingsState.dirty = {};
  settingsState.errors = {};
  updateSaveBar();
  renderSettingsFields();
}

async function saveSettings() {
  const delta = {};
  for (const [fullKey, value] of Object.entries(settingsState.dirty)) {
    const [surface, ...rest] = fullKey.split(".");
    const key = rest.join(".");
    if (!delta[surface]) delta[surface] = {};
    delta[surface][key] = value;
  }

  const { ok, data } = await sendJSON("/api/settings", "PUT", delta);
  if (!ok) {
    if (data && data.errors) {
      settingsState.errors = data.errors;
      renderSettingsFields();
    }
    return;
  }

  settingsState.dirty = {};
  settingsState.errors = {};
  settingsState.tiers = data.tiers || settingsState.tiers;
  updateSaveBar();
  renderSettingsFields();

  if (data.restart_required && data.restart_required.length) {
    const banner = $("settings-restart-banner");
    banner.textContent = "Restart required to apply: " +
      data.restart_required.map(k => k.split(".").slice(1).join(".")).join(", ");
    banner.hidden = false;
  }

  if (data.applied_live && data.applied_live.length) {
    for (const key of data.applied_live) {
      if (key === "player.theme" || key.endsWith(".theme")) {
        const tier = settingsState.tiers.find(t => t.surface === "player");
        if (tier) {
          for (const cat of tier.categories) {
            const f = cat.fields.find(f => f.key === "theme");
            if (f) {
              localStorage.setItem(THEME_KEY, f.stored);
              applyTheme(f.stored);
            }
          }
        }
      }
      if (key === "player.transport_mode" || key.endsWith(".transport_mode")) {
        const tier = settingsState.tiers.find(t => t.surface === "player");
        if (tier) {
          for (const cat of tier.categories) {
            const f = cat.fields.find(f => f.key === "transport_mode");
            if (f && f.stored) setMode(f.stored);
          }
        }
      }
    }
  }
}

function initSettingsSearch() {
  const input = $("settings-search");
  if (!input) return;
  input.addEventListener("input", () => {
    settingsState.searchQuery = input.value;
    renderSettingsFields();
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      input.value = "";
      settingsState.searchQuery = "";
      renderSettingsFields();
    }
  });
}

// --------------------------------------------------------------------------
// Wiring
// --------------------------------------------------------------------------
function wire() {
  $("btn-play").addEventListener("click", togglePlayPause);
  $("btn-stop").addEventListener("click", () => transport("stop"));
  $("btn-skip").addEventListener("click", () => transport("skip"));
  for (const b of document.querySelectorAll("#mode .mode-opt")) {
    b.addEventListener("click", () => setMode(b.dataset.mode));
  }
  for (const b of document.querySelectorAll("#bottomnav .navbtn")) {
    b.addEventListener("click", () =>
      b.dataset.view === "now" ? cycleNow() : setView(b.dataset.view));
  }
  document.addEventListener("keydown", onGlobalKeydown);
  $("btn-add").addEventListener("click", openAdd);
  // UP NEXT sort toggle: manual (drag) order <-> priority sort.
  const sortBtn = $("queue-sort");
  if (sortBtn) sortBtn.addEventListener("click", toggleSortMode);
  // Play-priority filter: [ALL|P0..P5] multi-select. ALL clears the filter;
  // each Pn toggles that level in/out of the set.
  const playFilter = $("play-filter");
  if (playFilter) {
    for (const opt of playFilter.querySelectorAll(".pf-opt")) {
      opt.addEventListener("click", () => {
        const raw = opt.dataset.priority;
        if (raw === "all") setPlayPriorities([]);
        else togglePlayPriority(Number(raw));
      });
    }
  }
  // Queue Add dropdown: New task / Add from playlist.
  $("btn-add-menu").addEventListener("click", (e) => { e.stopPropagation(); toggleAddMenu(); });
  for (const item of document.querySelectorAll("#add-menu .add-menu-item")) {
    item.addEventListener("click", () => {
      closeAddMenu();
      if (item.dataset.act === "new") openAdd();
      else openAddFrom();
    });
  }
  document.addEventListener("click", closeAddMenu);
  // The floating row-actions menu closes on any outside click, and on scroll
  // or resize (it's absolutely positioned to its trigger, so it would drift).
  document.addEventListener("click", (e) => {
    if (!rowMenuEl) return;
    const inMenu = rowMenuEl.contains(e.target);
    const inSub = rowSubmenuEl && rowSubmenuEl.contains(e.target);
    if (!inMenu && !inSub) closeRowMenu();
  });
  window.addEventListener("resize", closeRowMenu);
  window.addEventListener("scroll", closeRowMenu, true);
  $("addfrom-close").addEventListener("click", () => ($("addfrom-modal").hidden = true));
  $("addfrom-modal").addEventListener("click", (e) => {
    if (e.target === $("addfrom-modal")) $("addfrom-modal").hidden = true;
  });
  // Gear → popup menu (Settings / Workers / Repos), mirroring the Add dropdown.
  $("btn-settings").addEventListener("click", (e) => {
    e.stopPropagation();
    toggleSettingsMenu();
  });
  for (const item of document.querySelectorAll("#settings-menu .add-menu-item")) {
    item.addEventListener("click", () => {
      closeSettingsMenu();
      const act = item.dataset.act;
      if (act === "settings") openSettings();
      else if (act === "workers") setView("workers");
      else if (act === "repos") setView("repos");
    });
  }
  document.addEventListener("click", closeSettingsMenu);
  $("settings-discard").addEventListener("click", discardSettings);
  $("settings-save-btn").addEventListener("click", saveSettings);
  initSettingsSearch();
  $("btn-clear").addEventListener("click", clearCompleted);
  $("btn-stats").addEventListener("click", openStats);
  $("stats-back").addEventListener("click", closeStats);
  $("detail-back").addEventListener("click", closeDetailScreen);
  $("btn-add-playlist").addEventListener("click", openPlaylistCreate);
  $("playlist-cancel").addEventListener("click", () => ($("playlist-modal").hidden = true));
  $("playlist-save").addEventListener("click", savePlaylist);
  // Repos page: re-scan the workspace for repos (auto-resumes paused tasks).
  const rescanBtn = $("btn-rescan");
  if (rescanBtn) rescanBtn.addEventListener("click", () => rescanRepos(rescanBtn));
  // Back/forward and manual hash edits drive the deep-linked detail view.
  window.addEventListener("hashchange", applyHash);
}

// Periodic safety refresh: SSE drives live updates, but this also triggers the
// server's stale-run reconcile (phantom "running" → aborted) every 20s.
const REFRESH_MS = 20000;
function startAutoRefresh() {
  setInterval(() => {
    loadRuns();
    loadQueue();
    loadPlaylists();
  }, REFRESH_MS);
}

async function init() {
  wire();
  setView("now");
  let defaultTheme = "dark";
  try {
    const s = await getJSON("/api/settings");
    if (s.tiers) {
      const player = s.tiers.find(t => t.surface === "player");
      if (player) {
        for (const cat of player.categories) {
          for (const f of cat.fields) {
            if (f.key === "theme" && f.stored) defaultTheme = f.stored;
            if (f.key === "transport_mode" && f.stored) setMode(f.stored);
          }
        }
      }
    } else {
      defaultTheme = (s.values && s.values.theme) || defaultTheme;
      if (s.values && s.values.transport_mode) setMode(s.values.transport_mode);
    }
  } catch { /* settings optional at boot */ }
  initTheme(defaultTheme);
  // Establish the active queue first so queue/runs load against the right one.
  try {
    const active = await getJSON("/api/active");
    syncActivePlaylist(active && active.active_playlist);
  } catch { /* default to main */ }
  await Promise.all([loadQueue(), loadRuns(), loadPlaylists(), loadRepos()]);
  // Seed the per-queue state map, then drive the focused queue's view from it
  // (the aggregate flat state follows whichever queue is running, which may not
  // be the focused one).
  await refreshQueues();
  applyFocusedState();
  // Restore a deep-linked detail view (#task=<id>) once data is loaded.
  applyHash();
  connectEvents();
  startAutoRefresh();
  setInterval(tickElapsed, 1000);
}

init();
