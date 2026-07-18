"""Document resolution helpers — parse, normalize, sniff, path guards, cap/allow-list predicates.

Phase 1 primitives are pure (no git). Phase 2 adds :func:`resolve_task_docs`,
which orchestrates resolution against the target repo (path docs) and the
tasks repo (attachments) via :class:`~nightshift.git.runner.GitRunner`.
"""

from __future__ import annotations

import json
import mimetypes
import posixpath
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


DOCUMENT_CAP_CEILING_BYTES: int = 5 * 1024 * 1024
DEFAULT_DOCUMENT_CAP_BYTES: int = 256 * 1024
DEFAULT_DOCUMENT_BUDGET_BYTES: int = 4 * 1024 * 1024
DEFAULT_ALLOWED_DOC_MEDIA_TYPES: tuple[str, ...] = (
    "text/*",
    "application/json",
    "application/yaml",
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "application/pdf",
)

# Magic-byte signatures for binary types we recognise.
_MAGIC_TABLE: tuple[tuple[bytes, int, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", 0, "image/png"),
    (b"\xff\xd8\xff", 0, "image/jpeg"),
    (b"GIF87a", 0, "image/gif"),
    (b"GIF89a", 0, "image/gif"),
    (b"RIFF", 0, "image/webp"),  # RIFF header; WEBP at offset 8
    (b"%PDF", 0, "application/pdf"),
)


@dataclass(frozen=True)
class DocSpec:
    """Normalised document specification from ``docs:`` or ``attachments:`` frontmatter."""

    path: str
    range: str | None = None
    as_: str | None = None
    steps: tuple[str, ...] | None = None


@dataclass(frozen=True)
class PinRecord:
    """Per-document pin state persisted in ``docs_pin``."""

    sha: str
    media: str
    bytes: int


class DocumentError(ValueError):
    """Authoring/resolution error for a task document."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class DocumentUnavailable(RuntimeError):
    """Environment error — a pinned blob is unreachable at materialise time."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


# ---------------------------------------------------------------------------
# Parse helpers
# ---------------------------------------------------------------------------


def _parse_spec_object(obj: dict) -> DocSpec:
    """Convert a dict with ``path``/``range``/``as``/``steps`` into a DocSpec."""
    path = obj.get("path")
    if not path or not isinstance(path, str):
        raise DocumentError("not_found", "doc spec object missing 'path'")
    steps_raw = obj.get("steps")
    steps: tuple[str, ...] | None = None
    if steps_raw is not None:
        if isinstance(steps_raw, str):
            steps = (steps_raw,)
        else:
            steps = tuple(str(s) for s in steps_raw)
    return DocSpec(
        path=path,
        range=obj.get("range"),
        as_=obj.get("as"),
        steps=steps,
    )


def _parse_item(item: object) -> DocSpec:
    if isinstance(item, str):
        return DocSpec(path=item)
    if isinstance(item, dict):
        return _parse_spec_object(item)
    raise DocumentError(
        "not_found", f"unexpected doc entry type: {type(item).__name__}"
    )


def _parse_block_list_lines(fence_lines: list[str], key: str) -> list[DocSpec]:
    """Parse indented block-list lines below an empty ``docs:`` / ``attachments:`` key."""
    result: list[DocSpec] = []
    for line in fence_lines:
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        value = stripped[2:].strip()
        if not value:
            continue
        if value.startswith("{"):
            try:
                obj = json.loads(value)
            except json.JSONDecodeError:
                result.append(DocSpec(path=value))
            else:
                result.append(_parse_item(obj))
        elif ":" in value and not value.startswith("/"):
            parts: dict[str, str | None] = {}
            k, v = value.split(":", 1)
            parts[k.strip()] = v.strip() or None
            for extra in fence_lines:
                es = extra.strip()
                if es.startswith("- ") or not es or ":" not in es:
                    continue
            result.append(
                _parse_spec_object(
                    {
                        "path": parts.get("path", value),
                        **{
                            pk: pv
                            for pk, pv in parts.items()
                            if pk != "path" and pv is not None
                        },
                    }
                )
            )
        else:
            result.append(DocSpec(path=value))
    return result


def _parse_raw_value(
    raw: object, *, fence_lines: list[str] | None = None, key: str = "docs"
) -> list[DocSpec]:
    """Shared parse logic for docs/attachments fields."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if fence_lines:
            return _parse_block_list_lines(fence_lines, key)
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("["):
            try:
                items = json.loads(text)
            except json.JSONDecodeError:
                return [DocSpec(path=p.strip()) for p in text.split(",") if p.strip()]
            else:
                return [_parse_item(i) for i in items]
        if text.startswith("{"):
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                return [DocSpec(path=text)]
            else:
                return [_parse_item(obj)]
        return [DocSpec(path=p.strip()) for p in text.split(",") if p.strip()]
    if isinstance(raw, list):
        return [_parse_item(i) for i in raw]
    if isinstance(raw, dict):
        return [_parse_spec_object(raw)]
    return [DocSpec(path=str(raw))]


def parse_docs_field(
    raw: object, *, fence_lines: list[str] | None = None
) -> list[DocSpec]:
    return _parse_raw_value(raw, fence_lines=fence_lines, key="docs")


def parse_attachments_field(
    raw: object, *, fence_lines: list[str] | None = None
) -> list[DocSpec]:
    return _parse_raw_value(raw, fence_lines=fence_lines, key="attachments")


def parse_docs_pin(raw: object) -> dict[str, PinRecord]:
    """Parse ``docs_pin`` from frontmatter (stored as compact JSON string)."""
    if raw is None:
        return {}
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        return {}
    result: dict[str, PinRecord] = {}
    for key, val in raw.items():
        if isinstance(val, dict):
            result[key] = PinRecord(
                sha=val["sha"],
                media=val["media"],
                bytes=val["bytes"],
            )
    return result


def render_docs_pin(pin: dict[str, PinRecord]) -> str:
    """Render ``docs_pin`` as compact JSON for frontmatter storage."""
    obj = {
        k: {"sha": v.sha, "media": v.media, "bytes": v.bytes} for k, v in pin.items()
    }
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)


# ---------------------------------------------------------------------------
# Path / media helpers
# ---------------------------------------------------------------------------


def normalize_repo_path(path: str) -> str:
    """Validate and normalise a repo-relative path.

    Rejects absolute paths, ``..`` traversal, and paths that escape the repo root.
    """
    if not path or not path.strip():
        raise DocumentError("path_outside_repo", "empty document path")
    cleaned = posixpath.normpath(path.strip())
    if cleaned.startswith("/"):
        raise DocumentError(
            "path_outside_repo",
            f"referenced doc '{path}' is outside the repo",
        )
    if cleaned.startswith("..") or "/../" in f"/{cleaned}/" or cleaned == "..":
        raise DocumentError(
            "path_outside_repo",
            f"referenced doc '{path}' is outside the repo",
        )
    return cleaned


def sniff_media_type(name: str, head: bytes) -> str:
    """Classify media type from extension + magic bytes.

    Raises ``DocumentError`` on extension/magic mismatch.
    """
    ext_type, _ = mimetypes.guess_type(name)
    if ext_type is None:
        ext_type = "application/octet-stream"
    magic_type: str | None = None
    for sig, offset, mtype in _MAGIC_TABLE:
        if len(head) >= offset + len(sig) and head[offset : offset + len(sig)] == sig:
            if mtype == "image/webp":
                if len(head) >= 12 and head[8:12] == b"WEBP":
                    magic_type = mtype
            else:
                magic_type = mtype
            break

    if magic_type is not None and ext_type != "application/octet-stream":
        if magic_type != ext_type:
            raise DocumentError(
                "unsupported_document_type",
                f"'{name}' extension suggests {ext_type} but content is {magic_type}",
            )
    if magic_type is not None:
        return magic_type
    return ext_type


def media_is_binary(media: str) -> bool:
    if media.startswith("text/"):
        return False
    if media in ("application/json", "application/yaml"):
        return False
    return True


def media_allowed(media: str, allowed: Sequence[str]) -> bool:
    for pattern in allowed:
        if pattern == media:
            return True
        if pattern.endswith("/*"):
            prefix = pattern[:-1]
            if media.startswith(prefix):
                return True
    return False


def effective_document_cap(document_cap_bytes: int) -> int:
    return min(document_cap_bytes, DOCUMENT_CAP_CEILING_BYTES)


def extension_for_media(media: str, fallback_name: str) -> str:
    """Derive a file extension from media type."""
    _KNOWN: dict[str, str] = {
        "text/markdown": ".md",
        "text/plain": ".txt",
        "text/x-python": ".py",
        "application/json": ".json",
        "application/yaml": ".yaml",
        "application/pdf": ".pdf",
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }
    if media in _KNOWN:
        return _KNOWN[media]
    if media != "application/octet-stream":
        ext = mimetypes.guess_extension(media)
        if ext:
            return ext
    _, dot_ext = posixpath.splitext(fallback_name)
    return dot_ext or ".bin"


# ---------------------------------------------------------------------------
# Fence line extraction (block-list continuation for docs/attachments)
# ---------------------------------------------------------------------------


def read_fence_lines_for_key(text: str, key: str) -> list[str] | None:
    """Extract indented continuation lines under ``<key>:`` in a frontmatter
    fence.

    Returns the raw indented lines (``  - path`` etc.) if ``key:`` appears with
    an empty value and is followed by a block list; ``None`` if not found or
    the value is inline. Used to parse the YAML-ish block form that
    :func:`split_frontmatter` collapses to an empty scalar.
    """
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    fence = parts[1].splitlines()
    prefix = f"{key}:"
    for i, line in enumerate(fence):
        stripped = line.strip()
        if not stripped.startswith(prefix):
            continue
        after = line.split(":", 1)[1].strip()
        if after:
            return None
        following: list[str] = []
        for j in range(i + 1, len(fence)):
            fl = fence[j]
            if not fl.strip():
                continue
            if fl[:1] not in (" ", "\t"):
                break
            following.append(fl)
        return following
    return None


# ---------------------------------------------------------------------------
# Phase 2 — dispatch-time resolution
# ---------------------------------------------------------------------------


class DocsBlocked(Exception):
    """Authoring/environment error surfaced during :func:`resolve_task_docs`.

    Raised out of ``build_work_order`` so ``_lease_and_build`` can put the
    task on ``TaskHoldKind.BLOCKED`` without ever issuing a lease.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class DocsResolveResult:
    """Outcome of :func:`resolve_task_docs`.

    * ``entries`` — pin-only work-order entries (never content on the wire)
    * ``pin`` — the full ``docs_pin`` map to persist (key -> PinRecord)
    * ``pin_dirty`` — the pin differs from what was in frontmatter
    * ``blocked_reason`` — set when the task must go BLOCKED (entries and pin
      are ignored by the caller in that case)
    """

    entries: tuple[dict, ...]
    pin: dict[str, PinRecord]
    pin_dirty: bool
    blocked_reason: str | None


def _label_from_spec(spec: DocSpec, *, is_attach: bool) -> str:
    """Derive the operator-facing label for the prompt header (spec §3.2)."""
    if spec.as_:
        return f"the {spec.as_.strip()}" if not spec.as_.strip().lower().startswith(
            "the "
        ) else spec.as_.strip()
    name = spec.path.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0] if "." in name else name
    prefix = "" if is_attach else ""
    label_stem = stem.replace("_", " ").replace("-", " ")
    return f"the {prefix}{label_stem}".strip()


