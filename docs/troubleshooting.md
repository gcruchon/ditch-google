# Troubleshooting

Start with `ditch-google doctor` — it catches most setup problems and its output is safe to
paste into an issue.

## Setup

### `proton-drive: command not found`

Not installed, or not on your `PATH`. See [proton-setup.md](proton-setup.md).

### `proton-drive version 0.6.x is too old`

Photo and album commands arrived in `cli/v0.7.0`. Earlier versions cannot write to the
Photos timeline. Download a newer binary from
[Proton Drive downloads](https://proton.me/drive/download).

### `Not signed in to Proton`

Run `proton-drive login`. If it keeps failing, the stored session may be stale — try
`proton-drive logout` then log in again.

### `directory not found: drive:Takeout`

Either the export isn't finished, it wasn't sent to Drive, or your rclone remote isn't
called `drive`. Check with `rclone listremotes` and `rclone ls drive:`.

## During the migration

### It stopped partway through

Re-run the same command. The pipeline is resumable and tracks every item's stage in a local
ledger — completed work is not repeated and no duplicates are created.

```bash
ditch-google photos status     # see where it got to
ditch-google photos migrate    # continue
```

### Out of disk space

The pipeline needs roughly twice the size of the largest single Takeout archive. Point
`--work-dir` at a bigger volume, or re-export from Takeout with a smaller part size (which
lowers peak disk use, at the cost of more album fragmentation).

### Out of Proton storage

Free up space or upgrade your plan, then re-run — already-uploaded photos are not
re-uploaded.

## Results that look wrong

### Photos are in Proton but all dated today

The metadata stage didn't run or didn't apply. This is the failure mode the tool exists to
prevent, so it's worth diagnosing rather than working around.

Check a file before upload:

```bash
exiftool -DateTimeOriginal -GPSLatitude -GPSLongitude /path/to/staged/photo.jpg
```

If those are empty, the sidecar wasn't matched. `ditch-google photos verify` lists every
unmatched file. Please report the filenames via the
[Takeout edge case template](https://github.com/gcruchon/ditch-google/issues/new?template=takeout_edge_case.yml)
— those reports are what improve the matcher.

Note that re-running `fix` on already-uploaded photos won't repair them in Proton; the
metadata is read at upload time.

### Locations are missing

Often genuine — Google only has coordinates if Location History was on. But check whether
the sidecar has a zeroed `geoData` alongside a populated `geoDataExif`; this tool prefers
the latter, and if you're seeing it ignored, that's a bug worth reporting.

### Some photos didn't transfer

`ditch-google photos verify` reports every one with a reason. The common causes are
unsupported file types (Proton skips non-media), zero-byte files in the export, and
genuinely corrupt files Google exported that way.

### Albums are missing or empty

Album reconstruction runs as a **final pass** after all archives upload — it will look
incomplete until the run finishes. If it's still wrong afterwards, check
`proton-drive album list --json` and open an issue.

### Duplicate photos in Proton

Takeout intentionally includes a photo in both its album folder and its year folder;
deduplication should handle this. If you see genuine duplicates, note whether the run was
interrupted and resumed, and report it.

## Getting help

Include the output of `ditch-google doctor`, the command you ran, and `--verbose` logs. Use
the [bug report template](https://github.com/gcruchon/ditch-google/issues/new?template=bug_report.yml).

**Never paste credentials or real photos.** Redact coordinates and names from any sidecar
JSON — the structure is what's useful, not the values.

Problems with `rclone`, `exiftool` or `proton-drive` themselves belong upstream. But if this
tool is *using* them wrongly, that's ours — please tell us.
