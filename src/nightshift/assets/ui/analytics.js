/*
 * Shared analytics module — rendered by BOTH the manager UI (fleet-wide, over
 * /api/analytics/runs) and the worker UI (local, over /api/history). It is
 * deliberately self-contained: no dependency on either host's globals, its own
 * formatting helpers, hand-rolled SVG micro-charts, no build step, no chart
 * library. Each host supplies a tiny adapter:
 *
 *   Analytics.render(container, {
 *     title: "Analytics",
 *     fetchRuns: async (sinceIso) => [ <normalized run record>, ... ],
 *   })
 *
 * A normalized run record (both adapters must produce this shape):
 *   { task, queue, model, backend, worker_id, status, landed (bool),
 *     turns, input_tokens, output_tokens, cache_read_input_tokens,
 *     cache_creation_input_tokens, cost_usd, usage, failure_kind,
 *     started_at, finished_at }
 *
 * The tuning KPI is cost per LANDED change; `landed` is true only for a real
 * change reaching main (not a no-change completion), so the manager derives it
 * from the raw attempt state and the worker reads its explicit `landed` flag.
 */
(function (global) {
  "use strict";

  const WINDOWS = [
    { id: "24h", label: "24h", hours: 24 },
    { id: "7d", label: "7d", hours: 24 * 7 },
    { id: "30d", label: "30d", hours: 24 * 30 },
    { id: "all", label: "All", hours: null },
  ];

  // ---- formatting (host-independent) ------------------------------------- //

  function fmtMoney(n) {
    if (typeof n !== "number" || !isFinite(n)) return "—";
    if (n === 0) return "$0.00";
    if (Math.abs(n) < 0.01) return "<$0.01";
    return "$" + n.toFixed(2);
  }

  function fmtTokens(n) {
    if (typeof n !== "number" || !isFinite(n) || n === 0) return "0";
    if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
    return String(Math.round(n));
  }

  function fmtPct(n) {
    if (typeof n !== "number" || !isFinite(n)) return "—";
    return (n * 100).toFixed(0) + "%";
  }

  function fmtNum(n, digits) {
    if (typeof n !== "number" || !isFinite(n)) return "—";
    return n.toFixed(digits === undefined ? 1 : digits);
  }

  function num(v) {
    return typeof v === "number" && isFinite(v) ? v : 0;
  }

  function hasNum(v) {
    return typeof v === "number" && isFinite(v);
  }

  // ---- DOM helpers ------------------------------------------------------- //

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  // ---- record aggregation ------------------------------------------------ //

  // A run counts as terminal (finished, one way or another) when it has a
  // non-running status. The KPI denominator is landed runs; spend is summed
  // across ALL terminal runs (failures burned tokens too).
  function isTerminal(r) {
    return r.status && r.status !== "running" && r.status !== "leased";
  }

  function totalInput(r) {
    // input_tokens already folds cache tokens in (see backends._usage_tokens),
    // so it is the throughput figure; don't re-add the splits.
    return num(r.input_tokens);
  }

  function aggregate(runs) {
    const terminal = runs.filter(isTerminal);
    const landed = terminal.filter((r) => r.landed);
    let cost = 0;
    let inTok = 0;
    let outTok = 0;
    let cacheRead = 0;
    let costRuns = 0;
    let turnsSum = 0;
    let turnsCount = 0;
    let landedCost = 0;
    for (const r of terminal) {
      const c = num(r.cost_usd);
      cost += c;
      if (hasNum(r.cost_usd)) costRuns++;
      inTok += totalInput(r);
      outTok += num(r.output_tokens);
      cacheRead += num(r.cache_read_input_tokens);
      if (hasNum(r.turns)) {
        turnsSum += r.turns;
        turnsCount++;
      }
      if (r.landed) landedCost += c;
    }
    const landRate = terminal.length ? landed.length / terminal.length : null;
    return {
      runs: terminal.length,
      landedRuns: landed.length,
      landRate,
      cost,
      landedCost,
      costPerLanded: landed.length ? landedCost / landed.length : null,
      avgTokens: terminal.length ? (inTok + outTok) / terminal.length : null,
      inTok,
      outTok,
      cacheRead,
      cacheHitRate: inTok > 0 ? cacheRead / inTok : null,
      avgTurns: turnsCount ? turnsSum / turnsCount : null,
      hasCost: costRuns > 0,
    };
  }

  // Split runs into the current window and the immediately-preceding window of
  // equal length, so every KPI can show a delta (the measure-forward mechanic).
  function splitWindows(runs, hours) {
    if (!hours) return { current: runs, prior: [] };
    const now = Date.now();
    const winMs = hours * 3600 * 1000;
    const curStart = now - winMs;
    const priorStart = now - 2 * winMs;
    const current = [];
    const prior = [];
    for (const r of runs) {
      const t = Date.parse(r.started_at);
      if (isNaN(t)) continue;
      if (t >= curStart) current.push(r);
      else if (t >= priorStart) prior.push(r);
    }
    return { current, prior };
  }

  // ---- SVG micro-charts -------------------------------------------------- //

  const SVGNS = "http://www.w3.org/2000/svg";

  function svgEl(tag, attrs) {
    const node = document.createElementNS(SVGNS, tag);
    for (const k in attrs) node.setAttribute(k, attrs[k]);
    return node;
  }

  // A compact bar series: values[] with labels[]. Returns an <svg>.
  function barChart(values, labels, opts) {
    opts = opts || {};
    const w = 260;
    const h = 90;
    const pad = 4;
    const max = Math.max(1, ...values);
    const n = values.length || 1;
    const bw = (w - pad * 2) / n;
    const svg = svgEl("svg", {
      class: "an-chart",
      viewBox: `0 0 ${w} ${h}`,
      preserveAspectRatio: "none",
      role: "img",
    });
    values.forEach((v, i) => {
      const bh = max > 0 ? (v / max) * (h - pad * 2) : 0;
      const rect = svgEl("rect", {
        class: "an-bar",
        x: (pad + i * bw + 1).toFixed(1),
        y: (h - pad - bh).toFixed(1),
        width: Math.max(1, bw - 2).toFixed(1),
        height: bh.toFixed(1),
        rx: "1",
      });
      const title = svgEl("title", {});
      title.textContent = `${labels[i]}: ${opts.format ? opts.format(v) : v}`;
      rect.appendChild(title);
      svg.appendChild(rect);
    });
    return svg;
  }

  // ---- KPI header -------------------------------------------------------- //

  function deltaBadge(current, prior, opts) {
    // opts.lowerIsBetter: for cost/tokens/turns a decrease is good.
    if (!hasNum(current) || !hasNum(prior) || prior === 0) return null;
    const change = (current - prior) / Math.abs(prior);
    if (!isFinite(change) || Math.abs(change) < 0.005) {
      return el("span", "an-delta an-delta-flat", "→ 0%");
    }
    const up = change > 0;
    const good = opts && opts.lowerIsBetter ? !up : up;
    const badge = el(
      "span",
      "an-delta " + (good ? "an-delta-good" : "an-delta-bad"),
      (up ? "▲ " : "▼ ") + Math.abs(change * 100).toFixed(0) + "%"
    );
    return badge;
  }

  function kpiCard(label, value, sub, delta) {
    const card = el("div", "an-kpi");
    card.append(el("div", "an-kpi-label", label));
    const valueRow = el("div", "an-kpi-value-row");
    valueRow.append(el("div", "an-kpi-value", value));
    if (delta) valueRow.append(delta);
    card.append(valueRow);
    if (sub) card.append(el("div", "an-kpi-sub", sub));
    return card;
  }

  function renderKpiHeader(container, cur, prior) {
    const row = el("div", "an-kpi-row");
    row.append(
      kpiCard(
        "Cost / landed change",
        cur.hasCost ? fmtMoney(cur.costPerLanded) : "—",
        cur.landedRuns + " landed",
        deltaBadge(cur.costPerLanded, prior.costPerLanded, { lowerIsBetter: true })
      )
    );
    row.append(
      kpiCard(
        "Land rate",
        cur.landRate !== null ? fmtPct(cur.landRate) : "—",
        cur.runs + " runs",
        deltaBadge(cur.landRate, prior.landRate, { lowerIsBetter: false })
      )
    );
    row.append(
      kpiCard(
        "Avg tokens / task",
        cur.avgTokens !== null ? fmtTokens(cur.avgTokens) : "—",
        fmtTokens(cur.inTok) + " in · " + fmtTokens(cur.outTok) + " out",
        deltaBadge(cur.avgTokens, prior.avgTokens, { lowerIsBetter: true })
      )
    );
    row.append(
      kpiCard(
        "Avg turns",
        cur.avgTurns !== null ? fmtNum(cur.avgTurns) : "—",
        "per task",
        deltaBadge(cur.avgTurns, prior.avgTurns, { lowerIsBetter: true })
      )
    );
    row.append(
      kpiCard(
        "Cache hit rate",
        cur.cacheHitRate !== null ? fmtPct(cur.cacheHitRate) : "—",
        fmtTokens(cur.cacheRead) + " cached in",
        deltaBadge(cur.cacheHitRate, prior.cacheHitRate, { lowerIsBetter: false })
      )
    );
    container.append(row);
  }

  // ---- trends (per-day series) ------------------------------------------- //

  function dayKey(iso) {
    const t = Date.parse(iso);
    if (isNaN(t)) return null;
    return new Date(t).toISOString().slice(0, 10);
  }

  function renderTrends(container, runs) {
    const byDay = new Map();
    for (const r of runs.filter(isTerminal)) {
      const k = dayKey(r.started_at);
      if (!k) continue;
      if (!byDay.has(k)) byDay.set(k, []);
      byDay.get(k).push(r);
    }
    const days = Array.from(byDay.keys()).sort();
    if (days.length < 2) return; // a single day isn't a trend
    const costSeries = days.map((d) => aggregate(byDay.get(d)).landedCost);
    const tokenSeries = days.map((d) => {
      const a = aggregate(byDay.get(d));
      return a.avgTokens || 0;
    });
    const landSeries = days.map((d) => {
      const a = aggregate(byDay.get(d));
      return a.landRate || 0;
    });

    const panel = el("div", "an-panel");
    panel.append(el("h3", "an-panel-title", "Daily trend"));
    const grid = el("div", "an-trend-grid");
    grid.append(trendCell("Spend on landed", costSeries, days, fmtMoney));
    grid.append(trendCell("Avg tokens/task", tokenSeries, days, fmtTokens));
    grid.append(trendCell("Land rate", landSeries.map((v) => v * 100), days, (v) => v.toFixed(0) + "%"));
    panel.append(grid);
    container.append(panel);
  }

  function trendCell(title, values, labels, format) {
    const cell = el("div", "an-trend-cell");
    cell.append(el("div", "an-trend-label", title));
    cell.append(barChart(values, labels, { format }));
    const last = values.length ? values[values.length - 1] : null;
    cell.append(el("div", "an-trend-last", last !== null ? format(last) + " (latest day)" : "—"));
    return cell;
  }

  // ---- breakdown tables -------------------------------------------------- //

  function groupBy(runs, keyFn) {
    const groups = new Map();
    for (const r of runs.filter(isTerminal)) {
      const k = keyFn(r) || "—";
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(r);
    }
    return groups;
  }

  function renderBreakdown(container, title, runs, keyFn) {
    const groups = groupBy(runs, keyFn);
    if (!groups.size) return;
    const rows = Array.from(groups.entries())
      .map(([key, list]) => ({ key, agg: aggregate(list) }))
      .sort((a, b) => b.agg.cost - a.agg.cost);

    const panel = el("div", "an-panel");
    panel.append(el("h3", "an-panel-title", title));
    const table = el("table", "an-table");
    const thead = el("thead");
    const htr = el("tr");
    ["", "Runs", "Land %", "$/landed", "Avg turns", "Cache %", "Cost"].forEach((h) => {
      htr.append(el("th", null, h));
    });
    thead.append(htr);
    table.append(thead);
    const tbody = el("tbody");
    for (const { key, agg } of rows) {
      const tr = el("tr");
      tr.append(el("td", "an-td-key", key));
      tr.append(el("td", null, String(agg.runs)));
      tr.append(el("td", null, agg.landRate !== null ? fmtPct(agg.landRate) : "—"));
      tr.append(el("td", null, agg.hasCost ? fmtMoney(agg.costPerLanded) : "—"));
      tr.append(el("td", null, agg.avgTurns !== null ? fmtNum(agg.avgTurns) : "—"));
      tr.append(el("td", null, agg.cacheHitRate !== null ? fmtPct(agg.cacheHitRate) : "—"));
      tr.append(el("td", null, agg.hasCost ? fmtMoney(agg.cost) : "—"));
      tbody.append(tr);
    }
    table.append(tbody);
    panel.append(table);
    container.append(panel);
  }

  // ---- waste panel ------------------------------------------------------- //

  function renderWaste(container, runs) {
    const terminal = runs.filter(isTerminal);
    const nonLanded = terminal.filter((r) => !r.landed);
    const nonLandedCost = nonLanded.reduce((s, r) => s + num(r.cost_usd), 0);

    // Validation-failure burn: runs that ran an agent then failed the gate.
    const valFail = terminal.filter((r) => r.failure_kind === "validation_error");
    const valFailCost = valFail.reduce((s, r) => s + num(r.cost_usd), 0);

    // Tasks attempted multiple times that never landed (grouped by task).
    const byTask = groupBy(terminal, (r) => r.task);
    let neverLandedTasks = 0;
    let neverLandedCost = 0;
    byTask.forEach((list) => {
      if (list.length >= 2 && !list.some((r) => r.landed)) {
        neverLandedTasks++;
        neverLandedCost += list.reduce((s, r) => s + num(r.cost_usd), 0);
      }
    });

    const anyCost = terminal.some((r) => hasNum(r.cost_usd));
    const panel = el("div", "an-panel");
    panel.append(el("h3", "an-panel-title", "Waste"));
    const cards = el("div", "an-kpi-row");
    cards.append(
      kpiCard("Non-landed spend", anyCost ? fmtMoney(nonLandedCost) : "—", nonLanded.length + " runs")
    );
    cards.append(
      kpiCard("Validation burn", anyCost ? fmtMoney(valFailCost) : "—", valFail.length + " runs failed the gate")
    );
    cards.append(
      kpiCard("Never-landed retries", String(neverLandedTasks), anyCost ? fmtMoney(neverLandedCost) + " burned" : "tasks retried, no land")
    );
    panel.append(cards);

    // Top-5 most expensive runs.
    const priced = terminal.filter((r) => hasNum(r.cost_usd)).sort((a, b) => b.cost_usd - a.cost_usd);
    if (priced.length) {
      const table = el("table", "an-table");
      const thead = el("thead");
      const htr = el("tr");
      ["Task", "Model", "Turns", "Tokens", "Cost", "Landed"].forEach((h) => htr.append(el("th", null, h)));
      thead.append(htr);
      table.append(thead);
      const tbody = el("tbody");
      for (const r of priced.slice(0, 5)) {
        const tr = el("tr");
        tr.append(el("td", "an-td-key", r.task || "—"));
        tr.append(el("td", null, r.model || "—"));
        tr.append(el("td", null, hasNum(r.turns) ? String(r.turns) : "—"));
        tr.append(el("td", null, fmtTokens(totalInput(r) + num(r.output_tokens))));
        tr.append(el("td", null, fmtMoney(r.cost_usd)));
        const landedCell = el("td", null, r.landed ? "✓" : "✗");
        landedCell.className = r.landed ? "an-ok" : "an-bad";
        tr.append(landedCell);
        tbody.append(tr);
      }
      table.append(tbody);
      const label = el("div", "an-subhead", "Most expensive runs");
      panel.append(label, table);
    }
    container.append(panel);
  }

  // ---- run-shape (harness per-turn attribution) -------------------------- //

  // Only harness runs carry usage.per_turn. Compute, across those runs, the
  // median input-delta by turn index and the per-tool token attribution, using
  // the documented delta method: input(N) - output(N-1) ≈ tokens turn (N-1)'s
  // tool_calls appended, split by result_chars.
  function renderRunShape(container, runs) {
    const shaped = runs.filter(
      (r) => r.usage && Array.isArray(r.usage.per_turn) && r.usage.per_turn.length > 1
    );
    if (!shaped.length) return;

    const deltasByTurn = new Map(); // turn index -> [delta, ...]
    const toolTokens = new Map(); // tool name -> total attributed tokens
    let toolFallback = false;

    for (const r of shaped) {
      const pt = r.usage.per_turn;
      for (let i = 1; i < pt.length; i++) {
        const cur = pt[i].usage || {};
        const prev = pt[i - 1].usage || {};
        const curIn = foldInput(cur);
        const prevOut = num(prev.output_tokens);
        const delta = Math.max(0, curIn - prevOut);
        if (!deltasByTurn.has(i)) deltasByTurn.set(i, []);
        deltasByTurn.get(i).push(delta);

        // Attribute this delta to turn (i-1)'s tool calls by result_chars.
        const calls = Array.isArray(pt[i - 1].tool_calls) ? pt[i - 1].tool_calls : [];
        const totalChars = calls.reduce((s, c) => s + num(c.result_chars), 0);
        if (calls.length && totalChars > 0) {
          for (const c of calls) {
            const share = (num(c.result_chars) / totalChars) * delta;
            toolTokens.set(c.name || "?", (toolTokens.get(c.name || "?") || 0) + share);
          }
        } else if (calls.length) {
          const even = delta / calls.length;
          for (const c of calls) {
            toolTokens.set(c.name || "?", (toolTokens.get(c.name || "?") || 0) + even);
          }
          toolFallback = true;
        }
      }
    }

    const turns = Array.from(deltasByTurn.keys()).sort((a, b) => a - b);
    const medians = turns.map((t) => median(deltasByTurn.get(t)));

    const panel = el("div", "an-panel");
    panel.append(el("h3", "an-panel-title", "Run shape (harness runs)"));
    panel.append(
      el("div", "an-note", shaped.length + " harness run(s) with per-turn detail" + (toolFallback ? " · some tool splits estimated evenly" : ""))
    );

    const grid = el("div", "an-trend-grid");
    const cell = el("div", "an-trend-cell");
    cell.append(el("div", "an-trend-label", "Median input added by turn"));
    cell.append(barChart(medians, turns.map((t) => "turn " + t), { format: fmtTokens }));
    grid.append(cell);
    panel.append(grid);

    const toolRows = Array.from(toolTokens.entries()).sort((a, b) => b[1] - a[1]);
    if (toolRows.length) {
      const table = el("table", "an-table");
      const thead = el("thead");
      const htr = el("tr");
      ["Tool", "Input tokens added (total)"].forEach((h) => htr.append(el("th", null, h)));
      thead.append(htr);
      table.append(thead);
      const tbody = el("tbody");
      for (const [name, tokens] of toolRows) {
        const tr = el("tr");
        tr.append(el("td", "an-td-key", name));
        tr.append(el("td", null, fmtTokens(tokens)));
        tbody.append(tr);
      }
      table.append(tbody);
      panel.append(el("div", "an-subhead", "Context added per tool"), table);
    }
    container.append(panel);
  }

  function foldInput(usage) {
    // per_turn usage is raw (pre-fold), so reconstruct total input throughput.
    return (
      num(usage.input_tokens) +
      num(usage.cache_read_input_tokens) +
      num(usage.cache_creation_input_tokens)
    );
  }

  function median(arr) {
    if (!arr.length) return 0;
    const sorted = arr.slice().sort((a, b) => a - b);
    const mid = Math.floor(sorted.length / 2);
    return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
  }

  // ---- dimension filter -------------------------------------------------- //

  function distinct(runs, keyFn) {
    const set = new Set();
    for (const r of runs) {
      const v = keyFn(r);
      if (v) set.add(v);
    }
    return Array.from(set).sort();
  }

  // ---- top-level render -------------------------------------------------- //

  function render(container, opts) {
    opts = opts || {};
    const fetchRuns = opts.fetchRuns;
    if (typeof fetchRuns !== "function") {
      throw new Error("Analytics.render requires opts.fetchRuns");
    }

    // View state, kept locally on the container so re-render is cheap.
    const view = { window: "7d", dimension: "all", value: "all", runs: [], loaded: false };

    clear(container);
    container.classList.add("an-root");
    const controls = el("div", "an-controls");
    const body = el("div", "an-body");
    container.append(controls, body);

    function renderControls() {
      clear(controls);
      // Time window toggle.
      const winGroup = el("div", "an-toggle-group");
      for (const w of WINDOWS) {
        const btn = el("button", "an-toggle" + (view.window === w.id ? " an-toggle-active" : ""), w.label);
        btn.addEventListener("click", () => {
          view.window = w.id;
          reload();
        });
        winGroup.append(btn);
      }
      controls.append(winGroup);

      // Dimension filter (model / backend / queue), populated from loaded runs.
      const dims = [
        { id: "all", label: "All runs" },
        { id: "model", label: "Model" },
        { id: "backend", label: "Backend" },
        { id: "queue", label: "Queue" },
      ];
      const dimSel = el("select", "an-select");
      for (const d of dims) {
        const o = el("option", null, d.label);
        o.value = d.id;
        if (view.dimension === d.id) o.selected = true;
        dimSel.append(o);
      }
      dimSel.addEventListener("change", () => {
        view.dimension = dimSel.value;
        view.value = "all";
        renderControls();
        renderBody();
      });
      controls.append(dimSel);

      if (view.dimension !== "all") {
        const keyFn = (r) => r[view.dimension];
        const values = distinct(view.runs, keyFn);
        const valSel = el("select", "an-select");
        const allOpt = el("option", null, "All " + view.dimension + "s");
        allOpt.value = "all";
        valSel.append(allOpt);
        for (const v of values) {
          const o = el("option", null, v);
          o.value = v;
          if (view.value === v) o.selected = true;
          valSel.append(o);
        }
        valSel.addEventListener("change", () => {
          view.value = valSel.value;
          renderBody();
        });
        controls.append(valSel);
      }
    }

    function filteredRuns() {
      if (view.dimension === "all" || view.value === "all") return view.runs;
      return view.runs.filter((r) => r[view.dimension] === view.value);
    }

    function renderBody() {
      clear(body);
      if (!view.loaded) {
        body.append(el("p", "an-empty", "Loading…"));
        return;
      }
      const runs = filteredRuns();
      const hours = (WINDOWS.find((w) => w.id === view.window) || {}).hours;
      const { current, prior } = splitWindows(runs, hours);
      const terminalCurrent = current.filter(isTerminal);
      if (!terminalCurrent.length) {
        body.append(el("p", "an-empty", "No runs in this window."));
        return;
      }
      renderKpiHeader(body, aggregate(current), aggregate(prior));
      renderTrends(body, current);
      renderBreakdown(body, "By model", current, (r) => r.model);
      renderBreakdown(body, "By backend", current, (r) => r.backend);
      renderBreakdown(body, "By queue", current, (r) => r.queue);
      if (distinct(current, (r) => r.worker_id).length > 1) {
        renderBreakdown(body, "By worker", current, (r) => r.worker_id);
      }
      renderWaste(body, current);
      renderRunShape(body, current);
    }

    async function reload() {
      renderControls();
      view.loaded = false;
      renderBody();
      const hours = (WINDOWS.find((w) => w.id === view.window) || {}).hours;
      // Fetch the current + prior window so deltas have data (2x span). `null`
      // (All) fetches everything.
      let sinceIso = null;
      if (hours) sinceIso = new Date(Date.now() - hours * 2 * 3600 * 1000).toISOString();
      try {
        view.runs = (await fetchRuns(sinceIso)) || [];
      } catch (err) {
        view.runs = [];
        view.loaded = true;
        clear(body);
        body.append(el("p", "an-empty", "Failed to load analytics: " + (err && err.message ? err.message : err)));
        return;
      }
      view.loaded = true;
      renderControls();
      renderBody();
    }

    reload();
  }

  global.Analytics = { render };
})(window);
