// Headless test for the UP NEXT list's hide-disabled toggle (app.js +
// index.html).
//
// A queue that accumulates disabled tasks buries the ones that will actually
// run: the disabled rows sit in the list forever, and the only way past them
// was to scroll. The UP NEXT header now carries an eye toggle beside the sort
// control that filters the disabled rows out of the list — and back in, since
// they stay editable and must not become unreachable.
//
// Loads the REAL sources: the button's markup from index.html, and the filter /
// sync / toggle functions from app.js (sliced between sentinels), driven
// against a minimal DOM shim.
//
// Run: node --test tests/ui/ (or `node tests/ui/queue_hide_disabled.test.mjs`).

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

const visibleSrc = slice("function visibleQueueItems() {", "function renderNow() {");
const renderSrc = slice("function renderQueueNow() {", "// The UP NEXT header's hide-disabled toggle");
// syncHideDisabledButton + toggleHideDisabled, which sit together after it.
const toggleSrc = slice("function syncHideDisabledButton() {", "// The right-hand block of a queue row");

// ---- Minimal DOM shim -----------------------------------------------------
// Only what the header control and the list container touch.

function makeEl(tag = "div", cls = "") {
  const classes = new Set(cls.split(/\s+/).filter(Boolean));
  const el = {
    tag,
    title: "",
    textContent: "",
    html: "",
    hidden: false,
    attrs: {},
    children: [],
    classList: {
      add: (...cs) => cs.forEach((c) => classes.add(c)),
      contains: (c) => classes.has(c),
      toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
    },
    setAttribute(k, v) { this.attrs[k] = v; },
    append(...kids) { this.children.push(...kids); },
  };
  Object.defineProperty(el, "innerHTML", {
    get() { return el.html; },
    // Assigning innerHTML replaces the element's contents, as in a real DOM —
    // the list render relies on `ul.innerHTML = ""` to clear the old rows.
    set(v) { el.html = v; el.children = []; },
  });
  return el;
}

// A queue screen wired the way index.html ships it, holding `queue` items.
// Returns the shim elements plus the loaded functions.
function harness(queue, { hideDisabled = false, selected = [] } = {}) {
  const els = {
    queue: makeEl("ul", "queue"),
    "queue-empty": makeEl("p", "empty"),
    "queue-count": makeEl("span", "count"),
    "queue-playlist": makeEl("h2"),
    "queue-sort": makeEl("button", "queue-icon-btn"),
    "queue-hide-disabled": makeEl("button", "queue-icon-btn"),
  };
  const state = {
    queue,
    hideDisabled,
    sortMode: "manual",
    activePlaylist: null,
    player: { state: "idle", now_playing: null },
    selectedTask: selected[0] ?? null,
    selectedTasks: new Set(selected),
  };
  const renders = [];
  const factory = new Function(
    "$", "state", "EYE_ICON", "EYE_OFF_ICON", "closeRowMenu", "autoImportBadge",
    "activePlaylistRow", "upNextItems", "syncQueuePlayButton", "queueItemRow",
    "renderPauseBanner", "renderQueue",
    `${visibleSrc}
     ${renderSrc}
     ${toggleSrc}
     return { visibleQueueItems, renderQueueNow, syncHideDisabledButton, toggleHideDisabled };`,
  );
  const api = factory(
    (id) => els[id] ?? null,
    state,
    "<svg data-icon=\"eye\"></svg>",
    "<svg data-icon=\"eye-off\"></svg>",
    () => {},
    () => null,
    () => null,
    () => queue.filter((i) => !i.disabled),
    () => {},
    (item) => ({ task: item.task }),
    () => {},
    () => { renders.push(true); api.renderQueueNow(); },
  );
  return { els, state, api, renders };
}

const rows = (h) => h.els.queue.children.map((r) => r.task);

let passed = 0;
const tests = [];
function test(name, fn) { tests.push([name, fn]); }

// 1) Placement is half the request: the affordance has to be an actual control
//    in the UP NEXT header, sat with the sort toggle it lives beside.
test("the toggle ships in the UP NEXT header, beside the sort control", () => {
  const head = html.indexOf('class="queue-upnext"');
  const list = html.indexOf('<ul id="queue"', head);
  assert.ok(head !== -1 && list !== -1, "UP NEXT header not found in index.html");
  const eye = html.indexOf('id="queue-hide-disabled"', head);
  const sort = html.indexOf('id="queue-sort"', head);
  assert.ok(eye !== -1, "no #queue-hide-disabled button in the UP NEXT header");
  assert.ok(eye < list, "#queue-hide-disabled must sit in the header, above the list");
  assert.ok(eye < sort, "#queue-hide-disabled must come before the sort toggle");
});

