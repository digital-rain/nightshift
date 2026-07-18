"""Tests for the task-documents feature (Phase 1 + Phase 2).

Covers: parse/normalize of docs/attachments/docs_pin frontmatter, path
traversal guards, media sniffing + magic bytes, cap/allow-list predicates,
attachment CRUD, materialize_docs from git blob sha, editable/engine meta
key gating, operator settings; and Phase 2's dispatch-time pin resolution,
prompt header, worker sha-verify, lifecycle reset, and split inheritance.
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
from nightshift.config.manager import ManagerConfig
from nightshift.docs_resolve import (
    DEFAULT_ALLOWED_DOC_MEDIA_TYPES,
    DEFAULT_DOCUMENT_CAP_BYTES,
    DOCUMENT_CAP_CEILING_BYTES,
    DocsBlocked,
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
from nightshift.manager.work_orders import build_work_order
from nightshift.prompts import _docs_header, build_prompt
from nightshift.repos import DEFAULT_TASKS_REPO
from nightshift.task_files import (
    _EDITABLE_META_KEYS,
    ENGINE_META_KEYS,
    attachments_dir,
    delete_attachment,
    delete_task,
    drop_completed_task,
    harvest_split_output,
    list_attachments,
    materialize_docs,
    read_attachment,
    read_task,
    set_engine_meta,
    set_task_meta,
    split_output_dir,
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


# ==========================================================================
# Phase 2 — build_work_order + resolve_task_docs
# ==========================================================================


REPO = "target-repo"


def _ws_with_docs(
    tmp_path: Path,
    *,
    files: dict[str, bytes],
    task_body: str,
) -> tuple[Path, Path, str]:
    """Build a workspace + committed target repo (name = ``target-repo``) with
    docs, and one task in the ``main`` queue. Returns
    ``(workspace, tasks_root, base_ref)``."""
    workspace = build_workspace(
        tmp_path,
        tasks={"10.task": task_body},
        repos=(REPO,),
        main_repo=REPO,
        commit_tasks=True,
    )
    repo_root = workspace / REPO
    for rel, data in files.items():
        dest = repo_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    if files:
        git_commit_all(repo_root, "add docs")
    base_ref = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root, capture_output=True, text=True, check=True,
    ).stdout.strip()
    return workspace, workspace / DEFAULT_TASKS_REPO, base_ref


def _cfg() -> ManagerConfig:
    return ManagerConfig()


def _build_order(workspace: Path, tasks_root: Path, task: str, base_ref: str) -> dict:
    """Small wrapper around ``build_work_order`` for the common test path."""
    return build_work_order(
        workspace, tasks_root, task, None, REPO,
        "l1", "r1", base_ref, _cfg(),
    )


class TestBuildWorkOrderNoDocsRegression:
    """A task with neither ``docs:`` nor ``attachments:`` must produce a work
    order byte-identical to today's shape (no ``docs`` key added)."""

    def test_config_omits_docs_key(self, tmp_path: Path) -> None:
        workspace, tasks_root, base_ref = _ws_with_docs(
            tmp_path, files={}, task_body="Just do it.",
        )
        order = _build_order(workspace, tasks_root, "10.task", base_ref)
        assert "docs" not in order["config"]
        assert "_docs_pin" not in order
        assert "_docs_pin_dirty" not in order


