// Headless test for the paused-queue icon on a Playlists row (app.js).
//
// Pausing a queue used to leave no mark on the Playlists screen: the row looked
// exactly like a running or idle one, so a queue held by the operator (or
// quarantined by the failure policy) was invisible until you opened it. The row
// now carries a pause icon, immediately left of the "+", while that queue's
// runner is paused; clicking it resumes the queue, which drops the icon on the
// next render. This test loads the REAL row source from app.js (sliced between
// sentinels) and drives it against a minimal DOM shim.
//
// Run: node --test tests/ui/ (or `node tests/ui/playlist_pause_icon.test.mjs`).

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const appPath = resolve(here, "../../src/nightshift/assets/ui/app.js");
const src = readFileSync(appPath, "utf8");

// Slice the three shipped pieces this behaviour is made of: the paused
// predicate over the per-queue runner map, the icon button factory, and the row
// builder that decides where the icon goes.
function slice(start, after) {
  const startIdx = src.indexOf(start);
  const nextIdx = src.indexOf(after, startIdx);
  assert.ok(startIdx !== -1 && nextIdx !== -1, `sentinels not found in app.js: ${start}`);
  return src.slice(startIdx, src.lastIndexOf("}", nextIdx) + 1);
}

const pausedSrc = slice("function isQueuePaused(name) {", "function currentTaskRecord()");
// playlistSpinner through playlistPauseButton — the row's two paused affordances.
const glyphSrc = slice("function playlistSpinner(running, paused) {", "function renderPlaylists()");
const ciDotSrc = slice("function ciDot(ciState) {", "const CHART_PALETTE");
const rowSrc = slice("function playlistRow(pl) {", "// Fill the add-queue repo dropdown");

// The icon must be the shipped transport pause glyph, not a test stand-in.
const svgLine = src.split("\n").find((l) => l.startsWith("const PAUSE_SVG ="));
assert.ok(svgLine, "PAUSE_SVG declaration not found in app.js");

// ---- Minimal DOM shim -----------------------------------------------------
// Only what a playlist row touches: props, classList, append/addEventListener,
// and an innerHTML setter that records the markup the row assigns.

function makeEl(tag = "div") {
  const classes = new Set();
  const el = {
    tag,
    textContent: "",
    title: "",
    html: "",
    dataset: {},
    attrs: {},
    children: [],
    handlers: {},
    classList: {
      add: (...cs) => cs.forEach((c) => classes.add(c)),
      contains: (c) => classes.has(c),
      toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
    },
    setAttribute(k, v) { this.attrs[k] = v; },
    append(...kids) { this.children.push(...kids); },
    addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); },
    fire(type, event) { for (const fn of this.handlers[type] || []) fn(event); },
  };
  Object.defineProperty(el, "className", {
    get() { return [...classes].join(" "); },
    set(v) { classes.clear(); String(v).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c)); },
  });
  Object.defineProperty(el, "innerHTML", {
    get() { return el.html; },
    set(v) { el.html = v; if (v === "") el.children.length = 0; },
  });
  return el;
}

// Build one row for `name` against a given per-queue runner map.
function buildRow(players, pl = { name: "nightly", task_count: 3 }) {
  const calls = { transports: [] };
  const state = {
    players,
    activePlaylist: null,
    selectedPlaylist: null,
    selectedPlaylists: new Set(),
    player: { state: "idle", active_playlist: null, running_playlist: null },
    view: "playlists",
  };
  const document = { createElement: makeEl };
  const playlistTransport = (action, name) => { calls.transports.push({ action, name }); };
  // eslint-disable-next-line no-new-func
  const factory = new Function(
    "document", "state", "playlistTransport", "isQueueRunning",
    "playlistMenuButton", "openCreateTaskForPlaylist", "openPlaylistInfo",
    "activatePlaylist", "PAUSE_REASON_COPY",
    `${svgLine}
     ${pausedSrc}
     ${glyphSrc}
     ${ciDotSrc}
     ${rowSrc}
     return { playlistRow, isQueuePaused };`,
  );
  const api = factory(
    document, state, playlistTransport,
    // The real isQueueRunning: a paused queue counts as running (it holds its
    // place in the run), which is exactly the case the frozen ring is for.
    (name) => ["playing", "paused"].includes((players[name || "main"] || {}).state),
    () => makeEl("button"), () => {}, () => {}, () => {},
    { consecutive_failures: "Paused: two unrelated tasks failed in a row." },
  );
  return { row: api.playlistRow(pl), isQueuePaused: api.isQueuePaused, state, calls };
}

