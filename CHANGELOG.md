# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> Entries up to and including v0.1.0 are hand-written. Later releases are generated from
> [Conventional Commits](https://www.conventionalcommits.org/) — see
> [issue: automate the changelog](https://github.com/gcruchon/ditch-google/issues).

## [Unreleased]

### Added

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