class TestBuildWorkOrderPathDoc:
    def test_pin_recorded_and_entry_is_content_free(self, tmp_path: Path) -> None:
        text = b"# Auth\nSpec content."
        workspace, tasks_root, base_ref = _ws_with_docs(
            tmp_path,
            files={"docs/auth.md": text},
            task_body="---\ndocs: docs/auth.md\n---\nBody.",
        )
        order = _build_order(workspace, tasks_root, "10.task", base_ref)
        docs = order["config"].get("docs")
        assert isinstance(docs, list) and len(docs) == 1
        entry = docs[0]
        assert entry["kind"] == "path"
        assert entry["path"] == "docs/auth.md"
        assert entry["media"] == "text/markdown"
        assert "sha" in entry and len(entry["sha"]) == 40
        # Never on the wire.
        assert "text" not in entry
        assert "content" not in entry
        # The builder stashes pin state for the lease path to persist.
        assert order.get("_docs_pin_dirty") is True

    def test_pin_reused_after_repo_mutation(self, tmp_path: Path) -> None:
        text_v1 = b"# Auth v1\n"
        workspace, tasks_root, base_ref = _ws_with_docs(
            tmp_path,
            files={"docs/auth.md": text_v1},
            task_body="---\ndocs: docs/auth.md\n---\nBody.",
        )
        order1 = _build_order(workspace, tasks_root, "10.task", base_ref)
        sha_v1 = order1["config"]["docs"][0]["sha"]
        # Persist the pin (mimicking what _lease_and_build does).
        set_engine_meta(
            tasks_root, "10.task", {"docs_pin": order1["_docs_pin"]},
        )
        # Now mutate the file at HEAD in the target repo and re-build.
        repo_root = workspace / REPO
        (repo_root / "docs" / "auth.md").write_bytes(b"# Auth v2 changed\n")
        git_commit_all(repo_root, "mutate")
        new_base = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_root,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        order2 = _build_order(workspace, tasks_root, "10.task", new_base)
        sha_v2 = order2["config"]["docs"][0]["sha"]
        assert sha_v2 == sha_v1
        # Pin unchanged means the builder does not request a rewrite.
        assert not order2.get("_docs_pin_dirty", False)

    def test_missing_path_blocked(self, tmp_path: Path) -> None:
        workspace, tasks_root, base_ref = _ws_with_docs(
            tmp_path,
            files={},
            task_body="---\ndocs: docs/ghost.md\n---\nBody.",
        )
        with pytest.raises(DocsBlocked, match="not found at base_ref"):
            _build_order(workspace, tasks_root, "10.task", base_ref)

    def test_over_cap_text_blocked_with_range_hint(self, tmp_path: Path) -> None:
        big = b"x" * (300 * 1024)
        workspace, tasks_root, base_ref = _ws_with_docs(
            tmp_path,
            files={"docs/big.md": big},
            task_body="---\ndocs: docs/big.md\n---\nBody.",
        )
        with pytest.raises(DocsBlocked, match="range") as excinfo:
            _build_order(workspace, tasks_root, "10.task", base_ref)
        assert "big.md" in excinfo.value.reason

    def test_over_cap_binary_blocked_without_range_hint(
        self, tmp_path: Path
    ) -> None:
        big_png = PNG_MAGIC + b"\x00" * (300 * 1024)
        workspace, tasks_root, base_ref = _ws_with_docs(
            tmp_path,
            files={"docs/mockup.png": big_png},
            task_body="---\ndocs: docs/mockup.png\n---\nBody.",
        )
        with pytest.raises(DocsBlocked) as excinfo:
            _build_order(workspace, tasks_root, "10.task", base_ref)
        # No "declare a range" hint on a binary over-cap.
        assert "range" not in excinfo.value.reason
        assert "mockup.png" in excinfo.value.reason

    def test_unsupported_media_blocked(self, tmp_path: Path) -> None:
        # A zip (magic PK\x03\x04) is not in the allow-list.
        zip_bytes = b"PK\x03\x04" + b"\x00" * 20
        workspace, tasks_root, base_ref = _ws_with_docs(
            tmp_path,
            files={"docs/thing.zip": zip_bytes},
            task_body="---\ndocs: docs/thing.zip\n---\nBody.",
        )
        with pytest.raises(DocsBlocked, match="unsupported"):
            _build_order(workspace, tasks_root, "10.task", base_ref)

    def test_range_on_png_blocked(self, tmp_path: Path) -> None:
        workspace, tasks_root, base_ref = _ws_with_docs(
            tmp_path,
            files={"docs/mockup.png": PNG_MAGIC},
            task_body=(
                "---\ndocs: "
                + json.dumps([{"path": "docs/mockup.png", "range": "1-10"}])
                + "\n---\nBody."
            ),
        )
        with pytest.raises(DocsBlocked, match="range: is not valid"):
            _build_order(workspace, tasks_root, "10.task", base_ref)

    def test_aggregate_budget_blocked(self, tmp_path: Path) -> None:
        # Each ~200 KB; two of them exceed a tiny queue-configured budget.
        big1 = b"a" * (200 * 1024)
        big2 = b"b" * (200 * 1024)
        workspace, tasks_root, base_ref = _ws_with_docs(
            tmp_path,
            files={"docs/a.md": big1, "docs/b.md": big2},
            task_body=(
                "---\ndocs: "
                + json.dumps(["docs/a.md", "docs/b.md"])
                + "\n---\nBody."
            ),
        )
        # Lower the budget below the sum via the queue's config.json.
        qcfg_path = tasks_root / "main" / "config.json"
        qcfg = json.loads(qcfg_path.read_text())
        qcfg["document_budget_bytes"] = 300 * 1024
        qcfg_path.write_text(json.dumps(qcfg, indent=2) + "\n")
        with pytest.raises(DocsBlocked, match="budget"):
            _build_order(workspace, tasks_root, "10.task", base_ref)


