from nightshift.manager.failure_policy import QueueFailureState, record_outcome, pick_retry


def test_first_failure_arms_watch_no_pause():
    st = QueueFailureState()
    paused = record_outcome(st, is_failure=True)
    assert st.watch_armed is True
    assert paused is False


def test_second_consecutive_failure_pauses():
    st = QueueFailureState()
    record_outcome(st, is_failure=True)
    paused = record_outcome(st, is_failure=True)
    assert paused is True


def test_success_between_failures_disarms_watch():
    st = QueueFailureState()
    record_outcome(st, is_failure=True)
    record_outcome(st, is_failure=False)
    assert st.watch_armed is False
    paused = record_outcome(st, is_failure=True)
    assert paused is False


def test_neutral_outcome_does_not_arm_or_disarm():
    st = QueueFailureState()
    record_outcome(st, is_failure=True)
    record_outcome(st, is_failure=None)
    assert st.watch_armed is True
    paused = record_outcome(st, is_failure=True)
    assert paused is True


def test_pick_retry_earliest_in_order():
    rows = [{"task": "b"}, {"task": "a"}, {"task": "c"}]
    assert pick_retry(rows, order=["a", "b", "c"]) == "a"


def test_pick_retry_unordered_tasks_fall_back_to_list_order():
    rows = [{"task": "z"}, {"task": "a"}]
    assert pick_retry(rows, order=["a"]) == "a"
    assert pick_retry([{"task": "z"}], order=["a"]) == "z"


def test_pick_retry_empty_returns_none():
    assert pick_retry([], order=[]) is None
