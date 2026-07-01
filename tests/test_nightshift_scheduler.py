"""Tests for the manager's cross-queue, capability-based arbitration."""

from __future__ import annotations

from nightshift.manager.scheduler import (
    SchedulerState,
    TaskCandidate,
    WorkerFilter,
    _normalize_model,
    parse_required_mcps,
    pick_next,
    queue_head,
    unroutable,
)


def _cand(
    task, *, queue=None, priority=5, model="auto", required_mcps=(), after=()
) -> TaskCandidate:
    if isinstance(after, str):
        after = (after,)
    return TaskCandidate(
        queue=queue,
        task=task,
        priority=priority,
        model=model,
        required_mcps=tuple(required_mcps),
        after=tuple(after),
    )


def test_model_policy_unset_resolves_to_default() -> None:
    # The model-policy migration strips `model:` from briefs so they inherit the
    # manager default (`auto`). An absent or blank model must fall through to it;
    # an explicit pin is preserved verbatim.
    assert _normalize_model({}, "auto") == "auto"
    assert _normalize_model({"model": ""}, "auto") == "auto"
    assert _normalize_model({"model": "   "}, "auto") == "auto"
    assert _normalize_model({"model": "max"}, "auto") == "max"
    assert _normalize_model({"model": "claude-opus-4-8"}, "auto") == "claude-opus-4-8"


def test_parse_required_mcps_forms() -> None:
    assert parse_required_mcps({}) == ()
    assert parse_required_mcps({"mcp": "slack"}) == ("slack",)
    assert parse_required_mcps({"mcp": "slack, github"}) == ("slack", "github")
    # tolerates a bracketed / quoted list (the line-based parser keeps it a string)
    assert parse_required_mcps({"mcp": "[slack, 'github']"}) == ("slack", "github")


def test_worker_filter_queue_stickiness() -> None:
    main_only = WorkerFilter(worker_id="w1", queues=["main"])
    assert main_only.accepts(_cand("t", queue=None))
    assert not main_only.accepts(_cand("t", queue="alpha"))

    any_queue = WorkerFilter(worker_id="w1", queues=None)
    assert any_queue.accepts(_cand("t", queue="alpha"))


def test_worker_filter_priority_set() -> None:
    wf = WorkerFilter(worker_id="w1", priorities=[0, 1])
    assert wf.accepts(_cand("t", priority=0))
    assert not wf.accepts(_cand("t", priority=2))


def test_worker_filter_model_membership() -> None:
    # auto/max pin nothing: any worker takes them even with no advertised models.
    bare = WorkerFilter(worker_id="w1", models=[])
    assert bare.accepts(_cand("t", model="auto"))
    assert bare.accepts(_cand("t", model="max"))
    # a pinned model only matches a worker that advertises it (case-insensitive).
    assert not bare.accepts(_cand("t", model="claude-opus-4-8"))
    claude = WorkerFilter(worker_id="w1", models=["claude-opus-4-8", "gpt-5.5"])
    assert claude.accepts(_cand("t", model="claude-opus-4-8"))
    assert claude.accepts(_cand("t", model="CLAUDE-OPUS-4-8"))
    assert not claude.accepts(_cand("t", model="llama3.1"))


def test_worker_filter_mcp_superset() -> None:
    # No requirement → any worker. A requirement needs the worker to advertise a
    # superset of the declared connectors.
    none_adv = WorkerFilter(worker_id="w1", mcps=[])
    assert none_adv.accepts(_cand("t"))
    assert not none_adv.accepts(_cand("t", required_mcps=["slack"]))
    both = WorkerFilter(worker_id="w1", mcps=["slack", "github", "jira"])
    assert both.accepts(_cand("t", required_mcps=["slack", "github"]))
    assert not both.accepts(_cand("t", required_mcps=["slack", "linear"]))


def test_queue_dedication_cut() -> None:
    cand = _cand("t", queue="ops")
    dedication = {"ops": ["w-trusted"]}
    bound = WorkerFilter(worker_id="w-trusted", queues=None)
    other = WorkerFilter(worker_id="w-other", queues=None)
    assert bound.accepts(cand, dedication=dedication)
    assert not other.accepts(cand, dedication=dedication)
    # A non-dedicated queue is open to anyone.
    assert other.accepts(_cand("t", queue="open"), dedication=dedication)


def test_queue_head_skips_leased_blocked_and_after() -> None:
    cands = [
        _cand("a", priority=0),
        _cand("b", priority=0, after="a"),
        _cand("c", priority=0),
    ]
    wf = WorkerFilter(worker_id="w1")
    # 'a' leased, 'b' after-blocked by present 'a' → head is 'c'.
    head = queue_head(
        cands,
        worker=wf,
        leased={(None, "a")},
        blocked=set(),
        present_tasks={"a", "b", "c"},
    )
    assert head is not None and head.task == "c"


