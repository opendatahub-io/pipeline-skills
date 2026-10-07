"""Collect wheel failures, emit one audit report, and invoke PFA."""

import argparse
import importlib
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


def main() -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare: argparse.ArgumentParser = commands.add_parser(
        "prepare", help="Collect failures and write one audit child configuration"
    )
    prepare.add_argument("--work-dir", type=Path, default=Path(".wheel-triage"))
    prepare.add_argument(
        "--failures", type=Path, help="Offline collected failures JSON"
    )
    notify: argparse.ArgumentParser = commands.add_parser(
        "notify", help="Wait for the audit child, then start PFA with BOT_PAT"
    )
    notify.add_argument("--work-dir", type=Path, default=Path(".wheel-triage"))
    audit: argparse.ArgumentParser = commands.add_parser(
        "audit", help="Print failure counts and fail when the report is nonempty"
    )
    audit.add_argument("evidence", type=Path)
    args: argparse.Namespace = parser.parse_args()

    if args.command == "audit":
        data: dict[str, Any] = json.loads(args.evidence.read_text(encoding="utf-8"))
        failures: list[dict[str, Any]] = data["failures"]
        print(f"Failure occurrences: {len(failures)}")
        counts: dict[str, int] = dict.fromkeys(("wheel", "job", "evidence", "other"), 0)
        for failure in failures:
            kind: Any = failure.get("kind") if isinstance(failure, dict) else None
            bucket: str = kind if isinstance(kind, str) and kind in counts else "other"
            counts[bucket] += 1
        print(
            "Failure kinds: "
            + ", ".join(f"{kind}={count}" for kind, count in counts.items())
        )
        print("Complete report is retained as the failures.json artifact for PFA.")
        return int(bool(failures))

    core: ModuleType = importlib.import_module(
        f"{__package__}.wheel_triage" if __package__ else "wheel_triage"
    )
    environ: dict[str, str] = dict(os.environ)
    if args.command == "prepare":
        core.prepare(args.work_dir, environ, args.failures)
    else:
        core.notify(core.GitLab(environ), environ, args.work_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