def _entry_name(spec: DocSpec, *, is_attach: bool) -> str:
    """The scratch-file name basis (real ext preserved by materialize_docs)."""
    return spec.path.rsplit("/", 1)[-1]


def _apply_step_filter(
    spec: DocSpec, workflow_step_id: str | None
) -> bool:
    """True if ``spec`` should be emitted for this workflow step (or non-workflow)."""
    if not spec.steps:
        return True
    if not workflow_step_id:
        return True
    return workflow_step_id in spec.steps


def _cat_blob_size(git: object, sha: str) -> int | None:
    """``git cat-file -s <sha>`` — bytes size, or None if unreachable."""
    from nightshift.git.runner import GitRunner  # local import to keep top clean

    assert isinstance(git, GitRunner)
    result = git.run("cat-file", "-s", sha)
    if not result.ok:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def _cat_blob_bytes(git: object, sha: str) -> bytes | None:
    from nightshift.git.runner import GitRunner

    assert isinstance(git, GitRunner)
    rc, data = git.run_bytes("cat-file", "blob", sha)
    if rc != 0:
        return None
    return data


def _rev_parse_blob(git: object, ref: str, path: str) -> str | None:
    """``git rev-parse <ref>:<path>`` — blob sha, or None on failure."""
    from nightshift.git.runner import GitRunner

    assert isinstance(git, GitRunner)
    out = git.out("rev-parse", f"{ref}:{path}")
    return out or None


