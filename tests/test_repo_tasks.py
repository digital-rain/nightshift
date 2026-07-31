"""Repo task import — draining a target repo's publishing inboxes.

Scan-rule units on :mod:`nightshift.repo_tasks` (both inbox roots, ``.tasks/``
and ``docs/tasks/``) plus the operator endpoints
(``/api/queue/repo-tasks*``) end to end: briefs move into the content store,
the sources are removed from the repo's ``main`` through the landing pipeline,
and the never-lose paths (push failure, identical re-publish) converge instead
of duplicating. See ``docs/spec/2026-07-04-repo-task-import.md``.
"""

from __future__ import annotations

import json
from pathlib import Path

from starlette.testclient import TestClient

from _workspace import (
    add_remote,
    build_workspace,
    git,
    git_commit_all,
    make_bare_remote,
)
from nightshift.manager.app import create_app
from nightshift.manager.store_sqlite import SqliteStore
from nightshift.repo_tasks import (
    RepoTask,
    copy_repo_tasks,
    scan_repo_tasks,
    select_repo_tasks,
)


def _publish(repo_root: Path, files: dict[str, str], *, message: str = "publish tasks") -> None:
    """Commit ``files`` into a target repo — what external tooling does when it
    publishes briefs into the ``.tasks/`` inbox."""
    for rel, content in files.items():
        dest = repo_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
    git_commit_all(repo_root, message)


def _client(workspace: Path) -> TestClient:
    return TestClient(create_app(workspace, store=SqliteStore()))


# --------------------------------------------------------------------------- #
# Scan rules
# --------------------------------------------------------------------------- #


def test_scan_rules_and_order(tmp_path: Path) -> None:
    ws = build_workspace(tmp_path)
    repo_root = ws / "longitude"
    _publish(repo_root, {
        ".tasks/alpha.md": "Do alpha.\n",
        ".tasks/beta.md": (
            "---\ntitle: Beta task\npriority: 2\ndisabled: true\n---\n\nDo beta.\n"
        ),
        # Skipped: templates/inboxes (leading _ or .), recurring autosplit
        # sources, non-md files, config.json itself.
        ".tasks/_todo.md": "---\nevergreen: true\nautosplit: true\n---\nitems\n",
        ".tasks/.hidden.md": "not a brief\n",
        ".tasks/recurring.md": "---\nautosplit: true\n---\nitems\n",
        ".tasks/notes.txt": "not a brief\n",
        ".tasks/config.json": json.dumps({"order": ["beta", "alpha"]}) + "\n",
        # Queue-dir layout: only the subdir matching the queue's label counts.
        ".tasks/main/gamma.md": "Do gamma.\n",
        ".tasks/other/delta.md": "Belongs to another queue.\n",
    })
    # Published = committed on main: an uncommitted working-tree file is not
    # part of the inbox yet.
    (repo_root / ".tasks" / "uncommitted.md").write_text("Not published.\n")
    entries = scan_repo_tasks(ws, "longitude", "main", ws / "nightshift-tasks", "main")
    # Root files first in their published order, then the queue subdir's.
    assert [e.name for e in entries] == ["beta", "alpha", "gamma"]
    beta = entries[0]
    assert beta.title == "Beta task"
    assert beta.priority == 2
    assert beta.disabled is True
    assert beta.duplicate is False
    assert beta.source == ".tasks/beta.md"
    assert entries[2].source == ".tasks/main/gamma.md"


def test_scan_reads_the_docs_tasks_inbox(tmp_path: Path) -> None:
    """``docs/tasks/`` is the second inbox root: markdown briefs with their
    metadata in frontmatter and no json control file, so they publish in
    filename order (a stray ``config.json`` there orders nothing)."""
    ws = build_workspace(tmp_path)
    _publish(ws / "longitude", {
        "docs/tasks/zeta.md": "---\ntitle: Zeta task\npriority: 1\n---\n\nDo zeta.\n",
        "docs/tasks/alpha.md": "Do alpha.\n",
        # Same skip rules as the legacy inbox.
        "docs/tasks/_todo.md": "---\nautosplit: true\n---\nitems\n",
        "docs/tasks/recurring.md": "---\nautosplit: true\n---\nitems\n",
        "docs/tasks/notes.txt": "not a brief\n",
        "docs/tasks/config.json": json.dumps({"order": ["zeta", "alpha"]}) + "\n",
        # Queue-dir layout under the new root too: only the queue's own subdir.
        "docs/tasks/main/gamma.md": "Do gamma.\n",
        "docs/tasks/other/delta.md": "Belongs to another queue.\n",
        # Neighbouring docs are not an inbox.
        "docs/specs/some-spec.md": "A spec, not a brief.\n",
    })
    entries = scan_repo_tasks(ws, "longitude", "main", ws / "nightshift-tasks", "main")
    assert [e.name for e in entries] == ["alpha", "zeta", "gamma"]
    assert [e.source for e in entries] == [
        "docs/tasks/alpha.md", "docs/tasks/zeta.md", "docs/tasks/main/gamma.md",
    ]
    zeta = entries[1]
    assert zeta.title == "Zeta task"
    assert zeta.priority == 1


