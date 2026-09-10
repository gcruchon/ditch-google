# Architecture

## The pipeline

```
Google Takeout ──(user exports to Google Drive)──▶ drive:Takeout
     │
     ▼ rclone copy (resumable)
  local staging ──▶ unpack ──▶ exiftool: sidecar JSON → EXIF
                                    │
                                    ├─▶ proton-drive photo upload   ──▶ My Photos timeline
                                    └─▶ proton-drive album create
                                        proton-drive album add-photo ──▶ albums restored
```

Two decision records explain why the endpoints are what they are:
[ADR-0001](adr/0001-why-not-rclone-gphotos.md) (Takeout, not the Photos API) and
[ADR-0002](adr/0002-why-proton-cli-over-rclone-protondrive.md) (Proton's CLI, not rclone).

## Why metadata repair is load-bearing

Proton's Drive SDK derives a photo's `captureTime`, GPS location and camera info **from the
file's EXIF at upload time**. Takeout strips exactly those fields out of the media and into
sidecar JSON.

So the `fix` stage is not a cosmetic extra — it is the mechanism that makes the migration
correct. Without it the whole library lands on the upload date with no locations, and no
later pass can repair it, because the truth was never uploaded.

## Stages

| Stage | Module | Responsibility |
|---|---|---|
| `fetch` | `photos/fetch.py` | `rclone copy` the export from Drive to local staging |
| `unpack` | `photos/unpack.py` | Extract `.tgz`/`.zip`, safely (no path traversal) |
| `fix` | `photos/sidecar.py`, `photos/metadata.py` | Match sidecars to media, write EXIF |
| `upload` | `photos/upload.py` | `proton-drive photo upload` into the timeline |
| `albums` | `photos/albums.py` | Recreate albums, add members |
| `verify` | `photos/verify.py` | Reconcile the remote against the local ledger |

`photos/pipeline.py` orchestrates them; `photos/report.py` renders the outcome.

## Archives are untrusted input

A Takeout archive nominally comes from Google, but it travelled through a download and
sat on disk where anything could have altered it. [`unpack.py`](../src/ditch_google/photos/unpack.py)
therefore validates every member instead of trusting the paths inside the archive.

| Refused | Example member | Why |
|---|---|---|
| Path traversal | `../../.ssh/authorized_keys` | Writes outside the destination (CVE-2007-4559, Zip Slip) |
| Absolute paths | `/etc/cron.d/evil` | Same, more directly |
| Drive letters | `C:/Windows/system32/evil.dll` | The Windows form of the above |
| Symlinks / hard links | `passwd-link → /etc/passwd` | Turns a later innocuous write into an arbitrary-file write |
| Device nodes, FIFOs | `dev/null` | A photo archive has no legitimate reason to contain one |

Names are checked **before any filesystem call**, so a hostile entry is refused without
touching disk. A second check confirms the resolved target really lands under the
destination, which catches the case where an earlier extracted symlink would redirect an
otherwise-innocent relative path.

Links and special files are *skipped and reported*. Traversal and absolute paths **abort
the entire archive** — one hostile member discredits the whole thing, so we do not keep
extracting the rest of it. The archive is marked `failed` in the ledger with the reason.

## Streaming, not staging everything

A Takeout export of a real library is hundreds of gigabytes. Running each stage over the
whole export in turn would need that much local disk **twice** — the archives plus
everything unpacked out of them.

So [`pipeline.py`](../src/ditch_google/photos/pipeline.py) streams. Each archive is
fetched, unpacked, catalogued, repaired, uploaded, and then **deleted** before the next
one starts:

```
for each archive:
    fetch → unpack → discover → record albums → fix → upload → delete
then once:
    push albums → verify → report
```

Peak disk stays at roughly twice the largest single part. Takeout caps parts at 50 GB and
most people choose 10 GB, so that is tens of gigabytes rather than hundreds.
`--keep-local` disables the deletion for anyone who also wants a local copy.

Deleting as we go forces two design constraints, both of which shaped earlier stages:

- **Album membership is read per archive, into the ledger** — Takeout scatters one album
  across several parts, so the push to Proton has to happen at the end, by which time the
  folders are gone. `record_albums` runs while the files are present; `push_albums` reads
  only the ledger.
- **Capture times are stored in the ledger during `fix`** — verification matches on them,
  and the sidecar JSON is deleted long before verification runs.

Reclaiming happens only *after* the archive's photos are in Proton, so the copy being
deleted is never the only one. An archive is marked `completed` only after its upload
succeeds; marking it earlier would let a resumed run skip work that never happened.

## State and resume

SQLite at `~/.local/state/ditch-google/photos.db` (honouring `XDG_STATE_HOME`), in
[`state.py`](../src/ditch_google/state.py). Four tables:

| Table | Holds | Stages |
|---|---|---|
| `archives` | one row per Takeout part | `pending → fetched → unpacked → completed`, plus `failed` |
| `items` | one row per photo or video | `discovered → fixed → uploaded → verified`, plus `failed`, `skipped` |
| `albums` | Takeout album ↔ Proton album | — |
| `album_items` | membership, with an `added` flag | — |

Two levels of granularity, because they resume differently. **Archives** drive the
streaming loop, which handles one part at a time so peak disk stays bounded. **Items**
carry the per-photo detail that the final report is built from.

`stage` is the furthest point that unit reached. Resume re-enters the pipeline at each
recorded stage, so the whole thing is idempotent by construction — re-running after an
interruption is always safe and never the wrong thing to do. Concretely:

- `add_archive`, `add_item` and `add_album` never reset progress on something already
  recorded, so re-walking an unpacked archive is a no-op rather than a duplicate upload.
- `add_album` being idempotent on title is what stops a resumed run creating a second
  "Holiday 2019" beside the first.
- Advancing an item's stage never discards its `proton_uid` or `sha1`, so verification
  cannot erase what upload recorded.

`proton_uid` records the uploaded item's identity, so the albums pass can add members
without re-listing the remote timeline.

A failed archive is **not** retried automatically. The same failure would most likely
recur, and burying it behind an infinite retry helps nobody — it surfaces in
`photos status` and in the final report instead.

Schema changes are versioned migrations keyed off SQLite's `user_version`. Each migration
commits its schema change and its version bump in one transaction, so a crash can never
leave the two disagreeing.

## Verification

Proton's timeline offers no checksum comparison, so `verify` reconciles **counts and
identities** rather than hashes: `photo timeline --json` and `album photos --json` against
the ledger.

The report lists every item that didn't make it and why — unmatched sidecar, EXIF write
failure, upload error. **Nothing is ever silently skipped.** That report is the tool's main
trust signal; a user deciding whether it's safe to delete their Google library is relying
on it being complete.

## External tools

All subprocess work goes through `proc.py` so timeouts, streaming output and error handling
stay consistent. Each external tool has exactly one wrapper module:

| Tool | Wrapper | Notes |
|---|---|---|
| `rclone` | `rclone.py` | `drive` backend only |
| `exiftool` | `exiftool.py` | `-stay_open` batch mode: one process per run, not per file |
| `proton-drive` | `protondrive.py` | Requires ≥ 0.7.0; young command surface, isolated here |

`exiftool`'s `-stay_open` mode matters at scale — forking once per photo turns a
100,000-item run from minutes into hours.

`protondrive.py` is a deliberate seam. Proton's photo commands shipped in July 2026, so
churn is expected; confining it to one module means a breaking change upstream touches one
file, and a future rclone-based implementation could slot in behind the same interface.

## Credentials

`ditch-google` never handles them. Google auth lives in rclone's config; Proton auth is a
browser sign-in held in the OS keychain by `proton-drive`. There is no password flag and
there will not be one. See [SECURITY.md](../SECURITY.md).
