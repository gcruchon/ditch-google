# Security Policy

## Supported versions

While the project is pre-1.0, only the latest release receives security fixes.

| Version | Supported |
|---|---|
| latest `0.y.z` | ✅ |
| anything older | ❌ |

## Reporting a vulnerability

**Please do not open a public issue.**

Report privately via GitHub's
[private vulnerability reporting](https://github.com/gcruchon/ditch-google/security/advisories/new).

Please include: what you found, how to reproduce it, and what an attacker could achieve.
Redact any real credentials or personal data from your report.

You can expect an acknowledgement within 7 days and an assessment within 30. If a fix is
warranted we'll agree a disclosure timeline with you and credit you in the advisory unless
you'd rather stay anonymous.

## How this tool handles your credentials

**It doesn't.** This is a deliberate design constraint, not an accident:

- **Google** — authentication lives entirely in `rclone`'s own config. We shell out to
  `rclone` and never read its config file.
- **Proton** — authentication lives entirely in the `proton-drive` CLI, which uses a
  browser sign-in and stores the session in your OS keychain (Keychain / Credential
  Manager / libsecret). We shell out to it and never read that store.
- `ditch-google` has **no password or token flag**, and will not gain one. A PR adding one
  will be declined.
- Credentials are never written to the state database, logs, or `--json` output.

If you ever see a credential appear in this tool's output, that is a security bug — please
report it.

## What the tool does touch

Being clear about the blast radius:

- **Reads** your Google Drive via `rclone`, limited to the source path you pass.
- **Writes** to your local working directory and, unless `--keep-local` is set, **deletes
  local staging files** after each archive is uploaded and verified.
- **Modifies media files in place** during the metadata stage (`exiftool`). This happens on
  the local staging copy, never on your Google originals.
- **Uploads** to your Proton Photos library and creates albums there.
- **Never deletes anything** from Google or from Proton.

## Scope

In scope: credential leakage, path traversal in archive extraction, command injection via
crafted filenames or sidecar JSON, and unsafe handling of untrusted Takeout content.

Out of scope: vulnerabilities in `rclone`, `exiftool`, or the `proton-drive` CLI — please
report those to their maintainers. Do tell us if we're *using* them unsafely.