def test_scan_reads_both_inbox_roots(tmp_path: Path) -> None:
    """A repo publishing into both roots offers both, the legacy inbox first."""
    ws = build_workspace(tmp_path)
    _publish(ws / "longitude", {
        ".tasks/legacy.md": "Do legacy.\n",
        "docs/tasks/fresh.md": "Do fresh.\n",
    })
    entries = scan_repo_tasks(ws, "longitude", "main", ws / "nightshift-tasks", "main")
    assert [e.source for e in entries] == [".tasks/legacy.md", "docs/tasks/fresh.md"]


def test_scan_flags_duplicates(tmp_path: Path) -> None:
    ws = build_workspace(tmp_path, tasks={"alpha": "Do alpha.\n"})
    _publish(ws / "longitude", {
        ".tasks/alpha.md": "Do alpha.\n",   # byte-identical to the queue brief
        ".tasks/fresh.md": "Something new.\n",
    })
    entries = scan_repo_tasks(ws, "longitude", "main", ws / "nightshift-tasks", "main")
    assert {e.name: e.duplicate for e in entries} == {"alpha": True, "fresh": False}


def test_scan_without_inbox_is_empty(tmp_path: Path) -> None:
    ws = build_workspace(tmp_path)
    assert scan_repo_tasks(ws, "longitude", "main", ws / "nightshift-tasks", "main") == []


def test_copy_suffixes_collisions_and_appends_order(tmp_path: Path) -> None:
    ws = build_workspace(tmp_path, tasks={"alpha": "Existing different brief.\n"})
    tasks_root = ws / "nightshift-tasks"

    def entry(name: str, text: str, *, duplicate: bool = False) -> RepoTask:
        return RepoTask(
            name=name, title=name, source=f".tasks/{name}.md", priority=5,
            disabled=False, duplicate=duplicate, text=text,
        )

    imported = copy_repo_tasks(tasks_root, "main", [
        entry("alpha", "Published alpha.\n"),      # collides with different content
        entry("fresh", "New.\n"),
        entry("dup", "whatever\n", duplicate=True),  # duplicates are not copied
    ])
    assert [t["task"] for t in imported] == ["alpha-2", "fresh"]
    assert (tasks_root / "main" / "alpha-2.md").read_text() == "Published alpha.\n"
    assert (tasks_root / "main" / "alpha.md").read_text() == "Existing different brief.\n"
    assert not (tasks_root / "main" / "dup.md").exists()
    order = json.loads((tasks_root / "main" / "config.json").read_text())["order"]
    assert order[-2:] == ["alpha-2", "fresh"]


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #


def _entry(name: str, source: str) -> RepoTask:
    return RepoTask(
        name=name, title=name, source=source, priority=5,
        disabled=False, duplicate=False, text=f"{name}\n",
    )


def test_select_narrows_to_the_picked_sources_in_scan_order() -> None:
    entries = [_entry("a", ".tasks/a.md"), _entry("b", ".tasks/b.md"),
               _entry("c", ".tasks/c.md")]
    # Request order is irrelevant: the scan's published order is what runs.
    picked, missing = select_repo_tasks(entries, [".tasks/c.md", ".tasks/a.md"])
    assert [e.name for e in picked] == ["a", "c"]
    assert missing == []
    # No selection = the whole set; an empty selection = nothing.
    assert select_repo_tasks(entries, None) == (entries, [])
    assert select_repo_tasks(entries, []) == ([], [])


def test_select_keys_on_source_not_task_name() -> None:
    """The same stem published both flat and under the queue's subdir is two
    distinct briefs — picking one must not drag the other along."""
    entries = [_entry("dup", ".tasks/dup.md"), _entry("dup", ".tasks/main/dup.md")]
    picked, missing = select_repo_tasks(entries, [".tasks/main/dup.md"])
    assert [e.source for e in picked] == [".tasks/main/dup.md"]
    assert missing == []


