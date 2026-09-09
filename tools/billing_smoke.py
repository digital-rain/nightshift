#!/usr/bin/env python3
"""Operator-run billing smoke: which account will this box's claude-code runs bill?

Prints the ``claude_billing`` decision for the workspace's worker and manager
configs exactly as the backend will make it, then (with ``--run``) performs
one tiny tool-less print-mode completion under the *worker's* decided
environment and prints the ``billing`` value such a run would record.

The Anthropic console is the only ground truth for "not billed": run this with
``--run`` at a noted time, then confirm the console shows no new usage for it
while the printed billing says ``subscription``.

Never run by tests — it spawns the real ``claude`` CLI.

Usage::

    .venv/bin/python tools/billing_smoke.py [--workspace DIR] [--run]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from nightshift.billing import (
    CLI_AUTH_ENV_KEYS,
    BillingConfigError,
    claude_auth_status,
    decide_claude_billing,
    scrub_cli_auth,
)
from nightshift.config.manager import load_manager_config
from nightshift.config.worker import load_worker_config
from nightshift.prompts import build_claude_text_argv, resolve_claude_bin


def _decision(label: str, setting: str, claude_bin: str):
    try:
        decision = decide_claude_billing(
            setting, env=os.environ, claude_bin=claude_bin, log=print,
        )
    except BillingConfigError as exc:
        print(f"{label}: claude_billing={setting} -> ERROR: {exc}")
        return None
    print(f"{label}: claude_billing={setting} -> {decision.mode} ({decision.reason})")
    return decision


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--workspace", type=Path,
        default=Path(os.environ.get("NIGHTSHIFT_WORKSPACE") or Path.cwd()),
    )
    parser.add_argument(
        "--run", action="store_true",
        help="also run one tiny print-mode completion under the worker's decided env",
    )
    args = parser.parse_args(argv)
    workspace = args.workspace.expanduser().resolve()

    wcfg = load_worker_config(workspace)  # also loads <workspace>/.env
    mcfg = load_manager_config(workspace)
    claude_bin = resolve_claude_bin()
    present = [k for k in CLI_AUTH_ENV_KEYS if os.environ.get(k)]
    print(f"workspace: {workspace}")
    print(f"claude: {claude_bin}")
    print(f"auth-related vars in this process: {present or 'none'}")
    status = claude_auth_status(claude_bin, scrub_cli_auth(os.environ))
    print(f"claude auth status (scrubbed env): {json.dumps(status) if status else 'unavailable'}")

    worker = _decision("worker", wcfg.claude_billing, claude_bin)
    _decision("manager (enhance)", mcfg.claude_billing, claude_bin)
    if worker is None:
        return 1
    if not args.run:
        return 0

    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"\nrunning one print-mode completion at {stamp} under billing={worker.mode} ...")
    argv_cli = build_claude_text_argv(
        "Reply with the single word OK.", "ping", wcfg.auto_model.split("/", 1)[-1],
    )
    argv_cli[0] = claude_bin
    with tempfile.TemporaryDirectory(prefix="nightshift-billing-smoke-") as cwd:
        proc = subprocess.run(
            argv_cli, cwd=cwd, env=worker.env, capture_output=True, text=True, timeout=120,
        )
    if proc.returncode != 0:
        print(f"claude exited {proc.returncode}: {(proc.stderr or proc.stdout)[-400:]}")
        return 1
    payload = json.loads(proc.stdout)
    print(f"result: {payload.get('result')!r}")
    print(f"cli total_cost_usd (notional under subscription): {payload.get('total_cost_usd')}")
    print(f"billing this run would record: {worker.mode}")
    print(
        "now check the Anthropic console for usage at that timestamp: "
        + ("there must be none." if worker.mode == "subscription" else "it is expected (api mode).")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
