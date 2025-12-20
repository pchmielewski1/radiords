# radiords Gentoo overlay (local)

This folder is a minimal Gentoo overlay containing an ebuild for `media-radio/radiords`.

## Install (example)

1) Copy this overlay to your Gentoo machine (or add it as a local overlay):

```bash
sudo mkdir -p /var/db/repos/radiords-overlay
sudo rsync -a gentoo-overlay/ /var/db/repos/radiords-overlay/
```

2) Add it to Portage:

```bash
sudo tee /etc/portage/repos.conf/radiords-overlay.conf >/dev/null <<'EOF'
[radiords-overlay]
location = /var/db/repos/radiords-overlay
masters = gentoo
auto-sync = no
EOF
```

3) Generate the Manifest and install:

```bash
cd /var/db/repos/radiords-overlay/media-radio/radiords
sudo ebuild radiords-0.1.0_p20251220-r2.ebuild manifest
sudo emerge -av media-radio/radiords
```

## Notes

- The ebuild fetches the upstream GitHub tag `v0.1.0+20251220-2`.
- The app uses per-user XDG directories for settings/DB/logs when installed system-wide.
