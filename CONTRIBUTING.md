# Contributing to ditch-google

Thanks for helping people get their data out of Google. This document covers the workflow
and the conventions CI enforces.

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## Getting set up

```bash
git clone https://github.com/gcruchon/ditch-google
cd ditch-google
uv sync                  # creates .venv and installs dev dependencies
uv run pre-commit install --hook-type pre-commit --hook-type commit-msg
```

The `commit-msg` hook is what checks your commit messages locally — install it, or CI will
be the first thing to tell you the format is wrong.

Verify your setup:

```bash
uv run pytest
uv run ruff check .
uv run mypy
```

You do **not** need `rclone`, `exiftool` or `proton-drive` installed to work on most of the
codebase. The `fake_tool` fixture in [`tests/conftest.py`](tests/conftest.py) generates stub
binaries per test, so each one controls exactly what its tool prints and exits with. Tests
that need the real thing are marked and skipped automatically:

```bash
uv run pytest -m "not requires_exiftool and not requires_rclone and not requires_proton"
```

## Workflow — GitHub Flow

`main` is always releasable and is protected. All work happens on a short-lived branch:

```bash
git switch -c feat/sidecar-truncation main
# ... commit ...
gh pr create --fill
```

Branch prefixes: `feat/`, `fix/`, `docs/`, `chore/`, `refactor/`, `test/`, `ci/`.

PRs are **squash-merged**. The squash commit subject becomes a changelog entry, so your
**PR title must itself be a valid conventional commit** — CI lints it.

## Commit messages — Conventional Commits

We follow [Conventional Commits 1.0.0](https://www.conventionalcommits.org/):

```
<type>(<scope>): <description>

[optional body]

[optional footer(s)]
```

**Types:** `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`,
`chore`, `revert`.

**Scopes** follow the module layout: `sidecar`, `metadata`, `rclone`, `protondrive`,
`albums`, `upload`, `fetch`, `unpack`, `verify`, `cli`, `state`, `doctor`, `docs`.

Real examples from this repo:

```
feat(sidecar): match sidecars truncated to the 46-character budget
fix(metadata): fall back to geoDataExif when geoData is zeroed
docs(adr): record why rclone's gphotos backend is unusable
test(sidecar): add fixtures for motion-photo pairing
```

Breaking changes need a `!` **and** a footer explaining the migration:

```
feat(cli)!: rename --dest to --target

BREAKING CHANGE: `--dest` is now `--target`. Update any scripts that pass it.
```

If you're unsure, `uv run cz commit` walks you through it interactively.

## Versioning

[SemVer 2.0.0](https://semver.org/), derived from commit types: `fix:` → PATCH,
`feat:` → MINOR, `!` / `BREAKING CHANGE:` → MAJOR.

While on `0.y.z` the CLI surface is unstable and MINOR bumps may break flags.

**The public API is the CLI surface plus the config file schema** — not the Python
internals. Renaming an internal function is `refactor:`, not a breaking change.

## Code standards

- **Type annotations everywhere.** `mypy --strict` must pass.
- **`ruff` for lint and format.** Line length 100.
- **No `print`.** Use `rich`'s `console` for human-facing output, and **`typer.echo` for
  anything a script might parse** — version strings, `--json` payloads. rich
  syntax-highlights values like version numbers, which injects ANSI escapes into stdout
  whenever colour is forced (as it is on CI).
- **Never log a credential**, a token, or a full Proton session identifier.
- **Subprocess calls go through `proc.py`** — don't call `subprocess` directly, so timeouts,
  streaming and error handling stay consistent.

## Testing expectations

- New behaviour needs a test. Bug fixes need a regression test.
- [`sidecar.py`](src/ditch_google/photos/sidecar.py) has the highest bar in the repo:
  every naming variant it handles has an explicit case in
  [`tests/test_sidecar.py`](tests/test_sidecar.py). If you've seen a real Takeout filename
  we don't match, **a failing test case alone is a genuinely valuable contribution** —
  you don't have to write the matcher. Give the media filename and the sidecar filename
  exactly as they appear.
- Never make a test hit the network or a real Proton account.

## Reporting Takeout edge cases

Google's export format is inconsistent and undocumented. The most useful bug reports
include the **exact filenames** (media and sidecar) and the sidecar's JSON with any
personal data redacted. Please don't attach real photos.

## Security

Do not open a public issue for a security problem — see [SECURITY.md](SECURITY.md).
