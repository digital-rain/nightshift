"""Document resolution helpers — parse, normalize, sniff, path guards, cap/allow-list predicates.

Pure helpers only (no git, no HTTP). Used by ``task_files`` for attachment CRUD
and ``materialize_docs``, and by ``work_orders`` (Phase 2) for dispatch-time
resolution.
"""

from __future__ import annotations

import json
import mimetypes
import posixpath
from collections.abc import Sequence
from dataclasses import dataclass


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
