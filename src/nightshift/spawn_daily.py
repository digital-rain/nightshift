"""Spawn implementation tasks from autosplit queue files.

Autosplit tasks have `autosplit: true` in their frontmatter. They accumulate
list items in the body; at dispatch time each item becomes a separate subtask,
and the parent file resets to its template.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from nightshift._paths import asset
from nightshift.repos import DEFAULT_TASKS_REPO


ITEM_RE = re.compile(
    r"^(?:"
    r"\d+[.)]\s+"        # numbered: 1. or 1)
    r"|[*\-+]\s+"        # bullets: *, -, +
    r"|\[[ xX]?\]\s+"   # checkboxes: [ ], [x], [X]
    r")(.+)$"
)
SPAWN_NAME = re.compile(r"^(\d+)\.(\d+)\.")


@dataclass(frozen=True)
class SpawnedTask:
    name: str
    title: str
    body: str
    model: str
    max_turns: int | None
    automerge: bool
    draft: bool


@dataclass(frozen=True)
class InspectResult:
    source: str
    item_count: int
    items: list[str]


@dataclass(frozen=True)
class ExecuteResult:
    source: str
    spawned: list[SpawnedTask]
    reset_written: bool


def load_config(workspace: Path) -> dict:
    """Read the operator/host config at ``<workspace>/config.json`` (manager
    block, ``tasks_repo`` name, and global runner defaults). Returns ``{}`` when
    absent/malformed is *not* tolerated here — callers wrap the FileNotFoundError
    as today (``resolve_config`` falls back to ``{}``)."""
    return json.loads((workspace / "config.json").read_text())


def save_config_value(workspace: Path, key: str, value: object) -> object:
    """Set a single key in ``<workspace>/config.json``, preserving sibling keys
    and their order. Returns the value written.

    Used by the Settings UI for *global* knobs (e.g. ``max_concurrent_queues``)
    that live in the operator config rather than per-queue config or player
    settings. ``config.json`` is worker-forbidden but operator-editable, so
    writing it from the UI is an operator action consistent with the
    ``forbidden_paths`` model."""
    path = workspace / "config.json"
    data: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text())
            if isinstance(loaded, dict):
                data = loaded
        except (ValueError, OSError):
            data = {}
    data[key] = value
    path.write_text(json.dumps(data, indent=2) + "\n")
    return value


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` onto ``base`` (override wins, dicts merge)."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_queue_config(tasks_root: Path, tasks_rel: str = "main") -> dict:
    """Read a queue's ``config.json`` (``<tasks_root>/<tasks_rel>/config.json``;
    ``tasks_rel`` is the queue dir, default ``main``). Returns ``{}`` when the
    file is absent or malformed. The queue's ``repo`` key is read from here."""
    path = tasks_root / tasks_rel / "config.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def load_store_config(tasks_root: Path) -> dict:
    """Read the optional workspace-level/system-wide content-store config at
    ``<tasks_root>/config.json``. Returns ``{}`` when absent or malformed."""
    path = tasks_root / "config.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def resolve_config(workspace: Path, tasks_root: Path, tasks_rel: str = "main") -> dict:
    """Layered runner config for a queue, rebased onto the two roots.

    Resolution order (later wins): the operator/global ``<workspace>/config.json``,
    then the optional workspace-level content-store ``<tasks_root>/config.json``
    (the system-wide layer, formerly ``.tasks/config.json``), then the per-queue
    ``<tasks_root>/<tasks_rel>/config.json``. A queue therefore inherits every
    setting it does not itself override.
    """
    try:
        merged = load_config(workspace)
    except (FileNotFoundError, ValueError):
        merged = {}
    merged = _deep_merge(merged, load_store_config(tasks_root))
    merged = _deep_merge(merged, load_queue_config(tasks_root, tasks_rel))
    return merged


def split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta: dict = {}
    for line in parts[1].splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        raw = value.strip()
        if raw.lower() in {"true", "false"}:
            meta[key.strip()] = raw.lower() == "true"
        elif raw.isdigit():
            meta[key.strip()] = int(raw)
        else:
            meta[key.strip()] = raw
    return meta, parts[2].lstrip("\n")


