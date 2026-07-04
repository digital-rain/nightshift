"""Phase 2 tests — sandboxed tool registry (spec §5.2, invariants 3 & 7, §10).

Network-free: ``grep`` is forced down its pure-Python fallback (monkeypatch
``shutil.which`` → None) so the suite never depends on ripgrep, and ``run_bash``
uses trivial shell commands.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import nightshift.agent.tools as tools_mod
from nightshift.agent.tools import ToolRegistry, _resolve_in_sandbox, build_registry


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    (tmp_path / "a.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("nested\n", encoding="utf-8")
    return tmp_path


# --------------------------------------------------------------------------- #
# Sandbox guard
# --------------------------------------------------------------------------- #


def test_sandbox_rejects_absolute(sandbox: Path) -> None:
    with pytest.raises(tools_mod.SandboxError):
        _resolve_in_sandbox(sandbox, "/etc/passwd")


def test_sandbox_rejects_dotdot_escape(sandbox: Path) -> None:
    with pytest.raises(tools_mod.SandboxError):
        _resolve_in_sandbox(sandbox, "../outside.txt")


def test_sandbox_rejects_symlink_escape(sandbox: Path, tmp_path: Path) -> None:
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = sandbox / "link"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unsupported on this platform")
    with pytest.raises(tools_mod.SandboxError):
        _resolve_in_sandbox(sandbox, "link")


def test_sandbox_allows_nested(sandbox: Path) -> None:
    resolved = _resolve_in_sandbox(sandbox, "sub/b.txt")
    assert resolved == (sandbox / "sub" / "b.txt").resolve()


def test_every_tool_rejects_traversal(sandbox: Path) -> None:
    reg = build_registry(sandbox)
    bad = "../escape"
    assert reg.dispatch("read_file", {"path": bad}).is_error
    assert reg.dispatch("list_dir", {"path": bad}).is_error
    assert reg.dispatch("grep", {"pattern": "x", "path": bad}).is_error
    assert reg.dispatch("edit_file", {"path": bad, "edits": ""}).is_error
    assert reg.dispatch("write_file", {"path": bad, "contents": "x"}).is_error


# --------------------------------------------------------------------------- #
# read_file
# --------------------------------------------------------------------------- #


def test_read_file_line_numbering(sandbox: Path) -> None:
    reg = build_registry(sandbox)
    out = reg.dispatch("read_file", {"path": "a.txt"})
    assert not out.is_error
    assert "     1\talpha" in out.content
    assert "     3\tgamma" in out.content


def test_read_file_range(sandbox: Path) -> None:
    reg = build_registry(sandbox)
    out = reg.dispatch("read_file", {"path": "a.txt", "start": 2, "end": 2})
    assert "     2\tbeta" in out.content
    assert "alpha" not in out.content
    assert "gamma" not in out.content


def test_read_file_missing(sandbox: Path) -> None:
    reg = build_registry(sandbox)
    assert reg.dispatch("read_file", {"path": "nope.txt"}).is_error


# --------------------------------------------------------------------------- #
# grep (forced fallback)
# --------------------------------------------------------------------------- #


def test_grep_regex_fallback(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools_mod.shutil, "which", lambda _: None)
    reg = build_registry(sandbox)
    out = reg.dispatch("grep", {"pattern": "be.a"})
    assert "a.txt" in out.content and "beta" in out.content


def test_grep_literal_fallback(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools_mod.shutil, "which", lambda _: None)
    reg = build_registry(sandbox)
    # As a literal, "be.a" should NOT match "beta".
    out = reg.dispatch("grep", {"pattern": "be.a", "literal": True})
    assert out.content == "(no matches)"


def test_grep_no_matches(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools_mod.shutil, "which", lambda _: None)
    reg = build_registry(sandbox)
    assert reg.dispatch("grep", {"pattern": "zzz"}).content == "(no matches)"


# --------------------------------------------------------------------------- #
# edit_file / write_file
# --------------------------------------------------------------------------- #


def test_edit_file_success(sandbox: Path) -> None:
    reg = build_registry(sandbox)
    edits = "<<<<<<< SEARCH\nbeta\n=======\nBETA\n>>>>>>> REPLACE\n"
    out = reg.dispatch("edit_file", {"path": "a.txt", "edits": edits})
    assert not out.is_error
    assert (sandbox / "a.txt").read_text() == "alpha\nBETA\ngamma\n"


def test_edit_file_apply_error_leaves_file_untouched(sandbox: Path) -> None:
    reg = build_registry(sandbox)
    before = (sandbox / "a.txt").read_text()
    edits = "<<<<<<< SEARCH\nNOPE\n=======\nX\n>>>>>>> REPLACE\n"
    out = reg.dispatch("edit_file", {"path": "a.txt", "edits": edits})
    assert out.is_error
    assert "zero_match" in out.content
    assert (sandbox / "a.txt").read_text() == before  # atomic: nothing written


def test_edit_file_no_blocks(sandbox: Path) -> None:
    reg = build_registry(sandbox)
    out = reg.dispatch("edit_file", {"path": "a.txt", "edits": "just prose"})
    assert out.is_error


def test_write_file_creates(sandbox: Path) -> None:
    reg = build_registry(sandbox)
    out = reg.dispatch("write_file", {"path": "new/c.txt", "contents": "hi\n"})
    assert not out.is_error
    assert (sandbox / "new" / "c.txt").read_text() == "hi\n"


# --------------------------------------------------------------------------- #
# run_bash
# --------------------------------------------------------------------------- #


def test_run_bash_cwd_is_sandbox(sandbox: Path) -> None:
    reg = build_registry(sandbox)
    out = reg.dispatch("run_bash", {"command": "ls"})
    assert not out.is_error
    assert "a.txt" in out.content
    assert "[exit 0]" in out.content


def test_run_bash_nonzero_is_error(sandbox: Path) -> None:
    reg = build_registry(sandbox)
    out = reg.dispatch("run_bash", {"command": "exit 3"})
    assert out.is_error
    assert "[exit 3]" in out.content


def test_run_bash_truncation(sandbox: Path) -> None:
    reg = build_registry(sandbox)
    out = reg.dispatch("run_bash", {"command": "yes x | head -c 100000"})
    assert out.content.endswith(tools_mod._TRUNCATION_NOTE)
    assert out.truncated is True  # telemetry for tuning the output cap


def test_untruncated_result_not_flagged(sandbox: Path) -> None:
    reg = build_registry(sandbox)
    out = reg.dispatch("run_bash", {"command": "echo ok"})
    assert out.truncated is False


def test_run_bash_respects_should_abort(sandbox: Path) -> None:
    reg = build_registry(sandbox, should_abort=lambda: "stop")
    out = reg.dispatch("run_bash", {"command": "echo hi"})
    assert out.is_error
    assert "aborted" in out.content


# --------------------------------------------------------------------------- #
# Registry shape: immutability, allow-list, deterministic specs
# --------------------------------------------------------------------------- #


def test_specs_sorted_by_name(sandbox: Path) -> None:
    reg = build_registry(sandbox)
    names = [s["name"] for s in reg.specs()]
    assert names == sorted(names)


def test_specs_json_is_deterministic(sandbox: Path) -> None:
    a = build_registry(sandbox).specs_json()
    b = build_registry(sandbox).specs_json()
    assert a == b
    # And sorted-keys: a re-dump with sort_keys must equal the canonical form.
    reparsed = json.loads(a)
    assert json.dumps(reparsed, sort_keys=True, separators=(",", ":")) == a


def test_tools_enabled_allow_list(sandbox: Path) -> None:
    reg = build_registry(sandbox, tools_enabled=["read_file", "grep"])
    assert reg.names() == ["grep", "read_file"]
    assert reg.dispatch("write_file", {"path": "x", "contents": "y"}).is_error


def test_unknown_tool_is_error(sandbox: Path) -> None:
    reg = build_registry(sandbox)
    assert reg.dispatch("nope", {}).is_error


def test_registry_is_immutable_type(sandbox: Path) -> None:
    reg = build_registry(sandbox)
    assert isinstance(reg, ToolRegistry)
