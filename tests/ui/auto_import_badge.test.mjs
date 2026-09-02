// Headless test for the auto-import badge (app.js).
//
// The badge is the Playlists/queue-chrome surface for a switch that lives on
// the Repos page, so the thing worth pinning is that it is *only* on when the
// payload says the queue is draining, and that it never states its case in
// colour alone -- the house rule `ciDot`/`ciBadge` follow (colour plus a
// readable label), which a green pill carrying one letter can only satisfy
// through its title/aria-label.
//
// Loads the REAL builders from app.js (sliced between sentinels), like the
// sibling repos test.
//
// Run: node --test tests/ui/ (or `node tests/ui/auto_import_badge.test.mjs`).

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const appPath = resolve(here, "../../src/nightshift/assets/ui/app.js");
const src = readFileSync(appPath, "utf8");

// From activePlaylistRow() through the end of autoImportBadge(), which is the
// last `}` before the Repos screen renderer that follows.
const START = "function activePlaylistRow() {";
const startIdx = src.indexOf(START);
const nextIdx = src.indexOf("function renderRepos() {", startIdx);
assert.ok(startIdx !== -1 && nextIdx !== -1, "badge sentinels not found in app.js");
const badgeSrc = src.slice(startIdx, src.lastIndexOf("}", nextIdx) + 1);

// ---- Minimal DOM shim -----------------------------------------------------
// Unlike the sibling repos test this one records setAttribute: the badge's
// accessible name is the assertion, not incidental.

function makeEl(tag = "div") {
  return {
    tag,
    className: "",
    textContent: "",
    title: "",
    attrs: {},
    children: [],
    append(...kids) { this.children.push(...kids); },
    setAttribute(name, value) { this.attrs[name] = value; },
  };
}

function load(playlists, activePlaylist) {
  const document = { createElement: makeEl };
  const state = { playlists, activePlaylist };
  // eslint-disable-next-line no-new-func
  const factory = new Function(
    "document", "state",
    `${badgeSrc}; return { activePlaylistRow, autoImportBadge };`,
  );
  return factory(document, state);
}

const DRAINING = {
  name: "longitude", auto_import: true, host_queue: "longitude", repo: "longitude",
};
const IDLE = {
  name: "nightshift", auto_import: false, host_queue: null, repo: "nightshift",
};

let passed = 0;
async function test(name, fn) {
  await fn();
  passed++;
  console.log(`ok - ${name}`);
}

// 1) On: a green pill carrying the letter, naming the inbox it drains.
await test("a draining queue gets a green [A] badge naming its inbox", async () => {
  const p = load([DRAINING], "longitude");
  const badge = p.autoImportBadge(DRAINING);
  assert.ok(badge, "expected a badge");
  assert.equal(badge.textContent, "A");
  assert.deepEqual(badge.className.split(/\s+/).sort(), ["auto-import", "badge"]);
  assert.match(badge.attrs["aria-label"], /longitude\/\.tasks\/longitude/);
});

// 2) Colour is never the only signal: the accessible name says what it means,
//    and the title adds the one-at-a-time cadence the letter cannot carry.
await test("the badge states its meaning in text, not just colour", async () => {
  const badge = load([DRAINING], "longitude").autoImportBadge(DRAINING);
  assert.match(badge.attrs["aria-label"], /^Auto-import on/);
  assert.match(badge.title, /one at a time/);
});

// 3) Off: no badge at all, so both call sites can append unconditionally.
await test("a queue that is not draining gets no badge", async () => {
  const p = load([IDLE], "nightshift");
  assert.equal(p.autoImportBadge(IDLE), null);
});

// 4) The queue chrome reads the row for the focused queue by name...
await test("the chrome resolves the active playlist's row", async () => {
  const p = load([IDLE, DRAINING], "longitude");
  assert.equal(p.activePlaylistRow(), DRAINING,
    "should pick the row matching state.activePlaylist");
});

// 5) ...and the main queue has no row: it is the separate "library", with no
//    repo binding to import from, so the chrome must not throw looking for it.
await test("the main queue resolves to no row rather than throwing", async () => {
  const p = load([DRAINING], null);
  assert.equal(p.activePlaylistRow(), null);
  assert.equal(p.autoImportBadge(p.activePlaylistRow()), null);
});

// 6) A row the poll has not delivered yet (first paint) is the same no-badge
//    path, not a crash on an undefined field.
await test("a missing row is treated as not draining", async () => {
  const p = load([], "longitude");
  assert.equal(p.activePlaylistRow(), null);
  assert.equal(p.autoImportBadge(undefined), null);
});

console.log(`\n${passed} passed`);