def _over_cap_message(
    path: str, size: int, cap: int, *, binary: bool, kind: str
) -> str:
    """Assemble the operator-facing over-cap message (spec §5.1 / §7)."""
    n_kb = size // 1024
    cap_kb = cap // 1024
    if binary:
        return (
            f"referenced doc '{path}' is {n_kb} KB, over the document cap "
            f"({cap_kb} KB) — reduce the image or raise document_cap_bytes"
        )
    return (
        f"referenced doc '{path}' is {n_kb} KB, over the document cap "
        f"({cap_kb} KB) — declare a range (text) or raise document_cap_bytes"
    )


def entries_from_pin(
    meta: dict,
    task: str,
    *,
    workflow_step_id: str | None = None,
    task_file_text: str | None = None,
) -> list[dict]:
    """Reconstruct pin-only work-order entries from persisted ``docs_pin`` +
    ``docs``/``attachments`` frontmatter, without git access.

    Used by :mod:`nightshift.resolve_runner` so a resolve attempt sees the
    same reference docs the original attempt did (spec §3.2 / §8), without
    needing a ``base_ref`` or a full re-resolution pass.
    """
    if "docs" not in meta and "attachments" not in meta:
        return []
    try:
        pin = parse_docs_pin(meta.get("docs_pin"))
    except Exception:
        pin = {}
    if not pin:
        return []
    docs_fence = (
        read_fence_lines_for_key(task_file_text, "docs")
        if task_file_text is not None else None
    )
    att_fence = (
        read_fence_lines_for_key(task_file_text, "attachments")
        if task_file_text is not None else None
    )
    try:
        docs_specs = (
            parse_docs_field(meta.get("docs"), fence_lines=docs_fence)
            if "docs" in meta else []
        )
        att_specs = (
            parse_attachments_field(
                meta.get("attachments"), fence_lines=att_fence
            )
            if "attachments" in meta else []
        )
    except DocumentError:
        return []
    entries: list[dict] = []
    for spec in docs_specs:
        try:
            path = normalize_repo_path(spec.path)
        except DocumentError:
            continue
        record = pin.get(path)
        if record is None:
            continue
        if not _apply_step_filter(spec, workflow_step_id):
            continue
        binary = media_is_binary(record.media)
        entry: dict = {
            "name": _entry_name(spec, is_attach=False),
            "label": _label_from_spec(spec, is_attach=False),
            "kind": "path",
            "source": "target",
            "media": record.media,
            "path": path,
            "ref": "base_ref",
            "sha": record.sha,
            "bytes": record.bytes,
        }
        if spec.range and not binary:
            entry["range"] = spec.range
        entries.append(entry)
    for spec in att_specs:
        name = spec.path.rsplit("/", 1)[-1]
        record = pin.get(f"attach:{name}")
        if record is None:
            continue
        if not _apply_step_filter(spec, workflow_step_id):
            continue
        binary = media_is_binary(record.media)
        entry = {
            "name": name,
            "label": _label_from_spec(spec, is_attach=True),
            "kind": "attach",
            "source": "tasks",
            "media": record.media,
            "blob_ref": f"{task}.docs/{name}",
            "sha": record.sha,
            "bytes": record.bytes,
        }
        if spec.range and not binary:
            entry["range"] = spec.range
        entries.append(entry)
    return entries


