// Headless test for the shared analytics module's harness-telemetry views.
//
// Loads the REAL shipped analytics.js against a minimal DOM shim and renders
// synthetic run records shaped like the harness's usage.per_turn output —
// including the instrumented fields (stop / ms_model / ms_tools /
// transcript_chars, per-call ms/err/trunc, run-level exit_reason) and one
// LEGACY record without them, so backward compatibility is exercised too.

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

// ---- load the real module --------------------------------------------------

const window = {};
new Function("window", "document", src)(window, document);
assert.ok(window.Analytics && window.Analytics.render, "Analytics.render exported");

// ---- synthetic harness runs -------------------------------------------------

const now = Date.now();
const iso = (minsAgo) => new Date(now - minsAgo * 60000).toISOString();

// One instrumented turn record.
function turn(n, opts) {
  const o = opts || {};
  return {
    turn: n,
    usage: {
      input_tokens: o.uncached === undefined ? 100 : o.uncached,
      output_tokens: 50,
      cache_read_input_tokens: o.cacheRead === undefined ? 900 : o.cacheRead,
      cache_creation_input_tokens: o.cacheWrite === undefined ? 0 : o.cacheWrite,
    },
    stop: o.stop || "tool_use",
    ms_model: o.msModel === undefined ? 2000 : o.msModel,
    ms_tools: o.msTools === undefined ? 500 : o.msTools,
    transcript_chars: n * 1000,
    tool_calls: o.calls === undefined
      ? [{ name: "read_file", result_chars: 4000, ms: 500 }]
      : o.calls,
  };
}

// Run A: completed, landed, 3 turns ending in an edit.
const runA = {
  task: "a", queue: "q", model: "nightshift/anthropic/claude-sonnet-4-6",
  backend: "nightshift", worker_id: "w1", status: "completed", landed: true,
  turns: 3, input_tokens: 3000, output_tokens: 150,
  cache_read_input_tokens: 2700, cache_creation_input_tokens: 100,
  cost_usd: 0.05, failure_kind: null,
  started_at: iso(30), finished_at: iso(25),
  usage: {
    exit_reason: "completed",
    prompt_chars: { system: 9000, brief: 400 },
    per_turn: [
      turn(1, { cacheRead: 0, cacheWrite: 900 }),
      turn(2, {
        calls: [
          { name: "run_bash", result_chars: 2000, ms: 1500, err: true },
          { name: "grep", result_chars: 6000, ms: 300, trunc: true },
        ],
        msTools: 1800,
      }),
      turn(3, { stop: "end_turn", calls: [], msTools: 0 }),
    ],
  },
};

// Run B: died at max_turns after 12 tool turns — never landed.
const runB = {
  task: "b", queue: "q", model: "nightshift/anthropic/claude-sonnet-4-6",
  backend: "nightshift", worker_id: "w1", status: "error", landed: false,
  turns: 12, input_tokens: 12000, output_tokens: 600,
  cache_read_input_tokens: 10800, cache_creation_input_tokens: 900,
  cost_usd: 0.4, failure_kind: "worker_error",
  started_at: iso(20), finished_at: iso(10),
  usage: {
    exit_reason: "max_turns",
    prompt_chars: { system: 9000, brief: 700 },
    per_turn: Array.from({ length: 12 }, (_, i) =>
      turn(i + 1, i === 0 ? { cacheRead: 0, cacheWrite: 900 } : {})
    ),
  },
};

// Legacy run: pre-instrumentation shape (no stop/ms/exit_reason) must not crash
// anything and still contributes to the estimated attribution.
const runLegacy = {
  task: "c", queue: "q", model: "nightshift/anthropic/claude-sonnet-4-6",
  backend: "nightshift", worker_id: "w1", status: "completed", landed: true,
  turns: 2, input_tokens: 300, output_tokens: 40,
  cost_usd: 0.01, failure_kind: null,
  started_at: iso(15), finished_at: iso(14),
  usage: {
    per_turn: [
      {
        turn: 1,
        usage: { input_tokens: 100, output_tokens: 20 },
        tool_calls: [{ name: "read_file", result_chars: 900 }],
      },
      { turn: 2, usage: { input_tokens: 200, output_tokens: 20 }, tool_calls: [] },
    ],
  },
};

