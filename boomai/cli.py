"""
BoomAI CLI — AI-powered code fixer.

Usage (from inside your project):
  boomai fix
  boomai settings
"""

from __future__ import annotations

import argparse
import logging
import sys

from .app.commands.fix_command import cmd_fix
from .app.commands.settings_command import cmd_settings
from .core.config import settings
from .presentation.banner import print_banner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="boomai",
        description="BoomAI — AI-powered code fixer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples (run from inside your project):
  boomai fix                      # scan + auto-fix codebase
  boomai fix --deep               # deeper, slower scan for higher coverage
  boomai fix Assets/Scripts Game.cs  # only scan specific folders/files
  boomai settings                 # configure API key & preferences
""",
    )
    sub = parser.add_subparsers(dest="command")

    fix_parser = sub.add_parser("fix", help="Scan codebase and auto-apply fixes")
    fix_parser.add_argument(
        "--shallow",
        action="store_true",
        help="Only scan files in CWD, skip subdirectories",
    )
    fix_parser.add_argument(
        "--profile",
        choices=("default", "deep"),
        default=settings.scan_profile,
        help="Scan profile: default is faster/cheaper, deep spends more to increase coverage.",
    )
    fix_parser.add_argument(
        "--deep",
        action="store_true",
        help="Shortcut for --profile deep.",
    )
    fix_parser.add_argument(
        "--cost-report",
        action="store_true",
        help="Force detailed cost/reporting output for this run.",
    )
    fix_parser.add_argument(
        "--clean-run",
        action="store_true",
        help="Run without cost line, history write, or cost-report artifact.",
    )
    fix_parser.add_argument(
        "targets",
        nargs="*",
        help="Optional folders/files to scan, relative or absolute.",
    )

    sub.add_parser("settings", help="Configure API key & preferences")
    return parser


def configure_stdout() -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="replace")


def main() -> None:
    configure_stdout()
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    print_banner()

    if args.command == "fix":
        cmd_fix(args)
    elif args.command == "settings":
        cmd_settings(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
