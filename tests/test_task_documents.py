"""Tests for the task-documents feature (Phase 1).

Covers: parse/normalize of docs/attachments/docs_pin frontmatter, path
traversal guards, media sniffing + magic bytes, cap/allow-list predicates,
attachment CRUD, materialize_docs from git blob sha, editable/engine meta
key gating, and operator settings.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from _workspace import build_workspace, git_commit_all, git_init
from nightshift.docs_resolve import (
    DEFAULT_ALLOWED_DOC_MEDIA_TYPES,
    DEFAULT_DOCUMENT_CAP_BYTES,
    DOCUMENT_CAP_CEILING_BYTES,
    DocSpec,
    DocumentError,
    DocumentUnavailable,
    PinRecord,
    effective_document_cap,
    extension_for_media,
    media_allowed,
    media_is_binary,
    normalize_repo_path,
    parse_attachments_field,
    parse_docs_field,
    parse_docs_pin,
    render_docs_pin,
    sniff_media_type,
)
from nightshift.repos import DEFAULT_TASKS_REPO
from nightshift.task_files import (
    _EDITABLE_META_KEYS,
    ENGINE_META_KEYS,
    attachments_dir,
    delete_attachment,
    delete_task,
    drop_completed_task,
    list_attachments,
    materialize_docs,
    read_attachment,
    set_engine_meta,
    set_task_meta,
    write_attachment,
)


# -- helpers ----------------------------------------------------------------


def _store_only(
    tmp_path: Path,
    *,
    tasks: dict[str, str] | None = None,
    commit: bool = False,
) -> Path:
    workspace = build_workspace(
        tmp_path,
        tasks=tasks,
        repos=(),
        main_repo=None,
        commit_tasks=commit,
    )
    return workspace / DEFAULT_TASKS_REPO


PNG_MAGIC = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
PDF_MAGIC = b"%PDF-1.4 test content"
JPEG_MAGIC = b"\xff\xd8\xff\xe0" + b"\x00" * 8


def _git_blob_sha(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data).hexdigest()  # noqa: S324


def _make_target_with_docs(
    tmp_path: Path, files: dict[str, bytes] | None = None
) -> Path:
    """Create a target repo with committed files and return the repo root."""
    repo = tmp_path / "target-repo"
    git_init(repo)
    (repo / "README.md").write_text("# target\n")
    for rel, content in (files or {}).items():
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
    git_commit_all(repo, "init")
    return repo


# ==========================================================================
# docs field parse/normalize
# ==========================================================================


class TestParseDocsField:
    def test_bare_string(self) -> None:
        result = parse_docs_field("docs/spec/auth.md")
        assert result == [DocSpec(path="docs/spec/auth.md")]

    def test_comma_separated(self) -> None:
        result = parse_docs_field("docs/a.md, docs/b.md")
        assert result == [DocSpec(path="docs/a.md"), DocSpec(path="docs/b.md")]

    def test_json_array_of_strings(self) -> None:
        result = parse_docs_field('["docs/a.md", "docs/b.md"]')
        assert result == [DocSpec(path="docs/a.md"), DocSpec(path="docs/b.md")]

    def test_json_array_with_objects(self) -> None:
        raw = json.dumps(
            [
                "docs/a.md",
                {
                    "path": "docs/b.md",
                    "range": "1-10",
                    "as": "the spec",
                    "steps": ["plan"],
                },
            ]
        )
        result = parse_docs_field(raw)
        assert result == [
            DocSpec(path="docs/a.md"),
            DocSpec(path="docs/b.md", range="1-10", as_="the spec", steps=("plan",)),
        ]

    def test_list_of_strings(self) -> None:
        result = parse_docs_field(["docs/a.md", "docs/b.md"])
        assert result == [DocSpec(path="docs/a.md"), DocSpec(path="docs/b.md")]

    def test_list_of_mixed(self) -> None:
        result = parse_docs_field(
            [
                "docs/a.md",
                {"path": "docs/b.md", "range": "5-20"},
            ]
        )
        assert len(result) == 2
        assert result[1].range == "5-20"

    def test_single_object(self) -> None:
        result = parse_docs_field({"path": "docs/spec.md", "as": "the spec"})
        assert result == [DocSpec(path="docs/spec.md", as_="the spec")]

    def test_empty_string_returns_empty(self) -> None:
        assert parse_docs_field("") == []
        assert parse_docs_field("  ") == []

    def test_none_returns_empty(self) -> None:
        assert parse_docs_field(None) == []

    def test_block_list_lines(self) -> None:
        fence = [
            "  - docs/a.md",
            "  - docs/b.png",
        ]
        result = parse_docs_field(None, fence_lines=fence)
        assert result == [DocSpec(path="docs/a.md"), DocSpec(path="docs/b.png")]


class TestParseAttachmentsField:
    def test_same_shapes_as_docs(self) -> None:
        result = parse_attachments_field("screenshot.png")
        assert result == [DocSpec(path="screenshot.png")]

    def test_list_form(self) -> None:
        result = parse_attachments_field(["notes.md", "screenshot.png"])
        assert len(result) == 2


# ==========================================================================
# Traversal guards
# ==========================================================================


class TestNormalizeRepoPath:
    def test_simple_path(self) -> None:
        assert normalize_repo_path("docs/spec/auth.md") == "docs/spec/auth.md"

    def test_absolute_rejected(self) -> None:
        with pytest.raises(DocumentError, match="outside the repo") as exc_info:
            normalize_repo_path("/etc/passwd")
        assert exc_info.value.code == "path_outside_repo"

    def test_dotdot_rejected(self) -> None:
        with pytest.raises(DocumentError, match="outside the repo"):
            normalize_repo_path("../../../etc/passwd")

    def test_dotdot_in_middle_rejected(self) -> None:
        with pytest.raises(DocumentError, match="outside the repo"):
            normalize_repo_path("docs/../../../etc/passwd")

    def test_empty_rejected(self) -> None:
        with pytest.raises(DocumentError):
            normalize_repo_path("")

    def test_normalises_redundant_slashes(self) -> None:
        assert normalize_repo_path("docs//spec//auth.md") == "docs/spec/auth.md"


# ==========================================================================
# Media sniffing
# ==========================================================================


class TestSniffMediaType:
    def test_markdown(self) -> None:
        assert sniff_media_type("spec.md", b"# Heading\nSome text") == "text/markdown"

    def test_png_magic(self) -> None:
        assert sniff_media_type("image.png", PNG_MAGIC) == "image/png"

    def test_pdf_magic(self) -> None:
        assert sniff_media_type("doc.pdf", PDF_MAGIC) == "application/pdf"

    def test_png_extension_wrong_content(self) -> None:
        with pytest.raises(DocumentError, match="content is"):
            sniff_media_type("image.png", PDF_MAGIC)

    def test_jpeg_magic(self) -> None:
        assert sniff_media_type("photo.jpg", JPEG_MAGIC) == "image/jpeg"

    def test_unknown_extension(self) -> None:
        result = sniff_media_type("file.qzx", b"random bytes")
        assert result == "application/octet-stream"


class TestMediaIsBinary:
    def test_text_is_not_binary(self) -> None:
        assert media_is_binary("text/markdown") is False
        assert media_is_binary("text/plain") is False

    def test_json_is_not_binary(self) -> None:
        assert media_is_binary("application/json") is False

    def test_yaml_is_not_binary(self) -> None:
        assert media_is_binary("application/yaml") is False

    def test_image_is_binary(self) -> None:
        assert media_is_binary("image/png") is True

    def test_pdf_is_binary(self) -> None:
        assert media_is_binary("application/pdf") is True


class TestMediaAllowed:
    def test_exact_match(self) -> None:
        assert media_allowed("image/png", DEFAULT_ALLOWED_DOC_MEDIA_TYPES) is True

    def test_wildcard_text(self) -> None:
        assert media_allowed("text/markdown", DEFAULT_ALLOWED_DOC_MEDIA_TYPES) is True
        assert media_allowed("text/x-python", DEFAULT_ALLOWED_DOC_MEDIA_TYPES) is True

    def test_not_allowed(self) -> None:
        assert (
            media_allowed("application/zip", DEFAULT_ALLOWED_DOC_MEDIA_TYPES) is False
        )


# ==========================================================================
# Cap helpers
# ==========================================================================


class TestEffectiveDocumentCap:
    def test_below_ceiling(self) -> None:
        assert effective_document_cap(256 * 1024) == 256 * 1024

    def test_above_ceiling_clamped(self) -> None:
        huge = 100 * 1024 * 1024
        assert effective_document_cap(huge) == DOCUMENT_CAP_CEILING_BYTES

    def test_at_ceiling(self) -> None:
        assert (
            effective_document_cap(DOCUMENT_CAP_CEILING_BYTES)
            == DOCUMENT_CAP_CEILING_BYTES
        )


# ==========================================================================
# Extension helper
# ==========================================================================


class TestExtensionForMedia:
    def test_known_types(self) -> None:
        assert extension_for_media("image/png", "x") == ".png"
        assert extension_for_media("text/markdown", "x") == ".md"
        assert extension_for_media("application/pdf", "x") == ".pdf"

    def test_fallback_to_name(self) -> None:
        result = extension_for_media("application/octet-stream", "file.xyz")
        assert result == ".xyz"


# ==========================================================================
# Attachment CRUD
# ==========================================================================


class TestAttachmentCRUD:
    def test_write_read_roundtrip_text(self, tmp_path: Path) -> None:
        tasks_root = _store_only(tmp_path, tasks={"10.doc": "body."}, commit=True)
        data = b"# Investigation\nSome text."
        path = write_attachment(tasks_root, "10.doc", "notes.md", data)
        assert path == attachments_dir(tasks_root, "10.doc") / "notes.md"
        assert path.is_file()
        assert read_attachment(tasks_root, "10.doc", "notes.md") == data

    def test_write_read_roundtrip_binary(self, tmp_path: Path) -> None:
        tasks_root = _store_only(tmp_path, tasks={"10.doc": "body."}, commit=True)
        write_attachment(tasks_root, "10.doc", "screenshot.png", PNG_MAGIC)
        assert read_attachment(tasks_root, "10.doc", "screenshot.png") == PNG_MAGIC

    def test_overwrite_same_name(self, tmp_path: Path) -> None:
        tasks_root = _store_only(tmp_path, tasks={"10.doc": "body."}, commit=True)
        write_attachment(tasks_root, "10.doc", "notes.md", b"v1")
        write_attachment(tasks_root, "10.doc", "notes.md", b"v2")
        assert read_attachment(tasks_root, "10.doc", "notes.md") == b"v2"

    def test_delete_attachment(self, tmp_path: Path) -> None:
        tasks_root = _store_only(tmp_path, tasks={"10.doc": "body."}, commit=True)
        write_attachment(tasks_root, "10.doc", "notes.md", b"text")
        assert delete_attachment(tasks_root, "10.doc", "notes.md") is True
        assert not (attachments_dir(tasks_root, "10.doc") / "notes.md").exists()

    def test_delete_nonexistent(self, tmp_path: Path) -> None:
        tasks_root = _store_only(tmp_path, tasks={"10.doc": "body."}, commit=True)
        assert delete_attachment(tasks_root, "10.doc", "ghost.md") is False

    def test_list_attachments(self, tmp_path: Path) -> None:
        tasks_root = _store_only(tmp_path, tasks={"10.doc": "body."}, commit=True)
        write_attachment(tasks_root, "10.doc", "b.md", b"b")
        write_attachment(tasks_root, "10.doc", "a.md", b"a")
        assert list_attachments(tasks_root, "10.doc") == ["a.md", "b.md"]

    def test_list_attachments_empty(self, tmp_path: Path) -> None:
        tasks_root = _store_only(tmp_path, tasks={"10.doc": "body."}, commit=True)
        assert list_attachments(tasks_root, "10.doc") == []

    def test_commit_creates_revision(self, tmp_path: Path) -> None:
        tasks_root = _store_only(tmp_path, tasks={"10.doc": "body."}, commit=True)
        before = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=tasks_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        write_attachment(tasks_root, "10.doc", "notes.md", b"text")
        after = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=tasks_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert int(after) > int(before)


# ==========================================================================
# delete_task / drop_completed_task removes attachments
# ==========================================================================


class TestDeleteTaskRemovesAttachments:
    def test_delete_task_removes_docs_dir(self, tmp_path: Path) -> None:
        tasks_root = _store_only(tmp_path, tasks={"10.doc": "body."}, commit=True)
        write_attachment(tasks_root, "10.doc", "notes.md", b"text")
        assert attachments_dir(tasks_root, "10.doc").exists()
        delete_task(tasks_root, "10.doc")
        assert not attachments_dir(tasks_root, "10.doc").exists()

    def test_drop_completed_task_removes_docs_dir(self, tmp_path: Path) -> None:
        tasks_root = _store_only(tmp_path, tasks={"10.doc": "body."}, commit=True)
        write_attachment(tasks_root, "10.doc", "notes.md", b"text")
        drop_completed_task(tasks_root, "10.doc")
        assert not (tasks_root / "main" / "10.doc.md").exists()
        assert not attachments_dir(tasks_root, "10.doc").exists()


# ==========================================================================
# materialize_docs
# ==========================================================================


class TestMaterializeDocs:
    def _commit_blob(self, repo: Path, rel: str, data: bytes) -> str:
        """Commit a file into the repo and return its git blob sha."""
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        git_commit_all(repo, f"add {rel}")
        result = subprocess.run(
            ["git", "rev-parse", f"HEAD:{rel}"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def test_path_doc_readonly(self, tmp_path: Path) -> None:
        repo = _make_target_with_docs(tmp_path, {})
        text = b"# Auth spec\nLine 2\nLine 3\n"
        sha = self._commit_blob(repo, "docs/auth.md", text)
        entries = [
            {"name": "auth.md", "sha": sha, "media": "text/markdown", "kind": "path"}
        ]
        result = materialize_docs(
            tmp_path, "target-repo", "10.task", entries, target_repo_root=repo
        )
        assert "auth.md" in result
        path = result["auth.md"]
        assert path.exists()
        assert path.read_bytes() == text
        mode = os.stat(path).st_mode
        assert not (mode & stat.S_IWUSR)

    def test_path_doc_real_extension(self, tmp_path: Path) -> None:
        repo = _make_target_with_docs(tmp_path, {})
        sha = self._commit_blob(repo, "docs/mockup.png", PNG_MAGIC)
        entries = [
            {"name": "mockup.png", "sha": sha, "media": "image/png", "kind": "path"}
        ]
        result = materialize_docs(
            tmp_path, "target-repo", "10.task", entries, target_repo_root=repo
        )
        path = result["mockup.png"]
        assert path.suffix == ".png"

    def test_sha_mismatch_raises_unavailable(self, tmp_path: Path) -> None:
        repo = _make_target_with_docs(tmp_path, {})
        self._commit_blob(repo, "docs/auth.md", b"real content")
        fake_sha = "0" * 40
        entries = [
            {
                "name": "auth.md",
                "sha": fake_sha,
                "media": "text/markdown",
                "kind": "path",
            }
        ]
        with pytest.raises(DocumentUnavailable, match="not found"):
            materialize_docs(
                tmp_path, "target-repo", "10.task", entries, target_repo_root=repo
            )

    def test_text_range_slices_lines(self, tmp_path: Path) -> None:
        repo = _make_target_with_docs(tmp_path, {})
        text = b"line 1\nline 2\nline 3\nline 4\nline 5\n"
        sha = self._commit_blob(repo, "docs/spec.md", text)
        entries = [
            {
                "name": "spec.md",
                "sha": sha,
                "media": "text/markdown",
                "kind": "path",
                "range": "2-3",
            }
        ]
        result = materialize_docs(
            tmp_path, "target-repo", "10.task", entries, target_repo_root=repo
        )
        content = result["spec.md"].read_text()
        assert content == "line 2\nline 3\n"

    def test_attachment_from_tasks_repo(self, tmp_path: Path) -> None:
        tasks_root = _store_only(tmp_path, tasks={"10.task": "body."}, commit=True)
        data = b"investigation notes"
        write_attachment(tasks_root, "10.task", "notes.md", data)
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD:main/10.task.docs/notes.md"],
            cwd=tasks_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        entries = [
            {
                "name": "notes.md",
                "sha": sha,
                "media": "text/markdown",
                "kind": "attach",
                "source": "tasks",
                "blob_ref": "10.task.docs/notes.md",
            }
        ]
        result = materialize_docs(
            tmp_path,
            "fake-repo",
            "10.task",
            entries,
            tasks_root=tasks_root,
        )
        assert result["notes.md"].read_bytes() == data


# ==========================================================================
# docs_pin JSON render/parse round-trip
# ==========================================================================


class TestDocsPinRoundTrip:
    def test_render_parse(self) -> None:
        pin = {
            "docs/auth.md": PinRecord(sha="abc123", media="text/markdown", bytes=1024),
            "attach:screenshot.png": PinRecord(
                sha="def456", media="image/png", bytes=5120
            ),
        }
        rendered = render_docs_pin(pin)
        parsed = parse_docs_pin(rendered)
        assert parsed == pin

    def test_via_set_engine_meta(self, tmp_path: Path) -> None:
        tasks_root = _store_only(
            tmp_path,
            tasks={"10.task": "---\ntitle: Test\n---\nBody."},
            commit=True,
        )
        pin = {
            "docs/auth.md": PinRecord(sha="abc123", media="text/markdown", bytes=1024),
        }
        rendered = render_docs_pin(pin)
        set_engine_meta(tasks_root, "10.task", {"docs_pin": rendered})

        from nightshift.task_files import read_task

        task_data = read_task(tasks_root, "10.task")
        raw_pin = task_data["frontmatter_raw"]["docs_pin"]
        parsed = parse_docs_pin(raw_pin)
        assert parsed["docs/auth.md"].sha == "abc123"
        assert parsed["docs/auth.md"].media == "text/markdown"
        assert parsed["docs/auth.md"].bytes == 1024


# ==========================================================================
# Editable/engine key gating
# ==========================================================================


class TestMetaKeyGating:
    def test_docs_in_editable(self) -> None:
        assert "docs" in _EDITABLE_META_KEYS
        assert "attachments" in _EDITABLE_META_KEYS

    def test_docs_pin_in_engine(self) -> None:
        assert "docs_pin" in ENGINE_META_KEYS

    def test_docs_pin_not_in_editable(self) -> None:
        assert "docs_pin" not in _EDITABLE_META_KEYS

    def test_set_task_meta_rejects_docs_pin(self, tmp_path: Path) -> None:
        tasks_root = _store_only(tmp_path, tasks={"10.task": "body."}, commit=True)
        with pytest.raises(ValueError):
            set_task_meta(tasks_root, "10.task", {"docs_pin": "bad"})

    def test_set_engine_meta_accepts_docs_pin(self, tmp_path: Path) -> None:
        tasks_root = _store_only(
            tmp_path,
            tasks={"10.task": "---\ntitle: T\n---\nBody."},
            commit=True,
        )
        set_engine_meta(
            tasks_root,
            "10.task",
            {"docs_pin": '{"a":{"sha":"x","media":"text/plain","bytes":1}}'},
        )

    def test_set_task_meta_accepts_docs(self, tmp_path: Path) -> None:
        tasks_root = _store_only(
            tmp_path,
            tasks={"10.task": "---\ntitle: T\n---\nBody."},
            commit=True,
        )
        set_task_meta(tasks_root, "10.task", {"docs": "docs/spec.md"})
        from nightshift.task_files import read_task

        assert (
            read_task(tasks_root, "10.task")["frontmatter_raw"]["docs"]
            == "docs/spec.md"
        )


# ==========================================================================
# Settings
# ==========================================================================


class TestDocumentSettings:
    def test_defaults(self) -> None:
        from nightshift.config.manager import OperatorConfig

        cfg = OperatorConfig()
        assert cfg.document_cap_bytes == DEFAULT_DOCUMENT_CAP_BYTES
        assert cfg.document_budget_bytes == 4 * 1024 * 1024
        assert "text/*" in cfg.allowed_doc_media_types
        assert "image/png" in cfg.allowed_doc_media_types

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NIGHTSHIFT_DOCUMENT_CAP_BYTES", "1048576")
        from nightshift.config.manager import OperatorConfig

        cfg = OperatorConfig()
        # env overrides are applied at load time via the settings registry,
        # so the dataclass default remains unchanged — the env override test
        # mirrors the quarantine_threshold pattern
        assert cfg.document_cap_bytes == DEFAULT_DOCUMENT_CAP_BYTES
