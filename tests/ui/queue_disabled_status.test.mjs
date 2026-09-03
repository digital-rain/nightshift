// Headless test for how a disabled task reads in the UP NEXT list (app.js).
//
// A disabled row used to say two things at once: a small "disabled" badge out
// on the right, while the row's own status display still showed the blue
// QUEUED pill — the one place an operator looks to answer "is this going to
// run?" said yes. The status display now carries the state itself, as a gray
// DISABLED pill, and the badge is gone.
//
// Loads the REAL builders from app.js (sliced between sentinels), like the
// sibling queue tests.
//
// Run: node --test tests/ui/ (or `node tests/ui/queue_disabled_status.test.mjs`).

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

// STATE_LABELS through statusPill(), and the row's right-hand block.
const pillSrc = slice("const STATE_LABELS = {", "// Build-status dot for a Playlists row");
const asideSrc = slice("function queueRowAside(item, isNow) {", "// One queue row, shared by");

// ---- Minimal DOM shim -----------------------------------------------------

function makeEl(tag = "div") {
  return {
    tag,
    className: "",
    textContent: "",
    children: [],
    append(...kids) { this.children.push(...kids); },
    find(cls) { return this.children.find((k) => k.className.split(/\s+/).includes(cls)) || null; },
  };
}

// `rec` is the task's latest run record, or null when it has never run.
function load(rec = null, blockedTasks = {}) {
  const document = { createElement: makeEl };
  const state = { player: { state: "idle" }, blockedTasks };
  const latestRecordFor = () => (rec ? { rec, run: rec } : null);
  const formatWhen = () => "";
  const taskDuration = () => "";
  // eslint-disable-next-line no-new-func
  const factory = new Function(
    "document", "state", "latestRecordFor", "formatWhen", "taskDuration",
    `${pillSrc}\n${asideSrc}\n return { queueRowAside };`,
  );
  return factory(document, state, latestRecordFor, formatWhen, taskDuration);
}

// The pill inside a row's status column.
function pillFor(item, { rec = null, blocked = {} } = {}) {
  const aside = load(rec, blocked).queueRowAside(item, false);
  const box = aside.find("q-status");
  assert.ok(box && box.children.length === 1, "expected one status pill in .q-status");
  return box.children[0];
}

let passed = 0;
async function test(name, fn) {
  await fn();
  passed++;
  console.log(`ok - ${name}`);
}

// 1) The headline change: disabled reads as its own state, not as queued.
await test("a disabled task shows a gray DISABLED pill", async () => {
  const pill = pillFor({ task: "alpha", disabled: true });
  assert.equal(pill.textContent, "Disabled");
  assert.deepEqual(pill.className.split(/\s+/).sort(), ["disabled", "status"]);
});

// 2) The ordinary row is untouched — still the blue QUEUED pill.
await test("an enabled, never-run task still shows Queued", async () => {
  const pill = pillFor({ task: "alpha" });
  assert.equal(pill.textContent, "Queued");
  assert.deepEqual(pill.className.split(/\s+/).sort(), ["pending", "status"]);
});

// 3) Disabled is the task's current state, so it outranks a stale run record:
//    an old run's outcome must not mask the fact that it won't run again.
await test("disabled outranks a prior run record", async () => {
  const pill = pillFor({ task: "alpha", disabled: true }, { rec: { status: "stopped" } });
  assert.equal(pill.textContent, "Disabled");
});

// 4) …though the terminal flags the status control offers *instead* of
//    Disabled still win their own pill, and each keeps its own badge besides.
await test("completed still wins its pill over disabled", async () => {
  assert.equal(pillFor({ task: "a", disabled: true, completed: true }).textContent, "Completed");
  assert.equal(pillFor({ task: "a", disabled: true, quarantined: true }).textContent, "Quarantined");
});

// 5) A blocked overlay on a parked task is not news — the operator turned this
//    one off, so the row says so rather than asking for attention it won't get.
await test("disabled outranks a blocked overlay", async () => {
  const pill = pillFor({ task: "a", disabled: true }, { blocked: { a: { state: "blocked" } } });
  assert.equal(pill.textContent, "Disabled");
});

// 6) One signal, not two: the row builder no longer appends a "disabled" badge
//    beside the pill. Pinned against the source because building a whole row
//    would drag in drag handlers, menus and the workflow badge.
await test("the queue row no longer builds a disabled badge", async () => {
  const rowSrc = slice("function queueItemRow(item) {", "// Queue-row gestures");
  assert.doesNotMatch(rowSrc, /textContent = "disabled"/,
    "queueItemRow should leave the disabled state to the status pill");
});

console.log(`\n${passed} passed`);
