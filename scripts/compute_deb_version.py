#!/usr/bin/env python3
"""Compute the next Debian-style version for radiords.

Version format used:
  0.1.0+YYYYMMDD-N

- YYYYMMDD defaults to today's UTC date unless provided.
- N increments for the same base_version + date by inspecting git tags:
    v0.1.0+YYYYMMDD-N

Outputs the computed version to stdout.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys


TAG_RE = re.compile(r"^v(?P<base>\d+\.\d+\.\d+)\+(?P<date>\d{8})-(?P<n>\d+)$")


def _run(*args: str) -> str:
    return subprocess.check_output(args, text=True).strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="0.1.0", help="Base SemVer, e.g. 0.1.0")
    parser.add_argument("--date", default="", help="UTC date YYYYMMDD (optional)")
    args = parser.parse_args()

    base = args.base
    if args.date:
        date = args.date
    else:
        date = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")

    # Ensure tags are available.
    try:
        _run("git", "fetch", "--tags", "--force")
    except Exception:
        pass

    try:
        tags = _run("git", "tag", "--list").splitlines()
    except Exception:
        tags = []

    max_n = 0
    for t in tags:
        m = TAG_RE.match(t.strip())
        if not m:
            continue
        if m.group("base") != base:
            continue
        if m.group("date") != date:
            continue
        try:
            max_n = max(max_n, int(m.group("n")))
        except ValueError:
            continue

    version = f"{base}+{date}-{max_n + 1}"
    sys.stdout.write(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
