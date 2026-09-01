"""Auto-import — continuously draining a target repo's ``.tasks/`` host queues.

The manual import (:mod:`nightshift.repo_tasks`) is an operator action: open
the picker, tick briefs, drain them all at once. Auto-import is the same
machinery on a clock. A repo the operator has switched on publishes work into
``.tasks/<host-queue>/``; a Nightshift queue bound to that repo names one of
those host queues, and from then on the manager pulls briefs out of it one at
a time, runs them like any other task, and removes each source from the repo's
``main`` — so the repo's own inbox doubles as a queue Nightshift services.

Two settings drive it, both in the content store so they commit with the rest
of the queue configuration:

* ``auto_import_repos`` in the store-level ``<tasks_root>/config.json`` — the
  repos the switch is on for. Repo-level, because the ``.tasks/`` inbox is a
  property of the repo, not of any one queue bound to it.
* ``host_queue`` in a queue's own ``config.json`` — which ``.tasks/`` subdir
  *this* queue drains. Absent means "the subdir named after me"
  (:func:`resolve_host_queue`), which is the binding the operator almost
  always wants; an explicit empty string means "none, leave the inbox alone".

**Round-robin.** A host queue must not starve the queue it feeds into, so the
importer keeps at most one un-run imported brief in flight per queue
(:func:`pending_imports`) and inserts each new one directly behind the
destination queue's current head (:func:`insert_round_robin`).

**Frontmatter.** Briefs in a repo's inbox are written by whoever publishes
them, so their frontmatter is untrusted. :func:`normalize_frontmatter` fills
in a title, model, priority, and target repo, replacing anything unusable with
the resolved default rather than blocking the task on an authoring error
nobody here can fix.

See ``docs/specs/2026-09-01-auto-import.md`` for the operator surface and the
reasoning behind both rules.
"""

from __future__ import annotations

from pathlib import Path

from nightshift.model_id import is_qualified
from nightshift.playlists import is_valid_name
from nightshift.queue_config import (
    SORT_MANUAL,
    order_stems,
    save_order,
    save_store_config_value,
)
from nightshift.repos import is_valid_repo_ref
from nightshift.spawn_daily import (
    DEFAULT_PRIORITY,
    MAX_PRIORITY,
    MIN_PRIORITY,
    join_frontmatter,
    load_store_config,
    split_frontmatter,
)
from nightshift.task_files import live_ordered_queue, resolve_title


# Store-level config key: the repos whose ``.tasks/`` inbox is auto-imported.
AUTO_IMPORT_REPOS_KEY = "auto_import_repos"

# Per-queue config key: which ``.tasks/<name>`` host queue this queue drains.
HOST_QUEUE_KEY = "host_queue"

# Stamped onto every auto-imported brief as ``<repo>/<source>``: the provenance
# record, and the in-flight marker the round-robin reads (a brief carrying it
# came from a host queue and has not run yet).
PROVENANCE_KEY = "imported_from"

# Model keywords that pin nothing concrete (the scheduler's ``AGNOSTIC_MODELS``
# vocabulary): a published ``model: auto`` is usable, not unrecognised.
_AGNOSTIC_MODELS = frozenset({"auto", "max", "default"})


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def auto_import_repos(tasks_root: Path) -> list[str]:
    """The repos auto-import is switched on for, sorted and de-duped.

    Degrades to ``[]`` on a missing/malformed store config, and drops entries
    that are not valid repo references — the stored set feeds an inbox path,
    so it gets the same slug guard every persisted repo reference gets.
    """
    raw = load_store_config(tasks_root).get(AUTO_IMPORT_REPOS_KEY)
    if not isinstance(raw, list):
        return []
    return sorted({str(name) for name in raw if is_valid_repo_ref(str(name))})


def set_auto_import(tasks_root: Path, repo: str, enabled: bool) -> list[str]:
    """Switch auto-import on/off for one repo. Returns the resulting set.

    Idempotent in both directions, and a no-op write still rewrites the file
    so the caller's content-store commit has a stable story.
    """
    current = set(auto_import_repos(tasks_root))
    if enabled:
        current.add(repo)
    else:
        current.discard(repo)
    out = sorted(current)
    save_store_config_value(tasks_root, AUTO_IMPORT_REPOS_KEY, out)
    return out


def resolve_host_queue(
    queue_config: dict, queue_label: str, *, available: list[str]
) -> str | None:
    """The ``.tasks/`` host queue a Nightshift queue drains, or ``None``.

    Three cases, in precedence order:

    * the key is set to a name — that name wins, even when the repo does not
      (yet) publish it, exactly as a queue's ``repo`` binding survives the repo
      being absent;
    * the key is set to an empty string — an explicit "none": this queue does
      not auto-import even though its repo's switch is on;
    * the key is absent — default to the subdir named after the queue, when
      the repo publishes one. This is the spec's default binding (queue
      ``longitude`` drains ``.tasks/longitude``) and the reason the common
      case needs no configuration at all.
    """
    raw = queue_config.get(HOST_QUEUE_KEY)
    if raw is None:
        return queue_label if queue_label in available else None
    name = str(raw).strip()
    if not name or not is_valid_name(name):
        return None
    return name


