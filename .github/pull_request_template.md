<!--
  The PR title must be a valid Conventional Commit - it becomes the squash-merge
  subject and the changelog entry. CI will reject it otherwise.

  Examples:
    feat(sidecar): match sidecars truncated to the 46-character budget
    fix(metadata): fall back to geoDataExif when geoData is zeroed
-->

## What and why

<!-- What does this change, and what problem does it solve? Link any issue with "Closes #123". -->

## How it was tested

<!-- Commands you ran. Note if you tested against a real Takeout export or Proton account. -->

## Checklist

- [ ] PR title is a valid Conventional Commit
- [ ] `uv run pytest` passes
- [ ] `uv run ruff check .` and `uv run mypy` pass
- [ ] New behaviour has tests; bug fixes have a regression test
- [ ] Sidecar/metadata changes include a fixture in `tests/fixtures/takeout/`
- [ ] Docs updated if the CLI surface or config schema changed
- [ ] `CHANGELOG.md` updated under `[Unreleased]` (until changelog generation lands)

## Breaking change?

<!-- If yes: use `!` in the title and add a BREAKING CHANGE footer to the commit,
     stating what breaks and how users migrate. If no, delete this section. -->