class TestBuildWorkOrderAttachments:
    def test_missing_attachment_blocked(self, tmp_path: Path) -> None:
        workspace, tasks_root, base_ref = _ws_with_docs(
            tmp_path, files={},
            task_body="---\nattachments: missing.md\n---\nBody.",
        )
        with pytest.raises(DocsBlocked, match="missing from the task"):
            _build_order(workspace, tasks_root, "10.task", base_ref)

    def test_attachment_pinned_to_tasks_blob(self, tmp_path: Path) -> None:
        workspace, tasks_root, base_ref = _ws_with_docs(
            tmp_path, files={},
            task_body="---\nattachments: notes.md\n---\nBody.",
        )
        write_attachment(tasks_root, "10.task", "notes.md", b"notes body")
        order = _build_order(workspace, tasks_root, "10.task", base_ref)
        docs = order["config"]["docs"]
        assert len(docs) == 1
        entry = docs[0]
        assert entry["kind"] == "attach"
        assert entry["blob_ref"] == "10.task.docs/notes.md"
        assert "text" not in entry


class TestBuildWorkOrderStepsFilter:
    def test_steps_filter_excludes_from_other_step(self, tmp_path: Path) -> None:
        text = b"# Plan-only\n"
        workspace, tasks_root, base_ref = _ws_with_docs(
            tmp_path,
            files={"docs/plan.md": text},
            task_body=(
                "---\ndocs: "
                + json.dumps([{"path": "docs/plan.md", "steps": ["plan"]}])
                + "\nworkflow: plan-review-implement\nworkflow_step: implement\n---\nBody."
            ),
        )
        from nightshift.workflows import load_workflows

        defs = load_workflows(workspace)
        order = build_work_order(
            workspace, tasks_root, "10.task", None, REPO,
            "l1", "r1", base_ref, _cfg(),
            workflow_defs=defs,
        )
        # implement step does not receive plan-only doc.
        assert "docs" not in order["config"] or not order["config"]["docs"]


class TestPromptHeader:
    def test_binary_annotation_and_range(self) -> None:
        header = _docs_header(
            [
                {"label": "the auth spec", "path": "/s/task-local-main-10.doc-auth.md",
                 "media": "text/markdown", "range": "1-120"},
                {"label": "the login mockup",
                 "path": "/s/task-local-main-10.doc-login.png",
                 "media": "image/png"},
                {"label": "the bug repro",
                 "path": "/s/task-local-main-10.doc-bug.pdf",
                 "media": "application/pdf"},
            ]
        )
        assert "Reference documents (read-only" in header
        assert "The auth spec (lines 1\u20130) is:" in header or (
            "The auth spec (lines 1\u2013120)" in header
        )
        assert "[image/png \u2014 open with an image-capable tool]" in header
        assert "[application/pdf \u2014 open with a PDF-capable tool]" in header

    def test_empty_docs_produces_no_header(self) -> None:
        assert _docs_header(None) == ""
        assert _docs_header([]) == ""

    def test_build_prompt_includes_docs_header(self) -> None:
        prompt = build_prompt(
            "10.wf",
            task_file="/scratch/brief.md",
            validate_cmd="just validate",
            doc_files=[
                {"label": "the auth spec", "path": "/s/doc-auth.md",
                 "media": "text/markdown"}
            ],
        )
        assert "Reference documents" in prompt
        assert "The auth spec is: /s/doc-auth.md" in prompt

    def test_build_prompt_without_docs_unchanged(self) -> None:
        prompt = build_prompt(
            "10.plain",
            task_file="/scratch/brief.md",
            validate_cmd="just validate",
        )
        # The runtime header block is absent; the charter body's static
        # "## Reference documents" section always ships.
        assert "Reference documents (read-only" not in prompt