# --------------------------------------------------------------------------- #
# Frontmatter normalisation
# --------------------------------------------------------------------------- #


def _normalized_priority(raw: object) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_PRIORITY
    return value if MIN_PRIORITY <= value <= MAX_PRIORITY else DEFAULT_PRIORITY


def _normalized_model(raw: object, default_model: str) -> str:
    """A published ``model:`` kept only when Nightshift can route on it: an
    agnostic keyword, or a provider-qualified id. A bare ``sonnet`` would pin
    the task to a model no worker advertises and block it, so it falls back to
    the configured default instead."""
    model = str(raw or "").strip()
    if not model:
        return default_model
    if model.lower() in _AGNOSTIC_MODELS or is_qualified(model):
        return model
    return default_model


def normalize_frontmatter(
    meta: dict, *, stem: str, repo: str, default_model: str
) -> dict:
    """Fill in a published brief's frontmatter, replacing unusable values.

    Returns a new dict: ``title``/``model``/``priority``/``repo`` are always
    present and always valid; every other key the publisher wrote is carried
    through unchanged (inert to dispatch, and dropping it would lose author
    intent for fields Nightshift may learn later).
    """
    out = dict(meta)
    out["title"] = str(meta.get("title") or "").strip() or resolve_title(stem, {})
    out["model"] = _normalized_model(meta.get("model"), default_model)
    out["priority"] = _normalized_priority(meta.get("priority"))
    published_repo = str(meta.get("repo") or "").strip()
    out["repo"] = published_repo if is_valid_repo_ref(published_repo) else repo
    return out


def imported_brief_text(
    text: str, *, stem: str, repo: str, source: str, default_model: str
) -> str:
    """Rewrite a published brief for the destination queue: normalised
    frontmatter plus the :data:`PROVENANCE_KEY` stamp naming where it came
    from. A brief with no frontmatter at all gains a complete one."""
    meta, body = split_frontmatter(text) if text.startswith("---") else ({}, text)
    normalized = normalize_frontmatter(
        meta, stem=stem, repo=repo, default_model=default_model
    )
    normalized[PROVENANCE_KEY] = f"{repo}/{source}"
    return join_frontmatter(normalized, body)


# --------------------------------------------------------------------------- #
# Round-robin placement
# --------------------------------------------------------------------------- #


def pending_imports(tasks_root: Path, tasks_rel: str) -> dict[str, str]:
    """Auto-imported briefs still sitting in a queue, ``provenance -> stem``.

    A landed task's brief is dropped from the store, so a non-empty result
    means the queue's previous host pick has not run yet — the round-robin
    gate, and the replay guard for a removal that failed after the copy
    succeeded (the source is still published, and re-importing it would run
    the same work twice).
    """
    queue_dir = tasks_root / tasks_rel
    if not queue_dir.is_dir():
        return {}
    out: dict[str, str] = {}
    for path in sorted(queue_dir.glob("*.md")):
        text = path.read_text(errors="replace")
        if not text.startswith("---"):
            continue
        provenance = split_frontmatter(text)[0].get(PROVENANCE_KEY)
        if provenance:
            out[str(provenance)] = path.stem
    return out


def insert_round_robin(tasks_root: Path, tasks_rel: str, stem: str) -> list[str]:
    """Place a freshly imported brief one slot behind the queue's head.

    The head is whatever the queue would dispatch next, so the import takes
    the slot *after* it: the queue's own task runs, then the host's, and with
    the one-in-flight gate the pattern repeats. Inserting at the front instead
    would displace a task already about to run, and appending would let a busy
    queue starve its host inbox indefinitely.

    An empty queue puts the import at the front — there is nothing to
    alternate with. Returns the persisted order.
    """
    queue_dir = tasks_root / tasks_rel
    others = [p.stem for p in queue_dir.glob("*.md") if p.stem != stem]
    full = order_stems(tasks_root, others, tasks_rel, sort=SORT_MANUAL)
    runnable = [s for s in live_ordered_queue(tasks_root, tasks_rel) if s != stem]
    head = runnable[0] if runnable else None
    index = full.index(head) + 1 if head is not None and head in full else 0
    full.insert(index, stem)
    return save_order(tasks_root, full, tasks_rel)


def write_imported_brief(
    tasks_root: Path,
    tasks_rel: str,
    *,
    name: str,
    text: str,
    repo: str,
    source: str,
    default_model: str,
) -> str:
    """Write one published brief into the destination queue and slot it into
    the round-robin position. Returns the stem actually written.

    A name already taken in the destination gets a ``-2`` suffix, matching
    :func:`nightshift.repo_tasks.copy_repo_tasks`'s collision policy — the
    host queue and the Nightshift queue are independent namespaces and a
    collision between them is not a duplicate.
    """
    queue_dir = tasks_root / tasks_rel
    queue_dir.mkdir(parents=True, exist_ok=True)
    stem = name
    n = 2
    while (queue_dir / f"{stem}.md").exists():
        stem = f"{name}-{n}"
        n += 1
    (queue_dir / f"{stem}.md").write_text(
        imported_brief_text(
            text, stem=stem, repo=repo, source=source, default_model=default_model
        )
    )
    insert_round_robin(tasks_root, tasks_rel, stem)
    return stem
