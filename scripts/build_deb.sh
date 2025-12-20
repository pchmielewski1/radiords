#!/usr/bin/env bash
set -euo pipefail

VERSION="${1:?Usage: build_deb.sh <version>}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${ROOT_DIR}/dist"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$OUT_DIR"

# Layout matches the existing working .deb:
# - /usr/bin/radiords wrapper
# - /usr/lib/radiords/rtlsdr_fm_radio_gui.py main app
mkdir -p "$STAGE/DEBIAN" "$STAGE/usr/bin" "$STAGE/usr/lib/radiords"

sed "s/@VERSION@/${VERSION}/g" "$ROOT_DIR/packaging/debian/control.in" > "$STAGE/DEBIAN/control"

install -m 0755 "$ROOT_DIR/packaging/debian/usr/bin/radiords" "$STAGE/usr/bin/radiords"
install -m 0755 "$ROOT_DIR/rtlsdr_fm_radio_gui.py" "$STAGE/usr/lib/radiords/rtlsdr_fm_radio_gui.py"

DEB_NAME="radiords_${VERSION}_all.deb"

dpkg-deb --build "$STAGE" "$OUT_DIR/$DEB_NAME" >/dev/null

echo "$OUT_DIR/$DEB_NAME"
