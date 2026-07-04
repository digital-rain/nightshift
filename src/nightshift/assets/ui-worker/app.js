// Minimal Nightshift Worker UI: Now + History, polling the worker status API.
// Cadence is taken from the manager-provided refresh (config-driven, never a
// hardcoded magic number per invariant 13); we fall back to 3s only if unset.

"use strict";

const FALLBACK_REFRESH_MS = 3000;
let refreshMs = FALLBACK_REFRESH_MS;
let timer = null;
// Whether the shared analytics module is mounted on the stats page. Reset on
// explicit navigation to stats; kept across ticks so it retains the operator's
// window/filter selections.
let _analyticsMounted = false;

async function getJSON(path) {
  const resp = await fetch(path, { headers: { Accept: "application/json" } });
  if (!resp.ok) throw new Error(`${path} -> ${resp.status}`);
  return resp.json();
}

let currentView = "now";
let detailRecord = null;
let historyRows = [];

function setView(view) {
  currentView = view;
  document.body.dataset.view = view;
  const historyGroup = view === "detail" || view === "stats";
  document.querySelectorAll(".nav-opt").forEach((b) => {
    const navView = b.dataset.view;
    b.classList.toggle("active", navView === view || (navView === "history" && historyGroup));
  });
  document.getElementById("view-now").hidden = view !== "now";
  document.getElementById("view-history").hidden = view !== "history";
  document.getElementById("view-detail").hidden = view !== "detail";
  document.getElementById("view-stats").hidden = view !== "stats";
  document.getElementById("view-settings").hidden = view !== "settings";
  if (view === "detail") renderHistoryDetail();
  if (view === "stats") { _analyticsMounted = false; renderStats(); }
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
  document.getElementById("now-repo").textContent = now.repo || "—";
  document.getElementById("now-phase").textContent = now.phase || "worker";
  document.getElementById("now-model").textContent = `model: ${now.model || "auto"}`;
  document.getElementById("now-started").textContent = `started: ${now.started_at || "—"}`;
  document.getElementById("now-branch").textContent = now.branch ? `branch: ${now.branch}` : "—";
  document.getElementById("now-worktree").textContent = now.worktree ? `worktree: ${now.worktree}` : "—";
  const log = document.getElementById("now-log");
  log.textContent = (now.log_tail || []).join("");
  log.scrollTop = log.scrollHeight;
}

const STATE_LABELS = {
  pending: "Queued",
  blocked: "Blocked",
  quarantined: "Quarantined",
  running: "Running",
  paused: "Paused",
  repo_unavailable: "Paused",
  completed: "Completed",
  error: "Failed",
  stopped: "Cancelled",
  skipped: "Skipped",
  aborted: "Aborted",
};

const FAILURE_LABELS = {
  merge_conflict: "merge conflict",
  merge_rejected: "merge rejected",
  validation_error: "validation",
  worker_error: "worker error",
  worker_launch: "worker launch",
  timeout: "timeout",
  aborted: "aborted",
  no_changes: "no changes",
};

function stateLabel(status) {
  return STATE_LABELS[status] || (status ? status[0].toUpperCase() + status.slice(1) : "—");
}

function statusClass(status) {
  if (status === "repo_unavailable") return "paused";
  if (status === "blocked") return "error";
  if (status === "quarantined") return "quarantined";
  return status || "running";
}

function statusPill(status) {
  const span = document.createElement("span");
  span.className = "status " + statusClass(status);
  span.textContent = stateLabel(status);
  return span;
}

function failureBadge(kind) {
  const label = FAILURE_LABELS[kind];
  if (!label) return null;
  const span = document.createElement("span");
  span.className = "fail-badge fail-" + kind;
  span.textContent = label;
  return span;
}