def extract_items(body: str) -> tuple[str, list[str]]:
    """Split body into preamble and list items.

    Returns (preamble, items) where preamble is all non-heading text before
    the first list item, and items are extracted from any bullet/numbered format.
    Multi-line continuation (indented lines following a bullet) is included.
    Headings between items are ignored (they're organizational, not content).
    """
    lines = body.splitlines()
    preamble_lines: list[str] = []
    items: list[str] = []
    current_item: list[str] | None = None
    found_first_item = False

    for line in lines:
        stripped = line.strip()
        match = ITEM_RE.match(stripped)
        if match:
            if current_item is not None:
                items.append("\n".join(current_item).strip())
            current_item = [match.group(1).strip()]
            found_first_item = True
        elif found_first_item and current_item is not None:
            if stripped and (line.startswith("  ") or line.startswith("\t")):
                current_item.append(stripped)
            elif not stripped or stripped.startswith("#"):
                items.append("\n".join(current_item).strip())
                current_item = None
            else:
                items.append("\n".join(current_item).strip())
                current_item = None
        elif not found_first_item:
            if not stripped.startswith("#"):
                preamble_lines.append(line)

    if current_item is not None:
        items.append("\n".join(current_item).strip())

    preamble = "\n".join(preamble_lines).strip()
    return preamble, items


def is_disabled(meta: dict) -> bool:
    """Return True when frontmatter marks a task as disabled."""
    return bool(meta.get("disabled", False))


# Priority scale: 0 = highest, 5 = lowest. A task that doesn't set ``priority``
# sorts as the lowest (5) so explicitly-prioritised tasks float to the top.
MIN_PRIORITY = 0
MAX_PRIORITY = 5
DEFAULT_PRIORITY = MAX_PRIORITY


