// Headless test for the queue chrome's play/pause control (app.js + index.html).
//
// The UP NEXT screen could show a queue but not hold it: pausing meant reaching
// for the bottom transport, and there was no sign in the chrome that the queue
// you were looking at was already paused. The chrome now carries a play/pause
// button immediately left of "+ Add" that drives the focused queue — Pause
// while it plays, Resume once held, Play when idle — and toggles in place
// rather than following the run to Now.
//
// This test loads the REAL sources: the button's markup from index.html, and
// the sync/toggle/transport functions from app.js (sliced between sentinels),
// driven against a minimal DOM shim.
//
// Run: node --test tests/ui/ (or `node tests/ui/queue_chrome_transport.test.mjs`).

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const appPath = resolve(here, "../../src/nightshift/assets/ui/app.js");
const htmlPath = resolve(here, "../../src/nightshift/assets/ui/index.html");
const src = readFileSync(appPath, "utf8");
const html = readFileSync(htmlPath, "utf8");

function slice(start, after) {
  const startIdx = src.indexOf(start);
  const nextIdx = src.indexOf(after, startIdx);
  assert.ok(startIdx !== -1 && nextIdx !== -1, `sentinels not found in app.js: ${start}`);
  return src.slice(startIdx, src.lastIndexOf("}", nextIdx) + 1);
}

const transportSrc = slice("async function transport(action", "// The single play/pause control");
const toggleSrc = slice("function toggleQueuePlayPause() {", "// The map key for a queue");
const syncSrc = slice("function syncQueuePlayButton() {", "function renderQueue() {");

// The glyphs must be the shipped transport ones, not test stand-ins.
const svgLines = src.split("\n").filter((l) => /^const (PLAY|PAUSE)_SVG =/.test(l));
assert.equal(svgLines.length, 2, "PLAY_SVG/PAUSE_SVG declarations not found in app.js");

// ---- Minimal DOM shim -----------------------------------------------------
// Only what the chrome control touches: title/aria, classList.toggle, an
// innerHTML setter per child slot, and querySelector over direct children.

function makeEl(tag = "div", cls = "") {
  const classes = new Set(cls.split(/\s+/).filter(Boolean));
  const el = {
    tag,
    title: "",
    textContent: "",
    html: "",
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
    querySelector(sel) {
      const want = sel.replace(/^\./, "");
      return this.children.find((k) => k.classList.contains(want)) || null;
    },
  };
  Object.defineProperty(el, "className", {
    get() { return [...classes].join(" "); },
  });
  Object.defineProperty(el, "innerHTML", {
    get() { return el.html; },
    set(v) { el.html = v; },
  });
  return el;
}

// A chrome button wired the way index.html ships it.
function makeChromeButton() {
  const btn = makeEl("button", "ghost-btn queue-play");
  btn.append(makeEl("span", "queue-play-glyph"), makeEl("span", "queue-play-label"));
  return btn;
}

// Build the control for a focused queue in `playerState`, returning the button
// plus the transport requests its click produced.
function harness(playerState, transportReply) {
  const btn = makeChromeButton();
  const calls = { posts: [], views: [] };
  const state = { player: { state: playerState }, activePlaylist: "nightly" };
  const factory = new Function(
    "$", "state", "sendJSON", "setView", "getMode", "ingestState",
    "PLAY_SVG", "PAUSE_SVG",
    `${transportSrc}
     ${toggleSrc}
     ${syncSrc}
     return { transport, toggleQueuePlayPause, syncQueuePlayButton };`,
  );
  const api = factory(
    (id) => (id === "queue-play" ? btn : null),
    state,
    async (path, method, body) => {
      calls.posts.push({ path, method, body });
      return { ok: true, data: transportReply ?? { state: "paused" } };
    },
    (v) => calls.views.push(v),
    () => "auto",
    () => {},
    "<svg data-glyph=\"play\"></svg>",
    "<svg data-glyph=\"pause\"></svg>",
  );
  api.syncQueuePlayButton();
  return { btn, calls, state, api };
}

const glyphOf = (btn) => btn.querySelector(".queue-play-glyph").innerHTML;
const labelOf = (btn) => btn.querySelector(".queue-play-label").textContent;

let passed = 0;
const tests = [];
function test(name, fn) { tests.push([name, fn]); }