// ---- render and assert -------------------------------------------------------

let passed = 0;
function test(name, fn) {
  fn();
  passed++;
  console.log(`ok - ${name}`);
}

const container = new FakeNode("div");
window.Analytics.render(container, {
  fetchRuns: async () => [runA, runB, runLegacy],
});
// reload() awaits fetchRuns; flush the microtask queue before asserting.
await new Promise((r) => setTimeout(r, 0));

const text = textOf(container);

test("run-shape panel renders with estimated attribution note", () => {
  assert.match(text, /Run shape \(harness runs\)/);
  assert.match(text, /token attribution estimated/);
});

test("cache localization renders (uncached share + write tax + warm turn)", () => {
  assert.match(text, /Uncached input share by turn/);
  assert.match(text, /write tax/);
  assert.match(text, /warms at turn/);
});

test("time split renders model vs tools", () => {
  assert.match(text, /Where the time goes/);
  assert.match(text, /model \d+%/);
  assert.match(text, /tools \d+%/);
});

test("per-tool table carries measured latency, error and truncation columns", () => {
  assert.match(text, /p50 ms/);
  assert.match(text, /Err %/);
  assert.match(text, /Trunc %/);
  assert.match(text, /run_bash/); // the erroring tool appears
});

test("turn composition and exit reasons render", () => {
  assert.match(text, /Turn composition & exits/);
  assert.match(text, /Turns doing what/);
  assert.match(text, /Exit reasons/);
  assert.match(text, /max_turns/); // run B's expensive death is visible
});

test("marginal turn yield renders bucket rows", () => {
  assert.match(text, /Marginal turn yield/);
  assert.match(text, /1–10/);
  assert.match(text, /11–20/);
});

// ---- queue-scope fallback ----------------------------------------------------
// The host seeds defaultQueue with its focused queue, but that queue may have
// no runs at all (e.g. the manager's main queue while all work happens in
// playlists). The module must widen to "All queues" instead of rendering an
// empty "No runs in this window" over a fleet that has plenty of data.

const scoped = new FakeNode("div");
window.Analytics.render(scoped, {
  defaultQueue: "", // main queue — none of the synthetic runs live there
  fetchRuns: async () => [runA, runB, runLegacy],
});
await new Promise((r) => setTimeout(r, 0));
const scopedText = textOf(scoped);

test("empty seeded queue scope falls back to all queues", () => {
  assert.doesNotMatch(scopedText, /No runs in this window/);
  assert.match(scopedText, /Cost \/ landed change/);
});

// ---- CI-resolution cost ------------------------------------------------------
// The gate auto-spawns "fix ci: <repo> main is red at <sha>" work, tagged
// `kind: ci_resolution` on the brief and carried onto the attempt. That is the
// cost of keeping main green -- money the queue spends on itself -- so Stats
// reports it apart from feature spend: a KPI card (total + per-run) and a
// per-day trend row. Two days of runs, because the trend panel needs a trend.

const day = 24 * 60;
const ciRun = (n, minsAgo, opts) => ({
  task: `fix-ci-${n}`, queue: "q", model: "m", backend: "nightshift",
  worker_id: "w1", status: "completed", landed: true, kind: "ci_resolution",
  turns: 4, input_tokens: opts.input, output_tokens: opts.output,
  cost_usd: opts.cost, failure_kind: null,
  started_at: iso(minsAgo), finished_at: iso(minsAgo - opts.mins),
});