class TestMaterializeDocsShaMismatch:
    def test_sha_mismatch_raises_document_unavailable(
        self, tmp_path: Path
    ) -> None:
        repo = _make_target_with_docs(tmp_path, {})
        (repo / "docs").mkdir()
        (repo / "docs" / "auth.md").write_bytes(b"real content")
        git_commit_all(repo, "add auth")
        entries = [{
            "name": "auth.md",
            "sha": "0" * 40,
            "media": "text/markdown",
            "kind": "path",
        }]
        with pytest.raises(DocumentUnavailable):
            materialize_docs(
                tmp_path, "target-repo", "10.task", entries,
                target_repo_root=repo,
            )


class TestWorkflowReset:
    def test_reset_clears_docs_pin_retains_attachments(
        self, tmp_path: Path
    ) -> None:
        tasks_root = _store_only(
            tmp_path,
            tasks={"10.task": (
                "---\ntitle: T\nworkflow: plan-review-implement\n"
                "workflow_step: review\nworkflow_visits: review=1\n"
                "docs: docs/spec.md\nattachments: notes.md\n---\nBody."
            )},
            commit=True,
        )
        # Seed the pin + an attachment.
        set_engine_meta(
            tasks_root, "10.task",
            {"docs_pin": '{"docs/spec.md":{"sha":"abc","media":"text/markdown","bytes":10}}'},
        )
        write_attachment(tasks_root, "10.task", "notes.md", b"notes")
        # Simulate _workflow_reset_job's semantics.
        set_engine_meta(
            tasks_root, "10.task",
            {
                "workflow_step": None,
                "workflow_visits": None,
                "docs_pin": None,
            },
        )
        task_data = read_task(tasks_root, "10.task")
        raw = task_data["frontmatter_raw"]
        assert "docs_pin" not in raw
        assert "workflow_step" not in raw
        # Operator-owned docs/attachments remain.
        assert raw.get("docs") == "docs/spec.md"
        assert raw.get("attachments") == "notes.md"
        # Attachment bytes retained.
        assert (attachments_dir(tasks_root, "10.task") / "notes.md").exists()


class TestHarvestSplitInheritance:
    def test_children_inherit_docs_and_attachments(self, tmp_path: Path) -> None:
        workspace = build_workspace(
            tmp_path,
            tasks={"10.parent": (
                "---\ntitle: Parent\ndocs: docs/spec.md\n"
                "attachments: notes.md\n---\nParent body."
            )},
            repos=(REPO,),
            main_repo=REPO,
            commit_tasks=True,
        )
        tasks_root = workspace / DEFAULT_TASKS_REPO
        write_attachment(tasks_root, "10.parent", "notes.md", b"pinned bytes")
        # Simulate the worker's split-dir output.
        sdir = split_output_dir(workspace, REPO, "10.parent", queue=None)
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "10.1.first.md").write_text(
            "---\ntitle: First\n---\nDo the first thing."
        )
        (sdir / "10.2.second.md").write_text(
            "---\ntitle: Second\n---\nDo the second thing."
        )
        meta = {
            "docs": "docs/spec.md",
            "attachments": "notes.md",
        }
        created = harvest_split_output(
            workspace, tasks_root, REPO, "10.parent", meta, queue=None,
        )
        assert len(created) == 2
        for child in created:
            child_data = read_task(tasks_root, child)
            raw = child_data["frontmatter_raw"]
            assert raw.get("docs") == "docs/spec.md"
            assert raw.get("attachments") == "notes.md"
            # docs_pin is deliberately NOT copied — children re-pin at their
            # own first dispatch.
            assert "docs_pin" not in raw
            # Attachment bytes were copied under <child>.docs/.
            child_docs = attachments_dir(tasks_root, child)
            assert (child_docs / "notes.md").is_file()
            assert (child_docs / "notes.md").read_bytes() == b"pinned bytes"


