// Manager SSE convergence — the page's single EventSource. The manager's
// /api/events stream is snapshot-on-connect then a live delta stream
// ({type:"snapshot",...} / {type:"event", kind,...}). This client keeps every
// open browser converged: structural and lifecycle deltas (queue reorder,
// settings, worker/lease/run changes) trigger a refetch of the affected
// surfaces so two operators watching the same manager see the same thing
// without a manual reload. task_log frames stream to app.js's live Now-view
// tail (applyTaskLog) without a refetch.
//
// We lean on app.js's debounced scheduleRefresh() global where present
// (queue / runs / now), and always repaint the manager-only Workers page.

(() => {
  "use strict";

  const REFRESH_KINDS_APP = new Set([
    "queue_changed",
    "task_started",
    "task_status",
    "task_result",
    "run_started",
    "run_finished",
    "settings_changed",
  ]);

  const REFRESH_KINDS_WORKERS = new Set([
    "queue_changed",
    "worker_registered",
    "worker_status",
    "lease_acquired",
    "lease_released",
    "task_blocked",
    "run_started",
    "run_finished",
    "worker_started",
  ]);

  function setConn(live) {
    const pill = document.getElementById("workers-conn");
    if (!pill) return;
    pill.textContent = live ? "live" : "reconnecting…";
    pill.classList.toggle("stale", !live);
  }

  function refreshApp() {
    if (typeof window.scheduleRefresh === "function") window.scheduleRefresh();
  }

  function refreshWorkers() {
    if (typeof window.refreshWorkers === "function") window.refreshWorkers();
  }

  function onFrame(frame) {
    if (frame.type === "snapshot") {
      // A fresh snapshot means we (re)synced: repaint everything.
      refreshWorkers();
      refreshApp();
      return;
    }
    if (frame.type !== "event") return;
    const kind = frame.kind || "";
    if (kind === "task_log" && typeof window.applyTaskLog === "function") {
      // Live log tail: append + repaint directly, no refetch.
      window.applyTaskLog(frame);
      return;
    }
    if (kind === "workflows_changed" && typeof window.onWorkflowsChanged === "function") {
      // Definition/prompt library changed (editor save/delete, any browser):
      // refetch the workflow lists so pickers, badges, and the library view
      // converge without a reload.
      window.onWorkflowsChanged();
    }
    if (REFRESH_KINDS_WORKERS.has(kind)) refreshWorkers();
    if (REFRESH_KINDS_APP.has(kind)) refreshApp();
  }

  function connect() {
    const es = new EventSource("/api/events");
    es.onopen = () => setConn(true);
    es.onmessage = (msg) => {
      let frame;
      try {
        frame = JSON.parse(msg.data);
      } catch {
        return;
      }
      onFrame(frame);
    };
    es.onerror = () => {
      // EventSource auto-reconnects; reflect the gap in the UI meanwhile.
      setConn(false);
    };
  }

  connect();
})();
