"""DEPRECATED — re-export shim for the dissolved orchestration core.

Phase 3 of the rebuild-in-place migration split this module along its
section-divider seams into:

- :mod:`nightshift.git.worktrees`, :mod:`nightshift.git.locks`,
  :mod:`nightshift.git.squash`, :mod:`nightshift.git.sync`,
  :mod:`nightshift.git.transport`, :mod:`nightshift.git.store`,
  :mod:`nightshift.git.refs`
- :mod:`nightshift.preflight`, :mod:`nightshift.queue_config`,
  :mod:`nightshift.task_files`, :mod:`nightshift.prompts`
- :mod:`nightshift.runner_legacy` (the pre-split ``Controller`` /
  ``run_task`` / ``run_queue``; retired in Phase 9)

This shim re-exports every previously-public name (and the previously
imported-elsewhere private names, under their old underscore spellings) so
external imports keep working for one release. Import from the new modules;
this file is deleted in the next phase.
"""

from __future__ import annotations

from nightshift.git.locks import (
    integrate_lock as integrate_lock,
)
from nightshift.git.locks import (
    landing_lock as landing_lock,
)
from nightshift.git.refs import (
    branch_exists as branch_exists,
)
from nightshift.git.refs import (
    is_ancestor as is_ancestor,
)
from nightshift.git.refs import (
    rev_parse as rev_parse,
)
from nightshift.git.squash import (
    AUTOSTASH_MESSAGE as AUTOSTASH_MESSAGE,
)
from nightshift.git.squash import (
    compute_code_loc as compute_code_loc,
)
from nightshift.git.squash import (
    conflicted_paths as conflicted_paths,
)
from nightshift.git.squash import (
    landing_blockers as landing_blockers,
)
from nightshift.git.squash import (
    porcelain_path as porcelain_path,
)
from nightshift.git.squash import (
    restore_operator_work as restore_operator_work,
)
from nightshift.git.squash import (
    squash_failure_kind as squash_failure_kind,
)
from nightshift.git.squash import (
    squash_to_main as squash_to_main,
)
from nightshift.git.squash import (
    stash_operator_work as stash_operator_work,
)
from nightshift.git.store import (
    commit_dispatch as commit_dispatch,
)
from nightshift.git.store import (
    commit_queue_state as commit_queue_state,
)
from nightshift.git.store import (
    commit_tasks as commit_tasks,
)
from nightshift.git.sync import (
    maybe_sync_main_to_origin as maybe_sync_main_to_origin,
)
from nightshift.git.sync import (
    reset_origin_sync_throttle as reset_origin_sync_throttle,
)
from nightshift.git.sync import (
    sync_main_to_origin as sync_main_to_origin,
)
from nightshift.git.transport import (
    WIP_REF_PREFIX as WIP_REF_PREFIX,
)
from nightshift.git.transport import (
    fetch_rendezvous_branch as fetch_rendezvous_branch,
)
from nightshift.git.transport import (
    normalize_wip_prefix as normalize_wip_prefix,
)
from nightshift.git.transport import (
    prepare_worktree_base as prepare_worktree_base,
)
from nightshift.git.transport import (
    prune_rendezvous_branch as prune_rendezvous_branch,
)
from nightshift.git.transport import (
    publish_task_branch as publish_task_branch,
)
from nightshift.git.worktrees import (
    SYMLINK_TARGETS as SYMLINK_TARGETS,
)
from nightshift.git.worktrees import (
    abort_rebase as abort_rebase,
)
from nightshift.git.worktrees import (
    cleanup_task_worktree as cleanup_task_worktree,
)
from nightshift.git.worktrees import (
    ensure_worktree_for_branch as ensure_worktree_for_branch,
)
from nightshift.git.worktrees import (
    has_commits as has_commits,
)
from nightshift.git.worktrees import (
    queue_slug as queue_slug,
)
from nightshift.git.worktrees import (
    rebase_in_progress as rebase_in_progress,
)
from nightshift.git.worktrees import (
    rebase_onto_main as rebase_onto_main,
)
from nightshift.git.worktrees import (
    setup_worktree as setup_worktree,
)
from nightshift.git.worktrees import (
    teardown_worktree as teardown_worktree,
)
from nightshift.git.worktrees import (
    worktree_branch as worktree_branch,
)
from nightshift.git.worktrees import (
    worktree_dir as worktree_dir,
)
from nightshift.preflight import (
    DEFAULT_PREFLIGHT_CMD as DEFAULT_PREFLIGHT_CMD,
)
from nightshift.preflight import (
    LOCK_MARKER_NAME as LOCK_MARKER_NAME,
)
from nightshift.preflight import (
    MIN_FREE_PCT as MIN_FREE_PCT,
)
from nightshift.preflight import (
    PreflightResult as PreflightResult,
)
from nightshift.preflight import (
    acquire_lock as acquire_lock,
)
from nightshift.preflight import (
    check_preconditions as check_preconditions,
)
from nightshift.preflight import (
    enough_free_disk as enough_free_disk,
)
from nightshift.preflight import (
    ensure_env_synced as ensure_env_synced,
)
from nightshift.preflight import (
    invalidate_lock_marker as invalidate_lock_marker,
)
from nightshift.preflight import (
    kill_process_group as kill_process_group,
)
from nightshift.preflight import (
    lock_changed_between as lock_changed_between,
)
from nightshift.preflight import (
    lock_fingerprint as lock_fingerprint,
)
from nightshift.preflight import (
    preflight_cmd_from_blob as preflight_cmd_from_blob,
)
from nightshift.preflight import (
    resolve_preflight_cmd as resolve_preflight_cmd,
)
from nightshift.preflight import (
    run_interruptible as run_interruptible,
)
from nightshift.prompts import (
    EXTRA_BIN_DIRS as EXTRA_BIN_DIRS,
)
from nightshift.prompts import (
    RESOLVE_PROMPT_FILE as RESOLVE_PROMPT_FILE,
)
from nightshift.prompts import (
    build_claude_argv as build_claude_argv,
)
from nightshift.prompts import (
    build_prompt as build_prompt,
)
from nightshift.prompts import (
    build_resolve_prompt as build_resolve_prompt,
)
from nightshift.prompts import (
    extract_blocked_reason as extract_blocked_reason,
)
from nightshift.prompts import (
    extract_result_line as extract_result_line,
)
from nightshift.prompts import (
    resolve_claude_bin as resolve_claude_bin,
)
from nightshift.prompts import (
    worker_env as worker_env,
)
from nightshift.queue_config import (
    DEFAULT_VALIDATE_CMD as DEFAULT_VALIDATE_CMD,
)
from nightshift.queue_config import (
    ORDER_CONFIG as ORDER_CONFIG,
)
from nightshift.queue_config import (
    SORT_MANUAL as SORT_MANUAL,
)
from nightshift.queue_config import (
    SORT_MODES as SORT_MODES,
)
from nightshift.queue_config import (
    SORT_PRIORITY as SORT_PRIORITY,
)
from nightshift.queue_config import (
    apply_play_filter as apply_play_filter,
)
from nightshift.queue_config import (
    format_validate_cmd as format_validate_cmd,
)
from nightshift.queue_config import (
    load_order as load_order,
)
from nightshift.queue_config import (
    load_play_priorities as load_play_priorities,
)
from nightshift.queue_config import (
    load_sort_mode as load_sort_mode,
)
from nightshift.queue_config import (
    normalize_validate_command as normalize_validate_command,
)
from nightshift.queue_config import (
    order_stems as order_stems,
)
from nightshift.queue_config import (
    reorder_queue as reorder_queue,
)
from nightshift.queue_config import (
    resolve_validate_cmd as resolve_validate_cmd,
)
from nightshift.queue_config import (
    save_order as save_order,
)
from nightshift.queue_config import (
    save_play_priorities as save_play_priorities,
)
from nightshift.queue_config import (
    save_queue_config_value as save_queue_config_value,
)
from nightshift.queue_config import (
    save_sort_mode as save_sort_mode,
)
from nightshift.queue_config import (
    validate_cmd_from_blob as validate_cmd_from_blob,
)
from nightshift.runner_legacy import (
    DEFAULT_MAX_RESOLVE_ATTEMPTS as DEFAULT_MAX_RESOLVE_ATTEMPTS,
)
from nightshift.runner_legacy import (
    Controller as Controller,
)
from nightshift.runner_legacy import (
    RunSummary as RunSummary,
)
from nightshift.runner_legacy import (
    TaskResult as TaskResult,
)
from nightshift.runner_legacy import (
    attempt_repair as attempt_repair,
)
from nightshift.runner_legacy import (
    recover_task as recover_task,
)
from nightshift.runner_legacy import (
    resolve_task as resolve_task,
)
from nightshift.runner_legacy import (
    run_queue as run_queue,
)
from nightshift.runner_legacy import (
    run_task as run_task,
)
from nightshift.runner_legacy import (
    select_run_backend as select_run_backend,
)
from nightshift.runner_legacy import (
    write_failure_log as write_failure_log,
)

