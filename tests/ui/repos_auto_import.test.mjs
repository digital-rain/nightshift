// Headless test for the Repos page's auto-import controls (app.js).
//
// Two affordances, both live-persisting: the per-repo YES/NO switch on a
// Known-repos row, and the per-queue "Host task queue" dropdown that only
// appears once the bound repo's switch is on. This test loads the REAL row
// builders from app.js (sliced between sentinels) and drives them against the
// same minimal DOM shim the repo-import picker test uses.
//
// Run: node --test tests/ui/ (or `node tests/ui/repos_auto_import.test.mjs`).

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const appPath = resolve(here, "../../src/nightshift/assets/ui/app.js");
const src = readFileSync(appPath, "utf8");

// From repoRow() through the end of setQueueHostQueue(), which is the last `}`
// before the default-repo persist helper that follows.
const START = "function repoRow(r, tasksRepo) {";
const startIdx = src.indexOf(START);
const nextIdx = src.indexOf("// Persist a queue's default target repo", startIdx);
assert.ok(startIdx !== -1 && nextIdx !== -1, "repos sentinels not found in app.js");
const rowsSrc = src.slice(startIdx, src.lastIndexOf("}", nextIdx) + 1);

// ---- Minimal DOM shim -----------------------------------------------------

function makeEl(tag = "div") {
  const el = {
    tag,
    className: "",
    textContent: "",
    value: "",
    hidden: false,
    dataset: {},
    style: {},
    classes: new Set(),
    children: [],
    handlers: {},
    append(...kids) { this.children.push(...kids); },
    setAttribute() {},
    addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); },
    fire(type, event) { for (const fn of this.handlers[type] || []) fn(event); },
    classList: {
      toggle(cls, on) { on ? el.classes.add(cls) : el.classes.delete(cls); },
      contains(cls) { return el.classes.has(cls); },
    },
  };
  return el;
}

// Depth-first walk collecting every element whose className includes `cls`.
function find(root, cls) {
  const out = [];
  const walk = (node) => {
    for (const kid of node.children || []) {
      if (String(kid.className).split(/\s+/).includes(cls)) out.push(kid);
      walk(kid);
    }
  };
  walk(root);
  return out;
}

function load({ put } = {}) {
  const calls = { puts: [], repoReloads: 0, renders: 0 };
  const document = { createElement: makeEl };
  const sendJSON = async (url, method, body) => {
    calls.puts.push({ url, method, body });
    return { ok: put ? put.ok !== false : true, data: put ? put.data : {} };
  };
  const state = { repos: null };
  const loadRepos = async () => { calls.repoReloads++; };
  const renderRepos = () => { calls.renders++; };
  // Sibling builders the sliced block calls but does not define.
  const availabilityBadge = () => makeEl("span");
  const monitoringBadge = () => makeEl("span");
  const ciBadge = () => null;
  const repoSelect = () => {
    const el = makeEl("select");
    el.className = "ctl-select repo-select";   // as the real one names itself
    return el;
  };
  const setQueueRepo = async () => {};
  // eslint-disable-next-line no-new-func
  const factory = new Function(
    "document", "sendJSON", "state", "loadRepos", "renderRepos",
    "availabilityBadge", "monitoringBadge", "ciBadge", "repoSelect", "setQueueRepo",
    `${rowsSrc}; return { repoRow, repoQueueRow, hostQueueSelect, setQueueHostQueue };`,
  );
  const api = factory(
    document, sendJSON, state, loadRepos, renderRepos,
    availabilityBadge, monitoringBadge, ciBadge, repoSelect, setQueueRepo,
  );
  return { calls, state, ...api };
}

const REPO = { name: "longitude", available: true, monitored: false, auto_import: false,
               task_queues: [], ci: null };
const QUEUE = { queue: "main", repo: "longitude", available: true, ci_monitoring: false,
                auto_import: true, host_queue: "main", host_queues: ["main", "nightly"] };

