"""Self re-exec helper for the in-app restart affordance.

The manager and worker both offer an operator "Restart" control in their UI.
The mechanism is a self re-exec: the HTTP endpoint responds, uvicorn shuts down
gracefully, and then the process *replaces itself* with a fresh interpreter for
the same module and original CLI args. Re-exec (rather than spawn-and-exit)
preserves the terminal session the operator launched from, and picks up new
code on disk (the editable install serves the primary checkout's ``src/``).
"""

from __future__ import annotations

import os
import sys
from typing import NoReturn


def re_exec(module: str, argv: list[str]) -> NoReturn:
    """Replace the current process with ``python -m <module> *argv``.

    Flushes stdout/stderr first so any final log line is not lost across the
    ``execv`` boundary, then never returns (the process image is replaced).
    Kept deliberately tiny and side-effect-light so it can be unit-tested by
    mocking :func:`os.execv`.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, "-m", module, *argv])
