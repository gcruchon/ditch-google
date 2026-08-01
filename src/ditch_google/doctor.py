"""Preflight checks.

Migrations are long. Finding out three hours in that ``exiftool`` is missing, or that the
installed ``proton-drive`` predates photo support, is a bad way to learn it. Everything
checkable up front is checked here.

Every failing check carries a *remedy* - a specific next command or link, not just a
statement that something is wrong.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ditch_google import proc
from ditch_google.constants import (
    EXIFTOOL_MIN_VERSION,
    PROTON_DRIVE_MIN_VERSION,
    RCLONE_MIN_VERSION,
)

__all__ = ["Check", "Status", "format_version", "parse_version", "run_checks"]

#: Free space below this in the work directory is almost certainly not enough to stage
#: even a single Takeout archive, whose parts are up to 50 GB.
MIN_FREE_BYTES = 20 * 1024**3

_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


class Status(Enum):
    """Outcome of a single check."""

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class Check:
    """The result of one preflight check."""

    name: str
    status: Status
    detail: str
    remedy: str | None = None

    @property
    def failed(self) -> bool:
        return self.status is Status.FAIL


def parse_version(text: str) -> tuple[int, ...] | None:
    """Extract the first ``X.Y[.Z]`` version found in ``text``.

    Deliberately loose. Each tool formats its version differently, and the ``proton-drive``
    prefix varies by build (``cli-drive@0.7.0``, ``external-drive-sdkclijs@0.7.0``,
    ``cli-drive-<distro>@0.7.0``), so anchoring on the surrounding text would be fragile.
    """
    match = _VERSION_RE.search(text)
    if match is None:
        return None
    major, minor, patch = match.groups()
    return (int(major), int(minor), int(patch or 0))


def format_version(version: Sequence[int]) -> str:
    return ".".join(str(part) for part in version)


def _check_tool(
    *,
    name: str,
    argv: Sequence[str],
    minimum: tuple[int, ...],
    install_hint: str,
    version_line: int = 0,
) -> Check:
    """Probe a tool's version and compare it against a floor."""
    try:
        result = proc.run(argv, timeout=30)
    except proc.ToolNotFoundError:
        return Check(name, Status.FAIL, "not installed", install_hint)
    except proc.ToolTimeoutError:
        return Check(name, Status.FAIL, "version check timed out", install_hint)
    except proc.ToolFailedError as exc:
        return Check(name, Status.FAIL, f"version check failed: {exc}", install_hint)

    lines = [line for line in result.stdout.splitlines() if line.strip()]
    text = lines[version_line] if len(lines) > version_line else result.stdout
    version = parse_version(text)
    if version is None:
        # Unparseable but present. Warn rather than fail - a version scheme we don't
        # recognise shouldn't block someone whose tool works fine.
        return Check(name, Status.WARN, f"installed, but could not read its version: {text!r}")

    if version < minimum:
        return Check(
            name,
            Status.FAIL,
            f"version {format_version(version)} is older than the required "
            f"{format_version(minimum)}",
            install_hint,
        )
    return Check(name, Status.OK, f"version {format_version(version)}")


def check_rclone() -> Check:
    """rclone, used only for its ``drive`` backend. See ADR-0001."""
    return _check_tool(
        name="rclone",
        argv=["rclone", "version"],
        minimum=RCLONE_MIN_VERSION,
        install_hint="Install rclone: https://rclone.org/install/",
    )


def check_exiftool() -> Check:
    """exiftool, which writes Takeout's sidecar metadata back into the media files."""
    return _check_tool(
        name="exiftool",
        argv=["exiftool", "-ver"],
        minimum=EXIFTOOL_MIN_VERSION,
        install_hint="Install exiftool: https://exiftool.org/install.html",
    )


