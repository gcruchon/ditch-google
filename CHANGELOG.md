# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> Entries up to and including v0.1.0 are hand-written. Later releases are generated from
> [Conventional Commits](https://www.conventionalcommits.org/) — see
> [issue: automate the changelog](https://github.com/gcruchon/ditch-google/issues).

## [Unreleased]

### Added

- `ditch-google photos albums` — recreates Takeout albums in Proton Photos, merging
  albums split across archives and skipping Google's `Photos from <year>` folders.
  Idempotent: existing albums are reused and photos are never added twice.
- **A marker album per migration** (`Imported from Google Photos - <date> (<id>)`)
  holding everything one run uploaded. Proton has no upload-batch concept, so this gives
  a run a visible handle in the Proton app — somewhere to look, something to delete —
  without needing this tool or its database.
- **An XMP stamp in every processed file** recording the tool, version and migration id,
  written to XMP Media-Management history so the camera's own `EXIF:Software` is left
  intact. The stamp travels with the file, outside Proton and beyond the ledger.
- `ditch-google photos verify` — reconciles the ledger against the Proton timeline and
  prints the final report, naming everything that did not complete. Supports `--json`.

- `ditch-google photos upload` — uploads fixed media into the Proton Photos timeline via
  the official CLI, in batches, recording per-file outcomes in the ledger. Resume is safe:
  Proton de-duplicates on name plus SHA1, so re-running skips photos already there.
- `protondrive` — wrapper around every `proton-drive` command the pipeline uses.

- `ditch-google photos fix` — writes sidecar metadata back into the media files with
  exiftool: capture time (with an explicit UTC offset), GPS, description and tagged
  people, plus QuickTime tags for video and the file's modification time. This is what
  makes photos land on the right date in the Proton timeline.
- `exiftool` — a long-lived `-stay_open` session reused across the whole run. Starting
  exiftool per photo would add over five hours to a 100,000-photo library.
- `--prefer-existing-exif` on `photos fix`, to fill only gaps instead of letting the
  sidecar overwrite metadata already in the file.

- `sidecar` — pairs each media file with its Takeout metadata JSON, handling the modern
  `.supplemental-metadata` form, the legacy and stem-only forms, Google's 46-character
  truncation, duplicate `(1)` markers, localised `-edited` copies, and live-photo videos
  that inherit the still image's sidecar.
- `discover` — walks an unpacked archive and records every media file in the ledger with
  its sidecar. Unmatched media is recorded too, never dropped, and `photos unpack` now
  reports how many files have no metadata.

- `ditch-google photos fetch` — lists the Takeout parts in Drive with `rclone lsjson`,
  records them in the ledger and downloads them with a live progress bar. Non-archive
  files such as Takeout's HTML index are ignored. Resumable: already-downloaded parts
  are skipped.
- `ditch-google photos unpack` — extracts `.tgz` and `.zip` parts, **validating every
  member** against path traversal (CVE-2007-4559 / Zip Slip), absolute paths, and
  symlinks or hard links pointing outside the destination. Device nodes and other
  special members are skipped and reported. One hostile member aborts the whole archive.
- `proc.stream` can now stream stderr as well as stdout, which is where rclone writes
  its JSON progress log.

- `state` — the resumable SQLite ledger tracking archives, items, albums and album
  membership, with versioned migrations. Every write is idempotent, so re-running after
  an interruption never duplicates an upload, resets progress, or creates a second copy
  of an album.
- `ditch-google photos status` — migration progress from the ledger, including how many
  items could not be matched to a sidecar. Supports `--json`.

- `ditch-google doctor` — preflight checks for `rclone`, `exiftool` and `proton-drive`,
  including the `proton-drive` ≥ 0.7.0 floor required for photo support, Proton sign-in,
  the Takeout source location and free disk space. Every failure carries a specific
  remedy. Supports `--json`, and exits non-zero so it can gate a script.
- `proc` — shared subprocess runner used for all external tools, with consistent
  timeouts, output capture and errors.
- Test fixtures that stub the external tools, so the suite runs without `rclone`,
  `exiftool` or `proton-drive` installed.

### Fixed

- `photos verify` now promotes confirmed items to `verified` and closes finished
  archives. Without it, items sat at `uploaded` forever and a successful migration could
  never report as complete.
- `write_metadata` accepted a `migration_id` but never passed it to the argument builder,
  so no file was ever stamped.

- **Photos no longer land in the Proton timeline at the wrong time.** Capture times were
  written as UTC with an explicit `OffsetTimeOriginal`, but Proton ignores that tag and
  reads `DateTimeOriginal` as local time — shifting every photo by the migrating
  machine's UTC offset, and onto the wrong calendar day for anyone far enough from UTC.
  Timestamps are now written as local wall-clock time with the matching offset. Found by
  testing against a real Proton account.

- Corrected the sidecar truncation budget from 51 to **46** characters, and clarified that
  the clip applies to `<media filename>.supplemental-metadata` before `.json` is appended.
  The earlier figure would have silently failed to match long-named photos, losing their
  dates and locations.
- Corrected the documented `proton-drive` commands: sign-in is `auth login` /
  `auth logout`, and the version probe is `version`.

### Project

- Project scaffolding: packaging, Apache-2.0 license, ruff + mypy (strict) + pytest,
  pre-commit hooks, CI and release workflows, issue and PR templates, Dependabot.
- Governance: README with an honest comparison against Proton's official import routes,
  CONTRIBUTING, Code of Conduct, security policy, CODEOWNERS.
- Decision records: [ADR-0001](docs/adr/0001-why-not-rclone-gphotos.md) on why rclone's
  `gphotos` backend is unusable, and
  [ADR-0002](docs/adr/0002-why-proton-cli-over-rclone-protondrive.md) on choosing Proton's
  official CLI over rclone's `protondrive` backend.

[Unreleased]: https://github.com/gcruchon/ditch-google/commits/main
