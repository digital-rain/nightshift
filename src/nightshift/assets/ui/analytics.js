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
 *     defaultQueue: "<queue>",   // optional initial queue scope ("" = main);
 *                                // omit for "All queues"
 *   })
 *
 * fetchRuns must return FLEET-WIDE records (no server-side queue filter):
 * queue scoping is a client-side dropdown here, so failures in another queue
 * are one selection away instead of silently excluded from the stats.
 *
 * A normalized run record (both adapters must produce this shape):
 *   { task, queue, model, backend, worker_id, status, landed (bool),
 *     turns, input_tokens, output_tokens, cache_read_input_tokens,
 *     cache_creation_input_tokens, cost_usd, usage, failure_kind,
 *     started_at, finished_at }
 * Optional (manager records only): enhanced (bool — the brief went through
 * the enhance-on-create rewrite) and rating ('up' | 'down' | null — the
 * operator's thumbs verdict), which drive the "Brief enhancement" panel.
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
    const nonLanded = [];
    const valFail = [];
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
      else nonLanded.push(r);
      if (r.failure_kind === "validation_error") valFail.push(r);
    }
    const nonLandedCost = nonLanded.reduce((s, r) => s + num(r.cost_usd), 0);
    const valFailCost = valFail.reduce((s, r) => s + num(r.cost_usd), 0);
    const byTask = groupBy(terminal, (r) => r.task);
    let neverLandedTasks = 0;
    let neverLandedCost = 0;
    byTask.forEach((list) => {
      if (list.length >= 2 && !list.some((r) => r.landed)) {
        neverLandedTasks++;
        neverLandedCost += list.reduce((s, r) => s + num(r.cost_usd), 0);
      }
    });
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
      nonLandedCount: nonLanded.length,
      nonLandedCost,
      valFailCount: valFail.length,
      valFailCost,
      neverLandedTasks,
      neverLandedCost,
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
    row.append(
      kpiCard(
        "Non-landed spend",
        cur.hasCost ? fmtMoney(cur.nonLandedCost) : "—",
        cur.nonLandedCount + " runs",
        deltaBadge(cur.nonLandedCost, prior.nonLandedCost, { lowerIsBetter: true })
      )
    );
    row.append(
      kpiCard(
        "Validation burn",
        cur.hasCost ? fmtMoney(cur.valFailCost) : "—",
        cur.valFailCount + " runs failed the gate",
        deltaBadge(cur.valFailCost, prior.valFailCost, { lowerIsBetter: true })
      )
    );
    row.append(
      kpiCard(
        "Never-landed retries",
        String(cur.neverLandedTasks),
        cur.hasCost ? fmtMoney(cur.neverLandedCost) + " burned" : "tasks retried, no land",
        deltaBadge(cur.neverLandedTasks, prior.neverLandedTasks, { lowerIsBetter: true })
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

  // ---- brief enhancement (enhanced vs raw) -------------------------------- //

  // Compare runs whose brief went through the enhance-on-create rewrite with
  // raw-brief runs: outcome (land rate), operator satisfaction (thumbs), and
  // cost. Only rendered when the records carry the `enhanced` flag (the
  // manager's normalized records do; the worker UI's local records may not).
  function renderEnhancement(container, runs) {
    const terminal = runs.filter(isTerminal).filter((r) => typeof r.enhanced === "boolean");
    if (!terminal.length) return;
    const enhanced = terminal.filter((r) => r.enhanced);
    if (!enhanced.length && !terminal.some((r) => r.rating)) return; // nothing to compare yet

    const groups = [
      { label: "Enhanced brief", list: enhanced },
      { label: "Raw brief", list: terminal.filter((r) => !r.enhanced) },
    ].filter((g) => g.list.length);

    const panel = el("div", "an-panel");
    panel.append(el("h3", "an-panel-title", "Brief enhancement"));
    const table = el("table", "an-table");
    const thead = el("thead");
    const htr = el("tr");
    ["", "Runs", "Land %", "\u{1F44D}", "\u{1F44E}", "Avg turns", "$/landed", "Cost"].forEach((h) => {
      htr.append(el("th", null, h));
    });
    thead.append(htr);
    table.append(thead);
    const tbody = el("tbody");
    for (const g of groups) {
      const agg = aggregate(g.list);
      const up = g.list.filter((r) => r.rating === "up").length;
      const down = g.list.filter((r) => r.rating === "down").length;
      const tr = el("tr");
      tr.append(el("td", "an-td-key", g.label));
      tr.append(el("td", null, String(agg.runs)));
      tr.append(el("td", null, agg.landRate !== null ? fmtPct(agg.landRate) : "—"));
      tr.append(el("td", null, up ? String(up) : "—"));
      tr.append(el("td", null, down ? String(down) : "—"));
      tr.append(el("td", null, agg.avgTurns !== null ? fmtNum(agg.avgTurns) : "—"));
      tr.append(el("td", null, agg.hasCost ? fmtMoney(agg.costPerLanded) : "—"));
      tr.append(el("td", null, agg.hasCost ? fmtMoney(agg.cost) : "—"));
      tbody.append(tr);
    }
    table.append(tbody);
    panel.append(table);
    panel.append(el("div", "an-note",
      "Thumbs are manual operator ratings on runs; land rate and cost compare enhanced-on-create briefs against raw ones."));
    container.append(panel);
  }

  // ---- waste panel ------------------------------------------------------- //

  function renderWaste(container, runs) {
    const terminal = runs.filter(isTerminal);

    // Top-5 most expensive runs.
    const priced = terminal.filter((r) => hasNum(r.cost_usd)).sort((a, b) => b.cost_usd - a.cost_usd);
    if (!priced.length) return;
    const panel = el("div", "an-panel");
    panel.append(el("h3", "an-panel-title", "Waste"));
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
    container.append(panel);
  }

  // ---- harness telemetry (usage.per_turn detail) -------------------------- //

  // Only harness runs carry usage.per_turn (one record per loop turn). Legacy
  // records hold {turn, usage, tool_calls:[{name, result_chars}]}; instrumented
  // records add stop / ms_model / ms_tools / transcript_chars per turn, ms and
  // err/trunc (present-only-when-true) per tool call, and run-level
  // usage.exit_reason + usage.prompt_chars. Every stat below skips absent
  // fields (null ≠ 0), so old runs still render with fewer rows.
  function shapedRuns(runs) {
    return runs.filter(
      (r) => r.usage && Array.isArray(r.usage.per_turn) && r.usage.per_turn.length > 0
    );
  }

  // Classify a turn by what it dispatched. Precedence: a turn that edits is an
  // edit turn no matter what else it ran.
  const TURN_KINDS = ["edit", "bash", "search", "read", "text"];

  function turnKind(rec) {
    const names = (Array.isArray(rec.tool_calls) ? rec.tool_calls : []).map((c) => c.name);
    if (names.includes("edit_file") || names.includes("write_file")) return "edit";
    if (names.includes("run_bash")) return "bash";
    if (names.includes("grep")) return "search";
    if (names.includes("read_file") || names.includes("list_dir")) return "read";
    return "text";
  }

  // One pass over the shaped runs computing everything both harness panels
  // need. Estimated figures (delta attribution) are labeled estimated in the
  // UI; everything else is measured.
  function harnessStats(shaped) {
    const stats = {
      deltasByTurn: new Map(), // turn idx -> [input-delta, ...] (estimated)
      uncachedByTurn: new Map(), // turn idx -> [uncached input fraction, ...]
      modelMsByTurn: new Map(), // turn idx -> [ms_model, ...]
      tools: new Map(), // name -> {calls, chars, tokensEst, msList, errs, truncs}
      kindCounts: { edit: 0, bash: 0, search: 0, read: 0, text: 0 },
      msModelTotal: 0,
      msToolsTotal: 0,
      hasTiming: false,
      cacheRead: 0,
      cacheCreation: 0,
      cacheFold: 0,
      warmTurns: [], // per run: first turn idx with a cache read
      maxTokenStops: 0,
      toolFallback: false,
    };

    const tool = (name) => {
      if (!stats.tools.has(name)) {
        stats.tools.set(name, {
          calls: 0, chars: 0, tokensEst: 0, msList: [], errs: 0, truncs: 0,
        });
      }
      return stats.tools.get(name);
    };

    for (const r of shaped) {
      const pt = r.usage.per_turn;
      let warm = null;
      const runHasCacheKeys = pt.some((rec) => {
        const u = rec.usage || {};
        return "cache_read_input_tokens" in u || "cache_creation_input_tokens" in u;
      });

      for (let i = 0; i < pt.length; i++) {
        const rec = pt[i];
        const u = rec.usage || {};
        const fold = foldInput(u);

        stats.kindCounts[turnKind(rec)]++;
        if (rec.stop === "max_tokens") stats.maxTokenStops++;
        if (hasNum(rec.ms_model)) {
          stats.msModelTotal += rec.ms_model;
          stats.hasTiming = true;
          if (!stats.modelMsByTurn.has(i + 1)) stats.modelMsByTurn.set(i + 1, []);
          stats.modelMsByTurn.get(i + 1).push(rec.ms_model);
        }
        if (hasNum(rec.ms_tools)) stats.msToolsTotal += rec.ms_tools;

        // Cache accounting from the raw (pre-fold) per-turn splits — only for
        // runs whose vendor reports them, so Ollama never fakes a 100% miss.
        if (runHasCacheKeys && fold > 0) {
          stats.cacheRead += num(u.cache_read_input_tokens);
          stats.cacheCreation += num(u.cache_creation_input_tokens);
          stats.cacheFold += fold;
          if (!stats.uncachedByTurn.has(i + 1)) stats.uncachedByTurn.set(i + 1, []);
          stats.uncachedByTurn.get(i + 1).push(num(u.input_tokens) / fold);
          if (warm === null && num(u.cache_read_input_tokens) > 0) warm = i + 1;
        }

        // Per-tool measured stats (exact, from the call records).
        const calls = Array.isArray(rec.tool_calls) ? rec.tool_calls : [];
        for (const c of calls) {
          const t = tool(c.name || "?");
          t.calls++;
          t.chars += num(c.result_chars);
          if (hasNum(c.ms)) t.msList.push(c.ms);
          if (c.err) t.errs++;
          if (c.trunc) t.truncs++;
        }

        // Delta attribution (estimated): input(N) - output(N-1) ≈ tokens turn
        // (N-1)'s tool results appended, split by result_chars.
        if (i > 0) {
          const prev = pt[i - 1];
          const delta = Math.max(0, fold - num((prev.usage || {}).output_tokens));
          if (!stats.deltasByTurn.has(i)) stats.deltasByTurn.set(i, []);
          stats.deltasByTurn.get(i).push(delta);
          const prevCalls = Array.isArray(prev.tool_calls) ? prev.tool_calls : [];
          const totalChars = prevCalls.reduce((s, c) => s + num(c.result_chars), 0);
          if (prevCalls.length && totalChars > 0) {
            for (const c of prevCalls) {
              tool(c.name || "?").tokensEst += (num(c.result_chars) / totalChars) * delta;
            }
          } else if (prevCalls.length) {
            for (const c of prevCalls) {
              tool(c.name || "?").tokensEst += delta / prevCalls.length;
            }
            stats.toolFallback = true;
          }
        }
      }
      if (warm !== null) stats.warmTurns.push(warm);
    }
    return stats;
  }

  // A single horizontal stacked bar with a legend (shares of a whole).
  function stackBar(segments) {
    const total = segments.reduce((s, seg) => s + seg.value, 0);
    const wrap = el("div", "an-stack-wrap");
    const bar = el("div", "an-stack");
    const legend = el("div", "an-stack-legend");
    segments.forEach((seg, i) => {
      if (seg.value <= 0) return;
      const piece = el("div", "an-stack-seg an-stack-c" + (i % 6));
      piece.style.flexGrow = String(seg.value);
      piece.title = seg.label + ": " + seg.text;
      bar.append(piece);
      const item = el("span", "an-stack-item");
      item.append(el("span", "an-stack-dot an-stack-c" + (i % 6)));
      item.append(
        el("span", null, seg.label + " " + (total > 0 ? fmtPct(seg.value / total) : "—"))
      );
      legend.append(item);
    });
    wrap.append(bar, legend);
    return wrap;
  }

  function renderRunShape(container, runs) {
    const shaped = shapedRuns(runs).filter((r) => r.usage.per_turn.length > 1);
    if (!shaped.length) return;
    const s = harnessStats(shaped);

    const panel = el("div", "an-panel");
    panel.append(el("h3", "an-panel-title", "Run shape (harness runs)"));
    panel.append(
      el(
        "div",
        "an-note",
        shaped.length +
          " harness run(s) with per-turn detail · token attribution estimated" +
          (s.toolFallback ? " (some splits estimated evenly)" : "")
      )
    );

    const grid = el("div", "an-trend-grid");
    const turns = Array.from(s.deltasByTurn.keys()).sort((a, b) => a - b);
    grid.append(
      trendCell(
        "Median input added by turn (est.)",
        turns.map((t) => median(s.deltasByTurn.get(t))),
        turns.map((t) => "turn " + t),
        fmtTokens
      )
    );

    // Where cache efficiency breaks down: the uncached (full-price) share of
    // each turn's input. Rising late in the run = the rolling breakpoint fell
    // out of range; high on turn 1 is expected (cold prefill).
    if (s.cacheFold > 0) {
      const cTurns = Array.from(s.uncachedByTurn.keys()).sort((a, b) => a - b);
      grid.append(
        trendCell(
          "Uncached input share by turn",
          cTurns.map((t) => median(s.uncachedByTurn.get(t)) * 100),
          cTurns.map((t) => "turn " + t),
          (v) => v.toFixed(0) + "%"
        )
      );
    }

    // Model latency growth with context strengthens the eviction case: the
    // transcript costs wall-clock, not just dollars.
    if (s.modelMsByTurn.size > 1) {
      const mTurns = Array.from(s.modelMsByTurn.keys()).sort((a, b) => a - b);
      grid.append(
        trendCell(
          "Median model ms by turn",
          mTurns.map((t) => median(s.modelMsByTurn.get(t))),
          mTurns.map((t) => "turn " + t),
          (v) => fmtNum(v, 0) + " ms"
        )
      );
    }
    panel.append(grid);

    if (s.cacheFold > 0) {
      panel.append(
        el(
          "div",
          "an-note",
          "cache: " + fmtPct(s.cacheRead / s.cacheFold) + " read · write tax " +
            fmtPct(s.cacheCreation / s.cacheFold) +
            (s.warmTurns.length ? " · warms at turn " + fmtNum(median(s.warmTurns), 0) : " · never warms")
        )
      );
    }

    // Wall-clock split: model latency vs tool execution — decides whether the
    // time lever is routing/effort or tool timeouts/pagination.
    if (s.hasTiming && s.msModelTotal + s.msToolsTotal > 0) {
      panel.append(el("div", "an-subhead", "Where the time goes"));
      panel.append(
        stackBar([
          { label: "model", value: s.msModelTotal, text: fmtMs(s.msModelTotal) },
          { label: "tools", value: s.msToolsTotal, text: fmtMs(s.msToolsTotal) },
        ])
      );
    }

    const toolRows = Array.from(s.tools.entries()).sort(
      (a, b) => b[1].tokensEst - a[1].tokensEst
    );
    if (toolRows.length) {
      const table = el("table", "an-table");
      const thead = el("thead");
      const htr = el("tr");
      ["Tool", "Calls", "Tokens added (est.)", "Result chars", "p50 ms", "Err %", "Trunc %"].forEach(
        (h) => htr.append(el("th", null, h))
      );
      thead.append(htr);
      table.append(thead);
      const tbody = el("tbody");
      for (const [name, t] of toolRows) {
        const tr = el("tr");
        tr.append(el("td", "an-td-key", name));
        tr.append(el("td", null, String(t.calls)));
        tr.append(el("td", null, fmtTokens(t.tokensEst)));
        tr.append(el("td", null, fmtTokens(t.chars)));
        tr.append(el("td", null, t.msList.length ? fmtNum(median(t.msList), 0) : "—"));
        tr.append(el("td", null, t.calls ? fmtPct(t.errs / t.calls) : "—"));
        tr.append(el("td", null, t.calls ? fmtPct(t.truncs / t.calls) : "—"));
        tbody.append(tr);
      }
      table.append(tbody);
      panel.append(el("div", "an-subhead", "Context added per tool"), table);
    }
    container.append(panel);
  }

  // Marginal-turn-yield buckets: where extra turns stop buying landed changes
  // (informs the turn-cap / token stop-loss knobs).
  const TURN_BUCKETS = [
    { label: "1–10", lo: 1, hi: 10 },
    { label: "11–20", lo: 11, hi: 20 },
    { label: "21–30", lo: 21, hi: 30 },
    { label: "31–40", lo: 31, hi: 40 },
    { label: "41–50", lo: 41, hi: 50 },
    { label: "51+", lo: 51, hi: Infinity },
  ];

  function renderTurnComposition(container, runs) {
    const shaped = shapedRuns(runs);
    if (!shaped.length) return;
    const s = harnessStats(shaped);

    const panel = el("div", "an-panel");
    panel.append(el("h3", "an-panel-title", "Turn composition & exits (harness runs)"));

    // What the turns are spending on: hunting (read/search), changing (edit),
    // validating (bash), or stalling (text).
    let sections = 0;
    const totalTurns = TURN_KINDS.reduce((n, k) => n + s.kindCounts[k], 0);
    if (totalTurns > 0) {
      sections++;
      panel.append(el("div", "an-subhead", "Turns doing what"));
      panel.append(
        stackBar(
          TURN_KINDS.map((k) => ({
            label: k,
            value: s.kindCounts[k],
            text: s.kindCounts[k] + " turns",
          }))
        )
      );
    }
    if (s.maxTokenStops > 0) {
      panel.append(
        el(
          "div",
          "an-note",
          s.maxTokenStops + " turn(s) stopped at max_tokens — output clipped mid-turn (raise max_tokens)"
        )
      );
    }

    // Why runs ended, weighted by what they cost. "We cut it off" exits
    // (max_turns / timeout) carrying a big spend share are the stop-loss signal.
    const exits = new Map(); // reason -> {runs, cost, hasCost}
    for (const r of shaped) {
      const reason = r.usage.exit_reason || "—";
      if (!exits.has(reason)) exits.set(reason, { runs: 0, cost: 0, hasCost: false });
      const e = exits.get(reason);
      e.runs++;
      if (hasNum(r.cost_usd)) {
        e.cost += r.cost_usd;
        e.hasCost = true;
      }
    }
    if (exits.size > 1 || !exits.has("—")) {
      const table = el("table", "an-table");
      const thead = el("thead");
      const htr = el("tr");
      ["Exit", "Runs", "Spend"].forEach((h) => htr.append(el("th", null, h)));
      thead.append(htr);
      table.append(thead);
      const tbody = el("tbody");
      const rows = Array.from(exits.entries()).sort((a, b) => b[1].runs - a[1].runs);
      for (const [reason, e] of rows) {
        const tr = el("tr");
        tr.append(el("td", "an-td-key", reason));
        tr.append(el("td", null, String(e.runs)));
        tr.append(el("td", null, e.hasCost ? fmtMoney(e.cost) : "—"));
        tbody.append(tr);
      }
      table.append(tbody);
      panel.append(el("div", "an-subhead", "Exit reasons"), table);
      sections++;
    }

    // Marginal turn yield: of the runs that reached each turn bucket, how many
    // landed, and what the bucket's turns burned (measured throughput).
    const buckets = TURN_BUCKETS.map((b) => ({ ...b, runs: 0, landed: 0, tokens: [] }));
    for (const r of shaped) {
      const pt = r.usage.per_turn;
      for (const b of buckets) {
        if (pt.length < b.lo) continue;
        b.runs++;
        if (r.landed) b.landed++;
        let burned = 0;
        for (let i = b.lo - 1; i < Math.min(pt.length, b.hi); i++) {
          burned += foldInput(pt[i].usage || {});
        }
        b.tokens.push(burned);
      }
    }
    const reached = buckets.filter((b) => b.runs > 0);
    if (reached.length > 1) {
      const table = el("table", "an-table");
      const thead = el("thead");
      const htr = el("tr");
      ["Turns reached", "Runs", "Land %", "Median tokens in bucket"].forEach((h) =>
        htr.append(el("th", null, h))
      );
      thead.append(htr);
      table.append(thead);
      const tbody = el("tbody");
      for (const b of reached) {
        const tr = el("tr");
        tr.append(el("td", "an-td-key", b.label));
        tr.append(el("td", null, String(b.runs)));
        tr.append(el("td", null, fmtPct(b.landed / b.runs)));
        tr.append(el("td", null, fmtTokens(median(b.tokens))));
        tbody.append(tr);
      }
      table.append(tbody);
      panel.append(el("div", "an-subhead", "Marginal turn yield"), table);
      sections++;
    }

    if (sections > 0) container.append(panel);
  }

  function fmtMs(n) {
    if (!hasNum(n)) return "—";
    if (n >= 60000) return (n / 60000).toFixed(1) + "m";
    if (n >= 1000) return (n / 1000).toFixed(1) + "s";
    return Math.round(n) + "ms";
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

  // ---- queue scope ------------------------------------------------------- //

  // The storage key of the main queue is "" (falsy), so it needs its own
  // normalization and display label — distinct()/groupBy() would drop it.
  function queueKey(r) {
    return r.queue || "";
  }

  function queueLabel(key) {
    return key === "" ? "main" : key;
  }

  // ---- top-level render -------------------------------------------------- //

  function render(container, opts) {
    opts = opts || {};
    const fetchRuns = opts.fetchRuns;
    if (typeof fetchRuns !== "function") {
      throw new Error("Analytics.render requires opts.fetchRuns");
    }

    // View state, kept locally on the container so re-render is cheap.
    // `queue` is the client-side scope: null = all queues, else a queue key
    // ("" = main). Seeded from opts.defaultQueue (the host's focused queue) so
    // the page opens scoped, with "All queues" one selection away.
    const view = {
      window: "7d",
      dimension: "all",
      value: "all",
      queue: typeof opts.defaultQueue === "string" ? opts.defaultQueue : null,
      // The seeded queue is a guess (the host's focused queue may have no runs
      // at all — e.g. an unused main queue). Until the operator explicitly
      // picks a scope, an empty seed falls back to "All queues" on load.
      queueTouched: false,
      runs: [],
      loaded: false,
    };

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

      // Queue scope dropdown: every queue seen in the loaded (fleet-wide)
      // runs, then a separator, then "All queues". Scoping is client-side so
      // switching never refetches — and failures in another queue are one
      // selection away instead of invisible.
      const queueKeys = new Set(view.runs.map(queueKey));
      if (view.queue !== null) queueKeys.add(view.queue);
      const queueSel = el("select", "an-select");
      for (const key of Array.from(queueKeys).sort()) {
        const o = el("option", null, queueLabel(key));
        o.value = "q:" + key;
        if (view.queue === key) o.selected = true;
        queueSel.append(o);
      }
      const sep = el("option", null, "───");
      sep.disabled = true;
      queueSel.append(sep);
      const allQueues = el("option", null, "All queues");
      allQueues.value = "all";
      if (view.queue === null) allQueues.selected = true;
      queueSel.append(allQueues);
      queueSel.addEventListener("change", () => {
        view.queue = queueSel.value === "all" ? null : queueSel.value.slice(2);
        view.queueTouched = true;
        renderControls();
        renderBody();
      });
      controls.append(queueSel);

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
      let runs = view.runs;
      if (view.queue !== null) runs = runs.filter((r) => queueKey(r) === view.queue);
      if (view.dimension === "all" || view.value === "all") return runs;
      return runs.filter((r) => r[view.dimension] === view.value);
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
      renderEnhancement(body, current);
      renderWaste(body, current);
      renderRunShape(body, current);
      renderTurnComposition(body, current);
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
      // If the seeded queue scope matches nothing in the loaded fleet-wide
      // runs, showing "No runs in this window" would be a lie of scoping, not
      // data. Widen to "All queues" until the operator picks a scope herself.
      if (
        !view.queueTouched &&
        view.queue !== null &&
        !view.runs.some((r) => queueKey(r) === view.queue)
      ) {
        view.queue = null;
      }
      renderControls();
      renderBody();
    }

    reload();
  }

  global.Analytics = { render };
})(window);
