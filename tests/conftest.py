"""Shared fixtures.

The external tools are stubbed with generated shell scripts rather than committed ones so
each test can dictate exactly what its stub prints and exits with. That keeps the whole
suite runnable by a contributor who has none of `rclone`, `exiftool` or `proton-drive`
installed - which is most of them.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

#: Signature of the `fake_tool` fixture: (name, stdout, stderr, exit_code) -> path.
FakeTool = Callable[..., Path]

#: What a real `proton-drive version` prints. The name before `@` varies by build
#: (`cli-drive`, `external-drive-sdkclijs`, `cli-drive-<distro>`), which is why the
#: version parser keys off the `@` rather than the surrounding text.
PROTON_VERSION_OUTPUT = "Proton Drive CLI cli-drive@0.7.0\nProton Drive SDK js@0.15.1"

#: What a real `rclone version` prints, first line only plus a representative tail.
RCLONE_VERSION_OUTPUT = "rclone v1.74.2\n- os/version: darwin 14.3.1 (64 bit)"

#: `exiftool -ver` prints a bare version and nothing else.
EXIFTOOL_VERSION_OUTPUT = "13.10"


@pytest.fixture
def fake_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty directory placed first on PATH, isolating tests from real tools.

    PATH is replaced rather than prepended so that a developer's real `rclone` can never
    make a test pass (or fail) by accident.
    """
    bin_dir = tmp_path / "fake_bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir


@pytest.fixture
def fake_tool(fake_bin: Path) -> FakeTool:
    """Factory creating an executable stub on PATH.

    Example::

        fake_tool("proton-drive", stdout=PROTON_VERSION_OUTPUT)
        fake_tool("rclone", stderr="no such remote", exit_code=1)
    """

    def _make(
        name: str,
        *,
        stdout: str = "",
        stderr: str = "",
        exit_code: int = 0,
        script: str | None = None,
    ) -> Path:
        path = fake_bin / name
        # The stub needs a working PATH of its own for `cat`, `sleep` and friends, but
        # the *test process* must keep seeing only fake_bin - otherwise a real exiftool
        # in /usr/bin (as apt installs it on CI) would satisfy a lookup meant to fail.
        preamble = "#!/bin/sh\nPATH=/usr/bin:/bin:/usr/sbin:/sbin\nexport PATH\n"
        if script is None:
            body = preamble + "\n".join(
                [
                    f"cat <<'STDOUT_EOF'\n{stdout}\nSTDOUT_EOF" if stdout else ":",
                    f"cat >&2 <<'STDERR_EOF'\n{stderr}\nSTDERR_EOF" if stderr else ":",
                    f"exit {exit_code}",
                    "",
                ]
            )
        else:
            body = preamble + script + "\n"
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return path

    return _make


@pytest.fixture
def all_tools_healthy(fake_tool: FakeTool) -> None:
    """Stub all three tools at versions that satisfy every check."""
    fake_tool("rclone", stdout=RCLONE_VERSION_OUTPUT)
    fake_tool("exiftool", stdout=EXIFTOOL_VERSION_OUTPUT)
    fake_tool("proton-drive", stdout=PROTON_VERSION_OUTPUT)


@pytest.fixture
def has_exiftool() -> bool:
    """Whether a real exiftool is available, for tests marked `requires_exiftool`."""
    from shutil import which

    return which("exiftool") is not None


@pytest.fixture(autouse=True)
def _no_accidental_home_writes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep tests from touching the developer's real config or state directories."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    os.environ.pop("DITCH_GOOGLE_WORK_DIR", None)
