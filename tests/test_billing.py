"""Tests for the claude-code billing decision (`nightshift.billing`).

The unit under test decides which credential a ``claude`` CLI run spends: the
operator's claude.ai subscription (auth env scrubbed) or the Anthropic API key
(env passed through). Every probe here runs against the fake CLI from
``tests/_fake_claude.py`` — the real binary is never spawned.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from _fake_claude import install_fake_claude
from nightshift.billing import (
    BILLING_API,
    BILLING_SUBSCRIPTION,
    CLAUDE_BILLING_MODES,
    CLI_AUTH_ENV_KEYS,
    DEFAULT_CLAUDE_BILLING,
    BillingConfigError,
    billing_setting,
    claude_auth_status,
    clear_auth_status_cache,
    decide_claude_billing,
    scrub_cli_auth,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """The probe cache is module-level; no test may inherit another's answer."""
    clear_auth_status_cache()
    yield
    clear_auth_status_cache()


def _env(**extra: str) -> dict[str, str]:
    base = {"PATH": "/usr/bin:/bin", "HOME": "/home/nobody"}
    base.update(extra)
    return base


# ─── the scrub ───────────────────────────────────────────────────────────────


class TestScrub:
    def test_removes_exactly_the_declared_keys(self):
        env = _env(
            ANTHROPIC_API_KEY="sk-ant-1",
            ANTHROPIC_AUTH_TOKEN="tok",
            ANTHROPIC_BASE_URL="https://proxy.invalid",
            CLAUDE_CODE_USE_BEDROCK="1",
            CLAUDE_CODE_USE_VERTEX="1",
            FAKE_CLAUDE_ENV_DUMP="/tmp/dump.json",
        )
        scrubbed = scrub_cli_auth(env)
        assert set(env) - set(scrubbed) == set(CLI_AUTH_ENV_KEYS)

    def test_leaves_unrelated_keys_untouched(self):
        env = _env(ANTHROPIC_API_KEY="sk-ant-1", FAKE_CLAUDE_ENV_DUMP="/tmp/d.json")
        scrubbed = scrub_cli_auth(env)
        assert scrubbed["PATH"] == "/usr/bin:/bin"
        assert scrubbed["HOME"] == "/home/nobody"
        assert scrubbed["FAKE_CLAUDE_ENV_DUMP"] == "/tmp/d.json"

    def test_does_not_mutate_the_input(self):
        env = _env(ANTHROPIC_API_KEY="sk-ant-1")
        scrub_cli_auth(env)
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-1"

    def test_absent_keys_are_not_an_error(self):
        assert scrub_cli_auth(_env()) == _env()


# ─── the declared setting ────────────────────────────────────────────────────


class TestBillingSetting:
    def test_default_is_auto(self):
        assert DEFAULT_CLAUDE_BILLING == "auto"
        assert CLAUDE_BILLING_MODES == ("auto", "subscription", "api")

    def test_none_config_is_the_default(self):
        assert billing_setting(None) == DEFAULT_CLAUDE_BILLING

    def test_absent_key_is_the_default(self):
        assert billing_setting({"default_model": "auto"}) == DEFAULT_CLAUDE_BILLING

    def test_empty_value_is_the_default(self):
        assert billing_setting({"claude_billing": ""}) == DEFAULT_CLAUDE_BILLING
        assert billing_setting({"claude_billing": "   "}) == DEFAULT_CLAUDE_BILLING
        assert billing_setting({"claude_billing": None}) == DEFAULT_CLAUDE_BILLING

    def test_case_insensitive_and_trimmed(self):
        assert billing_setting({"claude_billing": " Subscription "}) == "subscription"
        assert billing_setting({"claude_billing": "API"}) == "api"

    def test_invalid_value_names_the_setting_and_the_modes(self):
        with pytest.raises(BillingConfigError) as exc:
            billing_setting({"claude_billing": "free"})
        message = str(exc.value)
        assert "claude_billing" in message
        assert "free" in message
        for mode in CLAUDE_BILLING_MODES:
            assert mode in message

    def test_error_is_a_value_error(self):
        assert issubclass(BillingConfigError, ValueError)


# ─── declared modes ──────────────────────────────────────────────────────────