def test_after_dependency_unblocks_when_target_gone() -> None:
    cands = [_cand("b", priority=0, after="a")]
    wf = WorkerFilter(worker_id="w1")
    # 'a' no longer present → 'b' is runnable.
    head = queue_head(
        cands, worker=wf, leased=set(), blocked=set(), present_tasks={"b"}
    )
    assert head is not None and head.task == "b"


def test_arbitration_prefers_lower_priority_number() -> None:
    by_queue = {
        None: [_cand("a", queue=None, priority=3)],
        "alpha": [_cand("x", queue="alpha", priority=1)],
    }
    wf = WorkerFilter(worker_id="w1")
    chosen = pick_next(
        by_queue, worker=wf, leased=set(), blocked=set(), state=SchedulerState()
    )
    assert chosen is not None and chosen.task == "x"


def test_round_robin_tiebreak_across_equal_priority_queues() -> None:
    state = SchedulerState()
    wf = WorkerFilter(worker_id="w1")

    def fresh():
        return {
            None: [_cand("a", queue=None, priority=0), _cand("a2", queue=None, priority=0)],
            "alpha": [_cand("x", queue="alpha", priority=0), _cand("x2", queue="alpha", priority=0)],
        }

    first = pick_next(fresh(), worker=wf, leased=set(), blocked=set(), state=state)
    second = pick_next(fresh(), worker=wf, leased=set(), blocked=set(), state=state)
    # Two equal-priority queues should alternate, not serve the same one twice.
    assert {first.label, second.label} == {"main", "alpha"}


def test_unroutable_reasons() -> None:
    by_queue = {
        None: [
            _cand("a", model="llama3.1"),               # model not advertised
            _cand("b", model="auto"),                   # agnostic → routable
            _cand("c", required_mcps=["slack"]),        # connector not advertised
            _cand("d", queue="ops"),                    # dedicated to offline worker
        ],
    }
    # Map d's queue label: queue=None → "main" for a/b/c; d is queue=None too here,
    # so give it its own queue for the dedication case.
    by_queue = {
        None: [_cand("a", model="llama3.1"), _cand("b", model="auto")],
        "ext": [_cand("c", queue="ext", required_mcps=["slack"])],
        "ops": [_cand("d", queue="ops")],
    }
    rows = unroutable(
        by_queue,
        available_models={"claude-opus-4-8"},
        available_mcps={"github"},
        dedication={"ops": ["w-down"]},
        online_workers=set(),  # w-down is offline
    )
    by_task = {c.task: reason for c, reason in rows}
    assert "a" in by_task and "model 'llama3.1'" in by_task["a"]
    assert "b" not in by_task
    assert "c" in by_task and "connector 'slack'" in by_task["c"]
    assert "d" in by_task and "dedicated" in by_task["d"]


def test_no_eligible_task_returns_none() -> None:
    by_queue = {None: [_cand("a", model="llama3.1")]}
    wf = WorkerFilter(worker_id="w1", models=["claude-opus-4-8"])  # no llama
    chosen = pick_next(
        by_queue, worker=wf, leased=set(), blocked=set(), state=SchedulerState()
    )
    assert chosen is None


# --------------------------------------------------------------------------- #
# Multi-dependency after: (DAG fan-in)
# --------------------------------------------------------------------------- #


def test_after_deps_parses_comma_list() -> None:
    from nightshift.manager.scheduler import _after_deps

    assert _after_deps({}) == ()
    assert _after_deps({"after": "a"}) == ("a",)
    assert _after_deps({"after": "a.md"}) == ("a",)
    assert _after_deps({"after": "a, b"}) == ("a", "b")
    assert _after_deps({"after": "a.md, b.md, c"}) == ("a", "b", "c")
    assert _after_deps({"after": "  a , b  "}) == ("a", "b")
    assert _after_deps({"after": ""}) == ()


def test_multi_dep_blocks_until_all_gone() -> None:
    """A fan-in task with after: a, b is blocked while any dependency remains."""
    cands = [_cand("merge", priority=0, after=("a", "b"))]
    wf = WorkerFilter(worker_id="w1")

    head = queue_head(
        cands, worker=wf, leased=set(), blocked=set(),
        present_tasks={"a", "merge"},
    )
    assert head is None

    head = queue_head(
        cands, worker=wf, leased=set(), blocked=set(),
        present_tasks={"b", "merge"},
    )
    assert head is None

    head = queue_head(
        cands, worker=wf, leased=set(), blocked=set(),
        present_tasks={"merge"},
    )
    assert head is not None and head.task == "merge"


def test_single_after_backward_compatible() -> None:
    """A single-element after tuple works identically to the old str field."""
    cands = [_cand("b", priority=0, after="a")]
    wf = WorkerFilter(worker_id="w1")

    head = queue_head(
        cands, worker=wf, leased=set(), blocked=set(),
        present_tasks={"a", "b"},
    )
    assert head is None

    head = queue_head(
        cands, worker=wf, leased=set(), blocked=set(),
        present_tasks={"b"},
    )
    assert head is not None and head.task == "b"