# The old engine imported these from spawn_daily at module top, so they were
# importable from ``nightshift.engine`` too — keep that surface alive.
from nightshift.spawn_daily import (
    DEFAULT_PRIORITY as DEFAULT_PRIORITY,
)
from nightshift.spawn_daily import (
    MAX_PRIORITY as MAX_PRIORITY,
)
from nightshift.spawn_daily import (
    MIN_PRIORITY as MIN_PRIORITY,
)
from nightshift.spawn_daily import (
    find_autosplit_sources as find_autosplit_sources,
)
from nightshift.spawn_daily import (
    is_completed as is_completed,
)
from nightshift.spawn_daily import (
    is_disabled as is_disabled,
)
from nightshift.spawn_daily import (
    is_failed as is_failed,
)
from nightshift.spawn_daily import (
    is_quarantined as is_quarantined,
)
from nightshift.spawn_daily import (
    load_config as load_config,
)
from nightshift.spawn_daily import (
    load_queue_config as load_queue_config,
)
from nightshift.spawn_daily import (
    resolve_config as resolve_config,
)
from nightshift.spawn_daily import (
    resolve_frontmatter as resolve_frontmatter,
)
from nightshift.spawn_daily import (
    slugify as slugify,
)
from nightshift.spawn_daily import (
    spawn_all as spawn_all,
)
from nightshift.spawn_daily import (
    spawn_source as spawn_source,
)
from nightshift.spawn_daily import (
    split_frontmatter as split_frontmatter,
)
from nightshift.spawn_daily import (
    task_priority as task_priority,
)
from nightshift.task_files import (
    _EDITABLE_CONTENT_KEYS as _EDITABLE_CONTENT_KEYS,
)
from nightshift.task_files import (
    _EDITABLE_META_KEYS as _EDITABLE_META_KEYS,
)
from nightshift.task_files import (
    TASK_TEMPLATE as TASK_TEMPLATE,
)
from nightshift.task_files import (
    build_task_list as build_task_list,
)
from nightshift.task_files import (
    create_task as create_task,
)
from nightshift.task_files import (
    delete_task as delete_task,
)
from nightshift.task_files import (
    drop_completed_task as drop_completed_task,
)
from nightshift.task_files import (
    failed_tasks as failed_tasks,
)
from nightshift.task_files import (
    find_autosplit_tasks as find_autosplit_tasks,
)
from nightshift.task_files import (
    frontmatter_held_tasks as frontmatter_held_tasks,
)
from nightshift.task_files import (
    harvest_split_output as harvest_split_output,
)
from nightshift.task_files import (
    import_task as import_task,
)
from nightshift.task_files import (
    list_queue as list_queue,
)
from nightshift.task_files import (
    live_ordered_queue as live_ordered_queue,
)
from nightshift.task_files import (
    materialize_brief as materialize_brief,
)
from nightshift.task_files import (
    read_task as read_task,
)
from nightshift.task_files import (
    resolve_title as resolve_title,
)
from nightshift.task_files import (
    set_task_meta as set_task_meta,
)
from nightshift.task_files import (
    split_output_dir as split_output_dir,
)
from nightshift.task_files import (
    task_is_evergreen as task_is_evergreen,
)


# Old private spellings, kept so external imports of the previously
# cross-module private names survive the shim's one-release window.
_worktree_has_commits = has_commits
_queue_slug = queue_slug
_rev_parse = rev_parse
_is_ancestor = is_ancestor
_branch_exists = branch_exists
_landing_blockers = landing_blockers
_porcelain_path = porcelain_path
_stash_operator_work = stash_operator_work
_restore_operator_work = restore_operator_work
_conflicted_paths = conflicted_paths
_squash_failure_kind = squash_failure_kind
_commit_dispatch = commit_dispatch
_find_autosplit_tasks = find_autosplit_tasks
_apply_play_filter = apply_play_filter
_attempt_repair = attempt_repair
_write_failure_log = write_failure_log
_kill_process_group = kill_process_group
_EXTRA_BIN_DIRS = EXTRA_BIN_DIRS
_ensure_worktree_for_branch = ensure_worktree_for_branch
_rebase_in_progress = rebase_in_progress
_abort_rebase = abort_rebase
_rebase_onto_main = rebase_onto_main
