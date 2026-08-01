# ditch-google

Migrate your **Google Photos** library to **Proton Photos** — with original dates, GPS
locations and album structure intact.

> [!WARNING]
> **Pre-1.0 and under active development.** The CLI surface is unstable and MINOR releases
> may break flags. It has not yet been exercised against a large real-world library.
> Do not delete anything from Google until you have verified the result yourself.

---

## Why this exists

Google Takeout hands you your photos with the metadata **deliberately removed from the
files** and dumped into sidecar JSON files instead. Upload that raw export anywhere and
your entire library lands on today's date with no locations.

Proton's Drive SDK reads each file's EXIF at upload time to determine when a photo was
taken. So the fix is to put the metadata back *into* the files first. That is what this
tool does, wrapped in a resumable pipeline that can survive a 100,000-photo library.

```
Google Takeout ──(export to Google Drive)──▶ drive:Takeout
     │
     ▼ rclone copy (resumable)
  local staging ──▶ unpack ──▶ exiftool: sidecar JSON → EXIF
                                    │
                                    ├─▶ proton-drive photo upload   ──▶ My Photos timeline
                                    └─▶ proton-drive album create
                                        proton-drive album add-photo ──▶ albums restored
```

## Should you use this?

Proton offers official import routes, and for many people those are the right answer.
Here is an honest comparison:

| | Proton web drag-and-drop | Proton Windows app importer | **ditch-google** |
|---|---|---|---|
| Platforms | Any browser | Windows only | Linux, macOS, Windows |
| Restores original photo dates | Only if EXIF survived | Partially | **Yes — rebuilt from sidecar JSON** |
| Restores GPS locations | Only if EXIF survived | Partially | **Yes, incl. `geoDataExif` fallback** |
| Restores albums | Manual | Yes | Yes |
| Resumable after interruption | No | Partial | **Yes — per-file ledger** |
| Unattended / scriptable | No | No | **Yes — headless, `--json`** |
| Report of what did *not* transfer | No | No | **Yes** |
| Effort | Lowest | Low | Install three CLIs |

**Use Proton's own tools if** your library is small, you're on Windows, and you're happy
to supervise it. → [Proton's import guide](https://proton.me/support/how-to-import-from-google-photos)

**Use this if** your library is large, you're not on Windows, you want it unattended, or
you care that the dates and locations come out right.

## Requirements

Three external tools, detected but never bundled. Run `ditch-google doctor` to check them.

| Tool | Purpose | Install |
|---|---|---|
| [`rclone`](https://rclone.org/) | Download the Takeout export from Google Drive | `brew install rclone` |
| [`exiftool`](https://exiftool.org/) | Write metadata back into the files | `brew install exiftool` |
| [`proton-drive`](https://proton.me/blog/proton-drive-cli) **≥ 0.7.0** | Upload to Proton Photos | [Proton Drive downloads](https://proton.me/drive/download) |

`proton-drive` v0.7.0 is the first release with photo and album commands. Earlier versions
will not work.

## Install

```bash
uv tool install ditch-google      # or: pipx install ditch-google
```

## Usage

```bash
# 1. Export your library with Google Takeout, choosing "Add to Drive" as the destination.
#    See docs/takeout-guide.md for the settings that matter.

# 2. Configure rclone for Google Drive, and sign in to Proton.
rclone config          # create a "drive" remote
proton-drive login     # opens your browser; no password on the command line

# 3. Check everything is ready.
ditch-google doctor

# 4. Migrate.
ditch-google photos migrate --source drive:Takeout
```

The pipeline is resumable — interrupt it and re-run the same command. Individual stages can
also be run on their own:

```bash
ditch-google photos fetch|unpack|fix|upload|albums|verify
ditch-google photos status
```

## How your credentials are handled

`ditch-google` never sees them. Google auth lives in rclone's config; Proton auth is a
browser sign-in stored in your OS keychain by the `proton-drive` CLI. This tool has no
password flag and never reads, stores, or logs a credential. See [SECURITY.md](SECURITY.md).

## Known limitations

- **Google Takeout is the only export path.** Google restricted the Photos Library API on
  2025-03-31 so apps can only read media they uploaded themselves; `rclone`'s `gphotos`
  backend can no longer download your library. See [ADR-0001](docs/adr/0001-why-not-rclone-gphotos.md).
- Takeout splits large exports into parts without keeping a year together, so album
  reconstruction runs as a final pass once everything is uploaded.
- Proton's photo and album CLI commands are new (July 2026). Expect rough edges and please
  report them.

## Documentation

- [Google Takeout walkthrough](docs/takeout-guide.md)
- [Proton setup](docs/proton-setup.md)
- [Architecture](docs/architecture.md)
- [Troubleshooting](docs/troubleshooting.md)
- Decision records: [why not rclone gphotos](docs/adr/0001-why-not-rclone-gphotos.md) ·
  [why the Proton CLI over rclone protondrive](docs/adr/0002-why-proton-cli-over-rclone-protondrive.md)

## Contributing

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). The project uses GitHub
Flow, [Conventional Commits](https://www.conventionalcommits.org/) and
[SemVer](https://semver.org/), all enforced in CI.

## License

[Apache-2.0](LICENSE) © 2026 Gilles Cruchon

Not affiliated with, endorsed by, or supported by Proton AG or Google LLC.