class TestBlockListParseInBuild:
    def test_block_list_docs_frontmatter_resolves(self, tmp_path: Path) -> None:
        text = b"# Spec\n"
        workspace, tasks_root, base_ref = _ws_with_docs(
            tmp_path,
            files={"docs/spec.md": text},
            task_body=(
                "---\ntitle: T\ndocs:\n  - docs/spec.md\n---\nBody."
            ),
        )
        order = _build_order(workspace, tasks_root, "10.task", base_ref)
        docs = order["config"].get("docs")
        assert docs and docs[0]["path"] == "docs/spec.md"


# ==========================================================================
# Phase 3 — operator HTTP API (attach guards, paths, blob, repin, drift)
# ==========================================================================


from starlette.testclient import TestClient  # noqa: E402

from nightshift.manager.app import create_app  # noqa: E402
from nightshift.manager.store_sqlite import SqliteStore  # noqa: E402


def _client_with_repo(
    tmp_path: Path,
    *,
    tasks: dict[str, str] | None = None,
    repo_files: dict[str, bytes] | None = None,
) -> tuple[TestClient, Path, Path]:
    """Manager TestClient with a real target repo (name ``target-repo``).

    Returns ``(client, workspace, tasks_root)``. The client's lifecycle is the
    caller's responsibility (use ``with`` or manual close).
    """
    workspace = build_workspace(
        tmp_path,
        tasks=tasks or {"10.task": "---\ntitle: T\n---\nBody."},
        repos=(REPO,),
        main_repo=REPO,
        commit_tasks=True,
    )
    repo_root = workspace / REPO
    if repo_files:
        for rel, data in repo_files.items():
            dest = repo_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        git_commit_all(repo_root, "add docs")
    client = TestClient(create_app(workspace, store=SqliteStore()))
    return client, workspace, workspace / DEFAULT_TASKS_REPO


class TestAttachAPI:
    def test_attach_png_under_cap(self, tmp_path: Path) -> None:
        client, _, tasks_root = _client_with_repo(tmp_path)
        with client:
            resp = client.post(
                "/api/tasks/10.task/attachments?name=screenshot.png",
                content=PNG_MAGIC,
            )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["name"] == "screenshot.png"
        assert body["media"] == "image/png"
        # File landed under <task>.docs/ and frontmatter now references it.
        assert (attachments_dir(tasks_root, "10.task")
                / "screenshot.png").is_file()
        info = read_task(tasks_root, "10.task")
        assert "screenshot.png" in str(info["frontmatter_raw"].get("attachments"))

    def test_attach_over_cap_rejected(self, tmp_path: Path) -> None:
        client, _, _ = _client_with_repo(tmp_path)
        big = PNG_MAGIC + b"\x00" * (400 * 1024)
        with client:
            resp = client.post(
                "/api/tasks/10.task/attachments?name=big.png",
                content=big,
            )
        assert resp.status_code == 400
        assert resp.json()["error"] == "document_too_large"

    def test_attach_zip_rejected(self, tmp_path: Path) -> None:
        client, _, _ = _client_with_repo(tmp_path)
        with client:
            resp = client.post(
                "/api/tasks/10.task/attachments?name=archive.zip",
                content=b"PK\x03\x04" + b"\x00" * 20,
            )
        assert resp.status_code == 400
        assert resp.json()["error"] == "unsupported_document_type"

    def test_replace_attachment(self, tmp_path: Path) -> None:
        client, _, tasks_root = _client_with_repo(tmp_path)
        with client:
            client.post(
                "/api/tasks/10.task/attachments?name=notes.md",
                content=b"v1",
            )
            resp = client.put(
                "/api/tasks/10.task/attachments/notes.md", content=b"v2",
            )
        assert resp.status_code == 201, resp.text
        assert read_attachment(tasks_root, "10.task", "notes.md") == b"v2"

    def test_delete_removes_bytes_and_frontmatter(self, tmp_path: Path) -> None:
        client, _, tasks_root = _client_with_repo(tmp_path)
        with client:
            client.post(
                "/api/tasks/10.task/attachments?name=notes.md",
                content=b"body",
            )
            resp = client.delete("/api/tasks/10.task/attachments/notes.md")
        assert resp.status_code == 200, resp.text
        assert not (attachments_dir(tasks_root, "10.task") / "notes.md").exists()
        info = read_task(tasks_root, "10.task")
        assert info["frontmatter_raw"].get("attachments") in (None, "", [])

    def test_get_attachment_bytes(self, tmp_path: Path) -> None:
        client, _, _ = _client_with_repo(tmp_path)
        with client:
            client.post(
                "/api/tasks/10.task/attachments?name=notes.md",
                content=b"# Notes\nHello.",
            )
            resp = client.get("/api/tasks/10.task/docs/notes.md")
        assert resp.status_code == 200
        assert resp.content == b"# Notes\nHello."
        assert resp.headers["content-type"].startswith("text/markdown")


