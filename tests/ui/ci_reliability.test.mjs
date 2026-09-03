// Headless test for the analytics module's "CI failures & resolutions" panel.
//
// Loads the REAL shipped analytics.js against a minimal DOM shim and renders
// synthetic CI-resolution runs, covering the two things the panel claims:
// failures counted per BRIEF (retries fold in, they are not separate breaks),
// and the per-day strip zero-filling the quiet days so a rate reads as a rate.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(
  resolve(here, "../../src/nightshift/assets/ui/analytics.js"),
  "utf8"
);

// ---- minimal DOM shim ------------------------------------------------------

class FakeNode {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.textContent = "";
    this.className = "";
    this.style = {};
    this.classList = { add() {} };
    this.attrs = {};
  }
  append(...nodes) {
    for (const n of nodes) this.children.push(n);
  }
  appendChild(n) {
    this.children.push(n);
    return n;
  }
  get firstChild() {
    return this.children[0] || null;
  }
  removeChild(n) {
    this.children = this.children.filter((c) => c !== n);
    return n;
  }
  addEventListener() {}
  setAttribute(k, v) {
    this.attrs[k] = v;
  }
}

const document = {
  createElement: (tag) => new FakeNode(tag),
  createElementNS: (_ns, tag) => new FakeNode(tag),
};

function textOf(node) {
  let out = String(node.textContent || "");
  for (const c of node.children) out += " " + textOf(c);
  return out;
}

const window = {};
new Function("window", "document", src)(window, document);
assert.ok(window.Analytics && window.Analytics.render, "Analytics.render exported");

// ---- clock anchors ---------------------------------------------------------

// Every fixture timestamp hangs off the most recent midday UTC that is not in
// the future. Anchoring at noon (rather than "N minutes ago") keeps the
// intra-day offsets below hours away from a UTC midnight, so which day a run
// buckets into never depends on what time the suite happens to run.
const DAY_MS = 86400000;
const MIN = 60000;
const HOUR = 3600000;
const noonToday = Math.floor(Date.now() / DAY_MS) * DAY_MS + 12 * HOUR;
const anchor = noonToday <= Date.now() ? noonToday : noonToday - DAY_MS;
const at = (daysAgo, offsetMs) => anchor - daysAgo * DAY_MS + (offsetMs || 0);
const iso = (ms) => new Date(ms).toISOString();
const dayOf = (ms) => new Date(ms).toISOString().slice(0, 10);

function ciAttempt(task, startMs, durMs, landed) {
  return {
    task, queue: "q", model: "m", backend: "nightshift", worker_id: "w1",
    kind: "ci_resolution",
    status: landed ? "completed" : "error",
    landed,
    turns: 4, input_tokens: 1000, output_tokens: 100,
    cost_usd: 0.1, failure_kind: landed ? null : "worker_error",
    started_at: iso(startMs), finished_at: iso(startMs + durMs),
  };
}

// A task run on day -2, purely to prove it is not mistaken for a CI failure:
// the CI strip's middle column must still read zero.
const taskRun = {
  task: "feature-work", queue: "q", model: "m", backend: "nightshift",
  worker_id: "w1", status: "completed", landed: true,
  turns: 3, input_tokens: 500, output_tokens: 50, cost_usd: 0.05,
  failure_kind: null,
  started_at: iso(at(2)), finished_at: iso(at(2, 5 * MIN)),
};

// Failure 1 (day -3): the first fix attempt dies, the retry lands 1h20m after
// the break — ONE failure, two attempts, TTR measured across both.
const failure1 = [
  ciAttempt("fix-ci-abc1234", at(3), 10 * MIN, false),
  ciAttempt("fix-ci-abc1234", at(3, HOUR), 20 * MIN, true),
];
// Failure 2 (day -1): cleared first try, 30m.
const failure2 = [ciAttempt("fix-ci-def5678", at(1), 30 * MIN, true)];

// ---- render and assert -----------------------------------------------------

