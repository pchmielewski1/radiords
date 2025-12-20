#!/usr/bin/env python3
"""Create/update a Gentoo ebuild to track a given GitHub tag.

Input: VERSION like 0.1.0+YYYYMMDD-N
Outputs: creates an ebuild file under gentoo-overlay/media-radio/radiords/.

Naming convention used here:
  radiords-<base>_p<YYYYMMDD>-r<N>.ebuild

Example:
  VERSION=0.1.0+20251220-3 -> radiords-0.1.0_p20251220-r3.ebuild
"""

from __future__ import annotations

import argparse
import pathlib
import re


VERSION_RE = re.compile(r"^(?P<base>\d+\.\d+\.\d+)\+(?P<date>\d{8})-(?P<n>\d+)$")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("version", help="e.g. 0.1.0+20251220-3")
    args = parser.parse_args()

    m = VERSION_RE.match(args.version)
    if not m:
        raise SystemExit("Invalid version format; expected 0.1.0+YYYYMMDD-N")

    base = m.group("base")
    date = m.group("date")
    n = m.group("n")

    tag = f"v{args.version}"

    overlay_dir = pathlib.Path("gentoo-overlay/media-radio/radiords")
    ebuild_dir = overlay_dir
    files_dir = overlay_dir / "files"

    if not ebuild_dir.exists():
        raise SystemExit("gentoo-overlay not found; run from repo root")

    target_name = f"radiords-{base}_p{date}-r{n}.ebuild"
    target_path = ebuild_dir / target_name

    # Use the newest existing ebuild as a template.
    existing = sorted(ebuild_dir.glob("radiords-*.ebuild"))
    if not existing:
        raise SystemExit("No existing ebuild to use as a template")

    template_path = existing[-1]
    txt = template_path.read_text(encoding="utf-8")

    # Update MY_TAG.
    if "MY_TAG=" not in txt:
        raise SystemExit("Template ebuild missing MY_TAG")

    txt = re.sub(r'^MY_TAG=".*"$', f'MY_TAG="{tag}"', txt, flags=re.MULTILINE)

    # Keep SRC_URI consistent if hard-coded.
    txt = re.sub(r"refs/tags/\$\{MY_TAG\}", "refs/tags/${MY_TAG}", txt)

    target_path.write_text(txt, encoding="utf-8")

    print(str(target_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
