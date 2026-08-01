# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> Entries up to and including v0.1.0 are hand-written. Later releases are generated from
> [Conventional Commits](https://www.conventionalcommits.org/) — see
> [issue: automate the changelog](https://github.com/gcruchon/ditch-google/issues).

## [Unreleased]

### Added

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
