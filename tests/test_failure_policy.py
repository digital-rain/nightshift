from nightshift.manager.failure_policy import QueueFailureState, pick_retry


def test_queue_failure_state_starts_disarmed():
    # The arm/disarm/pause fold lives in the transition table now (see the
    # watch tests in test_lifecycle.py); this state object just persists the
    # flag between submits.
    assert QueueFailureState().watch_armed is False


def test_pick_retry_earliest_in_order():
    rows = [{"task": "b"}, {"task": "a"}, {"task": "c"}]
    assert pick_retry(rows, order=["a", "b", "c"]) == "a"


def test_pick_retry_unordered_tasks_fall_back_to_list_order():
    rows = [{"task": "z"}, {"task": "a"}]
    assert pick_retry(rows, order=["a"]) == "a"
    assert pick_retry([{"task": "z"}], order=["a"]) == "z"


def test_pick_retry_empty_returns_none():
    assert pick_retry([], order=[]) is None