// 1) Placement is the whole request: the control must live in the queue chrome,
//    ahead of the "+ Add" menu it sits left of.
test("the control ships in the queue chrome, left of + Add", () => {
  const chrome = html.indexOf('<div class="queue-chrome">');
  const chromeEnd = html.indexOf('id="queue-pause-banner"', chrome);
  assert.ok(chrome !== -1 && chromeEnd !== -1, "queue chrome not found in index.html");
  const play = html.indexOf('id="queue-play"', chrome);
  const add = html.indexOf('id="btn-add-menu"', chrome);
  assert.ok(play !== -1, "no #queue-play button in the queue chrome");
  assert.ok(play < chromeEnd, "#queue-play must be inside the queue chrome");
  assert.ok(play < add, "#queue-play must come before the + Add button");
});

// 2) Playing queue: the control offers PAUSE, with the pause glyph.
test("a playing queue's control reads Pause", () => {
  const { btn } = harness("playing");
  assert.equal(labelOf(btn), "Pause");
  assert.equal(btn.title, "Pause this queue");
  assert.equal(btn.attrs["aria-label"], "Pause this queue");
  assert.match(glyphOf(btn), /data-glyph="pause"/);
  assert.ok(!btn.classList.contains("paused"));
});

// 3) The core of the ask: pressing PAUSE pauses the queue, and the button
//    becomes RESUME once the server reports the pause.
test("pressing Pause pauses the queue and turns into Resume", async () => {
  const h = harness("playing", { state: "paused" });
  await h.api.toggleQueuePlayPause();
  assert.deepEqual(h.calls.posts.map((p) => [p.path, p.method, p.body.action, p.body.queue]),
    [["/api/transport", "POST", "pause", "nightly"]]);

  // The server's reply is what flips the control; re-sync against it.
  h.state.player.state = "paused";
  h.api.syncQueuePlayButton();
  assert.equal(labelOf(h.btn), "Resume");
  assert.equal(h.btn.title, "Resume this queue");
  assert.match(glyphOf(h.btn), /data-glyph="play"/);
  // Amber while held, so the chrome carries the state and not just the action.
  assert.ok(h.btn.classList.contains("paused"));
});

// 4) …and pressing RESUME sends play and goes back to PAUSE.
test("pressing Resume plays the queue and turns back into Pause", async () => {
  const h = harness("paused", { state: "playing" });
  assert.equal(labelOf(h.btn), "Resume");
  await h.api.toggleQueuePlayPause();
  assert.deepEqual(h.calls.posts.map((p) => p.body.action), ["play"]);

  h.state.player.state = "playing";
  h.api.syncQueuePlayButton();
  assert.equal(labelOf(h.btn), "Pause");
  assert.ok(!h.btn.classList.contains("paused"));
});

// 5) Resuming from this control must not navigate away — the operator is
//    watching UP NEXT and the button has to flip under their cursor. (The
//    bottom-bar play still follows the run to Now; only `follow: false` opts
//    out, which is what this control passes.)
test("the chrome control never follows the run to the Now screen", async () => {
  const h = harness("paused", { state: "playing" });
  await h.api.toggleQueuePlayPause();
  assert.deepEqual(h.calls.views, [], "chrome play must stay on the queue view");

  // Same call without the opt-out still follows, so the default is intact.
  const other = harness("paused", { state: "playing" });
  await other.api.transport("play");
  assert.deepEqual(other.calls.views, ["now"]);
});

// 6) An idle queue is still actionable: the control starts the queue.
test("an idle queue's control reads Play and starts the queue", async () => {
  const h = harness("idle", { state: "playing" });
  assert.equal(labelOf(h.btn), "Play");
  assert.match(glyphOf(h.btn), /data-glyph="play"/);
  assert.ok(!h.btn.classList.contains("paused"));
  await h.api.toggleQueuePlayPause();
  assert.deepEqual(h.calls.posts.map((p) => p.body.action), ["play"]);
});

// 7) It drives the queue on screen, not "whatever ran last": the request
//    carries the focused queue, and the main queue sends null (the server's
//    "focused queue" sentinel) rather than a name.
test("the control targets the focused queue", async () => {
  const h = harness("playing");
  h.state.activePlaylist = "docs";
  await h.api.toggleQueuePlayPause();
  assert.equal(h.calls.posts[0].body.queue, "docs");

  const main = harness("playing");
  main.state.activePlaylist = null;
  await main.api.toggleQueuePlayPause();
  assert.equal(main.calls.posts[0].body.queue, null);
});

for (const [name, fn] of tests) {
  await fn();
  passed++;
  console.log(`ok - ${name}`);
}
console.log(`\n${passed} passing`);
