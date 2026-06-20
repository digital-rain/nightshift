// Minimal Nightshift Worker UI: Now + History, polling the worker status API.
// Cadence is taken from the manager-provided refresh (config-driven, never a
// hardcoded magic number per invariant 13); we fall back to 3s only if unset.

"use strict";

const FALLBACK_REFRESH_MS = 3000;
let refreshMs = FALLBACK_REFRESH_MS;
let timer = null;

async function getJSON(path) {
  const resp = await fetch(path, { headers: { Accept: "application/json" } });
  if (!resp.ok) throw new Error(`${path} -> ${resp.status}`);
  return resp.json();
}

function setView(view) {
  document.body.dataset.view = view;
  document.querySelectorAll(".nav-opt").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === view);
  });
  document.getElementById("view-now").hidden = view !== "now";
  document.getElementById("view-history").hidden = view !== "history";
}

async function loadInfo() {
  try {
    const info = await getJSON("/api/info");
    document.getElementById("worker-id").textContent = info.worker_id || "worker";
    document.getElementById("worker-backend").textContent = info.backend || "?";
    document.getElementById("brand-tag").textContent = info.brand_tag || "Nightshift Worker";
    const models = info.models && info.models.length ? info.models.join(", ") : "—";
    const mcps = info.mcps && info.mcps.length ? info.mcps.join(", ") : "—";
    document.getElementById("cap-models").textContent = models;
    document.getElementById("cap-mcps").textContent = mcps;
    if (typeof info.refresh_ms === "number" && info.refresh_ms > 0) {
      refreshMs = info.refresh_ms;
    }
  } catch (_e) {
    /* manager/worker not ready yet */
  }
}

async function refreshNow() {
  let now = null;
  try {
    now = await getJSON("/api/now");
  } catch (_e) {
    now = null;
  }
  const idle = document.getElementById("now-idle");
  const card = document.getElementById("now-card");
  if (!now || !now.run_id) {
    idle.hidden = false;
    card.hidden = true;
    return;
  }
  idle.hidden = true;
  card.hidden = false;
  document.getElementById("now-task").textContent = now.title || now.task;
  document.getElementById("now-queue").textContent = now.queue || "main";
  document.getElementById("now-phase").textContent = now.phase || "worker";
  document.getElementById("now-model").textContent = `model: ${now.model || "auto"}`;
  document.getElementById("now-started").textContent = `started: ${now.started_at || "—"}`;
  const log = document.getElementById("now-log");
  log.textContent = (now.log_tail || []).join("");
  log.scrollTop = log.scrollHeight;
}

async function refreshHistory() {
  try {
    const [rows, stats] = await Promise.all([
      getJSON("/api/history"),
      getJSON("/api/stats"),
    ]);
    document.getElementById("st-total").textContent = stats.total_runs || 0;
    document.getElementById("st-done").textContent = stats.completed || 0;
    document.getElementById("st-err").textContent = stats.errored || 0;
    document.getElementById("st-loc").textContent = stats.total_loc || 0;
    const body = document.getElementById("history-body");
    body.innerHTML = "";
    for (const r of rows) {
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td>${escapeHtml(r.title || r.task)}</td>` +
        `<td>${escapeHtml(r.queue || "main")}</td>` +
        `<td>${escapeHtml(r.model || "")}</td>` +
        `<td class="status-${escapeHtml(r.status || "")}">${escapeHtml(r.status || "")}</td>` +
        `<td>${escapeHtml(r.result_line || "")}</td>`;
      body.appendChild(tr);
    }
  } catch (_e) {
    /* ignore transient errors */
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

async function tick() {
  await refreshNow();
  if (document.body.dataset.view === "history") {
    await refreshHistory();
  }
  timer = setTimeout(tick, refreshMs);
}

document.addEventListener("DOMContentLoaded", async () => {
  document.querySelectorAll(".nav-opt").forEach((b) => {
    b.addEventListener("click", () => {
      setView(b.dataset.view);
      if (b.dataset.view === "history") refreshHistory();
    });
  });
  await loadInfo();
  if (timer) clearTimeout(timer);
  tick();
});