// Two CI resolutions today and one yesterday, so the trend has two points.
// The default window is 7d, so the card totals all three: $0.30 + $0.50 +
// $0.20 = $1.00 over 3 runs = $0.33/run.
const ciRuns = [
  ciRun(1, 60, { cost: 0.3, input: 1000, output: 100, mins: 10 }),
  ciRun(2, 120, { cost: 0.5, input: 3000, output: 200, mins: 30 }),
  ciRun(3, day + 90, { cost: 0.2, input: 500, output: 50, mins: 5 }),
];

// runA/runB are both from today; the trend panel needs two days on its axis,
// so give the task slice a run yesterday too.
const taskYesterday = {
  ...runA, task: "a-yesterday", cost_usd: 0.02,
  started_at: iso(day + 100), finished_at: iso(day + 95),
};

const ci = new FakeNode("div");
window.Analytics.render(ci, {
  fetchRuns: async () => [...ciRuns, runA, runB, taskYesterday],
});
await new Promise((r) => setTimeout(r, 0));
const ciText = textOf(ci);

test("the CI card reports total spend and the per-run rate to clear a red main", () => {
  assert.match(ciText, /Cost to clear CI {2}\$1\.00/);            // 0.30+0.50+0.20
  assert.match(ciText, /\$0\.33 \/ run to clear CI failures/);    // 1.00 / 3
});

// The two workloads must not bleed into each other. CI runs are held out of
// every other panel (renderBody), and untagged feature work is held out of the
// CI card: runA ($0.05) + runB ($0.40) total $0.45 and no more.
test("the CI card and the fleet cards do not blend the two workloads", () => {
  assert.match(ciText, /Total spend {2}\$0\.47/);   // 0.05 + 0.40 + 0.02
  assert.doesNotMatch(ciText, /Cost to clear CI {2}\$1\.47/);
});

test("the CI trend row renders spend, tokens and time per day", () => {
  assert.match(ciText, /Spend on CI resolution/);
  assert.match(ciText, /Avg tokens\/CI resolution/);
  assert.match(ciText, /Avg time\/CI resolution/);
  // The CI row shares the task slice's x-axis: yesterday's lone CI run is
  // $0.20, today's two are $0.80, read against the same two days as the rows
  // above it.
  assert.match(ciText, /Spend on CI resolution.*\$0\.20.*\$0\.80 \(latest day\)/s);
});

// The axis is the task slice's days, deliberately -- so every cell in the
// panel is read against one x-axis and "latest day" means the same thing in
// all of them. A CI-only day therefore carries no column here; its spend is
// still counted by the card and the CI resolution panel, which are
// window-totals rather than per-day series.
const ciOnlyDay = new FakeNode("div");
window.Analytics.render(ciOnlyDay, {
  fetchRuns: async () => [
    runA, runB,                                  // task work, today only
    ciRun(9, day + 60, { cost: 0.7, input: 100, output: 10, mins: 5 }),
  ],
});
await new Promise((r) => setTimeout(r, 0));
const ciOnlyText = textOf(ciOnlyDay);

test("a CI-only day does not widen the trend axis, but still counts in the card", () => {
  assert.doesNotMatch(ciOnlyText, /Daily trend/);        // one task day is no trend
  assert.match(ciOnlyText, /Cost to clear CI {2}\$0\.70/);
});

// A workspace with CI monitoring off would otherwise carry three permanently
// flat charts and a card averaging nothing.
const noCi = new FakeNode("div");
window.Analytics.render(noCi, { fetchRuns: async () => [runA, runB, runLegacy] });
await new Promise((r) => setTimeout(r, 0));
const noCiText = textOf(noCi);

test("with no CI resolutions the card says so and the trend row is absent", () => {
  assert.match(noCiText, /Cost to clear CI/);
  assert.match(noCiText, /no CI resolutions yet/);
  assert.doesNotMatch(noCiText, /Spend on CI resolution/);
});

console.log(`\n${passed} passed`);
