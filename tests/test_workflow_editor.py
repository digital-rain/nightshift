"""Workflow editor (spec ``docs/spec/2026-07-17-workflow-editor.md``).

Covers the definition CRUD + validate endpoints with hot reload and
last-known-good retention (§3/§3.1), the prompt CRUD endpoints (§4), prompt
custody (manager-resolved ``prompt_text`` riding the work order, worker
fallback), the startup prompt-reference check, and edit-during-flight
semantics (§6).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from _workspace import build_workspace
from nightshift._paths import asset
from nightshift.config.manager import ManagerConfig
from nightshift.manager.app import create_app
from nightshift.manager.store_sqlite import SqliteStore
from nightshift.manager.work_orders import build_work_order
from nightshift.prompts import build_doc_prompt
from nightshift.repos import DEFAULT_TASKS_REPO
from nightshift.workflows import WorkflowError, load_workflows


def _client(workspace: Path) -> TestClient:
    return TestClient(create_app(workspace, store=SqliteStore()))


def _valid_def(name: str = "custom-flow") -> dict[str, Any]:
    return {
        "name": name,
        "steps": [
            {
                "id": "plan", "kind": "doc", "role": "planner",
                "prompt": "workflow-plan.md", "inputs": ["brief"],
                "output": "plan",
            },
            {
                "id": "implement", "kind": "code", "role": "implementor",
                "inputs": ["brief", "plan"],
            },
        ],
    }


def _op_defs_dir(root: Path) -> Path:
    return root / ".nightshift" / "workflows"


def _op_prompts_dir(root: Path) -> Path:
    return root / ".nightshift" / "prompts"


# --------------------------------------------------------------------------- #
# GET /api/workflows — extended provenance payload
# --------------------------------------------------------------------------- #


def test_get_workflows_reports_shipped_provenance(tmp_path: Path) -> None:
    root = build_workspace(tmp_path, tasks={})
    with _client(root) as client:
        defs = client.get("/api/workflows").json()
        pri = defs["plan-review-implement"]
        assert pri["steps"] == ["plan", "review", "revise", "implement"]
        assert pri["source"] == "shipped"
        assert pri["shadows_shipped"] is False


# --------------------------------------------------------------------------- #
# PUT /api/workflows/{name} — save, hot reload, dispatch
# --------------------------------------------------------------------------- #


def test_put_workflow_round_trip_and_dispatch(tmp_path: Path) -> None:
    brief = "---\nworkflow: custom-flow\npriority: 1\n---\nBuild it."
    root = build_workspace(tmp_path, tasks={"10.wf": brief})
    with _client(root) as client:
        r = client.put("/api/workflows/custom-flow", json=_valid_def())
        assert r.status_code == 200
        assert r.json() == {
            "ok": True, "name": "custom-flow",
            "source": "operator", "shadows_shipped": False,
        }
        # Canonical formatting on disk: indent-2 + trailing newline, so hand
        # edits and editor edits produce identical diffs.
        path = _op_defs_dir(root) / "custom-flow.json"
        assert path.read_text() == json.dumps(_valid_def(), indent=2) + "\n"
        # Hot reload: the list reflects it without a restart …
        defs = client.get("/api/workflows").json()
        assert defs["custom-flow"]["source"] == "operator"
        assert defs["custom-flow"]["steps"] == ["plan", "implement"]
        single = client.get("/api/workflows/custom-flow").json()
        assert single["definition"] == _valid_def()
        assert single["shipped_definition"] is None
        # … and dispatch resolves the new definition immediately.
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        order = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"},
        ).json()["work"]
        assert order is not None and order["task"] == "10.wf"
        assert order["config"]["workflow"]["name"] == "custom-flow"
        assert order["config"]["workflow"]["step"] == "plan"


def test_put_invalid_definition_rejected_disk_untouched(tmp_path: Path) -> None:
    root = build_workspace(tmp_path, tasks={})
    bad = _valid_def()
    bad["steps"][0].pop("output")  # doc steps require prompt + output
    with _client(root) as client:
        r = client.put("/api/workflows/custom-flow", json=bad)
        assert r.status_code == 400
        assert "doc steps require" in r.json()["error"]
        assert not (_op_defs_dir(root) / "custom-flow.json").exists()
        assert "custom-flow" not in client.get("/api/workflows").json()


def test_put_name_path_mismatch_rejected(tmp_path: Path) -> None:
    root = build_workspace(tmp_path, tasks={})
    with _client(root) as client:
        r = client.put("/api/workflows/other-name", json=_valid_def())
        assert r.status_code == 400
        assert "must match" in r.json()["error"]
        r = client.put("/api/workflows/.hidden", json=_valid_def(".hidden"))
        assert r.status_code == 400


def test_put_unknown_prompt_rejected_until_operator_prompt_exists(
    tmp_path: Path,
) -> None:
    root = build_workspace(tmp_path, tasks={})
    custom = _valid_def()
    custom["steps"][0]["prompt"] = "my-charter.md"
    with _client(root) as client:
        r = client.put("/api/workflows/custom-flow", json=custom)
        assert r.status_code == 400
        assert "my-charter.md" in r.json()["error"]
        # The same check runs on the dry-run validate endpoint.
        v = client.post("/api/workflows/validate", json=custom).json()
        assert v["ok"] is False and "my-charter.md" in v["error"]
        # An operator prompt satisfies the reference.
        r = client.put(
            "/api/workflow-prompts/my-charter.md", json={"text": "# Charter\n"},
        )
        assert r.status_code == 200
        assert client.put("/api/workflows/custom-flow", json=custom).status_code == 200
        assert client.post("/api/workflows/validate", json=custom).json() == {"ok": True}


def test_put_with_broken_sibling_keeps_last_known_good(tmp_path: Path) -> None:
    root = build_workspace(tmp_path, tasks={})
    with _client(root) as client:
        # A different operator file goes bad on disk (hand-edited since startup).
        _op_defs_dir(root).mkdir(parents=True, exist_ok=True)
        (_op_defs_dir(root) / "broken.json").write_text("{not json")
        r = client.put("/api/workflows/custom-flow", json=_valid_def())
        assert r.status_code == 409
        assert "reload failed" in r.json()["error"]
        # The valid file was written, but the in-memory set stays last-known-good
        # — dispatch that was working a moment ago keeps working.
        assert (_op_defs_dir(root) / "custom-flow.json").exists()
        defs = client.get("/api/workflows").json()
        assert "custom-flow" not in defs
        assert "plan-review-implement" in defs


def test_validate_endpoint_never_touches_disk(tmp_path: Path) -> None:
    root = build_workspace(tmp_path, tasks={})
    with _client(root) as client:
        assert client.post(
            "/api/workflows/validate", json=_valid_def(),
        ).json() == {"ok": True}
        bad = _valid_def()
        bad["steps"][1]["next"] = "ghost"
        v = client.post("/api/workflows/validate", json=bad).json()
        assert v["ok"] is False and "ghost" in v["error"]
        assert not _op_defs_dir(root).exists()


# --------------------------------------------------------------------------- #
# DELETE /api/workflows/{name} — shadow restore, shipped refusal
# --------------------------------------------------------------------------- #


def test_delete_shadow_restores_shipped(tmp_path: Path) -> None:
    root = build_workspace(tmp_path, tasks={})
    shadow = _valid_def("plan-review-implement")
    with _client(root) as client:
        r = client.put("/api/workflows/plan-review-implement", json=shadow)
        assert r.status_code == 200 and r.json()["shadows_shipped"] is True
        defs = client.get("/api/workflows").json()
        assert defs["plan-review-implement"]["source"] == "operator"
        assert defs["plan-review-implement"]["shadows_shipped"] is True
        assert defs["plan-review-implement"]["steps"] == ["plan", "implement"]
        # The single view carries the shipped original for the diff view.
        single = client.get("/api/workflows/plan-review-implement").json()
        assert single["definition"] == shadow
        assert single["shipped_definition"]["name"] == "plan-review-implement"
        assert len(single["shipped_definition"]["steps"]) == 4
        # Deleting the shadow restores the shipped definition.
        r = client.delete("/api/workflows/plan-review-implement")
        assert r.status_code == 200 and r.json()["restored_shipped"] is True
        defs = client.get("/api/workflows").json()
        assert defs["plan-review-implement"]["source"] == "shipped"
        assert defs["plan-review-implement"]["steps"] == [
            "plan", "review", "revise", "implement",
        ]


def test_delete_shipped_only_name_refused(tmp_path: Path) -> None:
    root = build_workspace(tmp_path, tasks={})
    with _client(root) as client:
        assert client.delete("/api/workflows/plan-review-implement").status_code == 404
        assert client.delete("/api/workflows/no-such-workflow").status_code == 404


# --------------------------------------------------------------------------- #
# Prompt CRUD (§4)
# --------------------------------------------------------------------------- #


def test_prompt_list_and_shadow_round_trip(tmp_path: Path) -> None:
    root = build_workspace(tmp_path, tasks={})
    with _client(root) as client:
        prompts = client.get("/api/workflow-prompts").json()
        assert prompts["workflow-plan.md"] == {
            "source": "shipped", "shadows_shipped": False,
        }
        # Shadow a shipped prompt with an operator body.
        r = client.put(
            "/api/workflow-prompts/workflow-plan.md",
            json={"text": "# My plan charter\n"},
        )
        assert r.status_code == 200 and r.json()["shadows_shipped"] is True
        assert (_op_prompts_dir(root) / "workflow-plan.md").is_file()
        prompts = client.get("/api/workflow-prompts").json()
        assert prompts["workflow-plan.md"] == {
            "source": "operator", "shadows_shipped": True,
        }
        single = client.get("/api/workflow-prompts/workflow-plan.md").json()
        assert single["text"] == "# My plan charter\n"
        assert single["shipped_body"].strip()  # the shipped original rides along
        # Deleting the shadow restores the shipped prompt (references to a
        # still-shipped name stay satisfied, so no refusal).
        r = client.delete("/api/workflow-prompts/workflow-plan.md")
        assert r.status_code == 200 and r.json()["restored_shipped"] is True
        single = client.get("/api/workflow-prompts/workflow-plan.md").json()
        assert single["source"] == "shipped"


def test_prompt_put_rejects_empty_and_bad_names(tmp_path: Path) -> None:
    root = build_workspace(tmp_path, tasks={})
    with _client(root) as client:
        assert client.put(
            "/api/workflow-prompts/x.md", json={"text": "  "},
        ).status_code == 400
        assert client.put(
            "/api/workflow-prompts/.hidden.md", json={"text": "hi"},
        ).status_code == 400


def test_prompt_delete_refused_while_referenced(tmp_path: Path) -> None:
    root = build_workspace(tmp_path, tasks={})
    custom = _valid_def()
    custom["steps"][0]["prompt"] = "my-charter.md"
    with _client(root) as client:
        client.put("/api/workflow-prompts/my-charter.md", json={"text": "# C\n"})
        assert client.put("/api/workflows/custom-flow", json=custom).status_code == 200
        # The operator-only prompt is referenced by a loaded definition.
        r = client.delete("/api/workflow-prompts/my-charter.md")
        assert r.status_code == 409
        assert "custom-flow" in r.json()["error"]
        # Dropping the dependent definition releases the prompt.
        assert client.delete("/api/workflows/custom-flow").status_code == 200
        assert client.delete("/api/workflow-prompts/my-charter.md").status_code == 200


def test_prompt_delete_shipped_only_refused(tmp_path: Path) -> None:
    root = build_workspace(tmp_path, tasks={})
    with _client(root) as client:
        assert client.delete("/api/workflow-prompts/workflow-plan.md").status_code == 404
        assert client.delete("/api/workflow-prompts/nope.md").status_code == 404


# --------------------------------------------------------------------------- #
# Startup prompt-reference check (§3)
# --------------------------------------------------------------------------- #


def test_startup_fails_loud_on_unknown_prompt_reference(tmp_path: Path) -> None:
    root = build_workspace(tmp_path, tasks={})
    bad = _valid_def("bad-flow")
    bad["steps"][0]["prompt"] = "no-such-prompt.md"
    ddir = _op_defs_dir(root)
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "bad-flow.json").write_text(json.dumps(bad))
    with pytest.raises(WorkflowError, match="no-such-prompt.md"):
        load_workflows(root)
    # An operator prompt file satisfies the startup check too.
    pdir = _op_prompts_dir(root)
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "no-such-prompt.md").write_text("# Charter\n")
    assert "bad-flow" in load_workflows(root)


# --------------------------------------------------------------------------- #
# Prompt custody — manager-side resolution, prompt_text on the wire (§4)
# --------------------------------------------------------------------------- #


def test_work_order_embeds_prompt_text_operator_wins(tmp_path: Path) -> None:
    brief = "---\nworkflow: plan-review-implement\n---\nBuild it."
    root = build_workspace(tmp_path, tasks={"10.wf": brief})
    tasks_root = root / DEFAULT_TASKS_REPO
    defs = load_workflows(root)
    order = build_work_order(
        root, tasks_root, "10.wf", None, "longitude", "l1", "r1", "HEAD",
        ManagerConfig(), workflow_defs=defs,
    )
    # Shipped body rides the order.
    shipped_body = asset("prompts", "workflow-plan.md").read_text()
    assert order["config"]["workflow"]["prompt_text"] == shipped_body
    # An operator prompt of the same name shadows the shipped body.
    pdir = _op_prompts_dir(root)
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "workflow-plan.md").write_text("# Operator charter\n")
    order = build_work_order(
        root, tasks_root, "10.wf", None, "longitude", "l1", "r1", "HEAD",
        ManagerConfig(), workflow_defs=defs,
    )
    assert order["config"]["workflow"]["prompt_text"] == "# Operator charter\n"


def test_build_doc_prompt_prefers_embedded_text(tmp_path: Path) -> None:
    prompt = build_doc_prompt(
        "10.wf",
        prompt_asset="workflow-plan.md",
        task_file="/scratch/task.md",
        artifact_files={},
        output_file="/scratch/out.md",
        prompt_text="# Embedded operator charter\n",
    )
    assert "# Embedded operator charter" in prompt
    assert "The OUTPUT_FILE variable is: /scratch/out.md" in prompt
    # Fallback (older manager, no prompt_text): the shipped asset is read.
    fallback = build_doc_prompt(
        "10.wf",
        prompt_asset="workflow-plan.md",
        task_file="/scratch/task.md",
        artifact_files={},
        output_file="/scratch/out.md",
    )
    assert asset("prompts", "workflow-plan.md").read_text() in fallback


def test_worker_doc_step_uses_embedded_prompt_text(
    tmp_path: Path, monkeypatch,
) -> None:
    """The worker prefers the order's ``prompt_text`` — the prompt name may be
    an operator file the worker machine has never seen."""
    from nightshift import backends as backends_mod
    from nightshift.backends import WorkerResult
    from nightshift.worker.config import WorkerConfig
    from nightshift.worker.execute import execute_work_order

    workspace = build_workspace(tmp_path, tasks={"10.wf": "Do the thing."})
    seen: dict[str, str] = {}

    class _DocBackend:
        name = "claude-code"
        agentic = True
        tool_capable = True

        def available(self, config=None) -> bool:
            return True

        def run(self, spec, emit_log, should_abort, on_worker_start=None):
            seen["prompt"] = spec.prompt
            for line in spec.prompt.splitlines():
                if line.startswith("The OUTPUT_FILE variable is: "):
                    Path(line.split(": ", 1)[1]).write_text("# Doc\n")
            return WorkerResult(returncode=0, turns=1)

    monkeypatch.setattr(backends_mod, "require_backend", lambda p: _DocBackend())
    cfg = WorkerConfig(
        workspace=workspace, worker_id="w", manager_url="http://x",
        models=["claude-code/claude-sonnet-4-6"],
    )
    order = {
        "task": "10.wf", "repo": "longitude", "queue": "main",
        "body": "Do the thing.", "base_ref": "HEAD",
        "config": {
            "model": "claude-code/claude-sonnet-4-6", "validate": "",
            "workflow": {
                "name": "custom-flow", "step": "plan", "kind": "doc",
                # An operator-only prompt: no such shipped asset exists, so
                # only the embedded body can serve the run.
                "prompt": "my-charter.md", "output": "plan",
                "prompt_text": "# Operator charter body\n",
                "artifacts": {}, "signals": [],
            },
        },
    }
    outcome = execute_work_order(
        cfg, order, on_phase=lambda _p: None, on_log=lambda _l: None,
    )
    assert outcome.status.value == "completed"
    assert outcome.document == "# Doc\n"
    assert "# Operator charter body" in seen["prompt"]


# --------------------------------------------------------------------------- #
# Edit semantics for in-flight tasks (§6)
# --------------------------------------------------------------------------- #


def test_edit_removing_current_step_blocks_task(tmp_path: Path) -> None:
    """A task whose cursor sits on a step an edit removed goes blocked with the
    existing has-no-step reason; restoring the step releases it."""
    brief = "---\nworkflow: custom-flow\npriority: 1\n---\nBuild it."
    root = build_workspace(tmp_path, tasks={"10.wf": brief})
    # Pin the review step's role to a model w1 doesn't advertise so the doc
    # submit can't chain the next lease — the cursor parks on `review` with the
    # task unleased, the state an operator edits a definition in.
    qconf = root / DEFAULT_TASKS_REPO / "main" / "config.json"
    conf = json.loads(qconf.read_text())
    conf["workflow_models"] = {"reviewer": "special/model"}
    qconf.write_text(json.dumps(conf, indent=2) + "\n")
    review = {
        "id": "review", "kind": "doc", "role": "reviewer",
        "prompt": "workflow-review.md", "inputs": ["brief", "plan"],
        "output": "review",
    }
    two_doc = _valid_def()
    two_doc["steps"].insert(1, review)
    with _client(root) as client:
        assert client.put("/api/workflows/custom-flow", json=two_doc).status_code == 200
        client.post("/api/worker/checkin", json={"worker_id": "w1", "backend": "claude-code"})
        order = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"},
        ).json()["work"]
        assert order["config"]["workflow"]["step"] == "plan"
        # The doc submit advances the cursor onto `review` (no chain: w1
        # doesn't advertise the reviewer model).
        resp = client.post(
            f"/api/worker/runs/{order['run_id']}/submit",
            json={
                "worker_id": "w1", "lease_id": order["lease_id"], "task": "10.wf",
                "queue": "main", "title": "10.wf", "status": "completed",
                "landable": False, "document": "# Plan\n",
            },
        ).json()
        assert resp["workflow_step"] == "review"
        assert resp.get("next_order") is None
        # Edit the definition, renaming the step under the cursor.
        renamed = _valid_def()
        renamed["steps"].insert(1, {**review, "id": "check"})
        assert client.put("/api/workflows/custom-flow", json=renamed).status_code == 200
        # The next dispatch resolves against the edited graph: no such step.
        work = client.post(
            "/api/worker/poll", json={"worker_id": "w1", "backend": "claude-code"},
        ).json()["work"]
        assert work is None
        blocked = client.get("/api/blocked").json()
        row = next(b for b in blocked if b["task"] == "10.wf")
        assert "no step 'review'" in str(row)
        # The operator remedy: restore the step; the task resumes there (a
        # reviewer-capable worker picks it up at the recorded cursor).
        assert client.put("/api/workflows/custom-flow", json=two_doc).status_code == 200
        client.post("/api/tasks/10.wf/reset")
        client.post("/api/worker/checkin", json={
            "worker_id": "w2", "backend": "claude-code",
            "models": ["special/model"],
        })
        order = client.post(
            "/api/worker/poll", json={
                "worker_id": "w2", "backend": "claude-code",
                "models": ["special/model"],
            },
        ).json()["work"]
        assert order is not None
        assert order["config"]["workflow"]["step"] == "review"
