// Headless test for the repo-import picker's per-task selection (app.js).
//
// The "Import from repository" modal used to offer only import-everything or
// cancel. It now ticks briefs one by one: the operator controls exactly which
// ones are drained, and the POST carries their repo-relative `source` paths.
// This test loads the REAL picker source from app.js (sliced between
// sentinels) and drives it against a minimal DOM shim.
//
// Run: node --test tests/ui/ (or `node tests/ui/repo_import_select.test.mjs`).

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const appPath = resolve(here, "../../src/nightshift/assets/ui/app.js");
const src = readFileSync(appPath, "utf8");

// Pull the picker block out of the shipped file so we test real code: from
// openRepoImport() through the end of runRepoImport(), which is the last `}`
// before the Add-to-playlist section that follows.
const START = "async function openRepoImport() {";
const startIdx = src.indexOf(START);
const nextIdx = src.indexOf("// ----- Add to another playlist", startIdx);
assert.ok(startIdx !== -1 && nextIdx !== -1, "picker sentinels not found in app.js");
const pickerSrc = src.slice(startIdx, src.lastIndexOf("}", nextIdx) + 1);

// ---- Minimal DOM shim -----------------------------------------------------
// Only what the picker touches: element props, append/addEventListener, and a
// querySelectorAll that understands the one class selector it uses.

function makeEl(tag = "div") {
  const el = {
    tag,
    className: "",
    textContent: "",
    title: "",
    type: "",
    hidden: false,
    disabled: false,
    checked: false,
    indeterminate: false,
    dataset: {},
    children: [],
    handlers: {},
    append(...kids) { this.children.push(...kids); },
    addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); },
    fire(type, event) { for (const fn of this.handlers[type] || []) fn(event); },
    querySelectorAll(sel) {
      const cls = sel.replace(/^\./, "");
      const out = [];
      const walk = (node) => {
        for (const kid of node.children) {
          if (String(kid.className).split(/\s+/).includes(cls)) out.push(kid);
          walk(kid);
        }
      };
      walk(this);
      return out;
    },
  };
  // `innerHTML = ""` is how the picker empties the list.
  Object.defineProperty(el, "innerHTML", {
    get() { return ""; },
    set(v) { if (v === "") el.children.length = 0; },
  });
  return el;
}

const IDS = [
  "repoimport-modal", "repoimport-desc", "repoimport-status", "repoimport-empty",
  "repoimport-list", "repoimport-all", "repoimport-all-check", "repoimport-go",
];

function loadPicker({ preview, post } = {}) {
  const els = Object.fromEntries(IDS.map((id) => [id, makeEl()]));
  const calls = { previews: 0, posts: [], queueReloads: 0 };
  const $ = (id) => {
    assert.ok(els[id], `picker touched an unknown element id: ${id}`);
    return els[id];
  };
  const document = { createElement: makeEl };
  const getJSON = async () => {
    calls.previews++;
    return typeof preview === "function" ? preview(calls.previews) : preview;
  };
  const sendJSON = async (url, method, body) => {
    calls.posts.push({ url, method, body });
    return { ok: true, data: post || { imported: [], deduped: [], removed: true } };
  };
  const loadQueue = async () => { calls.queueReloads++; };
  // eslint-disable-next-line no-new-func
  const factory = new Function(
    "$", "document", "getJSON", "sendJSON", "loadQueue", "queueParam",
    `${pickerSrc}; return { openRepoImport, runRepoImport, syncRepoImportSelection,
       toggleAllRepoImport, repoImportChecks };`,
  );
  const api = factory($, document, getJSON, sendJSON, loadQueue, () => "");
  return { els, calls, ...api };
}

const PREVIEW = {
  available: true,
  repo: "longitude",
  count: 3,
  tasks: [
    { task: "alpha", title: "Alpha", source: ".tasks/alpha.md" },
    { task: "beta", title: "Beta", source: ".tasks/beta.md" },
    // Same stem as the flat one below it in a real inbox: a distinct brief.
    { task: "alpha", title: "Alpha (queue dir)", source: ".tasks/main/alpha.md" },
  ],
};

// A click on the row's title (not the box) — the row is the toggle.
function clickRow(picker, i) {
  const li = picker.els["repoimport-list"].children[i];
  li.fire("click", { target: li.children[1] });
}

let passed = 0;
async function test(name, fn) {
  await fn();
  passed++;
  console.log(`ok - ${name}`);
}

// 1) Everything starts ticked — draining the whole inbox is the common case.
await test("opens with every brief ticked and the count on the button", async () => {
  const p = loadPicker({ preview: PREVIEW });
  await p.openRepoImport();
  assert.equal(p.repoImportChecks().length, 3);
  assert.ok(p.repoImportChecks().every((b) => b.checked));
  assert.equal(p.els["repoimport-go"].textContent, "Import 3 tasks");
  assert.equal(p.els["repoimport-go"].disabled, false);
  assert.equal(p.els["repoimport-go"].hidden, false);
  assert.equal(p.els["repoimport-all"].hidden, false);
  assert.equal(p.els["repoimport-all-check"].checked, true);
  assert.equal(p.els["repoimport-all-check"].indeterminate, false);
});

