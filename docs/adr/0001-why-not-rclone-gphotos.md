# ADR-0001 — Google Takeout, not rclone's `gphotos` backend

- **Status:** Accepted
- **Date:** 2026-08-01

## Context

The obvious design for this tool is `rclone sync gphotos: protondrive:` — one tool, two
backends, done. That design does not work, for three independent reasons.

### 1. The API no longer permits it

On **2025-03-31** Google removed the broad `photoslibrary.readonly` scope from the Photos
Library API. Third-party applications may now only read media items **they themselves
uploaded** through the API.

For rclone this means it can no longer download a library it did not create. rclone's own
documentation states plainly: *"From March 31, 2025 rclone can only download photos it
uploaded."* Users hit `403 PERMISSION_DENIED` (rclone#8567), and album access broke
(rclone#8580).

The rclone project has an open issue, **rclone#8434, "googlephotos: plan to remove"**. We
would be building on a backend its own maintainers intend to delete.

### 2. Even before the lockout, it lost data

Two long-standing Google API defects made the backend unsuitable for an archival migration:

- **EXIF location is stripped on download** (Google bug 112096115). GPS data is silently
  lost — exactly the data a migration is supposed to preserve.
- **Videos download heavily transcoded**, materially worse than the Google Photos web UI
  (Google bug 113672044).

The API also does not return media file sizes, so rclone can only do an existence check,
never a real integrity comparison.

### 3. The Picker API is not a substitute

Google's replacement, the Photos Picker API, requires interactive per-session user
selection in a browser. It cannot enumerate a library and is unusable for an unattended
migration of 100,000 items.

## Decision

**Use Google Takeout as the export mechanism.** Instruct the user to export their Photos
library to Google Drive, then use rclone's mature and unaffected **`drive`** backend to
download it.

rclone stays in the stack — just on the Drive leg, not the Photos leg.

## Consequences

**Negative — this is the real cost:**

- The export is not fully automatable. The user must click through Takeout themselves;
  there is no API for it. Our docs carry that burden ([takeout-guide.md](../takeout-guide.md)).
- Takeout **strips metadata out of the media files** into sidecar JSON. We must put it
  back, which is why `sidecar.py` and `metadata.py` exist and are the most complex,
  most heavily tested parts of this codebase.
- Takeout splits exports into parts without keeping a year or an album together, forcing
  album reconstruction into a final pass after all uploads.
- Large exports mean large local staging, hence the streaming one-archive-at-a-time design.

**Positive:**

- Takeout gives **original-quality** files with untranscoded video — strictly better
  fidelity than the API ever offered, even before the restriction.
- It includes album structure and descriptions, which the API path could not reliably give
  us.
- No Google API quota, no OAuth app registration, no verification review.
- No dependency on a backend scheduled for removal.

## Revisit if

Google introduces a real bulk export API with original-quality downloads, or reverses the
2025 scope restriction. Neither looks likely.

## References

- [rclone Google Photos docs](https://rclone.org/googlephotos/) — limitations section
- [rclone#8434 — googlephotos: plan to remove](https://github.com/rclone/rclone/issues/8434)
- [rclone#8567 — 403 PERMISSION_DENIED](https://github.com/rclone/rclone/issues/8567)
- [rclone#8580 — albums no longer accessible](https://github.com/rclone/rclone/issues/8580)
