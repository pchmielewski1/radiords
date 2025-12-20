# Agent policy

This repository is maintained with a simple rule:

- After every code change, I update the documentation to reflect that change.

Scope of “documentation” in this repo typically includes:

- `docs/installation_requirements.md`
- `docs/rtlsdr_fm_radio_gui_low_level_migration.md`

If a change affects UX, settings, dependencies, or runtime behavior, the docs must be updated in the same work session.

## Git basics (for this repo)

**What is Git?**

Git is a version control system. It stores the full history of changes as a sequence of commits.
This lets you:

- see what changed and when,
- revert or bisect bugs,
- collaborate safely,
- publish versions (tags) and releases.

**Key terms**

- **Working tree**: your current files on disk
- **Stage / index**: the set of changes selected for the next commit (`git add`)
- **Commit**: a snapshot with message and author
- **Branch**: a movable pointer to commits (we use `main`)
- **Remote**: the GitHub repository (`origin`)
- **Tag**: an immutable version label (used for Releases)

## Default workflow rules (automation intent)

Goal: keep GitHub up to date without extra back-and-forth.

1) **After every meaningful change set** (feature, bugfix, docs change):
	- update docs (as per the repo rule),
	- run a minimal sanity check when possible (`python3 -m py_compile rtlsdr_fm_radio_gui.py`),
	- commit and push to `origin/main`.

2) **No questions for routine pushes**:
	- do `git add`, `git commit`, `git push` automatically.

3) **Still ask before destructive operations**:
	- force-push (`--force*`), rewriting history, deleting tags/releases,
	- anything that could remove data from GitHub.

### What counts as a “bigger change”

Treat a change as “bigger” (i.e. warrants an explicit commit/push boundary and docs update in the same session) if **any** of the following is true:

- **Code size:** >10 non-whitespace lines changed in `rtlsdr_fm_radio_gui.py`.
- **User experience:** any UI text/layout change, new/removed buttons/fields, new setting, or changed default.
- **Runtime behavior:** changes to threads/process management, shutdown behavior, SDR/GR pipeline, audio/RDS pipeline, recording.
- **Dependencies:** adding/removing Python deps or required system tools.
- **Data format:** changes to `fm_radio_settings.json` keys, `fm_stations_database.json` schema, or file locations (XDG/paths).

For “small changes” (typos, tiny doc fixes) it is still OK to commit/push, but do not force a release/tag.

### Commit message convention (keep it consistent)

Use short, consistent prefixes:

- `Fix:` bug fix
- `Feat:` new feature
- `Docs:` documentation only
- `Refactor:` internal rework without behavior change
- `Build:` packaging/build scripts
- `Release:` release/tag/packaging bump (no code changes unless necessary)

Format:

`<Prefix>: <short summary in English>`

Examples:

- `Fix: avoid blocking UI on shutdown`
- `Feat: add FM band presets`
- `Docs: update Debian requirements`

## Versioning & release numbering (simple and sequential)

We use a Debian-style sequential revision suffix for releases built from this repo.

- Release tag format: `v0.1.0+YYYYMMDD-N`
  - `YYYYMMDD` = release date
  - `N` = sequential build/revision counter for that date (1, 2, 3, ...)

Example: `v0.1.0+20251220-3`.

Rule: **every time we publish a new `.deb` to GitHub Releases, increment `N` by 1**.

## Debian/Kali/Ubuntu release process (GitHub Releases)

Source of truth for `.deb` artifacts: `dist/`.

When preparing a release:

1) Ensure repo is clean and pushed:
	- `git status` is clean
	- `git push` is up to date

2) Build the `.deb` into `dist/` (project-specific build step).

3) Create and push an annotated tag (example):
	- `git tag -a v0.1.0+20251220-3 -m "radiords 0.1.0+20251220-3"`
	- `git push origin v0.1.0+20251220-3`

4) Publish GitHub Release for that tag:
	- attach `dist/radiords_0.1.0+20251220-3_all.deb`
	- release title: `radiords 0.1.0+20251220-3`

Note: this environment may not have `gh` installed; in that case publish via GitHub Web UI.

## Gentoo overlay maintenance

We keep an in-repo overlay under `gentoo-overlay/`.

When a new Debian release is created:

1) Update the Gentoo ebuild to track the new GitHub tag:
	- edit `MY_TAG` in `gentoo-overlay/media-radio/radiords/radiords-*.ebuild`
	- bump the ebuild filename revision if needed (e.g. `-r2` → `-r3`)

2) Commit and push the overlay update.

Note: Gentoo users must regenerate `Manifest` on their machine after pulling the overlay.