// 2) De-selecting one row: the count follows and select-all goes tri-state.
await test("row click de-selects and the button count follows", async () => {
  const p = loadPicker({ preview: PREVIEW });
  await p.openRepoImport();
  clickRow(p, 1);
  assert.equal(p.repoImportChecks()[1].checked, false);
  assert.equal(p.els["repoimport-go"].textContent, "Import 2 tasks");
  assert.equal(p.els["repoimport-all-check"].checked, false);
  assert.equal(p.els["repoimport-all-check"].indeterminate, true);
  clickRow(p, 1);   // and back
  assert.equal(p.els["repoimport-go"].textContent, "Import 3 tasks");
  assert.equal(p.els["repoimport-all-check"].indeterminate, false);
});

// 3) A click landing on the box itself already flipped it — don't undo that.
await test("clicking the box itself toggles exactly once", async () => {
  const p = loadPicker({ preview: PREVIEW });
  await p.openRepoImport();
  const li = p.els["repoimport-list"].children[0];
  const box = li.children[0];
  box.checked = false;                    // what the browser does before the click
  li.fire("click", { target: box });
  assert.equal(box.checked, false);
  assert.equal(p.els["repoimport-go"].textContent, "Import 2 tasks");
});

// 4) Select-all drives every row; nothing ticked leaves the button inert.
await test("select-all toggles every row, empty selection disables Import", async () => {
  const p = loadPicker({ preview: PREVIEW });
  await p.openRepoImport();
  p.els["repoimport-all-check"].checked = false;
  p.toggleAllRepoImport();
  assert.ok(p.repoImportChecks().every((b) => !b.checked));
  assert.equal(p.els["repoimport-go"].textContent, "Import 0 tasks");
  assert.equal(p.els["repoimport-go"].disabled, true);

  p.els["repoimport-all-check"].checked = true;
  p.toggleAllRepoImport();
  assert.ok(p.repoImportChecks().every((b) => b.checked));
  assert.equal(p.els["repoimport-go"].disabled, false);
});

// 5) The POST carries exactly the ticked sources — by path, so the two briefs
//    sharing the stem "alpha" stay distinct.
await test("import posts only the ticked sources, keyed on source path", async () => {
  const p = loadPicker({
    preview: PREVIEW,
    post: { imported: [{ task: "alpha" }], deduped: [], removed: true, missing: [] },
  });
  await p.openRepoImport();
  clickRow(p, 0);   // drop the flat alpha
  clickRow(p, 1);   // drop beta
  await p.runRepoImport();
  assert.equal(p.calls.posts.length, 1);
  assert.deepEqual(p.calls.posts[0].body, { sources: [".tasks/main/alpha.md"] });
  assert.equal(p.calls.posts[0].method, "POST");
  assert.equal(p.calls.queueReloads, 1);
});

// 6) An inert button imports nothing (no accidental drain-everything).
await test("import with nothing ticked posts nothing", async () => {
  const p = loadPicker({ preview: PREVIEW });
  await p.openRepoImport();
  p.els["repoimport-all-check"].checked = false;
  p.toggleAllRepoImport();
  await p.runRepoImport();
  assert.equal(p.calls.posts.length, 0);
});

// 7) A partial import leaves the rest published, so the modal re-scans and
//    stays usable for a second pass.
await test("partial import re-scans and keeps the leftovers on offer", async () => {
  const rest = {
    available: true, repo: "longitude", count: 1,
    tasks: [{ task: "beta", title: "Beta", source: ".tasks/beta.md" }],
  };
  const p = loadPicker({
    preview: (n) => (n === 1 ? PREVIEW : rest),
    post: { imported: [{ task: "alpha" }], deduped: [], removed: true, missing: [] },
  });
  await p.openRepoImport();
  await p.runRepoImport();
  assert.equal(p.calls.previews, 2, "the inbox is re-scanned after an import");
  assert.deepEqual(p.repoImportChecks().map((b) => b.dataset.source), [".tasks/beta.md"]);
  assert.equal(p.els["repoimport-go"].textContent, "Import 1 task");
  assert.equal(p.els["repoimport-go"].disabled, false);
  assert.match(p.els["repoimport-status"].textContent, /^Imported 1 task/);
});

// 8) Stale picks are reported, not silently swallowed.
await test("missing sources are reported in the status line", async () => {
  const p = loadPicker({
    preview: PREVIEW,
    post: {
      imported: [{ task: "alpha" }], deduped: [], removed: true,
      missing: [".tasks/beta.md"],
    },
  });
  await p.openRepoImport();
  await p.runRepoImport();
  assert.match(
    p.els["repoimport-status"].textContent,
    /1 selected task was no longer published/,
  );
});

// 9) Drained inbox: empty state, no picker chrome left behind.
await test("a drained inbox shows the empty state and hides the picker", async () => {
  const p = loadPicker({
    preview: { available: true, repo: "longitude", count: 0, tasks: [] },
  });
  await p.openRepoImport();
  assert.equal(p.els["repoimport-empty"].hidden, false);
  assert.equal(
    p.els["repoimport-empty"].textContent,
    "No importable tasks in longitude/.tasks.",
  );
  assert.equal(p.els["repoimport-all"].hidden, true);
  assert.equal(p.els["repoimport-go"].hidden, true);
});

console.log(`\n${passed} passed`);
