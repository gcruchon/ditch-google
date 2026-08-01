# Setting up the Proton CLI

## Install `proton-drive` (≥ 0.7.0 required)

Download the binary for your platform from
[Proton Drive downloads](https://proton.me/download/drive/cli/index.html).

```bash
chmod +x proton-drive            # macOS / Linux
sudo mv proton-drive /usr/local/bin/
proton-drive version
```

> [!IMPORTANT]
> **Version 0.7.0 or later is required.** Photo and album commands landed in `cli/v0.7.0`
> (2026-07-30). Earlier versions can write files to Proton Drive but cannot reach the
> Photos timeline at all, which is the entire point of this tool. `ditch-google doctor`
> checks this for you.

There is no Homebrew or apt package yet, so upgrades are manual — re-download and replace
the binary.

## Sign in

```bash
proton-drive auth login
```

This opens your browser. **No password is typed on the command line**, and the session is
stored in your OS keychain (macOS Keychain, Windows Credential Manager, or libsecret on
Linux). Two-factor authentication is handled in the browser as normal.

`ditch-google` never sees, reads, or stores any of this — see [SECURITY.md](../SECURITY.md).

Confirm it worked:

```bash
proton-drive photo timeline --json | head
```

## Storage

Check you have room before starting. A Google Photos library moved at original quality is
usually **larger** than Google reported, because Google's own "storage saver" compression
isn't applied to what Takeout gives you.

Proton Free provides a few GB; migrating a real library generally needs a paid plan. It is
much less painful to sort this out before the migration than to have it fail two-thirds of
the way through — though if it does, the run is resumable once you have space.

## Configure rclone for Google Drive

The other half of the setup:

```bash
rclone config
```

Choose `n` for a new remote, name it `drive`, pick the **Google Drive** backend, and accept
the defaults. It opens a browser for Google sign-in. Read-only scope is sufficient and
safer — this tool never writes to or deletes from Google.

Verify:

```bash
rclone ls drive:Takeout
```

## Check everything at once

```bash
ditch-google doctor
```

This verifies all three binaries, the `proton-drive` version floor, that you're signed in
to Proton, that the rclone remote resolves, and that you have enough free disk space for
staging.