def test_select_reports_sources_the_scan_no_longer_offers() -> None:
    picked, missing = select_repo_tasks(
        [_entry("a", ".tasks/a.md")], [".tasks/a.md", ".tasks/gone.md"]
    )
    assert [e.name for e in picked] == ["a"]
    assert missing == [".tasks/gone.md"]


# --------------------------------------------------------------------------- #
# Operator endpoints, end to end
# --------------------------------------------------------------------------- #


def test_import_moves_briefs_into_queue_and_off_main(tmp_path: Path) -> None:
    ws = build_workspace(tmp_path)
    repo_root = ws / "longitude"
    tasks_root = ws / "nightshift-tasks"
    _publish(repo_root, {
        ".tasks/alpha.md": "Do alpha.\n",
        ".tasks/main/beta.md": "Do beta.\n",
    })
    with _client(ws) as client:
        preview = client.get("/api/queue/repo-tasks").json()
        assert preview["available"] is True
        assert preview["repo"] == "longitude"
        assert [t["task"] for t in preview["tasks"]] == ["alpha", "beta"]

        r = client.post("/api/queue/repo-tasks/import")
        assert r.status_code == 200
        data = r.json()
        assert [t["task"] for t in data["imported"]] == ["alpha", "beta"]
        assert data["deduped"] == []
        assert data["removed"] is True
        assert data["warning"] is None

        # Durable half: briefs canonical in the content store, order appended,
        # store committed.
        assert (tasks_root / "main" / "alpha.md").read_text() == "Do alpha.\n"
        assert (tasks_root / "main" / "beta.md").read_text() == "Do beta.\n"
        order = json.loads((tasks_root / "main" / "config.json").read_text())["order"]
        assert order[-2:] == ["alpha", "beta"]
        assert "import 2 task(s)" in git(tasks_root, "log", "-1", "--format=%s")

        # Cleanup half: sources removed from the repo's main in one manager
        # commit; the clean checkout advanced with it.
        assert git(repo_root, "log", "-1", "--format=%s") == (
            "nightshift: import 2 task(s) into queue main"
        )
        tree = git(repo_root, "ls-tree", "-r", "--name-only", "main")
        assert ".tasks/alpha.md" not in tree
        assert ".tasks/main/beta.md" not in tree
        assert not (repo_root / ".tasks" / "alpha.md").exists()

        # The queue serves the imported briefs; the inbox is drained.
        assert {t["task"] for t in client.get("/api/queue").json()} >= {"alpha", "beta"}
        assert client.get("/api/queue/repo-tasks").json()["count"] == 0


def test_import_moves_docs_tasks_briefs(tmp_path: Path) -> None:
    """The same move for the ``docs/tasks/`` root: briefs land in the content
    store and are removed from the repo's ``main``, so they never run twice."""
    ws = build_workspace(tmp_path)
    repo_root = ws / "longitude"
    tasks_root = ws / "nightshift-tasks"
    _publish(repo_root, {
        "docs/tasks/alpha.md": "---\ntitle: Alpha task\n---\n\nDo alpha.\n",
        "docs/tasks/main/beta.md": "Do beta.\n",
        "docs/specs/some-spec.md": "A spec, not a brief.\n",
    })
    with _client(ws) as client:
        preview = client.get("/api/queue/repo-tasks").json()
        assert [t["source"] for t in preview["tasks"]] == [
            "docs/tasks/alpha.md", "docs/tasks/main/beta.md",
        ]
        data = client.post("/api/queue/repo-tasks/import").json()
        assert [t["task"] for t in data["imported"]] == ["alpha", "beta"]
        assert data["removed"] is True
        assert data["warning"] is None
        assert client.get("/api/queue/repo-tasks").json()["count"] == 0

    assert (tasks_root / "main" / "alpha.md").read_text().endswith("Do alpha.\n")
    order = json.loads((tasks_root / "main" / "config.json").read_text())["order"]
    assert order[-2:] == ["alpha", "beta"]
    tree = git(repo_root, "ls-tree", "-r", "--name-only", "main")
    assert "docs/tasks/alpha.md" not in tree
    assert "docs/tasks/main/beta.md" not in tree
    # Neighbouring docs are untouched by the removal commit.
    assert "docs/specs/some-spec.md" in tree


