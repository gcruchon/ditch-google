# Exporting your Google Photos library with Takeout

This is the one step that **cannot be automated**. Google provides no API for requesting a
Takeout export, and the Photos Library API can no longer read a library it didn't create
(see [ADR-0001](adr/0001-why-not-rclone-gphotos.md)). Ten minutes of clicking here saves
you a broken migration later, so the settings below matter.

## 1. Start the export

Go to [takeout.google.com](https://takeout.google.com) and choose **Deselect all**, then
select **Google Photos** only. Exporting everything at once makes the archives larger and
slower for no benefit.

## 2. Choose which albums to include

Click **All photo albums included** and check what's selected.

Google lists your albums *and* automatic "Photos from YYYY" folders. Leave everything
selected for a complete migration. If you deselect year folders, photos that aren't in any
album will be missing from the export entirely.

## 3. Destination — this is the important one

| Setting | Choose | Why |
|---|---|---|
| Delivery method | **Add to Drive** | Lets `rclone` download it reliably and resumably. A browser download of 200 GB across a dozen links will fail somewhere. |
| Frequency | Export once | |
| File type | **`.tgz`** | Better compression than `.zip`, and Takeout's `.zip` splitting is more awkward to reassemble. |
| File size | **50 GB** | The largest available. Fewer archives means less album fragmentation — see below. |

> [!IMPORTANT]
> **Pick the largest file size.** Google splits the export into parts without keeping a
> year or an album together, so a photo and its album can land in different archives. Fewer,
> larger parts means less scattering. This tool handles the fragmentation regardless — album
> restoration runs as a final pass after everything uploads — but larger parts are faster
> and less error-prone.

## 4. Wait

Google emails you when it's ready. **This can take hours or several days** for a large
library; there's no way to speed it up. The export expires after about a week, so start the
migration reasonably promptly.

## 5. Check what landed in Drive

The export appears in a `Takeout` folder in your Google Drive:

```bash
rclone ls drive:Takeout
```

You should see files like `takeout-20260801T120000Z-001.tgz`. If you see far fewer archives
than you expect from your library size, the export may be incomplete — check your email for
a partial-failure notice from Google before proceeding.

## 6. Migrate

```bash
ditch-google doctor
ditch-google photos migrate --source drive:Takeout
```

## What's inside the export, and why it needs repair

Takeout gives you original-quality files — better than any API path — but it **moves the
metadata out of the media files** into sidecar JSON:

```
Takeout/Google Photos/Photos from 2023/
    IMG_20230715_142233.jpg
    IMG_20230715_142233.jpg.supplemental-metadata.json
Takeout/Google Photos/Summer in Lisbon/
    metadata.json                       ← album title and description
    IMG_20230715_142233.jpg             ← the same photo again
```

A sidecar looks roughly like this:

```json
{
  "title": "IMG_20230715_142233.jpg",
  "photoTakenTime": { "timestamp": "1689430953" },
  "geoData":     { "latitude": 0.0,   "longitude": 0.0 },
  "geoDataExif": { "latitude": 38.72, "longitude": -9.14 }
}
```

Upload that `.jpg` as-is and it lands in Proton on today's date with no location, because
the EXIF inside the file no longer holds the truth. The `fix` stage reads the sidecar and
writes those values back into the file before upload, which is what makes the Proton
timeline come out right.

Note `geoData` being zeroed while `geoDataExif` holds the real coordinates — common when
Location History is off, and a case this tool handles.

## Common problems

**"My export is split into 40 archives."** You left the file size at 2 GB. Either re-export
at 50 GB, or continue — the tool handles it, just more slowly.

**"Some photos have no sidecar."** Normal for a small number of items, usually ones Google
never had full metadata for. They still transfer; the final report lists every one so you
can spot-check them.

**"I see the same photo in several folders."** Also normal — a photo in an album appears
both in its album folder and its year folder. Deduplication is handled during upload.

**"The export failed partway."** Google emails you about this. Re-request the export; a
partial one will silently miss photos.
