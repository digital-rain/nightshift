// Headless test for the server-clock anchor and the running-task elapsed clock
// (app.js).
//
// Two faults, one surface. (1) Every age the UI draws — "12m ago", the live
// phase clock — was `Date.now() - <server timestamp>`, which is only correct
// while the browser and the manager host agree. They can silently disagree by
// hours: a suspended VM comes back slow and chrony *slews* rather than steps,
// so nothing catches up. A CI-fix task started 20 minutes earlier rendered as
// "10h ago", the exact width of the host's drift. The UI now subtracts against
// the server's own clock (sampled from /api/info's `server_now`) and warns when
// the gap is wide enough to be skewing scheduled work too. (2) The Now screen's
// live elapsed clock keyed off `rec.phase_started_at`, a field no server code
// has ever produced — so a running task showed no elapsed time at all. It now
// falls back to the attempt's own `started_at`.
//
// Loads the REAL source from app.js (sliced between sentinels) and drives it
// against a minimal DOM shim.
//
// Run: node --test tests/ui/ (or `node tests/ui/clock_skew_anchor.test.mjs`).

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const appPath = resolve(here, "../../src/nightshift/assets/ui/app.js");
const src = readFileSync(appPath, "utf8");

function slice(start, after) {
  const startIdx = src.indexOf(start);
  const nextIdx = src.indexOf(after, startIdx);
  assert.ok(startIdx !== -1 && nextIdx !== -1, `sentinels not found in app.js: ${start}`);
  return src.slice(startIdx, src.lastIndexOf("}", nextIdx) + 1);
}

// serverNow / applyServerClock / renderClockSkewBanner / formatWhen.
const clockSrc = slice("let clockOffsetMs = 0;", "async function clearCompleted()");
const elapsedSrc = slice("function formatElapsed(ms) {", "// Format a per-phase timing");
const execSrc = slice("function executionCard() {", "function phaseStepper(rec, status) {");

// ---- Minimal DOM shim -----------------------------------------------------

function makeEl(tag = "div") {
  const classes = new Set();
  const el = {
    tag,
    textContent: "",
    title: "",
    id: "",
    hidden: false,
    dataset: {},
    children: [],
    handlers: {},
    classList: {
      add: (...cs) => cs.forEach((c) => classes.add(c)),
      contains: (c) => classes.has(c),
      toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
    },
    setAttribute(k, v) { this.dataset[k] = v; },
    append(...kids) { this.children.push(...kids); },
    addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); },
  };
  Object.defineProperty(el, "className", {
    get() { return [...classes].join(" "); },
    set(v) { classes.clear(); String(v).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c)); },
  });
  Object.defineProperty(el, "innerHTML", { get() { return ""; }, set() {} });
  return el;
}

// Build the clock module with a stand-in banner element registered under its
// shipped id, so renderClockSkewBanner finds it exactly as the page would.
function makeClock() {
  const banner = makeEl("div");
  banner.hidden = true;
  const byId = { "clock-skew-banner": banner };
  const document = { createElement: makeEl, getElementById: (id) => byId[id] || null };
  const $ = (id) => document.getElementById(id);
  // eslint-disable-next-line no-new-func
  const factory = new Function(
    "document", "$",
    `${elapsedSrc}
     ${clockSrc}
     return { serverNow, applyServerClock, renderClockSkewBanner, formatWhen };`,
  );
  return { api: factory(document, $), banner };
}

// Build the Now screen's execution card for a given attempt record.
function buildExecCard(rec, { offsetMs = 0, playerState = "playing" } = {}) {
  const document = { createElement: makeEl };
  const state = {
    player: { state: playerState, now_playing: rec ? rec.task : null, run_id: "r1" },
    queue: [],
    logCache: { "r1/t1": "output" },
    currentRunId: "r1",
  };
  // eslint-disable-next-line no-new-func
  const factory = new Function(
    "document", "state", "clockOffsetMs", "currentTaskRecord", "openDetailScreen",
    "PAUSE_GLYPH", "PLAY_GLYPH", "togglePlayPause", "phaseLabels", "phaseIndex",
    "phaseStepper", "fetchLog", "renderNow", "logTail", "expando",
    `${elapsedSrc}
     function serverNow() { return Date.now() + clockOffsetMs; }
     ${execSrc}
     return executionCard;`,
  );
  const executionCard = factory(
    document, state, offsetMs, () => rec, () => {},
    "<svg/>", "<svg/>", () => {},
    () => [{ label: "Worker" }], () => 0,
    () => makeEl("div"), () => {}, () => {}, (t) => t,
    () => ({ panel: makeEl("div"), body: makeEl("div") }),
  );
  return executionCard();
}

// Depth-first search for a rendered element by id.
function findById(el, id) {
  if (el.id === id) return el;
  for (const kid of el.children || []) {
    const hit = findById(kid, id);
    if (hit) return hit;
  }
  return null;
}

let passed = 0;
function test(name, fn) {
  fn();
  passed++;
  console.log(`ok - ${name}`);
}

const TEN_HOURS = 10 * 3600 * 1000;