def check_proton_drive() -> Check:
    """The official Proton CLI.

    ``proton-drive version`` prints two lines::

        Proton Drive CLI cli-drive@0.7.0
        Proton Drive SDK js@0.15.1

    We want the first. Photo and album commands arrived in cli/v0.7.0; older builds can
    write files to Drive but cannot reach the Photos timeline at all. See ADR-0002.
    """
    return _check_tool(
        name="proton-drive",
        argv=["proton-drive", "version"],
        minimum=PROTON_DRIVE_MIN_VERSION,
        install_hint=(
            "Photo support needs proton-drive "
            f"{format_version(PROTON_DRIVE_MIN_VERSION)} or newer. "
            "Download it from https://proton.me/download/drive/cli/index.html"
        ),
    )


def check_proton_auth() -> Check:
    """Confirm we are signed in to Proton.

    ``album list`` is the cheapest probe that proves both authentication *and* that this
    build actually has photo support - it returns a small result even on a large account.
    """
    try:
        proc.run(["proton-drive", "album", "list", "--json"], timeout=60)
    except proc.ToolNotFoundError:
        return Check("proton auth", Status.FAIL, "proton-drive is not installed")
    except proc.ToolTimeoutError:
        return Check(
            "proton auth",
            Status.FAIL,
            "timed out talking to Proton",
            "Check your network, then retry.",
        )
    except proc.ToolFailedError as exc:
        return Check(
            "proton auth",
            Status.FAIL,
            f"not signed in, or the account is unreachable: {exc}",
            "Run: proton-drive auth login",
        )
    return Check("proton auth", Status.OK, "signed in")


def check_rclone_remote(source: str) -> Check:
    """Confirm the Takeout export is where the user says it is."""
    try:
        proc.run(["rclone", "lsd", source, "--max-depth", "1"], timeout=60)
    except proc.ToolNotFoundError:
        return Check("takeout source", Status.FAIL, "rclone is not installed")
    except proc.ToolTimeoutError:
        return Check("takeout source", Status.FAIL, f"timed out listing {source}")
    except proc.ToolFailedError as exc:
        return Check(
            "takeout source",
            Status.FAIL,
            f"cannot read {source}: {exc}",
            "Check the remote name with 'rclone listremotes', and that your "
            "Takeout export finished and was delivered to Drive.",
        )
    return Check("takeout source", Status.OK, f"{source} is readable")


def check_disk_space(work_dir: Path, minimum_bytes: int = MIN_FREE_BYTES) -> Check:
    """Check the staging directory has room.

    The pipeline stages one archive at a time, so it needs roughly twice the size of the
    largest Takeout part rather than twice the whole library - but that is still tens of
    gigabytes.
    """
    probe = work_dir
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent

    try:
        usage = shutil.disk_usage(probe)
    except OSError as exc:
        return Check("disk space", Status.WARN, f"could not check {work_dir}: {exc}")

    free_gb = usage.free / 1024**3
    if usage.free < minimum_bytes:
        return Check(
            "disk space",
            Status.WARN,
            f"{free_gb:.1f} GB free in {work_dir}",
            f"At least {minimum_bytes / 1024**3:.0f} GB is recommended. Use --work-dir "
            "to stage somewhere larger, or re-export from Takeout with a smaller part size.",
        )
    return Check("disk space", Status.OK, f"{free_gb:.1f} GB free in {work_dir}")


def run_checks(*, work_dir: Path, source: str | None = None) -> list[Check]:
    """Run every preflight check and return the results in report order.

    The Proton auth and remote checks are skipped when their tool is missing or too old,
    since they would only produce a second, less useful error about the same problem.
    """
    checks = [check_rclone(), check_exiftool(), check_proton_drive()]
    proton_ready = checks[-1].status is not Status.FAIL
    rclone_ready = checks[0].status is not Status.FAIL

    if proton_ready:
        checks.append(check_proton_auth())
    if source is not None and rclone_ready:
        checks.append(check_rclone_remote(source))

    checks.append(check_disk_space(work_dir))
    return checks