def test_import_drains_only_the_selected_briefs(tmp_path: Path) -> None:
    """Per-task selection: the operator picks which briefs move. Unpicked ones
    are neither copied into the queue nor removed from the repo, so the next
    preview offers them again."""
    ws = build_workspace(tmp_path)
    repo_root = ws / "longitude"
    tasks_root = ws / "nightshift-tasks"
    _publish(repo_root, {
        ".tasks/alpha.md": "Do alpha.\n",
        ".tasks/beta.md": "Do beta.\n",
        ".tasks/main/gamma.md": "Do gamma.\n",
    })
    with _client(ws) as client:
        data = client.post(
            "/api/queue/repo-tasks/import",
            json={"sources": [".tasks/beta.md", ".tasks/main/gamma.md"]},
        ).json()
        assert [t["task"] for t in data["imported"]] == ["beta", "gamma"]
        assert data["missing"] == []
        assert data["removed"] is True

        # The unpicked brief is still published — and still on offer.
        preview = client.get("/api/queue/repo-tasks").json()
        assert [t["task"] for t in preview["tasks"]] == ["alpha"]

        # ...and importable on a second pass, appended after the first batch.
        second = client.post(
            "/api/queue/repo-tasks/import", json={"sources": [".tasks/alpha.md"]}
        ).json()
        assert [t["task"] for t in second["imported"]] == ["alpha"]
        assert client.get("/api/queue/repo-tasks").json()["count"] == 0

    assert (tasks_root / "main" / "alpha.md").exists()
    order = json.loads((tasks_root / "main" / "config.json").read_text())["order"]
    assert order[-3:] == ["beta", "gamma", "alpha"]
    tree = git(repo_root, "ls-tree", "-r", "--name-only", "main")
    assert ".tasks/beta.md" not in tree
    assert ".tasks/main/gamma.md" not in tree
    assert ".tasks/alpha.md" not in tree


def test_import_of_nothing_selected_is_a_no_op(tmp_path: Path) -> None:
    ws = build_workspace(tmp_path)
    repo_root = ws / "longitude"
    _publish(repo_root, {".tasks/alpha.md": "Do alpha.\n"})
    head = git(repo_root, "rev-parse", "main")
    with _client(ws) as client:
        data = client.post(
            "/api/queue/repo-tasks/import", json={"sources": []}
        ).json()
        assert data == {
            "imported": [], "deduped": [], "removed": False,
            "warning": None, "missing": [],
        }
        assert client.get("/api/queue/repo-tasks").json()["count"] == 1
    # Nothing picked, nothing touched: no removal commit on the repo's main.
    assert git(repo_root, "rev-parse", "main") == head
    assert not (ws / "nightshift-tasks" / "main" / "alpha.md").exists()


def test_import_of_a_stale_selection_imports_the_rest(tmp_path: Path) -> None:
    """A brief the operator picked from a preview that has since gone stale is
    reported as ``missing`` instead of failing the whole batch."""
    ws = build_workspace(tmp_path)
    _publish(ws / "longitude", {".tasks/alpha.md": "Do alpha.\n"})
    with _client(ws) as client:
        data = client.post(
            "/api/queue/repo-tasks/import",
            json={"sources": [".tasks/alpha.md", ".tasks/vanished.md"]},
        ).json()
        assert [t["task"] for t in data["imported"]] == ["alpha"]
        assert data["missing"] == [".tasks/vanished.md"]
        assert data["removed"] is True


def test_import_without_a_body_drains_everything(tmp_path: Path) -> None:
    """No selection = the whole scanned set (the pre-selection API contract)."""
    ws = build_workspace(tmp_path)
    _publish(ws / "longitude", {
        ".tasks/alpha.md": "Do alpha.\n",
        ".tasks/beta.md": "Do beta.\n",
    })
    with _client(ws) as client:
        data = client.post("/api/queue/repo-tasks/import").json()
        assert [t["task"] for t in data["imported"]] == ["alpha", "beta"]
        data = client.post("/api/queue/repo-tasks/import", json={}).json()
        assert data["imported"] == []


def test_import_reads_and_drains_main_not_the_checkout(tmp_path: Path) -> None:
    """The inbox is the ``main`` *tree*: a checkout parked on a feature branch
    neither hides main's briefs nor re-offers drained ones (the operator's
    on-disk copy of the file is irrelevant to the preview)."""
    ws = build_workspace(tmp_path)
    repo_root = ws / "longitude"
    _publish(repo_root, {".tasks/alpha.md": "Do alpha.\n"})
    git(repo_root, "checkout", "-b", "feature")
    with _client(ws) as client:
        preview = client.get("/api/queue/repo-tasks").json()
        assert [t["task"] for t in preview["tasks"]] == ["alpha"]

        data = client.post("/api/queue/repo-tasks/import").json()
        assert [t["task"] for t in data["imported"]] == ["alpha"]
        assert data["removed"] is True

        # Drained from main -> gone from the preview, even though the feature
        # checkout still carries the file on disk.
        assert client.get("/api/queue/repo-tasks").json()["count"] == 0
    assert (repo_root / ".tasks" / "alpha.md").exists()
    assert ".tasks/alpha.md" not in git(repo_root, "ls-tree", "-r", "--name-only", "main")
    assert git(repo_root, "branch", "--show-current") == "feature"