class TestDeclaredModes:
    def test_subscription_scrubs_without_probing(self):
        # A nonexistent binary proves no probe ran: a probe would have to fail.
        decision = decide_claude_billing(
            "subscription",
            env=_env(ANTHROPIC_API_KEY="sk-ant-1"),
            claude_bin="/nonexistent/claude",
        )
        assert decision.mode == BILLING_SUBSCRIPTION
        assert "ANTHROPIC_API_KEY" not in decision.env
        assert decision.env["PATH"] == "/usr/bin:/bin"
        assert decision.reason

    def test_subscription_without_a_key_still_scrubs(self):
        decision = decide_claude_billing(
            "subscription", env=_env(), claude_bin="/nonexistent/claude"
        )
        assert decision.mode == BILLING_SUBSCRIPTION

    def test_api_passes_the_key_through(self):
        decision = decide_claude_billing(
            "api",
            env=_env(ANTHROPIC_API_KEY="sk-ant-1", ANTHROPIC_BASE_URL="https://x"),
            claude_bin="/nonexistent/claude",
        )
        assert decision.mode == BILLING_API
        assert decision.env["ANTHROPIC_API_KEY"] == "sk-ant-1"
        assert decision.env["ANTHROPIC_BASE_URL"] == "https://x"

    def test_api_without_a_key_errors_naming_the_setting(self):
        with pytest.raises(BillingConfigError) as exc:
            decide_claude_billing(
                "api", env=_env(), claude_bin="/nonexistent/claude"
            )
        assert "claude_billing=api" in str(exc.value)
        assert "ANTHROPIC_API_KEY" in str(exc.value)

    def test_api_with_an_empty_key_errors(self):
        with pytest.raises(BillingConfigError):
            decide_claude_billing(
                "api", env=_env(ANTHROPIC_API_KEY=""), claude_bin="/nonexistent/claude"
            )

    def test_invalid_setting_errors(self):
        with pytest.raises(BillingConfigError) as exc:
            decide_claude_billing(
                "free", env=_env(), claude_bin="/nonexistent/claude"
            )
        assert "claude_billing" in str(exc.value)

    def test_setting_is_normalised(self):
        decision = decide_claude_billing(
            " SUBSCRIPTION ", env=_env(), claude_bin="/nonexistent/claude"
        )
        assert decision.mode == BILLING_SUBSCRIPTION


# ─── the auto probe ──────────────────────────────────────────────────────────


class TestAutoProbe:
    def test_logged_in_chooses_the_subscription(self, tmp_path: Path):
        claude = install_fake_claude(tmp_path / "bin")
        decision = decide_claude_billing(
            "auto", env=_env(ANTHROPIC_API_KEY="sk-ant-1"), claude_bin=str(claude)
        )
        assert decision.mode == BILLING_SUBSCRIPTION
        assert "max" in decision.reason
        assert "ANTHROPIC_API_KEY" not in decision.env

    def test_probe_env_carries_no_auth_keys(self, tmp_path: Path):
        claude = install_fake_claude(tmp_path / "bin")
        dump = tmp_path / "env.json"
        decision = decide_claude_billing(
            "auto",
            env=_env(
                ANTHROPIC_API_KEY="sk-ant-1",
                ANTHROPIC_AUTH_TOKEN="tok",
                FAKE_CLAUDE_ENV_DUMP=str(dump),
            ),
            claude_bin=str(claude),
        )
        seen = json.loads(dump.read_text())
        for key in CLI_AUTH_ENV_KEYS:
            assert key not in seen, f"{key} leaked into the probe env"
        assert seen["FAKE_CLAUDE_ENV_DUMP"] == str(dump)
        # No key reached the CLI, so it reports no apiKeySource → a clean login.
        assert decision.mode == BILLING_SUBSCRIPTION

    def test_logged_out_with_a_key_falls_back_to_api_loudly(self, tmp_path: Path):
        claude = install_fake_claude(tmp_path / "bin", auth="logged_out")
        lines: list[str] = []
        decision = decide_claude_billing(
            "auto",
            env=_env(ANTHROPIC_API_KEY="sk-ant-1"),
            claude_bin=str(claude),
            log=lines.append,
        )
        assert decision.mode == BILLING_API
        assert decision.env["ANTHROPIC_API_KEY"] == "sk-ant-1"
        assert len(lines) == 1
        assert "claude.ai login not detected" in lines[0]
        assert "claude_billing=auto" in lines[0]

    def test_logged_out_without_a_key_errors_naming_both_remedies(self, tmp_path: Path):
        claude = install_fake_claude(tmp_path / "bin", auth="logged_out")
        with pytest.raises(BillingConfigError) as exc:
            decide_claude_billing("auto", env=_env(), claude_bin=str(claude))
        message = str(exc.value)
        assert "claude_billing" in message
        assert "claude login" in message
        assert "ANTHROPIC_API_KEY" in message

    def test_no_log_callback_is_fine(self, tmp_path: Path):
        claude = install_fake_claude(tmp_path / "bin", auth="logged_out")
        decision = decide_claude_billing(
            "auto", env=_env(ANTHROPIC_API_KEY="sk-ant-1"), claude_bin=str(claude)
        )
        assert decision.mode == BILLING_API

    def test_garbage_output_counts_as_not_logged_in(self, tmp_path: Path):
        claude = install_fake_claude(tmp_path / "bin", auth="garbage")
        decision = decide_claude_billing(
            "auto", env=_env(ANTHROPIC_API_KEY="sk-ant-1"), claude_bin=str(claude)
        )
        assert decision.mode == BILLING_API

    def test_a_missing_binary_counts_as_not_logged_in(self):
        decision = decide_claude_billing(
            "auto",
            env=_env(ANTHROPIC_API_KEY="sk-ant-1"),
            claude_bin="/nonexistent/claude",
        )
        assert decision.mode == BILLING_API

    def test_a_hanging_probe_is_bounded(self, tmp_path: Path):
        claude = install_fake_claude(tmp_path / "bin", auth="hang")
        started = time.monotonic()
        status = claude_auth_status(str(claude), _env(), timeout=1.0)
        elapsed = time.monotonic() - started
        assert status is None
        assert elapsed < 5.0, f"probe took {elapsed:.1f}s — the timeout did not bite"