function formatElapsed(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${String(m % 60).padStart(2, "0")}m`;
}

function formatDuration(start, end) {
  if (!start || !end) return "";
  const ms = Date.parse(end) - Date.parse(start);
  if (!(ms >= 0)) return "";
  return formatElapsed(ms);
}

function formatWhen(iso) {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const diff = Date.now() - t;
  const min = Math.round(diff / 60000);
  if (min < 1) return "just now";
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  return new Date(t).toLocaleDateString();
}

function historyRow(r) {
  const row = document.createElement("button");
  row.className = "hrow";
  row.type = "button";
  row.addEventListener("click", () => openHistoryDetail(r));

  const pill = statusPill(r.status || "running");
  pill.classList.add("hrow-pill");

  const main = document.createElement("div");
  main.className = "hrow-main";
  const title = document.createElement("div");
  title.className = "hrow-title";
  title.textContent = r.title || r.task;
  if (r.repo) {
    const rtag = document.createElement("span");
    rtag.className = "hrow-tag hrow-repo";
    rtag.textContent = r.repo;
    rtag.title = `Target repo: ${r.repo}`;
    title.append(rtag);
  }
  const meta = document.createElement("div");
  meta.className = "hrow-meta";
  const badge = r.status === "error" ? failureBadge(r.failure_kind) : null;
  if (badge) meta.append(badge);
  if (r.quarantined) {
    const qbadge = document.createElement("span");
    qbadge.className = "status quarantined";
    qbadge.style.fontSize = "10px";
    qbadge.style.padding = "1px 6px";
    qbadge.textContent = "Quarantined";
    meta.append(qbadge);
  }
  const metaText = document.createElement("span");
  metaText.className = "hrow-meta-text";
  metaText.textContent = r.result_line || `${r.task}.md`;
  meta.append(metaText);
  main.append(title, meta);

  const aside = document.createElement("div");
  aside.className = "hrow-aside";
  const dur = document.createElement("div");
  dur.className = "hrow-dur";
  dur.textContent = formatDuration(r.started_at, r.finished_at);
  const when = document.createElement("div");
  when.className = "hrow-when";
  when.textContent = formatWhen(r.finished_at || r.started_at);
  aside.append(dur, when);

  row.append(pill, main, aside);
  return row;
}

async function refreshHistory() {
  try {
    const [rows, stats] = await Promise.all([
      getJSON("/api/history"),
      getJSON("/api/stats"),
    ]);
    historyRows = rows;
    document.getElementById("st-total").textContent = stats.total_runs || 0;
    document.getElementById("st-done").textContent = stats.completed || 0;
    document.getElementById("st-err").textContent = stats.errored || 0;
    document.getElementById("st-loc").textContent = stats.total_loc || 0;
    const list = document.getElementById("history-list");
    list.innerHTML = "";
    const empty = document.getElementById("history-empty");
    empty.hidden = rows.length > 0;
    for (const r of rows.slice(0, 200)) {
      list.append(historyRow(r));
    }
  } catch (_e) {
    /* ignore transient errors */
  }
}

// --------------------------------------------------------------------------
// History detail view (mirrors the manager's read-only history detail)
// --------------------------------------------------------------------------

const CHEVRON_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>';

function compactTokens(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n === 0) return "0";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(Math.round(n));
}

function expando(caption, { open = true } = {}) {
  const panel = document.createElement("section");
  panel.className = "xpanel" + (open ? " open" : "");
  const head = document.createElement("button");
  head.type = "button";
  head.className = "xpanel-head";
  head.setAttribute("aria-expanded", open ? "true" : "false");
  const chev = document.createElement("span");
  chev.className = "xpanel-chev";
  chev.innerHTML = CHEVRON_SVG;
  const cap = document.createElement("span");
  cap.className = "xpanel-cap";
  cap.textContent = caption;
  head.append(chev, cap);
  const body = document.createElement("div");
  body.className = "xpanel-body";
  body.hidden = !open;
  head.addEventListener("click", () => {
    const isOpen = panel.classList.toggle("open");
    head.setAttribute("aria-expanded", isOpen ? "true" : "false");
    body.hidden = !isOpen;
  });
  panel.append(head, body);
  return { panel, body };
}

function metaGrid(pairs) {
  const grid = document.createElement("div");
  grid.className = "detail-meta";
  for (const [k, v] of pairs) {
    const key = document.createElement("div");
    key.className = "meta-k";
    key.textContent = k;
    const val = document.createElement("div");
    val.className = "meta-v";
    val.textContent = v;
    grid.append(key, val);
  }
  return grid;
}

function detailStatusHead(task, status) {
  return metaGrid([["File", `${task}.md`], ["Status", stateLabel(status)]]);
}

function runDetailPairs(rec) {
  const pairs = [
    ["Started", rec.started_at ? new Date(rec.started_at).toLocaleString() : "—"],
    ["Finished", rec.finished_at ? new Date(rec.finished_at).toLocaleString() : "—"],
    ["Duration", formatDuration(rec.started_at, rec.finished_at) || "—"],
  ];
  if (rec.repo) pairs.push(["Repo", rec.repo]);
  if (rec.worktree) pairs.push(["Worktree", rec.worktree]);
  if (rec.commit_sha) {
    const shas = String(rec.commit_sha).split(",").map(s => s.trim()).filter(Boolean);
    pairs.push([shas.length > 1 ? "Commits" : "Commit", `landed (${shas.join(", ")})`]);
  } else {
    pairs.push(["Commit", rec.status === "error" ? "not landed" : "—"]);
  }
  if (rec.model) pairs.push(["Model", rec.model]);
  if (rec.backend) pairs.push(["Backend", rec.backend]);
  if (typeof rec.turns === "number") pairs.push(["Turns", String(rec.turns)]);
  const inTok = typeof rec.input_tokens === "number" ? rec.input_tokens : null;
  const outTok = typeof rec.output_tokens === "number" ? rec.output_tokens : null;
  if (inTok !== null || outTok !== null) {
    const tok = [inTok !== null ? `${compactTokens(inTok)} in` : null,
                 outTok !== null ? `${compactTokens(outTok)} out` : null]
      .filter(Boolean).join(" · ");
    pairs.push(["Tokens", tok]);
  }
  if (typeof rec.cost_usd === "number") {
    pairs.push(["Cost", `$${rec.cost_usd.toFixed(4)}`]);
  }
  return pairs;
}

function openHistoryDetail(rec) {
  detailRecord = rec;
  setView("detail");
}

function closeDetailScreen() {
  detailRecord = null;
  setView("history");
}

function renderHistoryDetail() {
  const rec = detailRecord;
  const body = document.getElementById("detail-screen-body");
  if (!rec || !body) return;
  document.getElementById("detail-screen-title").textContent = rec.title || rec.task;
  body.innerHTML = "";

  body.append(detailStatusHead(rec.task, rec.quarantined ? "quarantined" : rec.status));

  if (rec.result_line) {
    const res = document.createElement("div");
    res.className = "detail-result";
    res.textContent = rec.result_line;
    const badge = rec.status === "error" ? failureBadge(rec.failure_kind) : null;
    if (badge) res.append(badge);
    body.append(res);
  }

  if (rec.error && (rec.status === "error" || rec.status === "aborted")) {
    const err = document.createElement("pre");
    err.className = "detail-error";
    err.textContent = rec.error;
    body.append(err);
  }

  const rd = expando("Run details", { open: true });
  rd.body.append(metaGrid(runDetailPairs(rec)));
  body.append(rd.panel);
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

// --------------------------------------------------------------------------
// Statistics — graphical summary of the worker's run history.
// --------------------------------------------------------------------------

function openStats() {
  setView("stats");
}

function closeStats() {
  setView("history");
}

// The stats page is the shared analytics module (served from /shared/
// analytics.js by the worker's static mount), fed by /api/history. It owns its
// own view state (window, dimension filter), so mount it once and let it manage
// itself rather than re-rendering on every tick. Mount state lives at the top
// of the file (_analyticsMounted).
function renderStats() {
  const body = document.getElementById("stats-body");
  if (!body) return;
  const empty = document.getElementById("stats-empty");
  if (empty) empty.hidden = true;

  if (typeof Analytics === "undefined" || !Analytics.render) {
    body.textContent = "Analytics module unavailable.";
    return;
  }
  if (_analyticsMounted && body.firstChild) return;

  _analyticsMounted = true;
  Analytics.render(body, {
    title: "Analytics",
    // The worker's /api/history has no `since` param; fetch a generous window
    // and let the shared module window client-side by started_at.
    fetchRuns: async () => getJSON("/api/history?limit=5000"),
  });
}

// --------------------------------------------------------------------------
// Worker Settings (compact renderer over worker surface)
// --------------------------------------------------------------------------
const wSettings = { tiers: [], activeSurface: null, activeCategory: null, searchQuery: "", dirty: {}, errors: {}, restartPending: false, providers: [] };

async function loadWorkerSettings() {
  try {
    const [data, bData] = await Promise.all([
      getJSON("/api/settings"),
      getJSON("/api/backends").catch(() => ({ backends: [] })),
    ]);
    wSettings.providers = (bData.backends || []).map(b => b.name);
    wSettings.tiers = data.tiers || [];
    wSettings.dirty = {};
    wSettings.errors = {};
    wSettings.searchQuery = "";
    if (wSettings.tiers.length && !wSettings.activeSurface) {
      const first = wSettings.tiers[0];
      wSettings.activeSurface = first.surface;
      if (first.categories.length) {
        wSettings.activeCategory = first.categories[0].name;
      }
    }
    const searchEl = document.getElementById("w-settings-search");
    if (searchEl) searchEl.value = "";
    renderWorkerSidebar();
    renderWorkerSettings();
    updateWorkerSaveBar();
  } catch (_e) { /* settings endpoint may not exist yet */ }
}

function renderWorkerSidebar() {
  const tree = document.getElementById("w-settings-tree");
  if (!tree) return;
  tree.innerHTML = "";
  for (const tier of wSettings.tiers) {
    const div = document.createElement("div");
    div.className = "w-st-tier";
    const label = document.createElement("div");
    label.className = "w-st-tier-label";
    label.textContent = tier.surface.charAt(0).toUpperCase() + tier.surface.slice(1);
    div.append(label);
    for (const cat of tier.categories) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "w-st-cat";
      if (tier.surface === wSettings.activeSurface && cat.name === wSettings.activeCategory) {
        btn.classList.add("active");
      }
      btn.textContent = cat.name;
      btn.addEventListener("click", () => {
        wSettings.activeSurface = tier.surface;
        wSettings.activeCategory = cat.name;
        wSettings.searchQuery = "";
        const searchEl = document.getElementById("w-settings-search");
        if (searchEl) searchEl.value = "";
        renderWorkerSidebar();
        renderWorkerSettings();
      });
      div.append(btn);
    }
    tree.append(div);
  }
}

function renderWorkerSettings() {
  const pane = document.getElementById("w-settings-fields");
  pane.innerHTML = "";
  const query = wSettings.searchQuery.toLowerCase().trim();
  if (query) {
    renderWorkerSearchResults(pane, query);
    return;
  }
  const tier = wSettings.tiers.find(t => t.surface === wSettings.activeSurface);
  if (!tier) return;
  const cat = tier.categories.find(c => c.name === wSettings.activeCategory);
  if (!cat) return;
  const title = document.createElement("h2");
  title.className = "w-settings-cat-title";
  title.textContent = cat.name;
  pane.append(title);
  for (const field of cat.fields) {
    pane.appendChild(buildWorkerFieldRow(field, tier.surface));
  }

  const allReadonly = cat.fields.every(f => f.type === "readonly");
  if (!allReadonly) {
    const rawDetails = document.createElement("details");
    rawDetails.className = "sf-raw-json";
    const summary = document.createElement("summary");
    summary.textContent = "Raw JSON";
    const textarea = document.createElement("textarea");
    textarea.rows = 8;
    textarea.spellcheck = false;
    const rawData = {};
    for (const f of cat.fields) {
      if (!f.secret && f.type !== "readonly") rawData[f.key] = f.stored;
    }
    textarea.value = JSON.stringify(rawData, null, 2);
    textarea.dataset.surface = tier.surface;
    textarea.dataset.category = cat.name;
    rawDetails.append(summary, textarea);
    pane.append(rawDetails);
  }
}

function renderWorkerSearchResults(pane, query) {
  const title = document.createElement("h2");
  title.className = "w-settings-cat-title";
  title.textContent = `Search: "${query}"`;
  pane.append(title);
  let count = 0;
  for (const tier of wSettings.tiers) {
    for (const cat of tier.categories) {
      for (const field of cat.fields) {
        const searchable = `${field.label} ${field.key} ${field.desc}`.toLowerCase();
        if (searchable.includes(query)) {
          pane.appendChild(buildWorkerFieldRow(field, tier.surface));
          count++;
        }
      }
    }
  }
  if (!count) {
    const empty = document.createElement("div");
    empty.className = "w-sf-empty-search";
    empty.textContent = "No settings match your search.";
    pane.append(empty);
  }
}

function buildWorkerFieldRow(field, surface) {
  const fullKey = `${surface}.${field.key}`;
  const row = document.createElement("div");
  row.className = "w-sf-row";
  if (fullKey in wSettings.dirty) row.classList.add("w-sf-dirty");
  if (fullKey in wSettings.errors) row.classList.add("w-sf-error");

  const head = document.createElement("div");
  head.className = "w-sf-head";
  const label = document.createElement("span");
  label.className = "w-sf-label";
  label.textContent = field.label;
  const key = document.createElement("code");
  key.className = "w-sf-key";
  key.textContent = field.key;
  const badges = document.createElement("span");
  badges.className = "w-sf-badges";
  // Only badge the consequential apply mode: restart. next-task and live are
  // the common cases and get no badge — they would just be noise.
  if (field.apply === "restart") {
    const b = document.createElement("span");
    b.className = "w-sf-badge w-sf-badge-restart";
    b.textContent = "restart";
    badges.appendChild(b);
  }
  if (field.secret) {
    const b = document.createElement("span");
    b.className = "w-sf-badge w-sf-badge-secret";
    b.textContent = "secret";
    badges.appendChild(b);
  }
  head.append(label, key, badges);
  row.appendChild(head);

  const desc = document.createElement("div");
  desc.className = "w-sf-desc";
  desc.textContent = field.desc;
  row.appendChild(desc);

  if (field.env_shadowed) {
    const warn = document.createElement("div");
    warn.className = "w-sf-env-warn";
    warn.textContent = `Overridden by ${field.env}`;
    row.appendChild(warn);
  }

  const control = document.createElement("div");
  control.className = "w-sf-control";
  control.appendChild(buildWorkerControl(field, surface, fullKey));
  row.appendChild(control);

  if (fullKey in wSettings.errors) {
    const err = document.createElement("div");
    err.className = "w-sf-error-msg";
    err.textContent = wSettings.errors[fullKey];
    row.appendChild(err);
  }

  return row;
}

function buildModelIdComposite(value, onChange, onBlur) {
  const wrap = document.createElement("div");
  wrap.className = "w-sf-model-id";
  let curProvider = "";
  let curModel = "";
  if (value && typeof value === "string") {
    const idx = value.indexOf("/");
    if (idx >= 0) {
      curProvider = value.substring(0, idx);
      curModel = value.substring(idx + 1);
    } else {
      curModel = value;
    }
  }
  const select = document.createElement("select");
  select.className = "w-sf-model-vendor";
  const providerSet = new Set(wSettings.providers);
  if (curProvider) providerSet.add(curProvider);
  for (const p of providerSet) {
    const o = document.createElement("option");
    o.value = p; o.textContent = p;
    if (p === curProvider) o.selected = true;
    select.appendChild(o);
  }
  if (!curProvider && providerSet.size) {
    select.selectedIndex = 0;
    curProvider = select.value;
  }
  const input = document.createElement("input");
  input.type = "text";
  input.className = "w-sf-model-name";
  input.value = curModel;
  input.placeholder = "model-id";
  const compose = () => {
    const p = select.value;
    const m = input.value.trim();
    return (p && m) ? p + "/" + m : m || null;
  };
  select.addEventListener("change", () => { onChange(compose()); if (onBlur) onBlur(); });
  input.addEventListener("input", () => onChange(compose()));
  if (onBlur) input.addEventListener("blur", onBlur);
  wrap.append(select, input);
  return wrap;
}

function buildWorkerControl(field, surface, fullKey) {
  const value = fullKey in wSettings.dirty ? wSettings.dirty[fullKey] : field.stored;

  if (field.secret) {
    const wrap = document.createElement("div");
    const status = document.createElement("div");
    status.className = "w-sf-secret-status";
    status.textContent = field.is_set ? "Currently set" : "Not set";
    const input = document.createElement("input");
    input.type = "password";
    input.placeholder = "Leave blank to keep current";
    input.addEventListener("input", () => {
      if (input.value) wSettings.dirty[fullKey] = input.value;
      else delete wSettings.dirty[fullKey];
      updateWorkerSaveBar();
    });
    wrap.append(status, input);
    return wrap;
  }

  switch (field.type) {
    case "bool": {
      const wrap = document.createElement("div");
      wrap.className = "w-sf-toggle";
      const track = document.createElement("div");
      track.className = "w-sf-toggle-track" + (value ? " on" : "");
      const thumb = document.createElement("div");
      thumb.className = "w-sf-toggle-thumb";
      track.appendChild(thumb);
      const lbl = document.createElement("span");
      lbl.textContent = value ? "On" : "Off";
      track.addEventListener("click", () => {
        const nv = !track.classList.contains("on");
        track.classList.toggle("on", nv);
        lbl.textContent = nv ? "On" : "Off";
        wMarkDirty(field, fullKey, nv);
      });
      wrap.append(track, lbl);
      return wrap;
    }
    case "enum": {
      const select = document.createElement("select");
      for (const opt of (field.options || [])) {
        const o = document.createElement("option");
        o.value = opt; o.textContent = opt;
        if (opt === value) o.selected = true;
        select.appendChild(o);
      }
      select.addEventListener("change", () => wMarkDirty(field, fullKey, select.value));
      return select;
    }
    case "int":
    case "float": {
      const input = document.createElement("input");
      input.type = "number";
      input.value = value != null ? value : "";
      if (field.type === "float") input.step = "any";
      input.addEventListener("input", () => {
        const v = field.type === "int" ? parseInt(input.value, 10) : parseFloat(input.value);
        if (!isNaN(v)) wMarkDirty(field, fullKey, v);
      });
      return input;
    }
    case "string_list":
    case "int_list":
    case "regex_list": {
      if (field.key === "queues") {
        return buildQueuesTagControl(field, surface, fullKey, value);
      }
      const items = Array.isArray(value) ? [...value] : [];
      const wrap = document.createElement("div");
      wrap.className = "w-sf-chips";
      wrap.dataset.wsfKey = fullKey;
      const rerender = () => {
        wrap.innerHTML = "";
        const isModelList = field.validate === "model_id_list";
        items.forEach((item, i) => {
          const row = document.createElement("div");
          row.className = "w-sf-chip-row";
          let editor;
          if (isModelList) {
            editor = buildModelIdComposite(
              item,
              v => { items[i] = v || ""; wMarkDirty(field, fullKey, [...items]); },
              () => wOnFieldBlur(field, fullKey),
            );
          } else {
            editor = document.createElement("input");
            editor.type = field.type === "int_list" ? "number" : "text";
            editor.value = item;
            editor.addEventListener("input", () => {
              items[i] = field.type === "int_list" ? parseInt(editor.value, 10) : editor.value;
              wMarkDirty(field, fullKey, [...items]);
            });
            if (field.validate) {
              editor.addEventListener("blur", () => wOnFieldBlur(field, fullKey));
            }
          }
          const del = document.createElement("button");
          del.type = "button";
          del.className = "w-sf-chip-del";
          del.textContent = "\u00d7";
          del.addEventListener("click", () => {
            items.splice(i, 1);
            wMarkDirty(field, fullKey, [...items]);
            rerender();
          });
          row.append(editor, del);
          wrap.appendChild(row);
        });
        const addBtn = document.createElement("button");
        addBtn.type = "button";
        addBtn.className = "w-sf-chip-add";
        addBtn.textContent = "+ Add";
        addBtn.addEventListener("click", () => {
          items.push(field.type === "int_list" ? 0 : "");
          wMarkDirty(field, fullKey, [...items]);
          rerender();
        });
        wrap.appendChild(addBtn);
      };
      rerender();
      return wrap;
    }
    case "str_map": {
      const entries = Object.entries(value || {}).map(([k, v]) => ({ k, v }));
      const wrap = document.createElement("div");
      wrap.className = "w-sf-map";
      wrap.dataset.wsfKey = fullKey;
      const collectMap = () => {
        const obj = {};
        for (const e of entries) { if (e.k) obj[e.k] = e.v; }
        return obj;
      };
      const isModelMap = field.validate === "model_id_map";
      const rerender = () => {
        wrap.innerHTML = "";
        entries.forEach((entry, i) => {
          const row = document.createElement("div");
          row.className = "w-sf-map-row";
          let keyEl, valEl;
          if (isModelMap) {
            keyEl = buildModelIdComposite(
              entry.k,
              v => { entries[i].k = v || ""; wMarkDirty(field, fullKey, collectMap()); },
              () => wOnFieldBlur(field, fullKey),
            );
            valEl = buildModelIdComposite(
              entry.v,
              v => { entries[i].v = v || ""; wMarkDirty(field, fullKey, collectMap()); },
              () => wOnFieldBlur(field, fullKey),
            );
          } else {
            keyEl = document.createElement("input");
            keyEl.placeholder = "key"; keyEl.value = entry.k;
            keyEl.addEventListener("input", () => { entries[i].k = keyEl.value; wMarkDirty(field, fullKey, collectMap()); });
            if (field.validate) keyEl.addEventListener("blur", () => wOnFieldBlur(field, fullKey));
            valEl = document.createElement("input");
            valEl.placeholder = "value"; valEl.value = entry.v;
            valEl.addEventListener("input", () => { entries[i].v = valEl.value; wMarkDirty(field, fullKey, collectMap()); });
            if (field.validate) valEl.addEventListener("blur", () => wOnFieldBlur(field, fullKey));
          }
          const del = document.createElement("button");
          del.type = "button"; del.className = "w-sf-chip-del"; del.textContent = "\u00d7";
          del.addEventListener("click", () => { entries.splice(i, 1); wMarkDirty(field, fullKey, collectMap()); rerender(); });
          row.append(keyEl, valEl, del);
          wrap.appendChild(row);
        });
        const addBtn = document.createElement("button");
        addBtn.type = "button"; addBtn.className = "w-sf-chip-add"; addBtn.textContent = "+ Add";
        addBtn.addEventListener("click", () => { entries.push({ k: "", v: "" }); rerender(); });
        wrap.appendChild(addBtn);
      };
      rerender();
      return wrap;
    }
    case "readonly": {
      const el = document.createElement("code");
      el.className = "w-sf-readonly-value";
      el.textContent = value != null ? String(value) : "—";
      return el;
    }
    default: {
      if (field.validate === "model_id" || field.validate === "model_id_or_keyword") {
        const composite = buildModelIdComposite(
          value != null ? String(value) : "",
          v => wMarkDirty(field, fullKey, v),
          () => wOnFieldBlur(field, fullKey),
        );
        composite.dataset.wsfKey = fullKey;
        return composite;
      }
      const input = document.createElement("input");
      input.type = "text";
      input.value = value != null ? value : "";
      input.dataset.wsfKey = fullKey;
      input.addEventListener("input", () => wMarkDirty(field, fullKey, input.value || null));
      if (field.validate) {
        input.addEventListener("blur", () => wOnFieldBlur(field, fullKey));
      }
      return input;
    }
  }
}

function buildQueuesTagControl(field, surface, fullKey, value) {
  const items = Array.isArray(value) ? [...value] : [];
  const wrap = document.createElement("div");
  wrap.className = "w-sf-queue-tags";

  const tagContainer = document.createElement("div");
  tagContainer.className = "w-sf-queue-tag-list";
  wrap.appendChild(tagContainer);

  const actions = document.createElement("div");
  actions.className = "w-sf-queue-actions";
  const rescanBtn = document.createElement("button");
  rescanBtn.type = "button";
  rescanBtn.className = "w-sf-queue-rescan";
  rescanBtn.textContent = "\u21BB Rescan";
  actions.appendChild(rescanBtn);
  wrap.appendChild(actions);

  const renderTags = () => {
    tagContainer.innerHTML = "";
    if (items.length === 0) {
      const empty = document.createElement("span");
      empty.className = "w-sf-queue-empty";
      empty.textContent = "No queues — click Rescan to populate from workspace";
      tagContainer.appendChild(empty);
      return;
    }
    for (let i = 0; i < items.length; i++) {
      const tag = document.createElement("span");
      tag.className = "w-sf-queue-tag";
      const name = document.createElement("span");
      name.className = "w-sf-queue-tag-name";
      name.textContent = items[i];
      const del = document.createElement("button");
      del.type = "button";
      del.className = "w-sf-queue-tag-del";
      del.textContent = "\u00d7";
      del.addEventListener("click", () => {
        items.splice(i, 1);
        wMarkDirty(field, fullKey, items.length ? [...items] : null);
        renderTags();
      });
      tag.append(name, del);
      tagContainer.appendChild(tag);
    }
  };

  rescanBtn.addEventListener("click", async () => {
    rescanBtn.disabled = true;
    rescanBtn.textContent = "\u21BB Scanning\u2026";
    try {
      const resp = await fetch("/api/scan-queues");
      if (resp.ok) {
        const data = await resp.json();
        items.length = 0;
        for (const q of (data.queues || [])) items.push(q);
        wMarkDirty(field, fullKey, items.length ? [...items] : null);
        renderTags();
      }
    } finally {
      rescanBtn.disabled = false;
      rescanBtn.textContent = "\u21BB Rescan";
    }
  });

  renderTags();
  return wrap;
}

function wMarkDirty(field, fullKey, value) {
  if (JSON.stringify(value) === JSON.stringify(field.stored)) {
    delete wSettings.dirty[fullKey];
  } else {
    wSettings.dirty[fullKey] = value;
  }
  delete wSettings.errors[fullKey];
  updateWorkerSaveBar();
  const row = document.querySelector(`[data-wsf-key="${fullKey}"]`)?.closest(".w-sf-row");
  if (row) {
    row.classList.remove("w-sf-error");
    const errEl = row.querySelector(".w-sf-error-msg");
    if (errEl) errEl.remove();
  }
}

const W_MODEL_ID_KEYWORDS = new Set(["auto", "max", "default", ""]);

function wValidateModelId(value, allowKeywords) {
  if (value == null) return null;
  const v = String(value).trim();
  if (!v) return allowKeywords ? null : "model id must not be empty";
  if (W_MODEL_ID_KEYWORDS.has(v.toLowerCase())) {
    return allowKeywords ? null : `'${v}' is a keyword, not a qualified model id \u2014 use provider/model`;
  }
  if (!v.includes("/")) return `'${v}' requires a provider/ prefix (e.g. claude-code/${v})`;
  return null;
}

function wRunFieldValidation(field, fullKey, value) {
  const hint = field.validate || null;
  if (!hint) return null;
  switch (hint) {
    case "model_id":
      return (value != null) ? wValidateModelId(value, false) : null;
    case "model_id_or_keyword":
      return (value != null) ? wValidateModelId(value, true) : null;
    case "model_id_list":
      if (!Array.isArray(value)) return null;
      for (let i = 0; i < value.length; i++) {
        const err = wValidateModelId(value[i], false);
        if (err) return `item ${i + 1}: ${err}`;
      }
      return null;
    case "model_id_map":
      if (!value || typeof value !== "object") return null;
      for (const [k, v] of Object.entries(value)) {
        let err = wValidateModelId(k, false);
        if (err) return `key '${k}': ${err}`;
        err = wValidateModelId(v, false);
        if (err) return `value for '${k}': ${err}`;
      }
      return null;
  }
  return null;
}

function wShowFieldError(fullKey, msg) {
  wSettings.errors[fullKey] = msg;
  const row = document.querySelector(`[data-wsf-key="${fullKey}"]`)?.closest(".w-sf-row");
  if (row) {
    row.classList.add("w-sf-error");
    let errEl = row.querySelector(".w-sf-error-msg");
    if (!errEl) {
      errEl = document.createElement("div");
      errEl.className = "w-sf-error-msg";
      row.append(errEl);
    }
    errEl.textContent = msg;
  }
}

function wClearFieldError(fullKey) {
  delete wSettings.errors[fullKey];
  const row = document.querySelector(`[data-wsf-key="${fullKey}"]`)?.closest(".w-sf-row");
  if (row) {
    row.classList.remove("w-sf-error");
    const errEl = row.querySelector(".w-sf-error-msg");
    if (errEl) errEl.remove();
  }
}

function wOnFieldBlur(field, fullKey) {
  const value = fullKey in wSettings.dirty ? wSettings.dirty[fullKey] : field.stored;
  const err = wRunFieldValidation(field, fullKey, value);
  if (err) {
    wShowFieldError(fullKey, err);
  } else {
    wClearFieldError(fullKey);
  }
}

function wFindFieldByFullKey(fullKey) {
  const [surface, ...rest] = fullKey.split(".");
  const key = rest.join(".");
  for (const tier of wSettings.tiers) {
    if (tier.surface !== surface) continue;
    for (const cat of tier.categories) {
      const field = cat.fields.find(f => f.key === key);
      if (field) return field;
    }
  }
  return null;
}

function hasDirtyRestartField() {
  for (const fullKey of Object.keys(wSettings.dirty)) {
    const [surface, ...rest] = fullKey.split(".");
    const key = rest.join(".");
    const tier = wSettings.tiers.find(t => t.surface === surface);
    if (!tier) continue;
    for (const cat of tier.categories) {
      const field = cat.fields.find(f => f.key === key);
      if (field && field.apply === "restart") return true;
    }
  }
  return false;
}

function updateNavRestart() {
  const btn = document.getElementById("nav-restart");
  if (!btn) return;
  const show = wSettings.restartPending || hasDirtyRestartField();
  btn.hidden = !show;
  btn.classList.toggle("pending", wSettings.restartPending);
}

function updateWorkerSaveBar() {
  const bar = document.getElementById("w-settings-savebar");
  const count = Object.keys(wSettings.dirty).length;
  if (count === 0) { bar.hidden = true; updateNavRestart(); return; }
  bar.hidden = false;
  document.getElementById("w-settings-dirty-count").textContent =
    `${count} unsaved change${count === 1 ? "" : "s"}`;
  updateNavRestart();
}

async function saveWorkerSettings() {
  const delta = {};
  for (const [fullKey, value] of Object.entries(wSettings.dirty)) {
    const [surface, ...rest] = fullKey.split(".");
    const key = rest.join(".");
    if (!delta[surface]) delta[surface] = {};
    delta[surface][key] = value;
  }

  // Client-side validation before save.
  const clientErrors = {};
  for (const [fullKey, value] of Object.entries(wSettings.dirty)) {
    const field = wFindFieldByFullKey(fullKey);
    if (!field) continue;
    const err = wRunFieldValidation(field, fullKey, value);
    if (err) clientErrors[fullKey] = err;
  }
  if (Object.keys(clientErrors).length) {
    wSettings.errors = clientErrors;
    renderWorkerSettings();
    return;
  }

  const resp = await fetch("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(delta),
  });
  const data = await resp.json();
  if (!resp.ok) {
    if (data.errors) {
      wSettings.errors = data.errors;
      renderWorkerSettings();
    }
    return;
  }
  wSettings.dirty = {};
  wSettings.errors = {};
  wSettings.tiers = data.tiers || wSettings.tiers;
  if (data.restart_required && data.restart_required.length) {
    wSettings.restartPending = true;
  }
  updateWorkerSaveBar();
  renderWorkerSettings();
}

document.addEventListener("DOMContentLoaded", async () => {
  document.querySelectorAll(".nav-opt").forEach((b) => {
    b.addEventListener("click", () => {
      setView(b.dataset.view);
      if (b.dataset.view === "history") refreshHistory();
      if (b.dataset.view === "settings") loadWorkerSettings();
    });
  });
  document.getElementById("detail-back").addEventListener("click", closeDetailScreen);
  document.getElementById("btn-stats").addEventListener("click", () => {
    refreshHistory().then(openStats);
  });
  document.getElementById("stats-back").addEventListener("click", closeStats);
  document.getElementById("w-settings-discard").addEventListener("click", () => {
    wSettings.dirty = {};
    wSettings.errors = {};
    updateWorkerSaveBar();
    renderWorkerSettings();
  });
  document.getElementById("w-settings-save").addEventListener("click", saveWorkerSettings);
  const searchEl = document.getElementById("w-settings-search");
  if (searchEl) {
    searchEl.addEventListener("input", () => {
      wSettings.searchQuery = searchEl.value;
      renderWorkerSettings();
    });
  }
  await loadInfo();
  if (timer) clearTimeout(timer);
  tick();
});