// The Yes/No pair inside a row, in render order.
function segButtons(row) {
  return find(row, "seg-opt");
}

let passed = 0;
async function test(name, fn) {
  await fn();
  passed++;
  console.log(`ok - ${name}`);
}

// 1) The switch paints the stored state — off means No is the active option.
await test("a repo row renders the auto-import switch reflecting its state", async () => {
  const p = load();
  const off = segButtons(p.repoRow(REPO, "nightshift-tasks"));
  assert.deepEqual(off.map((b) => b.textContent), ["Yes", "No"]);
  assert.equal(off[0].classList.contains("on"), false);
  assert.equal(off[1].classList.contains("on"), true);

  const on = segButtons(p.repoRow({ ...REPO, auto_import: true }, "nightshift-tasks"));
  assert.equal(on[0].classList.contains("on"), true);
});

// 2) Flipping it PUTs the repo-scoped body and re-pulls the page — turning the
//    switch on is what makes the host-queue dropdowns below appear.
await test("flipping the switch PUTs the repo and reloads the page", async () => {
  const p = load();
  const [yes] = segButtons(p.repoRow(REPO, "nightshift-tasks"));
  await yes.fire("click");
  assert.equal(p.calls.puts.length, 1);
  assert.equal(p.calls.puts[0].url, "/api/repos/auto-import");
  assert.equal(p.calls.puts[0].method, "PUT");
  assert.deepEqual(p.calls.puts[0].body, { repo: "longitude", enabled: true });
  assert.equal(p.calls.repoReloads, 1);
});

// 3) The host-queue dropdown only exists once the repo's switch is on.
await test("the host-queue dropdown appears only for an auto-import repo", async () => {
  const p = load();
  assert.equal(find(p.repoQueueRow(QUEUE), "repo-select").length, 2);
  const off = p.repoQueueRow({ ...QUEUE, auto_import: false, host_queue: null });
  assert.equal(find(off, "repo-select").length, 1);   // just the Default repo one
});

// 4) Options are the discovered subdirs, and the resolved default (which the
//    operator never saved) shows pre-selected.
await test("the dropdown offers the discovered host queues, default selected", async () => {
  const p = load();
  const select = p.hostQueueSelect("main", ["main", "nightly"]);
  assert.deepEqual(select.children.map((o) => o.value), ["", "main", "nightly"]);
  assert.deepEqual(
    select.children.map((o) => o.textContent),
    ["— none —", ".tasks/main", ".tasks/nightly"],
  );
  assert.equal(select.value, "main");

  // A binding the repo no longer publishes round-trips rather than vanishing.
  const absent = p.hostQueueSelect("retired", ["main"]);
  assert.deepEqual(absent.children.map((o) => o.value), ["", "main", "retired"]);
  assert.equal(absent.children[2].textContent, ".tasks/retired (absent)");
  assert.equal(absent.value, "retired");
});

// 5) Choosing a host queue PUTs it; "main" is sent as the null default queue.
await test("choosing a host queue PUTs the binding and repaints from the reply", async () => {
  const p = load({ put: { data: { repos: [], queues: [] } } });
  await p.setQueueHostQueue("main", "nightly", null);
  assert.deepEqual(p.calls.puts[0], {
    url: "/api/queue/host-queue",
    method: "PUT",
    body: { queue: null, host_queue: "nightly" },
  });
  assert.deepEqual(p.state.repos, { repos: [], queues: [] });
  assert.equal(p.calls.renders, 1);
});

// 6) The empty option is an explicit "none", not an unset field — the server
//    distinguishes them, so the wire value must be "" and not null.
await test("the none option sends an empty string, not null", async () => {
  const p = load({ put: { data: {} } });
  await p.setQueueHostQueue("nightly", "", null);
  assert.deepEqual(p.calls.puts[0].body, { queue: "nightly", host_queue: "" });
});

console.log(`\n${passed} passed`);