# ─── the probe itself ────────────────────────────────────────────────────────


class TestAuthStatus:
    def test_parses_the_logged_in_payload(self, tmp_path: Path):
        claude = install_fake_claude(tmp_path / "bin")
        status = claude_auth_status(str(claude), _env())
        assert status is not None
        assert status["loggedIn"] is True
        assert status["authMethod"] == "claude.ai"
        assert status["subscriptionType"] == "max"

    def test_runs_auth_status(self, tmp_path: Path):
        claude = install_fake_claude(tmp_path / "bin")
        dump = tmp_path / "argv.json"
        claude_auth_status(str(claude), _env(FAKE_CLAUDE_ARGV_DUMP=str(dump)))
        assert json.loads(dump.read_text()) == ["auth", "status"]

    def test_a_cached_answer_does_not_respawn(self, tmp_path: Path):
        claude = install_fake_claude(tmp_path / "bin")
        first, second = tmp_path / "a.json", tmp_path / "b.json"

        claude_auth_status(str(claude), _env(FAKE_CLAUDE_ARGV_DUMP=str(first)))
        claude_auth_status(str(claude), _env(FAKE_CLAUDE_ARGV_DUMP=str(second)))

        assert first.exists()
        assert not second.exists(), "the second call spawned the CLI again"

    def test_clear_cache_forces_a_respawn(self, tmp_path: Path):
        claude = install_fake_claude(tmp_path / "bin")
        first, second = tmp_path / "a.json", tmp_path / "b.json"

        claude_auth_status(str(claude), _env(FAKE_CLAUDE_ARGV_DUMP=str(first)))
        clear_auth_status_cache()
        claude_auth_status(str(claude), _env(FAKE_CLAUDE_ARGV_DUMP=str(second)))

        assert second.exists()

    def test_use_cache_false_bypasses_the_cache(self, tmp_path: Path):
        claude = install_fake_claude(tmp_path / "bin")
        first, second = tmp_path / "a.json", tmp_path / "b.json"

        claude_auth_status(str(claude), _env(FAKE_CLAUDE_ARGV_DUMP=str(first)))
        claude_auth_status(
            str(claude), _env(FAKE_CLAUDE_ARGV_DUMP=str(second)), use_cache=False
        )

        assert second.exists()

    def test_a_failure_is_not_cached(self, tmp_path: Path):
        """A transient failure must retry, so ``None`` never enters the cache."""
        bin_dir = tmp_path / "bin"
        claude = install_fake_claude(bin_dir, auth="garbage")
        assert claude_auth_status(str(claude), _env()) is None

        install_fake_claude(bin_dir, auth="logged_in")
        status = claude_auth_status(str(claude), _env())
        assert status is not None and status["loggedIn"] is True


class TestRealCliShapes:
    """The real CLI (v2.1.183) prints the logged-out answer and exits 1; that
    is an answer, cached like any other, not a failure to retry every spawn."""

    def test_logged_out_answer_is_cached(self, tmp_path: Path):
        claude = install_fake_claude(tmp_path / "bin", auth="logged_out")
        dump = tmp_path / "argv.json"
        env = _env(FAKE_CLAUDE_ARGV_DUMP=str(dump))
        assert claude_auth_status(str(claude), env) == {"loggedIn": False, "authMethod": "none"}
        dump.unlink()
        assert claude_auth_status(str(claude), env) is not None
        assert not dump.exists(), "second call must be served from the cache"

    def test_an_unreadable_probe_is_reported_as_such(self, tmp_path: Path):
        claude = install_fake_claude(tmp_path / "bin", auth="garbage")
        lines: list[str] = []
        decision = decide_claude_billing(
            "auto", env=_env(ANTHROPIC_API_KEY="sk-ant-1"),
            claude_bin=str(claude), log=lines.append,
        )
        assert decision.mode == BILLING_API
        assert "probe unreadable" in decision.reason
        assert "could not be read" in lines[0]
        assert "claude login" not in lines[0]  # wrong advice for a logged-in box