def task_priority(meta: dict, default: int = DEFAULT_PRIORITY) -> int:
    """Resolve a task's 0-5 priority from frontmatter, clamped to range.

    A missing or non-integer ``priority`` falls back to ``default`` (lowest), and
    any out-of-range value is clamped to ``[MIN_PRIORITY, MAX_PRIORITY]``.
    """
    raw = meta.get("priority", default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(MIN_PRIORITY, min(MAX_PRIORITY, value))


def find_autosplit_sources(tasks_root: Path, tasks_rel: str = "main") -> list[str]:
    """Return sorted stems of task files with autosplit: true in frontmatter.

    Scans the queue dir ``<tasks_root>/<tasks_rel>`` of the content store (the
    default queue is ``main``).
    """
    tasks_dir = tasks_root / tasks_rel
    sources: list[str] = []
    for p in sorted(tasks_dir.glob("*.md")):
        text = p.read_text(errors="replace")
        if not text.startswith("---"):
            continue
        meta, _ = split_frontmatter(text)
        if is_disabled(meta):
            continue
        if meta.get("autosplit"):
            sources.append(p.stem)
    return sources


def slugify(text: str, *, limit: int = 48) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return (slug[:limit].rstrip("-") or "item")


def next_sub_id(tasks_dir: Path, parent_num: str) -> int:
    highest = 0
    for path in tasks_dir.glob(f"{parent_num}.*.md"):
        match = SPAWN_NAME.match(path.stem)
        if match and match.group(1) == parent_num:
            highest = max(highest, int(match.group(2)))
    return highest + 1


def unique_spawn_name(tasks_dir: Path, base: str) -> str:
    candidate = base
    if not (tasks_dir / f"{candidate}.md").exists():
        return candidate
    n = 2
    while (tasks_dir / f"{base}-{n}.md").exists():
        n += 1
    return f"{base}-{n}"


def resolve_frontmatter(meta: dict, config: dict) -> dict:
    raw_turns = meta.get("turns", config.get("max_turns"))
    return {
        "model": meta.get("model", config.get("model", "claude-sonnet-4-6")),
        "max_turns": int(raw_turns) if raw_turns is not None else None,
        "automerge": bool(meta.get("automerge", config.get("automerge", True))),
        "draft": bool(meta.get("draft", config.get("draft", False))),
    }


def render_spawned_task(
    *,
    template: Path,
    title: str,
    body: str,
    resolved: dict,
) -> str:
    text = template.read_text()
    text = text.replace("title: short descriptive title for the PR", f"title: {title}", 1)
    text = text.replace("model: claude-sonnet-4-6", f"model: {resolved['model']}", 1)
    text = text.replace(
        "automerge: true",
        f"automerge: {'true' if resolved['automerge'] else 'false'}",
        1,
    )
    text = text.replace(
        "draft: false",
        f"draft: {'true' if resolved['draft'] else 'false'}",
        1,
    )
    text = text.replace("evergreen: false", "evergreen: false", 1)
    text = text.replace("Task description goes here.", body, 1)
    return text


def inspect_source(
    tasks_root: Path, source: str, tasks_rel: str = "main"
) -> InspectResult:
    path = tasks_root / tasks_rel / f"{source}.md"
    if not path.exists():
        return InspectResult(source=source, item_count=0, items=[])
    _meta, body = split_frontmatter(path.read_text())
    _preamble, items = extract_items(body)
    return InspectResult(source=source, item_count=len(items), items=items)


def spawn_source(
    tasks_root: Path, source: str, *, write: bool = False, tasks_rel: str = "main"
) -> ExecuteResult | None:
    inspect = inspect_source(tasks_root, source, tasks_rel)
    if inspect.item_count == 0:
        return None

    path = tasks_root / tasks_rel / f"{source}.md"
    meta, body = split_frontmatter(path.read_text())
    # ``tasks_root`` is ``<workspace>/<tasks_repo>`` by construction, so the
    # workspace is its parent — use the layered queue config for model defaults.
    config = resolve_config(tasks_root.parent, tasks_root, tasks_rel)
    resolved = resolve_frontmatter(meta, config)
    tasks_dir = tasks_root / tasks_rel
    template = asset("templates", "task.md")
    evergreen_template = asset("templates", f"{source}.md")
    parent_num = source.split(".", 1)[0]
    sub_id = next_sub_id(tasks_dir, parent_num) - 1

    preamble, items = extract_items(body)
    spawned: list[SpawnedTask] = []

    for item in items:
        sub_id += 1
        title = item if len(item) <= 80 else item[:77] + "..."
        slug = slugify(item)
        name = unique_spawn_name(tasks_dir, f"{parent_num}.{sub_id}.{slug}")
        task_body = f"{preamble}\n\n{item}" if preamble else item
        task = SpawnedTask(
            name=name,
            title=title,
            body=task_body,
            model=resolved["model"],
            max_turns=resolved["max_turns"],
            automerge=resolved["automerge"],
            draft=resolved["draft"],
        )
        spawned.append(task)
        if write:
            content = render_spawned_task(
                template=template,
                title=task.title,
                body=task.body,
                resolved=resolved,
            )
            (tasks_dir / f"{name}.md").write_text(content)

    if write:
        (tasks_dir / f"{source}.md").write_text(evergreen_template.read_text())

    return ExecuteResult(source=source, spawned=spawned, reset_written=write)


def spawn_all(
    tasks_root: Path, *, write: bool = False, tasks_rel: str = "main"
) -> list[ExecuteResult]:
    results: list[ExecuteResult] = []
    for source in find_autosplit_sources(tasks_root, tasks_rel):
        result = spawn_source(tasks_root, source, write=write, tasks_rel=tasks_rel)
        if result is not None:
            results.append(result)
    return results


def matrix_entries(results: list[ExecuteResult]) -> list[dict]:
    entries: list[dict] = []
    for result in results:
        for task in result.spawned:
            entries.append(
                {
                    "task": task.name,
                    "model": task.model,
                    "max_turns": task.max_turns,
                }
            )
    return entries


def matrix_from_task_names(
    tasks_root: Path,
    names: list[str],
    *,
    scheduled_only: bool = False,
    tasks_rel: str = "main",
) -> list[dict]:
    # ``tasks_root`` is ``<workspace>/<tasks_repo>``; resolve the layered queue
    # config (its parent is the workspace by construction).
    config = resolve_config(tasks_root.parent, tasks_root, tasks_rel)
    scheduled_models = config.get("scheduled_models")
    entries: list[dict] = []
    for name in names:
        path = tasks_root / tasks_rel / f"{name}.md"
        meta = split_frontmatter(path.read_text())[0] if path.exists() else {}
        if is_disabled(meta):
            print(f"skip {name}: disabled", file=sys.stderr)
            continue
        resolved = resolve_frontmatter(meta, config)
        if scheduled_only and scheduled_models and resolved["model"] not in scheduled_models:
            print(
                f"skip {name}: model {resolved['model']} is dispatch-only "
                "(not in scheduled_models)",
                file=sys.stderr,
            )
            continue
        entries.append(
            {
                "task": name,
                "model": resolved["model"],
                "max_turns": resolved["max_turns"],
            }
        )
    return entries


def recover_matrix(
    tasks_root: Path, *, base_ref: str, tasks_rel: str = "main"
) -> list[dict]:
    out = subprocess.check_output(
        [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=A",
            f"{base_ref}...HEAD",
            "--",
            f"{tasks_rel}/",
        ],
        text=True,
        cwd=tasks_root,
    )
    names: list[str] = []
    for line in out.splitlines():
        if not line.endswith(".md"):
            continue
        stem = Path(line).name.removesuffix(".md")
        if SPAWN_NAME.match(stem):
            names.append(stem)
    names.sort()
    if not names:
        return []
    return matrix_from_task_names(tasks_root, names, tasks_rel=tasks_rel)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list-autosplit", help="List autosplit source names (JSON array)")

    inspect_p = sub.add_parser("inspect", help="Inspect one autosplit source file")
    inspect_p.add_argument("--source", required=True)

    sub.add_parser("inspect-all", help="Inspect all autosplit source files")

    execute_p = sub.add_parser("execute-all", help="Spawn tasks and reset autosplit files")
    execute_p.add_argument("--write", action="store_true", help="Write files to disk")
    execute_p.add_argument(
        "--sources",
        default="",
        help="Optional JSON array of autosplit sources to dispatch",
    )

    matrix_p = sub.add_parser(
        "matrix-for",
        help="Build worker matrix entries (honouring frontmatter) for explicit task names",
    )
    matrix_p.add_argument("--tasks", required=True, help="JSON array of task names")
    matrix_p.add_argument(
        "--scheduled-only",
        action="store_true",
        help="Drop tasks whose model is not in config scheduled_models",
    )

    recover_p = sub.add_parser(
        "recover-matrix",
        help="Build worker matrix from spawned task files added since base_ref",
    )
    recover_p.add_argument(
        "--base-ref",
        default="origin/main",
        help="Git ref to diff against (default: origin/main)",
    )

    args = parser.parse_args(argv)
    workspace = args.workspace.resolve()
    # The content store ``tasks_root = <workspace>/<tasks_repo>`` holds briefs and
    # queue config; the CLI subcommands operate on its default ``main`` queue.
    try:
        host_config = load_config(workspace)
    except (FileNotFoundError, ValueError):
        host_config = {}
    tasks_repo = str(host_config.get("tasks_repo") or DEFAULT_TASKS_REPO)
    tasks_root = workspace / tasks_repo
    tasks_rel = "main"

    if args.cmd == "list-autosplit":
        print(json.dumps(find_autosplit_sources(tasks_root, tasks_rel)))
        return 0

    if args.cmd == "inspect":
        print(json.dumps(asdict(inspect_source(tasks_root, args.source, tasks_rel))))
        return 0

    if args.cmd == "inspect-all":
        payload = [
            asdict(inspect_source(tasks_root, source, tasks_rel))
            for source in find_autosplit_sources(tasks_root, tasks_rel)
        ]
        print(json.dumps(payload))
        return 0

    if args.cmd == "execute-all":
        sources = find_autosplit_sources(tasks_root, tasks_rel)
        if args.sources:
            parsed = json.loads(args.sources)
            if not isinstance(parsed, list):
                raise SystemExit("--sources must be a JSON array")
            sources = parsed
        results: list[ExecuteResult] = []
        for source in sources:
            result = spawn_source(tasks_root, source, write=args.write, tasks_rel=tasks_rel)
            if result is not None:
                results.append(result)
        print(
            json.dumps(
                {
                    "results": [
                        {
                            "source": r.source,
                            "spawned": [asdict(t) for t in r.spawned],
                            "reset_written": r.reset_written,
                        }
                        for r in results
                    ],
                    "matrix": matrix_entries(results),
                }
            )
        )
        return 0

    if args.cmd == "matrix-for":
        names = json.loads(args.tasks)
        if not isinstance(names, list):
            raise SystemExit("--tasks must be a JSON array")
        entries = matrix_from_task_names(
            tasks_root, names, scheduled_only=args.scheduled_only, tasks_rel=tasks_rel
        )
        print(json.dumps(entries))
        return 0

    if args.cmd == "recover-matrix":
        print(json.dumps(recover_matrix(tasks_root, base_ref=args.base_ref, tasks_rel=tasks_rel)))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