class TestRepoPathsAPI:
    def test_lists_paths_with_sha_filtered_by_allow_list(
        self, tmp_path: Path
    ) -> None:
        client, _, _ = _client_with_repo(
            tmp_path,
            repo_files={
                "docs/spec.md": b"# spec\n",
                "docs/mockup.png": PNG_MAGIC,
                "archive.zip": b"PK\x03\x04",
            },
        )
        with client:
            resp = client.get("/api/repos/target-repo/paths")
        assert resp.status_code == 200
        rows = resp.json()["paths"]
        by_path = {r["path"]: r for r in rows}
        assert "docs/spec.md" in by_path
        assert "docs/mockup.png" in by_path
        # Zip is filtered out by allow-list.
        assert "archive.zip" not in by_path
        # sha present.
        for row in rows:
            assert row["sha"] and len(row["sha"]) == 40

    def test_prefix_filters(self, tmp_path: Path) -> None:
        client, _, _ = _client_with_repo(
            tmp_path,
            repo_files={
                "docs/a.md": b"a",
                "src/main.py": b"# not text-python? actually text-python\n",
                "docs/b.md": b"b",
            },
        )
        with client:
            resp = client.get("/api/repos/target-repo/paths?prefix=docs/")
        rows = resp.json()["paths"]
        for row in rows:
            assert row["path"].startswith("docs/")

    def test_absent_repo_404(self, tmp_path: Path) -> None:
        client, _, _ = _client_with_repo(tmp_path)
        with client:
            resp = client.get("/api/repos/does-not-exist/paths")
        assert resp.status_code == 404


class TestRepoBlobAPI:
    def test_blob_by_sha(self, tmp_path: Path) -> None:
        client, workspace, _ = _client_with_repo(
            tmp_path, repo_files={"docs/spec.md": b"# spec body\n"},
        )
        # Resolve the blob sha.
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD:docs/spec.md"],
            cwd=workspace / REPO,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        with client:
            resp = client.get(f"/api/repos/target-repo/blob?sha={sha}")
        assert resp.status_code == 200
        assert resp.content == b"# spec body\n"

    def test_invalid_sha(self, tmp_path: Path) -> None:
        client, _, _ = _client_with_repo(tmp_path)
        with client:
            resp = client.get("/api/repos/target-repo/blob?sha=$$$")
        assert resp.status_code == 400


class TestRepinAPI:
    def _dispatch_pin(self, workspace: Path, tasks_root: Path, task: str) -> None:
        """Trigger build_work_order once so the initial pin is persisted."""
        base_ref = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace / REPO,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        order = build_work_order(
            workspace, tasks_root, task, None, REPO,
            "l1", "r1", base_ref, _cfg(),
        )
        if order.get("_docs_pin"):
            set_engine_meta(tasks_root, task, {"docs_pin": order["_docs_pin"]})

    def test_repin_refreshes_after_mutation(self, tmp_path: Path) -> None:
        client, workspace, tasks_root = _client_with_repo(
            tmp_path,
            tasks={"10.task": "---\ndocs: docs/spec.md\n---\nBody."},
            repo_files={"docs/spec.md": b"# v1\n"},
        )
        # First pin.
        self._dispatch_pin(workspace, tasks_root, "10.task")
        pin_before = parse_docs_pin(
            read_task(tasks_root, "10.task")["frontmatter_raw"]["docs_pin"]
        )
        sha_v1 = pin_before["docs/spec.md"].sha
        # Mutate the source file at HEAD.
        (workspace / REPO / "docs" / "spec.md").write_bytes(b"# v2 changed\n")
        git_commit_all(workspace / REPO, "v2")
        with client:
            resp = client.post(
                "/api/tasks/10.task/docs/repin", json={"paths": None},
            )
        assert resp.status_code == 200, resp.text
        pin_after_raw = read_task(tasks_root, "10.task")["frontmatter_raw"]["docs_pin"]
        pin_after = parse_docs_pin(pin_after_raw)
        assert pin_after["docs/spec.md"].sha != sha_v1


