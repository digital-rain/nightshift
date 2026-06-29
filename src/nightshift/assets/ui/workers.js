// Workers page (manager-era). Self-contained: it renders #screen-workers from
// the manager operator API and re-renders on demand. The existing app.js owns
// view switching (it sets body[data-view] when its nav delegate fires); we just
// observe that attribute and paint when "workers" becomes active. Live
// convergence is driven by manager-events.js, which calls window.refreshWorkers
// on relevant deltas.

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  async function getJSON(url) {
    const r = await fetch(url, { headers: { Accept: "application/json" } });
    if (!r.ok) throw new Error(`${url} -> ${r.status}`);
    return r.json();
  }

  function fmtRouting(w) {
    const q =
      w.queues == null
        ? "any queue"
        : `queues: ${(w.queues || []).join(", ") || "—"}`;
    const p =
      w.priorities == null
        ? "any priority"
        : `pri: ${(w.priorities || []).join(", ") || "—"}`;
    return `${q} · ${p}`;
  }

  function fmtNow(w) {
    if (w.status === "busy" && w.current_task) {
      const q = w.current_queue ? `${w.current_queue}/` : "";
      return `▶ ${q}${w.current_task}`;
    }
    return w.status === "offline" ? "—" : "idle";
  }

  // Queues this worker is dedicated to (manager-side binding map).
  function dedicatedQueues(workerId, dedication) {
    const out = [];
    for (const [queue, ids] of Object.entries(dedication || {})) {
      if ((ids || []).includes(workerId)) out.push(queue);
    }
    return out;
  }

  function capLine(label, items) {
    const div = document.createElement("div");
    div.className = "worker-caps";
    const k = document.createElement("span");
    k.className = "caps-label";
    k.textContent = label;
    const v = document.createElement("span");
    v.className = "caps-val";
    v.textContent = items && items.length ? items.join(", ") : "—";
    div.append(k, v);
    return div;
  }

  function workerCard(w, dedication) {
    const li = document.createElement("li");
    li.className = "worker-card";
    li.dataset.worker = w.id;

    const head = document.createElement("div");
    head.className = "worker-card-head";
    const workerUrl = (w.meta || {}).worker_url;
    let id;
    if (workerUrl) {
      id = document.createElement("a");
      id.href = workerUrl;
      id.target = "_blank";
      id.rel = "noopener";
    } else {
      id = document.createElement("span");
    }
    id.className = "worker-id";
    id.textContent = w.id;
    const backend = document.createElement("span");
    backend.className = "worker-backend";
    backend.textContent = w.backend || "?";
    const status = document.createElement("span");
    const s = (w.status || "idle").toLowerCase();
    status.className = `worker-status ${s}`;
    status.textContent = s;
    head.append(id, backend, status);

    const routing = document.createElement("div");
    routing.className = "worker-routing";
    routing.textContent = fmtRouting(w);

    const now = document.createElement("div");
    now.className = "worker-now";
    now.textContent = fmtNow(w);

    li.append(head, routing, now);
    li.append(capLine("models", w.models || []));
    li.append(capLine("mcp", w.mcps || []));
    const ded = dedicatedQueues(w.id, dedication);
    if (ded.length) li.append(capLine("dedicated", ded));
    return li;
  }

  function num(v) {
    const n = Number(v || 0);
    return Number.isFinite(n) ? Math.round(n) : 0;
  }

  // Compact token count: 1234 -> "1.2k", 2_300_000 -> "2.3M".
  function compact(v) {
    const n = Number(v || 0);
    if (!Number.isFinite(n) || n === 0) return "0";
    if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
    return String(Math.round(n));
  }

  function cost(v) {
    const n = Number(v || 0);
    if (!Number.isFinite(n) || n === 0) return "—";
    return "$" + n.toFixed(n < 1 ? 3 : 2);
  }

  function avg1(v) {
    const n = Number(v || 0);
    return Number.isFinite(n) && n > 0 ? n.toFixed(1) : "0";
  }

  function statRow(cells) {
    const tr = document.createElement("tr");
    for (let i = 0; i < cells.length; i++) {
      const td = document.createElement("td");
      td.textContent = cells[i];
      tr.appendChild(td);
    }
    return tr;
  }

  const STAT_COLS = 9;

  function renderStatTable(bodyId, rows, keyField) {
    const body = $(bodyId);
    if (!body) return;
    body.replaceChildren();
    if (!rows || !rows.length) {
      body.appendChild(statRow(Array(STAT_COLS).fill("—").map((v, i) => (i ? "" : v))));
      return;
    }
    for (const r of rows) {
      const key = keyField === "queue" ? r[keyField] || "main" : r[keyField] || "—";
      body.appendChild(
        statRow([
          key,
          num(r.total_runs),
          num(r.completed),
          num(r.errored),
          num(r.total_loc),
          num(r.avg_seconds),
          `${num(r.total_turns)} (${avg1(r.avg_turns)})`,
          compact(r.total_tokens),
          cost(r.total_cost_usd),
        ]),
      );
    }
  }

  function renderBlocked(blocked) {
    const wrap = $("workers-blocked");
    const list = $("blocked-list");
    if (!wrap || !list) return;
    list.replaceChildren();
    if (!blocked || !blocked.length) {
      wrap.hidden = true;
      return;
    }
    wrap.hidden = false;
    for (const b of blocked) {
      const li = document.createElement("li");
      const task = document.createElement("strong");
      task.textContent = (b.queue ? `${b.queue}/` : "") + (b.task || "?");
      const reason = document.createElement("span");
      reason.className = "reason";
      reason.textContent = b.blocked_reason ? ` — ${b.blocked_reason}` : "";
      li.append(task, reason);
      list.appendChild(li);
    }
  }

  // Editor row: one queue, its bound worker ids (comma-separated), and a save
  // button that PUTs the new binding. Empty input clears the dedication.
  function dedicationRow(queue, boundIds) {
    const li = document.createElement("li");
    li.className = "dedication-row";
    const name = document.createElement("span");
    name.className = "dedication-queue";
    name.textContent = queue;
    const input = document.createElement("input");
    input.className = "dedication-input";
    input.type = "text";
    input.value = (boundIds || []).join(", ");
    input.placeholder = "any worker";
    input.setAttribute("aria-label", `Workers dedicated to ${queue}`);
    const save = document.createElement("button");
    save.className = "dedication-save";
    save.type = "button";
    save.textContent = "Save";
    save.addEventListener("click", async () => {
      const ids = input.value
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      save.disabled = true;
      try {
        const r = await fetch(
          `/api/queue/dedication?queue=${encodeURIComponent(queue)}`,
          {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ worker_ids: ids }),
          },
        );
        if (!r.ok) throw new Error(`save -> ${r.status}`);
        await refreshWorkers();
      } catch (e) {
        save.disabled = false;
        alert("could not save queue dedication");
      }
    });
    li.append(name, input, save);
    return li;
  }

  function renderDedication(queues, dedication) {
    const list = $("dedication-list");
    if (!list) return;
    list.replaceChildren();
    for (const q of queues) {
      list.appendChild(dedicationRow(q, (dedication || {})[q] || []));
    }
  }

  // The set of queues the operator can dedicate: 'main' plus every playlist.
  function allQueues(playlists) {
    const names = (playlists || []).map((p) => p.name).filter(Boolean);
    return ["main", ...names];
  }

  let inFlight = false;
  async function refreshWorkers() {
    if (inFlight) return;
    inFlight = true;
    try {
      const [workers, stats, blocked, dedicationResp, playlists] =
        await Promise.all([
          getJSON("/api/workers").catch(() => []),
          getJSON("/api/stats").catch(() => ({})),
          getJSON("/api/blocked").catch(() => []),
          getJSON("/api/queue/dedication").catch(() => ({})),
          getJSON("/api/playlists").catch(() => []),
        ]);
      const dedication = (dedicationResp && dedicationResp.dedication) || {};

      const list = $("workers-list");
      const empty = $("workers-empty");
      const count = $("workers-count");
      if (list) {
        list.replaceChildren();
        for (const w of workers) list.appendChild(workerCard(w, dedication));
      }
      if (empty) empty.hidden = workers.length > 0;
      if (count) count.textContent = workers.length ? String(workers.length) : "";

      renderDedication(allQueues(playlists), dedication);
      renderStatTable("by-backend-body", stats.by_backend, "backend");
      renderStatTable("by-worker-body", stats.by_worker, "worker_id");
      renderStatTable("by-model-body", stats.by_model, "model");
      renderStatTable("by-queue-body", stats.by_queue, "queue");
      renderBlocked(blocked);
    } finally {
      inFlight = false;
    }
  }

  window.refreshWorkers = refreshWorkers;

  // Paint when the operator switches to the Workers tab. app.js drives the
  // attribute; we react to it without coupling to its internals.
  const observer = new MutationObserver(() => {
    if (document.body.getAttribute("data-view") === "workers") refreshWorkers();
  });
  observer.observe(document.body, {
    attributes: true,
    attributeFilter: ["data-view"],
  });

  if (document.body.getAttribute("data-view") === "workers") refreshWorkers();
})();
