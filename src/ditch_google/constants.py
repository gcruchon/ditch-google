"""Project-wide constants.

Version floors live here rather than inline so `doctor`, the docs tests and the error
messages can never drift apart.
"""

from __future__ import annotations

from typing import Final

#: First ``proton-drive`` release with ``photo upload`` and the ``album`` command group.
#: See docs/adr/0002-why-proton-cli-over-rclone-protondrive.md
PROTON_DRIVE_MIN_VERSION: Final = (0, 7, 0)

#: rclone is only used for its ``drive`` backend; any reasonably modern release works.
RCLONE_MIN_VERSION: Final = (1, 60, 0)

#: exiftool ``-stay_open`` batch mode has been stable for far longer than this.
EXIFTOOL_MIN_VERSION: Final = (12, 0)

#: Google clips a sidecar name to this many characters *before* appending ``.json``,
#: which is never clipped. So the budget covers ``<media filename>.supplemental-metadata``.
#: See src/ditch_google/photos/sidecar.py for the full derivation and its edge cases.
SIDECAR_FILENAME_BUDGET: Final = 46

APP_NAME: Final = "ditch-google"