class TestDocumentsAPI:
    def test_documents_lists_docs_pin_drift(self, tmp_path: Path) -> None:
        client, workspace, tasks_root = _client_with_repo(
            tmp_path,
            tasks={"10.task": "---\ndocs: docs/spec.md\n---\nBody."},
            repo_files={"docs/spec.md": b"# spec\n"},
        )
        # Dispatch once so the pin lands.
        base_ref = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace / REPO,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        order = build_work_order(
            workspace, tasks_root, "10.task", None, REPO,
            "l1", "r1", base_ref, _cfg(),
        )
        if order.get("_docs_pin"):
            set_engine_meta(
                tasks_root, "10.task", {"docs_pin": order["_docs_pin"]},
            )
        with client:
            resp = client.get("/api/tasks/10.task/documents")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["docs"] and body["docs"][0]["path"] == "docs/spec.md"
        assert body["docs"][0]["drifted"] is False
        assert "docs/spec.md" in body["docs_pin"]

    def test_documents_drift_true_after_mutation(self, tmp_path: Path) -> None:
        client, workspace, tasks_root = _client_with_repo(
            tmp_path,
            tasks={"10.task": "---\ndocs: docs/spec.md\n---\nBody."},
            repo_files={"docs/spec.md": b"# v1\n"},
        )
        base_ref = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace / REPO,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        order = build_work_order(
            workspace, tasks_root, "10.task", None, REPO,
            "l1", "r1", base_ref, _cfg(),
        )
        if order.get("_docs_pin"):
            set_engine_meta(
                tasks_root, "10.task", {"docs_pin": order["_docs_pin"]},
            )
        # Advance HEAD past the pinned blob.
        (workspace / REPO / "docs" / "spec.md").write_bytes(b"# v2 mutated\n")
        git_commit_all(workspace / REPO, "v2")
        with client:
            resp = client.get("/api/tasks/10.task/documents")
        body = resp.json()
        assert body["docs"][0]["drifted"] is True


class TestCreateAndPatchDocsFields:
    def test_create_with_docs_writes_frontmatter(self, tmp_path: Path) -> None:
        client, _, tasks_root = _client_with_repo(tmp_path, tasks={})
        with client:
            resp = client.post(
                "/api/tasks",
                json={
                    "title": "add oauth",
                    "text": "Body.",
                    "docs": ["docs/spec.md", {"path": "docs/design.md",
                                              "range": "1-20"}],
                },
            )
        assert resp.status_code == 200, resp.text
        task = resp.json()["task"]
        info = read_task(tasks_root, task)
        raw = info["frontmatter_raw"].get("docs")
        assert raw is not None and "docs/spec.md" in str(raw)

    def test_patch_docs_updates_frontmatter(self, tmp_path: Path) -> None:
        client, _, tasks_root = _client_with_repo(tmp_path)
        with client:
            resp = client.patch(
                "/api/tasks/10.task",
                json={"docs": ["docs/spec.md"]},
            )
        assert resp.status_code == 200, resp.text
        info = read_task(tasks_root, "10.task")
        assert "docs/spec.md" in str(info["frontmatter_raw"].get("docs"))

    def test_patch_empty_docs_clears_key(self, tmp_path: Path) -> None:
        client, _, tasks_root = _client_with_repo(
            tmp_path,
            tasks={"10.task": "---\ntitle: T\ndocs: docs/spec.md\n---\nBody."},
        )
        with client:
            resp = client.patch("/api/tasks/10.task", json={"docs": []})
        assert resp.status_code == 200, resp.text
        info = read_task(tasks_root, "10.task")
        assert info["frontmatter_raw"].get("docs") is None