def test_import_scopes_to_the_queues_subdir(tmp_path: Path) -> None:
    ws = build_workspace(
        tmp_path,
        queues={"web": {"tasks": {}, "config": {"repo": "longitude", "order": []}}},
    )
    repo_root = ws / "longitude"
    _publish(repo_root, {
        ".tasks/web/x.md": "X.\n",
        ".tasks/other/y.md": "Y.\n",
    })
    with _client(ws) as client:
        preview = client.get("/api/queue/repo-tasks?queue=web").json()
        assert [t["task"] for t in preview["tasks"]] == ["x"]
        data = client.post("/api/queue/repo-tasks/import?queue=web").json()
        assert [t["task"] for t in data["imported"]] == ["x"]
    # The other queue's inbox dir is untouched.
    tree = git(repo_root, "ls-tree", "-r", "--name-only", "main")
    assert ".tasks/other/y.md" in tree
    assert ".tasks/web/x.md" not in tree
    assert (ws / "nightshift-tasks" / "web" / "x.md").exists()


def test_import_pushes_removal_to_origin(tmp_path: Path) -> None:
    ws = build_workspace(tmp_path / "ws")
    repo_root = ws / "longitude"
    origin = make_bare_remote(tmp_path / "remotes" / "longitude.git")
    add_remote(repo_root, "origin", origin)
    _publish(repo_root, {".tasks/alpha.md": "Do alpha.\n"})
    git(repo_root, "push", "origin", "main")
    with _client(ws) as client:
        data = client.post("/api/queue/repo-tasks/import").json()
        assert data["removed"] is True
        assert data["warning"] is None
    # The removal commit reached origin — it can't be lost to a later sync.
    assert git(repo_root, "rev-parse", "main") == git(origin, "rev-parse", "main")


def test_import_survives_push_failure(tmp_path: Path) -> None:
    ws = build_workspace(tmp_path)
    repo_root = ws / "longitude"
    git(repo_root, "remote", "add", "origin", str(tmp_path / "missing.git"))
    _publish(repo_root, {".tasks/alpha.md": "Do alpha.\n"})
    with _client(ws) as client:
        data = client.post("/api/queue/repo-tasks/import").json()
        assert [t["task"] for t in data["imported"]] == ["alpha"]
        # Local removal commit kept; the failed push is a warning, never an
        # unwind — the brief is already durable in the content store.
        assert data["removed"] is True
        assert data["warning"] is not None and "push" in data["warning"]
    assert not (repo_root / ".tasks" / "alpha.md").exists()
    assert (ws / "nightshift-tasks" / "main" / "alpha.md").exists()


def test_republished_identical_brief_dedupes(tmp_path: Path) -> None:
    ws = build_workspace(tmp_path)
    repo_root = ws / "longitude"
    _publish(repo_root, {".tasks/alpha.md": "Do alpha.\n"})
    with _client(ws) as client:
        client.post("/api/queue/repo-tasks/import")
        # Tooling re-publishes the identical brief (or a removal was lost):
        # the replay removes it again without writing a second copy.
        _publish(repo_root, {".tasks/alpha.md": "Do alpha.\n"}, message="republish")
        data = client.post("/api/queue/repo-tasks/import").json()
        assert data["imported"] == []
        assert data["deduped"] == ["alpha"]
        assert data["removed"] is True
    assert not (repo_root / ".tasks" / "alpha.md").exists()
    briefs = sorted(p.name for p in (ws / "nightshift-tasks" / "main").glob("*.md"))
    assert briefs == ["alpha.md"]


def test_import_is_inert_without_a_repo(tmp_path: Path) -> None:
    ws = build_workspace(tmp_path, main_repo=None)
    with _client(ws) as client:
        preview = client.get("/api/queue/repo-tasks").json()
        assert preview["available"] is False
        assert preview["tasks"] == []
        assert client.post("/api/queue/repo-tasks/import").status_code == 409
        assert client.get("/api/queue/repo-tasks?queue=nope").status_code == 404
        assert client.post("/api/queue/repo-tasks/import?queue=nope").status_code == 404