let passed = 0;
function test(name, fn) {
  fn();
  passed++;
  console.log(`ok - ${name}`);
}

const container = new FakeNode("div");
window.Analytics.render(container, {
  fetchRuns: async () => [...failure1, ...failure2, taskRun],
});
// reload() awaits fetchRuns; flush the microtask queue before asserting.
await new Promise((r) => setTimeout(r, 0));
const text = textOf(container);

test("the panel renders its card strip and its per-day bar strip", () => {
  assert.match(text, /CI failures & resolutions/);
  assert.match(text, /Per day/);
});

test("a failure is one brief, not one attempt — the retry folds in", () => {
  assert.match(text, /CI failures {2}2 3 fix attempts/);
  assert.match(text, /Resolved {2}2 100% of failures · 0 still open/);
});

test("the failure rate is per day of the selected window, not per day seen", () => {
  // Default window is 7d: 2 failures / 7 days.
  assert.match(text, /Failures \/ day {2}0\.29 0\.29 resolved \/ day over 7\.0d/);
});

test("TTR spans the whole failure, first attempt's start to the landing", () => {
  // (1h20m + 30m) / 2 = 55m. The dead first attempt is inside the 1h20m.
  assert.match(text, /TTR {2}55m 0s mean over 2 resolved/);
});

test("TTF is the interval between consecutive failures", () => {
  // Day -3 to day -1 is exactly two days, and n failures give n-1 intervals.
  assert.match(text, /TTF {2}48h 0m mean over 1 interval/);
});

test("the per-day strip zero-fills the days nothing broke", () => {
  const [d3, d2, d1] = [dayOf(at(3)), dayOf(at(2)), dayOf(at(1))];
  assert.match(text, new RegExp(`CI failures {3}${d3}: 1 {2}${d2}: 0 {2}${d1}: 1 1 \\(latest day\\)`));
  assert.match(text, new RegExp(`Resolutions {3}${d3}: 1 {2}${d2}: 0 {2}${d1}: 1 1 \\(latest day\\)`));
});

test("the per-day strip charts TTR on the day the failure closed", () => {
  const [d3, d2, d1] = [dayOf(at(3)), dayOf(at(2)), dayOf(at(1))];
  assert.match(text, new RegExp(`Time to resolution {3}${d3}: 1h 20m {2}${d2}: 0s {2}${d1}: 30m 0s`));
  // The first failure has no predecessor, so it contributes no interval.
  assert.match(text, new RegExp(`Time between failures {3}${d3}: 0s {2}${d2}: 0s {2}${d1}: 48h 0m`));
});

// ---- degenerate windows ----------------------------------------------------

const single = new FakeNode("div");
window.Analytics.render(single, { fetchRuns: async () => [...failure2, taskRun] });
await new Promise((r) => setTimeout(r, 0));
const singleText = textOf(single);

test("one failure reports no interval rather than an interval of zero", () => {
  assert.match(singleText, /TTF {2}— one failure — no interval yet/);
  assert.match(singleText, /CI failures {2}1 1 fix attempt/);
});

const unresolved = new FakeNode("div");
window.Analytics.render(unresolved, {
  fetchRuns: async () => [ciAttempt("fix-ci-open", at(1), 10 * MIN, false), taskRun],
});
await new Promise((r) => setTimeout(r, 0));
const unresolvedText = textOf(unresolved);

test("a failure nothing has cleared counts as open, with no TTR", () => {
  assert.match(unresolvedText, /Resolved {2}0 0% of failures · 1 still open/);
  assert.match(unresolvedText, /TTR {2}— nothing resolved in this window/);
});

// A workspace with CI monitoring off must not carry an empty panel.
const noCi = new FakeNode("div");
window.Analytics.render(noCi, { fetchRuns: async () => [taskRun] });
await new Promise((r) => setTimeout(r, 0));

test("with no CI-resolution runs the panel is absent entirely", () => {
  assert.doesNotMatch(textOf(noCi), /CI failures & resolutions/);
});

console.log(`\n${passed} passed`);
