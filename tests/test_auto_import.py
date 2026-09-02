"""Auto-import — draining a repo's ``.tasks/<host>`` inbox on a cadence.

Three layers: the pure rules in :mod:`nightshift.auto_import` (host-queue
resolution, frontmatter normalisation, round-robin placement), the reconciler
duty end to end (a published brief becomes a queued task and leaves the repo's
``main``), and the Repos-page API surface the operator drives it from.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from _workspace import build_workspace, git, git_commit_all
from nightshift.auto_import import (
    HOST_QUEUE_KEY,
    IMPORTED_HOLD_KEY,
    PROVENANCE_KEY,
    auto_import_repos,
    blocking_imports,
    imported_brief_text,
    insert_round_robin,
    normalize_frontmatter,
    pending_imports,
    resolve_host_queue,
    set_auto_import,
)
from nightshift.manager.app import create_app
from nightshift.manager.store_sqlite import SqliteStore
from nightshift.queue_config import load_order, save_order
from nightshift.repo_tasks import list_repo_task_queues, scan_repo_inbox
from nightshift.spawn_daily import load_queue_config, split_frontmatter


TASKS_REPO = "nightshift-tasks"


def _publish(repo_root: Path, files: dict[str, str], *, message: str = "publish") -> None:
    """Commit briefs into a target repo's ``.tasks/`` inbox — what the external
    tooling that owns the host queue does."""
    for rel, content in files.items():
        dest = repo_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
    git_commit_all(repo_root, message)


def _tasks_root(ws: Path) -> Path:
    return ws / TASKS_REPO


def _reconcile(client: TestClient) -> None:
    client.portal.call(client.app.state.reconciler.reconcile_once)


def _open_throttle(client: TestClient) -> None:
    """Clear the per-queue auto-import throttle so the next reconcile checks
    the inbox again (the duty is deliberately slower than the poll loop)."""
    client.app.state.reconciler._auto_import_checked_at.clear()


def _stems(ws: Path, queue: str = "main") -> list[str]:
    return sorted(p.stem for p in (_tasks_root(ws) / queue).glob("*.md"))


# --------------------------------------------------------------------------- #
# Host-queue discovery + resolution
# --------------------------------------------------------------------------- #


def test_list_repo_task_queues_reads_subdirs_from_main(tmp_path: Path) -> None:
    ws = build_workspace(tmp_path)
    repo_root = ws / "longitude"
    _publish(repo_root, {
        ".tasks/longitude/alpha.md": "Do alpha.\n",
        ".tasks/nightly/beta.md": "Do beta.\n",
        # Flat briefs are not a host queue, and a non-slug dir is not offered
        # (its name is concatenated into an inbox path).
        ".tasks/loose.md": "Do loose.\n",
        ".tasks/Not A Slug/x.md": "skipped\n",
    })
    # Discovery reads ``main``, like the brief scan: an uncommitted subdir is
    # not published yet, and the operator checkout may be parked anywhere.
    (repo_root / ".tasks" / "draft").mkdir()
    (repo_root / ".tasks" / "draft" / "a.md").write_text("x\n")
    assert list_repo_task_queues(ws, "longitude") == ["longitude", "nightly"]


@pytest.mark.parametrize(("config", "available", "expected"), [
    # Unset: default to the subdir named after the queue, when published.
    ({}, ["longitude", "other"], "longitude"),
    ({}, ["other"], None),
    # Explicit wins over the default -- and outlives the subdir being absent,
    # exactly as a queue's ``repo`` binding outlives the repo being absent.
    ({HOST_QUEUE_KEY: "nightly"}, ["longitude"], "nightly"),
    ({HOST_QUEUE_KEY: "nightly"}, [], "nightly"),
    # Blank is an explicit "none", NOT an unset key: it opts this queue out
    # while its repo's switch stays on for the queues beside it.
    ({HOST_QUEUE_KEY: ""}, ["longitude"], None),
    # The name is concatenated into an inbox path, so it is slug-guarded.
    ({HOST_QUEUE_KEY: "../escape"}, ["longitude"], None),
])
def test_host_queue_resolution(config, available, expected) -> None:
    assert resolve_host_queue(config, "longitude", available=available) == expected


# --------------------------------------------------------------------------- #
# The repo-level switch
# --------------------------------------------------------------------------- #


def test_auto_import_switch_round_trips_through_the_store_config(tmp_path: Path) -> None:
    ws = build_workspace(tmp_path)
    root = _tasks_root(ws)
    (root / "config.json").write_text(json.dumps({"default_model": "auto"}) + "\n")
    assert auto_import_repos(root) == []
    set_auto_import(root, "longitude", True)
    set_auto_import(root, "longitude", True)     # idempotent
    assert auto_import_repos(root) == ["longitude"]
    # The store-level layer, with the settings already there left alone.
    assert json.loads((root / "config.json").read_text()) == {
        "default_model": "auto", "auto_import_repos": ["longitude"],
    }
    set_auto_import(root, "longitude", False)
    assert auto_import_repos(root) == []


def test_auto_import_repos_drops_malformed_entries(tmp_path: Path) -> None:
    ws = build_workspace(tmp_path)
    root = _tasks_root(ws)
    (root / "config.json").write_text(
        json.dumps({"auto_import_repos": ["longitude", "../escape", "/abs"]}) + "\n"
    )
    assert auto_import_repos(root) == ["longitude"]


# --------------------------------------------------------------------------- #
# Frontmatter normalisation
# --------------------------------------------------------------------------- #


def test_missing_frontmatter_gets_a_complete_one() -> None:
    text = imported_brief_text(
        "Just do the thing.\n",
        stem="do-the-thing", repo="longitude",
        source=".tasks/longitude/do-the-thing.md", default_model="auto",
    )
    meta, body = split_frontmatter(text)
    assert meta["title"] == "do-the-thing"
    assert meta["model"] == "auto"
    assert meta["priority"] == 5
    assert meta["repo"] == "longitude"
    assert meta[PROVENANCE_KEY] == "longitude/.tasks/longitude/do-the-thing.md"
    assert body.strip() == "Just do the thing."


def test_recognized_frontmatter_is_kept() -> None:
    meta = normalize_frontmatter(
        {"title": "Ship it", "model": "claude-code/claude-opus-4-8", "priority": 1,
         "repo": "atlas"},
        stem="ship", repo="longitude", default_model="auto",
    )
    assert meta == {
        "title": "Ship it", "model": "claude-code/claude-opus-4-8",
        "priority": 1, "repo": "atlas",
    }


def test_unusable_values_fall_back_to_the_default() -> None:
    """A bare model id pins the task to something no worker advertises and an
    out-of-range priority is meaningless — both would block work nobody here
    can unblock, so they take the default instead."""
    meta = normalize_frontmatter(
        {"model": "sonnet", "priority": "urgent", "repo": "../escape"},
        stem="ship", repo="longitude", default_model="claude-code/claude-sonnet-4-6",
    )
    assert meta["model"] == "claude-code/claude-sonnet-4-6"
    assert meta["priority"] == 5
    assert meta["repo"] == "longitude"


def test_keywords_are_recognized_and_unknown_keys_pass_through() -> None:
    """``max`` pins nothing concrete, so it is a usable model. Keys outside the
    resolved set are inert to dispatch, and dropping them would lose author
    intent for fields Nightshift may learn later."""
    meta = normalize_frontmatter(
        {"model": "max", "reviewer": "jim", "after": "setup"},
        stem="s", repo="longitude", default_model="auto",
    )
    assert meta["model"] == "max"
    assert meta["reviewer"] == "jim"
    assert meta["after"] == "setup"


def test_published_quarantine_is_demoted_to_a_disable() -> None:
    """Quarantine is a statement about a run *this* manager made, and an
    imported brief has none — its History would be empty and its
    ``quarantine_reason`` would point at another system's logs. Disabled says
    the true thing (arrived held, wants an operator) and, unlike quarantine,
    does not hold the round-robin slot shut."""
    meta = normalize_frontmatter(
        {"quarantined": True, "quarantine_reason": "no progress after 2 runs"},
        stem="held", repo="longitude", default_model="auto",
    )
    assert meta["quarantined"] is False
    assert meta["disabled"] is True
    # The publisher's reason survives verbatim, as provenance rather than as a
    # claim about a quarantine this manager imposed.
    assert meta[IMPORTED_HOLD_KEY] == "no progress after 2 runs"
    assert "quarantine_reason" not in meta


def test_demotion_leaves_an_unheld_brief_alone() -> None:
    """No flag keys are invented for a brief that never carried them, and an
    explicit ``quarantined: false`` is not promoted into a disable."""
    for published in ({}, {"quarantined": False}):
        meta = normalize_frontmatter(
            dict(published), stem="s", repo="longitude", default_model="auto",
        )
        assert "disabled" not in meta
        assert IMPORTED_HOLD_KEY not in meta


def test_demotion_survives_the_brief_rewrite() -> None:
    """End to end through :func:`imported_brief_text`, since that is what the
    duty actually writes to disk."""
    text = imported_brief_text(
        "---\nquarantined: true\nquarantine_reason: budget\n---\n\nFix it.\n",
        stem="held", repo="longitude",
        source=".tasks/longitude/held.md", default_model="auto",
    )
    meta = split_frontmatter(text)[0]
    assert (meta["quarantined"], meta["disabled"]) == (False, True)
    assert meta[IMPORTED_HOLD_KEY] == "budget"


# --------------------------------------------------------------------------- #
# Round-robin placement
# --------------------------------------------------------------------------- #


def _seed(ws: Path, stems: list[str]) -> Path:
    queue_dir = _tasks_root(ws) / "main"
    for stem in stems:
        (queue_dir / f"{stem}.md").write_text(f"do {stem}\n")
    save_order(_tasks_root(ws), stems, "main")
    return queue_dir


def test_import_lands_one_slot_behind_the_head(tmp_path: Path) -> None:
    """The queue's own next task keeps its place; the host's takes the slot
    after it, so dispatch alternates native / host / native / host. An empty
    queue has nothing to alternate with, so the import goes first."""
    ws = build_workspace(tmp_path)
    queue_dir = _seed(ws, ["n1", "n2", "n3"])
    (queue_dir / "h1.md").write_text("do h1\n")
    assert insert_round_robin(_tasks_root(ws), "main", "h1") \
        == ["n1", "h1", "n2", "n3"]

    empty = build_workspace(tmp_path / "empty")
    (_tasks_root(empty) / "main" / "h1.md").write_text("do h1\n")
    assert insert_round_robin(_tasks_root(empty), "main", "h1") == ["h1"]


def test_placement_skips_a_disabled_head(tmp_path: Path) -> None:
    """The head is what the queue would actually *dispatch* next, so a
    disabled brief is not the task the import alternates with."""
    ws = build_workspace(tmp_path)
    queue_dir = _seed(ws, ["n1", "n2"])
    (queue_dir / "n1.md").write_text("---\ndisabled: true\n---\n\ndo n1\n")
    (queue_dir / "h1.md").write_text("do h1\n")
    assert insert_round_robin(_tasks_root(ws), "main", "h1") == ["n1", "n2", "h1"]


def test_pending_imports_reports_only_stamped_briefs(tmp_path: Path) -> None:
    ws = build_workspace(tmp_path)
    queue_dir = _tasks_root(ws) / "main"
    (queue_dir / "native.md").write_text("---\ntitle: Native\n---\n\nwork\n")
    (queue_dir / "plain.md").write_text("no frontmatter\n")
    (queue_dir / "pulled.md").write_text(
        f"---\n{PROVENANCE_KEY}: longitude/.tasks/nightly/pulled.md\n---\n\nwork\n"
    )
    assert pending_imports(_tasks_root(ws), "main") == {
        "longitude/.tasks/nightly/pulled.md": "pulled",
    }


@pytest.mark.parametrize(("frontmatter", "blocks"), [
    ("", True),
    ("disabled: true\n", False),
    ("quarantined: true\n", False),
    ("completed: true\n", False),
    ("failed: true\n", True),          # still dispatchable (Phase B retries it)
])
def test_only_a_live_import_holds_the_round_robin_slot(
    tmp_path: Path, frontmatter: str, blocks: bool
) -> None:
    """The slot is held by a pick that can still run. One that cannot — held
    by an operator, or already finished — has had its turn; waiting on it
    would stall the inbox until somebody noticed, because those flags are
    cleared by hand and not by the run that drops the brief."""
    ws = build_workspace(tmp_path)
    (_tasks_root(ws) / "main" / "pulled.md").write_text(
        f"---\n{PROVENANCE_KEY}: longitude/.tasks/main/pulled.md\n{frontmatter}---\n\nwork\n"
    )
    pending = pending_imports(_tasks_root(ws), "main")
    assert pending == {"longitude/.tasks/main/pulled.md": "pulled"}
    assert bool(blocking_imports(_tasks_root(ws), "main", pending)) is blocks


# --------------------------------------------------------------------------- #
# The reconciler duty, end to end
# --------------------------------------------------------------------------- #


def _host_workspace(tmp_path: Path) -> Path:
    """A workspace whose ``longitude`` repo publishes a two-brief host queue,
    with the switch on and the default binding (``.tasks/main``) in play."""
    ws = build_workspace(tmp_path, tasks={"native": "do the native thing"})
    save_order(_tasks_root(ws), ["native"], "main")
    _publish(ws / "longitude", {
        ".tasks/main/alpha.md": "---\ntitle: Alpha\npriority: 2\n---\n\nDo alpha.\n",
        ".tasks/main/beta.md": "Do beta.\n",
        # Another queue's inbox: never pulled into this one.
        ".tasks/nightly/gamma.md": "Do gamma.\n",
    })
    set_auto_import(_tasks_root(ws), "longitude", True)
    return ws


@pytest.fixture
def imported(tmp_path):
    """The workspace above with the manager running. Entering the client runs
    the reconciler's startup pass, so one brief has *already* been pulled by
    the time a test body starts — every assertion below is written against
    that first pass having happened."""
    ws = _host_workspace(tmp_path)
    with TestClient(create_app(ws, store=SqliteStore())) as client:
        yield ws, client


def test_duty_pulls_one_brief_and_removes_the_source(imported) -> None:
    ws, client = imported
    _reconcile(client)
    assert _stems(ws) == ["alpha", "native"]
    body = (_tasks_root(ws) / "main" / "alpha.md").read_text()
    meta = split_frontmatter(body)[0]
    assert meta["title"] == "Alpha"
    assert meta["priority"] == 2
    assert meta["repo"] == "longitude"
    assert meta[PROVENANCE_KEY] == "longitude/.tasks/main/alpha.md"
    # The source is gone from the repo's main, so it can never run twice, and
    # the copy is committed in the content store rather than left dirty.
    tree = git(ws / "longitude", "ls-tree", "-r", "--name-only", "main")
    assert ".tasks/main/alpha.md" not in tree.splitlines()
    assert ".tasks/main/beta.md" in tree.splitlines()
    tracked = git(_tasks_root(ws), "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert "main/alpha.md" in tracked


def test_duty_takes_the_slot_behind_the_queues_own_head(imported) -> None:
    ws, client = imported
    _reconcile(client)
    assert load_order(_tasks_root(ws), "main") == ["native", "alpha"]


def test_duty_holds_the_next_brief_until_the_last_one_has_run(imported) -> None:
    """One host pick in flight at a time — otherwise a busy inbox would flood
    the queue it is supposed to take turns with."""
    ws, client = imported
    _reconcile(client)
    _open_throttle(client)
    _reconcile(client)
    assert _stems(ws) == ["alpha", "native"]
    # The pulled brief runs and its file is consumed; the host gets its turn.
    (_tasks_root(ws) / "main" / "alpha.md").unlink()
    _open_throttle(client)
    _reconcile(client)
    assert _stems(ws) == ["beta", "native"]


def test_a_held_import_does_not_stall_the_inbox(imported) -> None:
    """The regression this pair of rules exists for. A pick that stops being
    runnable used to hold the slot forever — its brief is dropped on land, and
    a held task never lands — so the host queue silently stopped draining."""
    ws, client = imported
    _reconcile(client)
    assert _stems(ws) == ["alpha", "native"]
    # The operator (or the demotion above) parks the pulled brief. It stays in
    # the queue, so it is still `pending`, but it can no longer dispatch.
    alpha = _tasks_root(ws) / "main" / "alpha.md"
    alpha.write_text("---\ndisabled: true\n" + alpha.read_text().split("---\n", 1)[1])
    _open_throttle(client)
    _reconcile(client)
    assert _stems(ws) == ["alpha", "beta", "native"]


def test_a_source_whose_removal_failed_is_not_imported_twice(imported) -> None:
    """The replay guard, on the path the liveness gate opened up: the copy
    landed but the removal did not, so the source is still published. It is
    skipped by provenance — brief text cannot catch it, because an import is
    rewritten on the way in and never matches its source verbatim."""
    ws, client = imported
    _reconcile(client)
    published = ".tasks/main/alpha.md"
    _publish(ws / "longitude", {published: "Do alpha.\n"}, message="removal failed")
    # Park the import so the slot is free and the scan actually runs.
    alpha = _tasks_root(ws) / "main" / "alpha.md"
    alpha.write_text("---\ndisabled: true\n" + alpha.read_text().split("---\n", 1)[1])
    _open_throttle(client)
    _reconcile(client)
    # `beta` is pulled past it; no second copy of alpha under a `-2` suffix.
    assert _stems(ws) == ["alpha", "beta", "native"]


def test_duty_never_drains_a_host_queue_this_queue_did_not_bind(imported) -> None:
    ws, client = imported
    for _ in range(3):   # long enough to drain `.tasks/main` dry
        for brief in (_tasks_root(ws) / "main").glob("*.md"):
            if brief.stem != "native":
                brief.unlink()
        _open_throttle(client)
        _reconcile(client)
    tree = git(ws / "longitude", "ls-tree", "-r", "--name-only", "main").splitlines()
    assert ".tasks/nightly/gamma.md" in tree


def test_duty_is_throttled_between_passes(imported) -> None:
    """Two reconciles inside one ``auto_import_seconds`` window read the inbox
    once — the second pass leaves the second brief published."""
    ws, client = imported
    _reconcile(client)
    (_tasks_root(ws) / "main" / "alpha.md").unlink()
    _reconcile(client)          # throttle still closed
    assert _stems(ws) == ["native"]


def test_switch_off_stops_the_importer(tmp_path) -> None:
    ws = build_workspace(tmp_path, tasks={"native": "do the native thing"})
    _publish(ws / "longitude", {".tasks/main/alpha.md": "Do alpha.\n"})
    with TestClient(create_app(ws, store=SqliteStore())) as client:
        _reconcile(client)
        assert _stems(ws) == ["native"]


def test_explicit_none_keeps_a_bound_queue_out_of_auto_import(imported) -> None:
    ws, client = imported
    (_tasks_root(ws) / "main" / "alpha.md").unlink()
    client.put("/api/queue/host-queue", json={"queue": "main", "host_queue": ""})
    _open_throttle(client)
    _reconcile(client)
    assert _stems(ws) == ["native"]


def test_a_brief_already_in_the_queue_is_not_copied_twice(imported) -> None:
    """The operator drained this one by hand earlier: the source is still
    removed so the inbox converges, but no second copy is written."""
    ws, client = imported
    (_tasks_root(ws) / "main" / "alpha.md").write_text(
        "---\ntitle: Alpha\npriority: 2\n---\n\nDo alpha.\n"
    )
    _reconcile(client)
    assert _stems(ws) == ["alpha", "native"]
    tree = git(ws / "longitude", "ls-tree", "-r", "--name-only", "main").splitlines()
    assert ".tasks/main/alpha.md" not in tree


def test_pulled_brief_is_dispatchable_like_any_other_task(imported) -> None:
    """It counts as an ordinary task from here — same poll, same attempt row,
    so it lands in History and the stats with everything else."""
    ws, client = imported
    client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
    seen = set()
    for _ in range(2):
        work = client.post(
            "/api/worker/poll",
            json={"worker_id": "w1", "backend": "claude-code", "models": ["auto"]},
        ).json()["work"]
        if work:
            seen.add(work["task"])
    # Native first (it kept the head), then the pulled brief -- the round-robin
    # the placement encodes, observed through the real dispatch path.
    assert list(seen) and seen == {"native", "alpha"}


# --------------------------------------------------------------------------- #
# The Repos-page API surface
# --------------------------------------------------------------------------- #


@pytest.fixture
def api(tmp_path):
    ws = build_workspace(tmp_path, tasks={"native": "work"})
    _publish(ws / "longitude", {
        ".tasks/main/alpha.md": "Do alpha.\n",
        ".tasks/nightly/beta.md": "Do beta.\n",
    })
    with TestClient(create_app(ws, store=SqliteStore())) as client:
        yield ws, client


def _repo_row(payload: dict, name: str) -> dict:
    return next(r for r in payload["repos"] if r["name"] == name)


def _queue_row(payload: dict, label: str) -> dict:
    return next(q for q in payload["queues"] if q["queue"] == label)


def test_repos_payload_reports_the_switch_off_by_default(api) -> None:
    _, client = api
    payload = client.get("/api/repos").json()
    row = _repo_row(payload, "longitude")
    assert row["auto_import"] is False
    # Nothing is read from a switched-off repo, so it offers no host queues
    # and its bound queues show no binding to make.
    assert row["task_queues"] == []
    assert _queue_row(payload, "main")["auto_import"] is False
    assert _queue_row(payload, "main")["host_queue"] is None


def test_enabling_the_switch_surfaces_the_host_queues(api) -> None:
    _, client = api
    payload = client.put(
        "/api/repos/auto-import", json={"repo": "longitude", "enabled": True}
    ).json()
    assert _repo_row(payload, "longitude")["auto_import"] is True
    assert _repo_row(payload, "longitude")["task_queues"] == ["main", "nightly"]
    queue = _queue_row(payload, "main")
    assert queue["host_queues"] == ["main", "nightly"]
    # The default binding shows pre-selected without the operator saving it.
    assert queue["host_queue"] == "main"


def test_binding_a_host_queue_persists_into_the_queue_config(api) -> None:
    ws, client = api
    client.put("/api/repos/auto-import", json={"repo": "longitude", "enabled": True})
    payload = client.put(
        "/api/queue/host-queue", json={"queue": "main", "host_queue": "nightly"}
    ).json()
    assert _queue_row(payload, "main")["host_queue"] == "nightly"
    assert load_queue_config(_tasks_root(ws), "main")[HOST_QUEUE_KEY] == "nightly"


def test_malformed_names_are_rejected_where_they_are_authored(api) -> None:
    """Both values are concatenated into a path, so neither reaches the disk."""
    _, client = api
    host = client.put(
        "/api/queue/host-queue", json={"queue": "main", "host_queue": "../escape"}
    )
    assert host.status_code == 400
    assert "invalid host queue" in host.json()["error"]
    repo = client.put(
        "/api/repos/auto-import", json={"repo": "../escape", "enabled": True}
    )
    assert repo.status_code == 400


def test_host_queue_binding_for_an_unknown_queue_is_404(api) -> None:
    _, client = api
    resp = client.put(
        "/api/queue/host-queue", json={"queue": "ghost", "host_queue": "nightly"}
    )
    assert resp.status_code == 404


def test_switching_off_keeps_the_binding_for_next_time(api) -> None:
    ws, client = api
    client.put("/api/repos/auto-import", json={"repo": "longitude", "enabled": True})
    client.put("/api/queue/host-queue", json={"queue": "main", "host_queue": "nightly"})
    client.put("/api/repos/auto-import", json={"repo": "longitude", "enabled": False})
    assert load_queue_config(_tasks_root(ws), "main")[HOST_QUEUE_KEY] == "nightly"
    payload = client.put(
        "/api/repos/auto-import", json={"repo": "longitude", "enabled": True}
    ).json()
    assert _queue_row(payload, "main")["host_queue"] == "nightly"


def test_scan_repo_inbox_reads_only_the_named_host_queue(tmp_path: Path) -> None:
    ws = build_workspace(tmp_path)
    _publish(ws / "longitude", {
        ".tasks/nightly/alpha.md": "Do alpha.\n",
        ".tasks/main/beta.md": "Do beta.\n",
        ".tasks/loose.md": "Do loose.\n",
    })
    entries = scan_repo_inbox(
        ws, "longitude", ".tasks/nightly", _tasks_root(ws), "main"
    )
    assert [e.source for e in entries] == [".tasks/nightly/alpha.md"]


def test_partial_removal_failure_does_not_re_import(tmp_path, monkeypatch) -> None:
    """The copy is durable before the removal runs. When the removal fails the
    source is still published — but the pulled brief is still in the queue, so
    the round-robin gate keeps the next pass from running the same work twice.
    """
    ws = _host_workspace(tmp_path)
    monkeypatch.setattr(
        "nightshift.manager.reconciler.remove_repo_tasks_locked", _failed_removal
    )
    with TestClient(create_app(ws, store=SqliteStore())) as client:
        assert _stems(ws) == ["alpha", "native"]
        tree = git(
            ws / "longitude", "ls-tree", "-r", "--name-only", "main"
        ).splitlines()
        assert ".tasks/main/alpha.md" in tree
        _open_throttle(client)
        _reconcile(client)
        assert _stems(ws) == ["alpha", "native"]


def _failed_removal(*args, **kwargs) -> dict:
    return {"removed": False, "warning": "removal failed"}
