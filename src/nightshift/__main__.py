"""``python -m nightshift`` — top-level CLI entry point.

Supports subcommands:
  init  — scaffold a workspace's config from shipped templates.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from nightshift._paths import CONFIG_TEMPLATES_DIR


def _resolve_workspace(cli_workspace: str | None) -> Path:
    if cli_workspace:
        return Path(cli_workspace).expanduser().resolve()
    env = os.environ.get("NIGHTSHIFT_WORKSPACE")
    if env:
        return Path(env).expanduser().resolve()
    return Path.cwd().resolve()


def cmd_init(args: argparse.Namespace) -> int:
    """Scaffold a workspace's config from shipped templates."""
    workspace = _resolve_workspace(args.workspace)
    config_dir = workspace / ".nightshift"
    config_dir.mkdir(parents=True, exist_ok=True)

    templates = ["manager.json", "worker.json", "player.json"]
    for name in templates:
        src = CONFIG_TEMPLATES_DIR / name
        dst = config_dir / name
        if dst.exists():
            print(f"  skipped (exists): {dst}")
        else:
            shutil.copy2(src, dst)
            print(f"  created: {dst}")

    env_dst = workspace / ".env"
    env_example = Path(__file__).resolve().parent.parent.parent / ".env.example"
    if not env_example.exists():
        env_example = CONFIG_TEMPLATES_DIR.parent.parent.parent.parent / ".env.example"
    if env_dst.exists():
        print(f"  skipped (exists): {env_dst}")
    elif env_example.exists():
        shutil.copy2(env_example, env_dst)
        print(f"  created: {env_dst}")
    else:
        print(f"  skipped (.env.example not found): {env_dst}")

    print(f"\nWorkspace: {workspace}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="nightshift", description="Nightshift CLI")
    sub = parser.add_subparsers(dest="command")

    init_p = sub.add_parser("init", help="Scaffold workspace config from templates")
    init_p.add_argument("--workspace", type=str, default=None,
                        help="Workspace directory (default: NIGHTSHIFT_WORKSPACE or cwd)")

    args = parser.parse_args()
    if args.command == "init":
        return cmd_init(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