// 1) The reported fault, reproduced to the minute: the manager host is 10h
//    behind the browser, and a task it started 20 minutes ago must read "20m
//    ago" — not the drift.
test("a host clock 10h behind still ages a 20-minute-old task as 20m", () => {
  const { api } = makeClock();
  const hostNow = Date.now() - TEN_HOURS;
  api.applyServerClock({ server_now: new Date(hostNow).toISOString() });
  const startedAt = new Date(hostNow - 20 * 60000).toISOString();
  assert.equal(api.formatWhen(startedAt), "20m ago");
});

// 2) The same drift the other way (host ahead) must not read as "just now" —
//    an age clamped to zero hides a running task's real duration.
test("a host clock 10h ahead ages the same task as 20m, not 'just now'", () => {
  const { api } = makeClock();
  const hostNow = Date.now() + TEN_HOURS;
  api.applyServerClock({ server_now: new Date(hostNow).toISOString() });
  assert.equal(api.formatWhen(new Date(hostNow - 20 * 60000).toISOString()), "20m ago");
});

// 3) Agreeing clocks must be left alone — the anchor is a correction, not an
//    offset the common case pays for.
test("agreeing clocks leave ages unchanged and raise no banner", () => {
  const { api, banner } = makeClock();
  api.applyServerClock({ server_now: new Date().toISOString() });
  assert.equal(api.formatWhen(new Date(Date.now() - 20 * 60000).toISOString()), "20m ago");
  assert.equal(banner.hidden, true);
});

// 4) Correcting the display is not enough: the host fires scheduled work
//    against its own clock, so a wide gap has to be said out loud, with its
//    size and direction.
test("a wide gap raises the skew banner naming size and direction", () => {
  const { api, banner } = makeClock();
  api.applyServerClock({ server_now: new Date(Date.now() - TEN_HOURS).toISOString() });
  assert.equal(banner.hidden, false);
  assert.match(banner.textContent, /10h 00m behind/);
  assert.match(banner.textContent, /resync/i);
});

// 5) The operator fixing the host clock mid-session must clear the banner
//    without a reload — which is why the offset is re-sampled on every poll.
test("a later sample with the clocks agreeing clears the banner", () => {
  const { api, banner } = makeClock();
  api.applyServerClock({ server_now: new Date(Date.now() - TEN_HOURS).toISOString() });
  assert.equal(banner.hidden, false);
  api.applyServerClock({ server_now: new Date().toISOString() });
  assert.equal(banner.hidden, true);
});

// 6) A server that does not stamp server_now (an older manager, or the
//    single-process server) must leave the offset alone rather than zeroing it
//    into a wrong correction.
test("a payload without server_now leaves the anchor untouched", () => {
  const { api } = makeClock();
  api.applyServerClock({ server_now: new Date(Date.now() - TEN_HOURS).toISOString() });
  const before = api.serverNow();
  api.applyServerClock({});
  api.applyServerClock(null);
  api.applyServerClock({ server_now: "not a date" });
  assert.ok(Math.abs(api.serverNow() - before) < 1000);
});

// 7) The user's literal question: a running task must show how long it has been
//    executing. `phase_started_at` is emitted by nothing server-side, so the
//    clock keyed off it never rendered.
test("a running attempt shows an elapsed clock from started_at", () => {
  const card = buildExecCard({
    task: "t1", title: "fix ci", status: "running",
    started_at: new Date(Date.now() - 20 * 60000).toISOString(),
  });
  const el = findById(card, "now-elapsed");
  assert.ok(el, "no elapsed clock rendered for a running attempt");
  assert.match(el.textContent, /^Worker · 20m \d\ds$/);
});

// 8) That clock is subject to the same anchor: a drifted host must not inflate
//    the elapsed reading either.
test("the elapsed clock is anchored to the server clock too", () => {
  const hostNow = Date.now() - TEN_HOURS;
  const card = buildExecCard(
    {
      task: "t1", title: "fix ci", status: "running",
      started_at: new Date(hostNow - 20 * 60000).toISOString(),
    },
    { offsetMs: -TEN_HOURS },
  );
  const el = findById(card, "now-elapsed");
  assert.ok(el);
  assert.match(el.textContent, /^Worker · 20m/);
});

// 9) A real phase_started_at, should the server ever start emitting one, still
//    wins — the fallback is a fallback, not a replacement.
test("an explicit phase_started_at still takes precedence", () => {
  const card = buildExecCard({
    task: "t1", title: "fix ci", status: "running",
    started_at: new Date(Date.now() - 60 * 60000).toISOString(),
    phase_started_at: new Date(Date.now() - 5 * 60000).toISOString(),
  });
  const el = findById(card, "now-elapsed");
  assert.ok(el);
  assert.match(el.textContent, /^Worker · 5m/);
});

// 10) A paused transport still shows no clock — a held task is not accruing
//     execution time, and a ticking number would say it is.
test("a paused task shows no elapsed clock", () => {
  const card = buildExecCard(
    { task: "t1", title: "fix ci", status: "running", started_at: new Date().toISOString() },
    { playerState: "paused" },
  );
  assert.equal(findById(card, "now-elapsed"), null);
});

console.log(`\n${passed} passed`);