const kids = (row, cls) => row.children.filter((k) => k.classList.contains(cls));
const indexOfClass = (row, cls) => row.children.findIndex((k) => k.classList.contains(cls));

let passed = 0;
function test(name, fn) {
  fn();
  passed++;
  console.log(`ok - ${name}`);
}

// 1) The common case: nothing paused, nothing added. The icon is state, so it
//    must not occupy the row when the queue is idle or playing.
test("an idle or playing queue's row carries no pause icon", () => {
  for (const st of ["idle", "playing"]) {
    const { row } = buildRow({ nightly: { state: st, pause_reason: null } });
    assert.equal(kids(row, "pl-paused").length, 0, `state=${st}`);
  }
});

// 2) A paused queue shows exactly one icon, and it sits immediately left of the
//    "+" — the placement the operator reads as "this queue is held".
test("a paused queue's row shows the pause icon just left of the +", () => {
  const { row } = buildRow({ nightly: { state: "paused", pause_reason: "operator" } });
  const pause = kids(row, "pl-paused");
  assert.equal(pause.length, 1);
  assert.equal(indexOfClass(row, "pl-paused") + 1, indexOfClass(row, "pl-add-task"));
  assert.match(pause[0].innerHTML, /^<svg /);
  assert.equal(pause[0].title, "Queue paused — click to resume");
});

// 3) The leading ring must not keep spinning under the pause icon: a held queue
//    would otherwise look exactly like one that's making progress.
test("the row's ring freezes while the queue is paused", () => {
  const paused = buildRow({ nightly: { state: "paused", pause_reason: "operator" } });
  // Located by class, not by index: the CI build-status dot now leads the row,
  // so the ring is no longer children[0]. The invariant under test is the
  // ring's frozen state, not its position.
  const held = kids(paused.row, "q-spinner")[0];
  assert.ok(held.classList.contains("spinning") && held.classList.contains("paused"));
  assert.equal(held.title, "Paused");

  const playing = buildRow({ nightly: { state: "playing", pause_reason: null } });
  const live = kids(playing.row, "q-spinner")[0];
  assert.ok(live.classList.contains("spinning") && !live.classList.contains("paused"));
  assert.equal(live.title, "Running");
});

// 4) A failure-policy pause is the same icon with the reason as its tooltip, so
//    hovering explains *why* the queue stopped.
test("a failure-policy pause explains itself in the tooltip", () => {
  const { row } = buildRow({ nightly: { state: "paused", pause_reason: "consecutive_failures" } });
  assert.equal(
    kids(row, "pl-paused")[0].title,
    "Paused: two unrelated tasks failed in a row.",
  );
});

// 5) Clicking the icon resumes *that* queue by name and must not bubble into
//    the row's own select handler.
test("clicking the pause icon resumes that queue without selecting the row", () => {
  const { row, calls } = buildRow({ nightly: { state: "paused", pause_reason: "operator" } });
  let stopped = false;
  kids(row, "pl-paused")[0].fire("click", { stopPropagation: () => { stopped = true; } });
  assert.deepEqual(calls.transports, [{ action: "play", name: "nightly" }]);
  assert.ok(stopped, "click must stopPropagation so the row isn't selected");
});

// 6) Whichever affordance unpauses — the icon or the row menu's Play — the
//    server reports the queue unpaused and the next render drops the icon.
test("the icon is gone once the queue reports unpaused", () => {
  const resumed = buildRow({ nightly: { state: "idle", pause_reason: null } });
  assert.equal(kids(resumed.row, "pl-paused").length, 0);
});

// 7) Queues are independent: pausing one must not mark the others.
test("only the paused queue's row is marked", () => {
  const players = {
    nightly: { state: "paused", pause_reason: "operator" },
    docs: { state: "playing", pause_reason: null },
  };
  assert.equal(kids(buildRow(players, { name: "nightly", task_count: 1 }).row, "pl-paused").length, 1);
  assert.equal(kids(buildRow(players, { name: "docs", task_count: 1 }).row, "pl-paused").length, 0);
  // A queue with no frame yet falls back to the single-context state, which
  // belongs to the focused queue — never to some other row.
  assert.equal(kids(buildRow(players, { name: "fresh", task_count: 0 }).row, "pl-paused").length, 0);
});

console.log(`\n${passed} passing`);
