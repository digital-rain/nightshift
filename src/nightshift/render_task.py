"""Fill nightshift task template placeholders (title + body)."""

from __future__ import annotations

import os
import sys
from pathlib import Path


BODY_MARKER = "Task description goes here."


def main() -> None:
    dest = Path(os.environ["TASK_DEST"])
    title = os.environ["TASK_TITLE"]
    body = os.environ["TASK_BODY"]
    lines = dest.read_text().splitlines()
    out: list[str] = []
    body_written = False
    for line in lines:
        if line.startswith("title: "):
            out.append(f"title: {title}")
        elif line == BODY_MARKER and not body_written:
            out.append(body)
            body_written = True
        else:
            out.append(line)
    if not body_written:
        print(f"template missing marker: {BODY_MARKER!r}", file=sys.stderr)
        raise SystemExit(1)
    dest.write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
