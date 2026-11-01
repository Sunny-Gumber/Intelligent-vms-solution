#!/usr/bin/env python3
"""Stamp build metadata into the environment for a release build.

CI calls this to produce the VMS_BUILD_* variables that app/version.py reads.
It never writes a fake commit hash: if git is unavailable the value stays
"dev", which is visibly not a release.

Usage:
    eval "$(python tools/stamp_build.py --export)"
    python tools/stamp_build.py --github-env >> "$GITHUB_ENV"
"""

from __future__ import annotations

import argparse
import datetime as dt
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(*args: str) -> str | None:
    try:
        result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                                text=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def metadata(build_number: str | None) -> dict[str, str]:
    return {
        "VMS_BUILD_COMMIT": git("rev-parse", "--short", "HEAD") or "dev",
        "VMS_BUILD_NUMBER": build_number or "0",
        "VMS_BUILD_DATE": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-number", default=None)
    parser.add_argument("--export", action="store_true", help="emit shell export lines")
    parser.add_argument("--github-env", action="store_true", help="emit KEY=value lines")
    args = parser.parse_args()

    values = metadata(args.build_number)
    for key, value in values.items():
        if args.export:
            print(f'export {key}="{value}"')
        else:
            print(f"{key}={value}")

    if values["VMS_BUILD_COMMIT"] == "dev" and not args.github_env:
        print("# note: git metadata unavailable; this is not a release build")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
