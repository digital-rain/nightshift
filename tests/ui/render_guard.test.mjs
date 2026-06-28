// Headless test for the app.js render guard (renderList / pointer-hold deferral).
//
// The manager UI rebuilds its list rows from scratch on every poll/SSE event.
// If a rebuild lands between mousedown and mouseup it swaps the row node out and
// the browser drops the click — the "controls feel unresponsive" bug. The guard
// defers rebuilds while the pointer is held inside an interactive list and
// replays them on release. This test loads the REAL guard source from app.js
// (sliced between sentinels) and exercises the exact failing sequence.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const appPath = resolve(here, "../../src/nightshift/assets/ui/app.js");
const src = readFileSync(appPath, "utf8");

// Pull just the guard block out of the shipped file so we test real code. The
// block runs from the guard state declaration through the end of renderList(),
// which is the last `}` before the `const state = {` declaration that follows.
const START = "const guardedRenderers = new Map();";
const startIdx = src.indexOf(START);
const stateIdx = src.indexOf("const state = {", startIdx);
assert.ok(startIdx !== -1 && stateIdx !== -1, "guard block sentinels not found in app.js");
const endIdx = src.lastIndexOf("}", stateIdx);
const guardSrc = src.slice(startIdx, endIdx + 1);

// Minimal document shim: capture-phase listeners + a closest() that honours the
// list selectors the guard cares about.
function makeDoc() {
  const listeners = {};
  return {
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    _fire(type, target) { for (const fn of listeners[type] || []) fn({ target }); },
  };
}

function inList(id) {
  return {
    closest(sel) {
      return sel.includes(`#${id}`) ? { id } : null;
    },
  };
}
const outsideList = { closest: () => null };

function loadGuard() {
  const document = makeDoc();
  // eslint-disable-next-line no-new-func
  const factory = new Function(
    "document",
    `${guardSrc}; return { renderList, _fire: document._fire.bind(document) };`,
  );
  const api = factory(document);
  return { document, ...api };
}

let passed = 0;
function test(name, fn) {
  fn();
  passed++;
  console.log(`ok - ${name}`);
}

// 1) No press: a render runs immediately.
test("render runs immediately when no pointer is held", () => {
  const g = loadGuard();
  let builds = 0;
  g.renderList("queue", () => builds++);
  assert.equal(builds, 1);
});

// 2) The failing scenario: press in a list, render storm, release.
//    Rebuilds must be deferred during the press and replayed exactly once after.
test("rebuilds deferred during press, replayed on release", () => {
  const g = loadGuard();
  let builds = 0;
  const build = () => builds++;

  g._fire("pointerdown", inList("playlists")); // user presses a playlist row
  g.renderList("playlists", build);            // SSE storm tries to rebuild...
  g.renderList("playlists", build);            // ...repeatedly
  g.renderList("playlists", build);
  assert.equal(builds, 0, "rebuild must be deferred while the row is held");

  g._fire("pointerup", inList("playlists"));   // release: the click can land first
  assert.equal(builds, 1, "deferred rebuild replays exactly once on release");
});

// 3) A press OUTSIDE the interactive lists must not defer anything.
test("press outside the lists does not defer renders", () => {
  const g = loadGuard();
  let builds = 0;
  g._fire("pointerdown", outsideList);
  g.renderList("queue", () => builds++);
  assert.equal(builds, 1);
});

// 4) Multiple lists deferred during one gesture all replay on release.
test("all guarded lists replay on release", () => {
  const g = loadGuard();
  const counts = { queue: 0, playlists: 0, "now-body": 0 };
  g._fire("pointerdown", inList("queue"));
  g.renderList("queue", () => counts.queue++);
  g.renderList("playlists", () => counts.playlists++);
  g.renderList("now-body", () => counts["now-body"]++);
  assert.deepEqual(counts, { queue: 0, playlists: 0, "now-body": 0 });
  g._fire("pointerup", inList("queue"));
  assert.deepEqual(counts, { queue: 1, playlists: 1, "now-body": 1 });
});

// 5) pointercancel (e.g. scroll/gesture interrupt) also releases + replays.
test("pointercancel releases the hold and replays", () => {
  const g = loadGuard();
  let builds = 0;
  g._fire("pointerdown", inList("queue"));
  g.renderList("queue", () => builds++);
  assert.equal(builds, 0);
  g._fire("pointercancel", inList("queue"));
  assert.equal(builds, 1);
});

console.log(`\n${passed} passed`);
