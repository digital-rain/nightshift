// Headless test for the actual/notional spend split in the shared analytics
// module's KPI header.
//
// The `claude` CLI reports total_cost_usd in both billing modes, but under a
// claude.ai subscription that figure is notional list price, not money spent.
// `billing` says which: "api" and an absent stamp are actual money (the
// conservative rule -- an unattributed dollar can only overstate the real
// spend), "subscription" is notional. The two must never be summed together
// into one headline figure.
//
// Loads the REAL shipped analytics.js against the same minimal DOM shim as
// analytics_harness.test.mjs.

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

let passed = 0;
function test(name, fn) {
  fn();
  passed++;
  console.log(`ok - ${name}`);
}

// ---- synthetic runs, one per billing state ---------------------------------

const now = Date.now();
const iso = (minsAgo) => new Date(now - minsAgo * 60000).toISOString();

const run = (task, cost, billing, minsAgo) => ({
  task, queue: "q", model: "claude-code/claude-opus-5", backend: "claude-code",
  worker_id: "w1", status: "completed", landed: true,
  turns: 3, input_tokens: 1000, output_tokens: 100,
  cost_usd: cost, billing, failure_kind: null,
  started_at: iso(minsAgo), finished_at: iso(minsAgo - 5),
});

// $1 billed to an API key, $2 notional under a subscription, $4 unattributed
// (a record that predates the field, or a backend that cannot say).
const runs = [
  run("a", 1, "api", 60),
  run("b", 2, "subscription", 50),
  run("c", 4, undefined, 40),
];

const container = new FakeNode("div");
window.Analytics.render(container, { fetchRuns: async () => runs });
await new Promise((r) => setTimeout(r, 0));
const text = textOf(container);

test("the spend card reports actual money only, unattributed included", () => {
  // $1 (api) + $4 (unattributed) -- the $2 subscription run is held out.
  assert.match(text, /Actual spend {2}\$5\.00/);
  assert.doesNotMatch(text, /Actual spend {2}\$7\.00/);
});

test("the notional sub-line reports the subscription total apart from it", () => {
  assert.match(text, /\+ \$2\.00 notional \(subscription\)/);
});

// With nothing billed to a subscription the card must not grow a sub-line
// claiming $0.00 notional -- it falls back to the landed-spend sub-line.
const apiOnly = new FakeNode("div");
window.Analytics.render(apiOnly, {
  fetchRuns: async () => [run("a", 1, "api", 60), run("c", 4, null, 40)],
});
await new Promise((r) => setTimeout(r, 0));
const apiText = textOf(apiOnly);

test("with no subscription runs the notional sub-line is absent", () => {
  assert.match(apiText, /Actual spend {2}\$5\.00/);
  assert.doesNotMatch(apiText, /notional \(subscription\)/);
  assert.match(apiText, /\$5\.00 on landed/);
});

console.log(`\n${passed} passed`);
