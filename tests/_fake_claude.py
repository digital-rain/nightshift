"""A fake ``claude`` CLI for tests — never spawn the real one.

The real binary costs money and depends on the box's auth state, so every test
that exercises the ``claude-code`` path installs this script into a ``tmp_path``
bin dir instead. It answers the three shapes nightshift drives: ``auth status``
(the billing probe), print mode (``-p … --output-format json``), and streaming
(``--output-format stream-json``).

It also dumps the environment and argv it was handed to files named by
``FAKE_CLAUDE_ENV_DUMP`` / ``FAKE_CLAUDE_ARGV_DUMP``, which is how a test
asserts that the auth keys were scrubbed (or passed through) and how it counts
spawns.
"""

from __future__ import annotations

import sys
from pathlib import Path


_SCRIPT = '''\
#!/usr/bin/env python3
"""Fake `claude` CLI written by tests/_fake_claude.py — never the real binary."""

import json
import os
import sys
import time


AUTH = {auth!r}
RESULT_TEXT = {result_text!r}

RESULT = {{
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": RESULT_TEXT,
    "session_id": "fake-session",
    "num_turns": 1,
    "usage": {{
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }},
    "total_cost_usd": 0.0015,
    "stop_reason": "end_turn",
}}


def _dump():
    env_dump = os.environ.get("FAKE_CLAUDE_ENV_DUMP")
    if env_dump:
        with open(env_dump, "w") as fh:
            fh.write(json.dumps(dict(os.environ)))
    argv_dump = os.environ.get("FAKE_CLAUDE_ARGV_DUMP")
    if argv_dump:
        with open(argv_dump, "w") as fh:
            fh.write(json.dumps(sys.argv[1:]))


def _auth_status():
    if AUTH == "hang":
        time.sleep(60)
        return 0
    if AUTH == "garbage":
        print("not json")
        return 0
    if AUTH == "logged_out":
        # The real CLI (v2.1.183) prints the answer and exits 1 when logged out.
        print(json.dumps({{"loggedIn": False, "authMethod": "none"}}))
        return 1
    status = {{
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "subscriptionType": "max",
    }}
    if "ANTHROPIC_API_KEY" in os.environ:
        status["apiKeySource"] = "ANTHROPIC_API_KEY"
    print(json.dumps(status))
    return 0


def main():
    _dump()
    argv = sys.argv[1:]
    if argv[:2] == ["auth", "status"]:
        return _auth_status()
    if "--output-format" in argv:
        fmt = argv[argv.index("--output-format") + 1]
        if fmt == "stream-json":
            print(json.dumps(
                {{"type": "system", "subtype": "init", "session_id": "fake-session"}}))
            print(json.dumps({{
                "type": "assistant",
                "message": {{"content": [{{"type": "text", "text": RESULT_TEXT}}]}},
                "session_id": "fake-session",
            }}))
            print(json.dumps(RESULT))
            return 0
        if fmt == "json" and "-p" in argv:
            print(json.dumps(RESULT))
            return 0
    sys.stderr.write("fake claude: unsupported invocation %r\\n" % (argv,))
    return 2


if __name__ == "__main__":
    sys.exit(main())
'''


def install_fake_claude(
    bin_dir: Path, *, auth: str = "logged_in", result_text: str = "ok"
) -> Path:
    """Write an executable ``claude`` script into *bin_dir* and return its path.

    ``auth`` ∈ ``{"logged_in", "logged_out", "hang", "garbage"}`` is baked into
    the script, so one installed fake always answers ``auth status`` the same
    way. ``result_text`` is the text the print/stream modes return.
    """
    if auth not in ("logged_in", "logged_out", "hang", "garbage"):
        raise ValueError(f"unknown auth variant: {auth!r}")
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / "claude"
    body = _SCRIPT.format(auth=auth, result_text=result_text)
    path.write_text(f"#!{sys.executable}\n" + body.split("\n", 1)[1])
    path.chmod(0o755)
    return path