def resolve_task_docs(
    *,
    workspace: Path,
    tasks_root: Path,
    task: str,
    queue: str | None,
    repo: str | None,
    base_ref: str | None,
    meta: dict,
    merged_config: dict,
    workflow_step_id: str | None,
    task_file_text: str | None = None,
) -> DocsResolveResult:
    """Resolve a task's ``docs:`` / ``attachments:`` frontmatter into pin-only
    work-order entries + a full ``docs_pin`` map to persist.

    The **no-docs** path is byte-identical: when neither key is set (nor
    exposed as an empty scalar with a block-list continuation), returns an
    empty result with ``pin_dirty=False`` — callers must not touch the config
    blob or emit a ``docs_pin`` change.

    See :class:`DocsResolveResult` for the shape. On authoring/environment
    errors ``blocked_reason`` is set and the caller must raise
    :class:`DocsBlocked` (or hold the task itself) — no entries are returned.
    """
    from nightshift.git.runner import GitRunner

    has_docs_key = "docs" in meta
    has_att_key = "attachments" in meta
    # Even when split_frontmatter collapses to "" the key is present.
    if not has_docs_key and not has_att_key:
        return DocsResolveResult(
            entries=(), pin={}, pin_dirty=False, blocked_reason=None
        )

    # Block-list continuation: split_frontmatter returns "" for `docs:` with
    # only indented `- path` lines below. Recover those lines from the raw
    # task file text.
    docs_fence: list[str] | None = None
    att_fence: list[str] | None = None
    if task_file_text is not None:
        if meta.get("docs") in ("", None) and has_docs_key:
            docs_fence = read_fence_lines_for_key(task_file_text, "docs")
        if meta.get("attachments") in ("", None) and has_att_key:
            att_fence = read_fence_lines_for_key(task_file_text, "attachments")

    try:
        docs_specs = (
            parse_docs_field(meta.get("docs"), fence_lines=docs_fence)
            if has_docs_key
            else []
        )
        att_specs = (
            parse_attachments_field(
                meta.get("attachments"), fence_lines=att_fence
            )
            if has_att_key
            else []
        )
    except DocumentError as exc:
        return DocsResolveResult(
            entries=(), pin={}, pin_dirty=False, blocked_reason=exc.message
        )

    if not docs_specs and not att_specs:
        return DocsResolveResult(
            entries=(), pin={}, pin_dirty=False, blocked_reason=None
        )

    try:
        existing_pin = parse_docs_pin(meta.get("docs_pin"))
    except Exception:
        existing_pin = {}

    cap_setting = int(merged_config.get(
        "document_cap_bytes", DEFAULT_DOCUMENT_CAP_BYTES
    ))
    cap = effective_document_cap(cap_setting)
    budget = int(merged_config.get(
        "document_budget_bytes", DEFAULT_DOCUMENT_BUDGET_BYTES
    ))
    allowed_raw = merged_config.get(
        "allowed_doc_media_types", DEFAULT_ALLOWED_DOC_MEDIA_TYPES
    )
    allowed: tuple[str, ...] = (
        tuple(allowed_raw) if isinstance(allowed_raw, (list, tuple))
        else DEFAULT_ALLOWED_DOC_MEDIA_TYPES
    )

    target_git: GitRunner | None = None
    if repo is not None:
        target_git = GitRunner((workspace / repo).resolve())
    tasks_git = GitRunner(tasks_root.resolve())

    entries: list[dict] = []
    new_pin: dict[str, PinRecord] = {}
    pin_dirty = False
    total_bytes = 0

    from nightshift import playlists as _playlists

    tasks_rel = _playlists.tasks_rel(queue)

    # --- path docs ------------------------------------------------------- #
    for spec in docs_specs:
        try:
            path = normalize_repo_path(spec.path)
        except DocumentError as exc:
            return DocsResolveResult(
                entries=(), pin={}, pin_dirty=False, blocked_reason=exc.message
            )
        if target_git is None or base_ref is None:
            return DocsResolveResult(
                entries=(), pin={}, pin_dirty=False,
                blocked_reason=f"referenced doc '{path}' has no target repo",
            )
        pin_key = path
        prior = existing_pin.get(pin_key)
        sha: str | None = None
        size: int | None = None
        media: str | None = None

        if prior is not None:
            reachable_size = _cat_blob_size(target_git, prior.sha)
            if reachable_size is not None:
                sha = prior.sha
                size = reachable_size
                media = prior.media
        if sha is None:
            resolved_sha = _rev_parse_blob(target_git, base_ref, path)
            if resolved_sha is None:
                return DocsResolveResult(
                    entries=(), pin={}, pin_dirty=False,
                    blocked_reason=(
                        f"referenced doc '{path}' not found at base_ref"
                    ),
                )
            sha = resolved_sha
            resolved_size = _cat_blob_size(target_git, sha)
            if resolved_size is None:
                return DocsResolveResult(
                    entries=(), pin={}, pin_dirty=False,
                    blocked_reason=(
                        f"referenced doc '{path}' not found at base_ref"
                    ),
                )
            size = resolved_size
            data = _cat_blob_bytes(target_git, sha)
            if data is None:
                return DocsResolveResult(
                    entries=(), pin={}, pin_dirty=False,
                    blocked_reason=(
                        f"referenced doc '{path}' not found at base_ref"
                    ),
                )
            try:
                media = sniff_media_type(path, data[:64])
            except DocumentError as exc:
                return DocsResolveResult(
                    entries=(), pin={}, pin_dirty=False,
                    blocked_reason=exc.message,
                )
            if prior is None or prior.sha != sha or prior.media != media \
                    or prior.bytes != size:
                pin_dirty = True

        assert sha is not None and size is not None and media is not None

        if not media_allowed(media, allowed):
            return DocsResolveResult(
                entries=(), pin={}, pin_dirty=False,
                blocked_reason=(
                    f"unsupported document type '{media}' for '{path}'"
                ),
            )
        binary = media_is_binary(media)
        if spec.range and binary:
            return DocsResolveResult(
                entries=(), pin={}, pin_dirty=False,
                blocked_reason=(
                    f"range: is not valid on binary document '{path}' "
                    f"({media})"
                ),
            )
        if size > cap:
            return DocsResolveResult(
                entries=(), pin={}, pin_dirty=False,
                blocked_reason=_over_cap_message(
                    path, size, cap, binary=binary, kind="path"
                ),
            )

        # Pin is recorded regardless of ``steps:`` filter (spec §7 —
        # workflow steps share byte-identical pins).
        new_pin[pin_key] = PinRecord(sha=sha, media=media, bytes=size)
        if not _apply_step_filter(spec, workflow_step_id):
            continue
        total_bytes += size
        entry: dict = {
            "name": _entry_name(spec, is_attach=False),
            "label": _label_from_spec(spec, is_attach=False),
            "kind": "path",
            "source": "target",
            "media": media,
            "path": path,
            "ref": "base_ref",
            "sha": sha,
            "bytes": size,
        }
        if spec.range:
            entry["range"] = spec.range
        entries.append(entry)

    # --- attachments ----------------------------------------------------- #
    for spec in att_specs:
        name = spec.path.rsplit("/", 1)[-1]
        try:
            normalize_repo_path(name)
        except DocumentError as exc:
            return DocsResolveResult(
                entries=(), pin={}, pin_dirty=False, blocked_reason=exc.message
            )
        pin_key = f"attach:{name}"
        rel_blob = f"{tasks_rel}/{task}.docs/{name}"
        prior = existing_pin.get(pin_key)
        sha: str | None = None
        size: int | None = None
        media: str | None = None

        if prior is not None:
            reachable_size = _cat_blob_size(tasks_git, prior.sha)
            if reachable_size is not None:
                sha = prior.sha
                size = reachable_size
                media = prior.media
        if sha is None:
            resolved_sha = _rev_parse_blob(tasks_git, "HEAD", rel_blob)
            if resolved_sha is None:
                return DocsResolveResult(
                    entries=(), pin={}, pin_dirty=False,
                    blocked_reason=(
                        f"attached document '{name}' is missing from the task"
                    ),
                )
            sha = resolved_sha
            resolved_size = _cat_blob_size(tasks_git, sha)
            if resolved_size is None:
                return DocsResolveResult(
                    entries=(), pin={}, pin_dirty=False,
                    blocked_reason=(
                        f"attached document '{name}' is missing from the task"
                    ),
                )
            size = resolved_size
            data = _cat_blob_bytes(tasks_git, sha)
            if data is None:
                return DocsResolveResult(
                    entries=(), pin={}, pin_dirty=False,
                    blocked_reason=(
                        f"attached document '{name}' is missing from the task"
                    ),
                )
            try:
                media = sniff_media_type(name, data[:64])
            except DocumentError as exc:
                return DocsResolveResult(
                    entries=(), pin={}, pin_dirty=False,
                    blocked_reason=exc.message,
                )
            if prior is None or prior.sha != sha or prior.media != media \
                    or prior.bytes != size:
                pin_dirty = True

        assert sha is not None and size is not None and media is not None

        if not media_allowed(media, allowed):
            return DocsResolveResult(
                entries=(), pin={}, pin_dirty=False,
                blocked_reason=(
                    f"unsupported document type '{media}' for '{name}'"
                ),
            )
        binary = media_is_binary(media)
        if spec.range and binary:
            return DocsResolveResult(
                entries=(), pin={}, pin_dirty=False,
                blocked_reason=(
                    f"range: is not valid on binary document '{name}' "
                    f"({media})"
                ),
            )
        if size > cap:
            return DocsResolveResult(
                entries=(), pin={}, pin_dirty=False,
                blocked_reason=_over_cap_message(
                    name, size, cap, binary=binary, kind="attach"
                ),
            )

        new_pin[pin_key] = PinRecord(sha=sha, media=media, bytes=size)
        if not _apply_step_filter(spec, workflow_step_id):
            continue
        total_bytes += size
        entry = {
            "name": name,
            "label": _label_from_spec(spec, is_attach=True),
            "kind": "attach",
            "source": "tasks",
            "media": media,
            "blob_ref": f"{task}.docs/{name}",
            "sha": sha,
            "bytes": size,
        }
        if spec.range:
            entry["range"] = spec.range
        entries.append(entry)

    # Any pin key present in the old map that we didn't touch (because the
    # doc was removed from frontmatter) should not be carried forward.
    if set(new_pin) != set(existing_pin):
        pin_dirty = True

    if total_bytes > budget:
        return DocsResolveResult(
            entries=(), pin={}, pin_dirty=False,
            blocked_reason=(
                f"total document bytes {total_bytes} exceeds budget {budget}"
            ),
        )

    return DocsResolveResult(
        entries=tuple(entries),
        pin=new_pin,
        pin_dirty=pin_dirty,
        blocked_reason=None,
    )