// 2) Default is unchanged: every row still shows, disabled ones included.
test("disabled tasks show by default", () => {
  const h = harness([{ task: "a" }, { task: "b", disabled: true }, { task: "c" }]);
  h.api.renderQueueNow();
  assert.deepEqual(rows(h), ["a", "b", "c"]);
  assert.equal(h.els["queue-empty"].hidden, true);
});

// 3) The headline behaviour: with the toggle on, disabled rows leave the list.
test("the toggle filters disabled tasks out of the list", () => {
  const h = harness([{ task: "a" }, { task: "b", disabled: true }, { task: "c" }]);
  h.api.toggleHideDisabled();
  assert.equal(h.state.hideDisabled, true);
  assert.deepEqual(rows(h), ["a", "c"]);
});

// 4) …and it is a toggle, not a one-way door — a disabled task must stay
//    reachable, since it is still editable from its row.
test("toggling again brings the disabled tasks back", () => {
  const h = harness([{ task: "a" }, { task: "b", disabled: true }]);
  h.api.toggleHideDisabled();
  h.api.toggleHideDisabled();
  assert.equal(h.state.hideDisabled, false);
  assert.deepEqual(rows(h), ["a", "b"]);
});

// 5) The button says which way the filter is set: an open eye offers to hide,
//    a lit crossed-out eye reports that rows are being held back.
test("the button reflects the filter state", () => {
  const h = harness([{ task: "a", disabled: true }]);
  h.api.syncHideDisabledButton();
  const btn = h.els["queue-hide-disabled"];
  assert.match(btn.innerHTML, /data-icon="eye"/);
  assert.equal(btn.attrs["aria-pressed"], "false");
  assert.equal(btn.title, "Hide disabled tasks");
  assert.ok(!btn.classList.contains("active"));

  h.api.toggleHideDisabled();
  assert.match(btn.innerHTML, /data-icon="eye-off"/);
  assert.equal(btn.attrs["aria-pressed"], "true");
  assert.match(btn.title, /click to show/);
  assert.ok(btn.classList.contains("active"));
});

// 6) A hidden row must not stay the target of a visible action: filtering the
//    selected task away drops it from the selection and the cursor, so a
//    following Delete or "…" can't fire at a row nobody can see.
test("filtering away the selected task clears the selection", () => {
  const h = harness(
    [{ task: "a" }, { task: "b", disabled: true }],
    { selected: ["a", "b"] },
  );
  h.state.selectedTask = "b";
  h.api.toggleHideDisabled();
  assert.deepEqual([...h.state.selectedTasks], ["a"]);
  assert.equal(h.state.selectedTask, null);
});

// 7) An enabled selection survives the filter untouched.
test("an enabled selection survives the toggle", () => {
  const h = harness([{ task: "a" }, { task: "b", disabled: true }], { selected: ["a"] });
  h.api.toggleHideDisabled();
  assert.deepEqual([...h.state.selectedTasks], ["a"]);
  assert.equal(h.state.selectedTask, "a");
});

// 8) When the filter is what emptied the list, the empty line has to say so —
//    "No pending tasks." would read as an empty queue with the tasks hidden.
test("the empty line explains a list emptied by the filter", () => {
  const h = harness([{ task: "a", disabled: true }]);
  h.api.renderQueueNow();
  assert.equal(h.els["queue-empty"].hidden, true, "an unfiltered disabled row still shows");

  h.api.toggleHideDisabled();
  assert.equal(h.els["queue-empty"].hidden, false);
  assert.match(h.els["queue-empty"].textContent, /All tasks are disabled/);

  // A genuinely empty queue keeps the plain copy.
  const bare = harness([], { hideDisabled: true });
  bare.api.renderQueueNow();
  assert.equal(bare.els["queue-empty"].hidden, false);
  assert.equal(bare.els["queue-empty"].textContent, "No pending tasks.");
});

// 9) The filter is display-only: the header count still tracks what will run,
//    which never included the disabled tasks in the first place.
test("the queued count is unaffected by the filter", () => {
  const h = harness([{ task: "a" }, { task: "b", disabled: true }, { task: "c" }]);
  h.api.renderQueueNow();
  assert.equal(h.els["queue-count"].textContent, "(2)");
  h.api.toggleHideDisabled();
  assert.equal(h.els["queue-count"].textContent, "(2)");
});

for (const [name, fn] of tests) {
  await fn();
  passed++;
  console.log(`ok - ${name}`);
}
console.log(`\n${passed} passing`);
