# ADR-0002 — Upload via Proton's official CLI, not rclone's `protondrive` backend

- **Status:** Accepted
- **Date:** 2026-08-01
- **Supersedes:** the original plan to use `rclone protondrive:` for the upload leg

## Context

Having settled on Takeout for export ([ADR-0001](0001-why-not-rclone-gphotos.md)), we need
a way to write into Proton. Two options exist.

### Option A — rclone `protondrive` backend

Reverse-engineered from Proton's open-source clients via
[Proton-API-Bridge](https://github.com/henrybear327/Proton-API-Bridge). Its limitations are
disqualifying for a *photos* migration:

- **It cannot reach the Photos volume.** Proton stores photos on a separate volume from
  `/My files`, and the backend only sees the latter. Photos uploaded this way are inert
  files — they never appear in the Photos timeline, and the mobile gallery never shows them.
- **No album support.** Album structure from Takeout would be lost entirely.
- **Tier 4 (Experimental)**, explicitly a best-effort reimplementation against an
  undocumented API.
- **No modification-time support**, so even ordering by date fails.

That reduces the tool to "a folder of files in Proton Drive" — which the user could achieve
by dragging a folder into the web app.

### Option B — the official `proton-drive` CLI

Proton shipped an official CLI, GA on 2026-06-09, built on the same official Drive SDK that
powers their own clients ([ProtonDriveApps/sdk](https://github.com/ProtonDriveApps/sdk),
MIT). Photo and album support landed in **`cli/v0.7.0` (2026-07-30)**:

```
photo upload      photo download      photo timeline
album create      album list          album update      album delete
album add-photo   album remove-photo  album photos
```

`photo upload` writes into **My Photos** — the real timeline — and every command supports
`--json`. Authentication is a browser sign-in stored in the OS keychain, so no credential
ever passes through our process.

### The decisive detail

The SDK's `generateAdditionalPhotoNodeMetadata` derives `captureTime`, GPS location and
camera information **from each file's EXIF at upload time**.

This turns our metadata-repair stage from a nice-to-have into the mechanism that makes the
migration correct: repair the EXIF from Takeout's sidecar JSON, and photos land on their
original date in the Proton timeline with their locations intact. Skip it, and the entire
library piles onto the upload date. Option A offers no equivalent hook at all.

## Decision

**Use the official `proton-drive` CLI for the upload leg**, requiring **≥ 0.7.0**.

Isolate all interaction with it behind a single module, `protondrive.py`, so churn in a
young command surface touches one file.

## Consequences

**Negative:**

- A third external binary the user must install, and one Proton distributes as a download
  rather than through package managers.
- The photos commands are **days old** at time of writing. We mitigate with a hard version
  floor checked in `doctor`, feature detection, and the single-module wrapper.
- Not currently in Homebrew or apt, so install instructions are manual.
- `photo upload` flattens folder structure. Harmless here — the timeline is date-driven and
  we restore album membership explicitly — but it means the on-Proton layout is not a
  mirror of the local one.
- No checksum verification is possible against the timeline, so `verify.py` reconciles
  counts and identities rather than hashes.

**Positive:**

- Photos land in the **actual Photos timeline**, visible in every Proton client.
- **Albums are preserved** — the single feature that makes this tool worth publishing
  rather than telling users to drag a folder into the browser.
- Official and supported, tracking the same SDK as Proton's own apps, rather than a
  reverse-engineered bridge.
- Credentials never touch our process.
- Built-in conflict strategies (`keep-both`, `skip`) give us safe resume semantics for free.

## Revisit if

Proton discontinues the CLI, or rclone's `protondrive` backend gains Photos-volume and
album support with EXIF-derived capture times. In that case the `protondrive.py` boundary
is where a second implementation would slot in.

## References

- [ProtonDriveApps/sdk](https://github.com/ProtonDriveApps/sdk) — official SDK and CLI, MIT
- [`cli/CHANGELOG.md` v0.7.0](https://github.com/ProtonDriveApps/sdk/blob/main/cli/CHANGELOG.md) — "Add albums commands", "Upload photos to timeline"
- [`additionalNodeMetadata` README](https://github.com/ProtonDriveApps/sdk/blob/main/client/js/src/additionalNodeMetadata/README.md) — EXIF → `captureTime`, `tags`
- [Proton Drive CLI announcement](https://proton.me/blog/proton-drive-cli)
- [rclone Proton Drive docs](https://rclone.org/protondrive/) — Tier 4, no modtimes
- [rclone forum — Photos folder not visible](https://forum.rclone.org/t/proton-drive-photos-folder-not-visible/49393)
