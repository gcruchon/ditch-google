"""Shared subprocess runner for the external tools this project drives.

Every call to ``rclone``, ``exiftool`` and ``proton-drive`` goes through here so that
timeouts, output capture and error reporting behave identically everywhere.

**Never pass a credential in ``argv``.** Command lines appear in error messages, and on
most systems are visible to other processes. This project has no reason to: Google auth
lives in rclone's own config and Proton auth in the OS keychain.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "CommandResult",
    "ToolError",
    "ToolFailedError",
    "ToolNotFoundError",
    "ToolTimeoutError",
    "resolve",
    "run",
    "run_json",
    "stream",
]

#: Enough for a version probe or a listing; transfers use `stream` with their own timeout.
DEFAULT_TIMEOUT = 60.0


class ToolError(Exception):
    """Base class for every failure involving an external tool."""


class ToolNotFoundError(ToolError):
    """The tool is not installed, or not on PATH."""

    def __init__(self, tool: str) -> None:
        self.tool = tool
        super().__init__(f"{tool!r} was not found on PATH")


class ToolFailedError(ToolError):
    """The tool ran but exited non-zero."""

    def __init__(self, argv: Sequence[str], returncode: int, stderr: str) -> None:
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr = stderr
        detail = stderr.strip().splitlines()
        tail = detail[-1] if detail else "no error output"
        super().__init__(f"{argv[0]} exited {returncode}: {tail}")


class ToolTimeoutError(ToolError):
    """The tool did not finish within its timeout."""

    def __init__(self, argv: Sequence[str], timeout: float) -> None:
        self.argv = list(argv)
        self.timeout = timeout
        super().__init__(f"{argv[0]} did not finish within {timeout:g}s")


@dataclass(frozen=True)
class CommandResult:
    """Outcome of a finished command."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def resolve(tool: str) -> Path:
    """Return the absolute path to ``tool``, or raise :class:`ToolNotFoundError`.

    Resolving through PATH is deliberate: users install these tools in many different
    places (Homebrew, apt, a manual download in ~/bin), so hard-coding paths would be
    worse, not safer.
    """
    found = shutil.which(tool)
    if found is None:
        raise ToolNotFoundError(tool)
    return Path(found)


def _prepare(argv: Sequence[str]) -> list[str]:
    if not argv:
        raise ValueError("argv must not be empty")
    return [str(resolve(argv[0])), *(str(a) for a in argv[1:])]


def _environment(env: Mapping[str, str] | None) -> dict[str, str]:
    merged = dict(os.environ)
    if env:
        merged.update(env)
    # Keep child output parseable regardless of the user's locale or colour settings.
    merged.setdefault("LC_ALL", "C")
    merged["NO_COLOR"] = "1"
    return merged


def run(
    argv: Sequence[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    check: bool = True,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    """Run a command to completion and capture its output.

    Args:
        argv: Command and arguments. ``argv[0]`` is resolved through PATH.
        timeout: Seconds before the process is killed.
        check: Raise :class:`ToolFailedError` on a non-zero exit.
        cwd: Working directory.
        env: Extra environment variables, merged over the current environment.

    Raises:
        ToolNotFoundError: ``argv[0]`` is not installed.
        ToolTimeoutError: The command exceeded ``timeout``.
        ToolFailedError: The command exited non-zero and ``check`` is true.
    """
    resolved = _prepare(argv)
    try:
        completed = subprocess.run(
            resolved,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=_environment(env),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolTimeoutError(resolved, timeout) from exc

    result = CommandResult(
        argv=tuple(resolved),
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )
    if check and not result.ok:
        raise ToolFailedError(result.argv, result.returncode, result.stderr)
    return result


def run_json(
    argv: Sequence[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Any:
    """Run a command that emits JSON on stdout and return the parsed value.

    Raises:
        ToolFailedError: The command failed, or its output was not valid JSON.
    """
    result = run(argv, timeout=timeout, check=True, cwd=cwd, env=env)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ToolFailedError(
            result.argv,
            result.returncode,
            f"expected JSON on stdout but could not parse it: {exc}",
        ) from exc


def stream(
    argv: Sequence[str],
    *,
    on_line: Callable[[str], None] | None = None,
    on_stderr_line: Callable[[str], None] | None = None,
    timeout: float | None = None,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = True,
) -> CommandResult:
    """Run a long-lived command, calling back for each line it writes.

    Used for transfers, where waiting for completion before showing anything would leave
    the user staring at nothing for hours. ``timeout`` defaults to no limit, because a
    large upload legitimately takes a very long time.

    ``on_stderr_line`` matters more than it looks: rclone writes its JSON progress log to
    **stderr**, not stdout. stderr is captured in full either way, and reported if the
    command fails.
    """
    resolved = _prepare(argv)
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    timed_out = threading.Event()

    with subprocess.Popen(
        resolved,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd=cwd,
        env=_environment(env),
    ) as process:
        assert process.stdout is not None  # noqa: S101 - guaranteed by stdout=PIPE
        assert process.stderr is not None  # noqa: S101 - guaranteed by stderr=PIPE

        # stderr is drained on its own thread. Reading the two pipes in sequence would
        # deadlock as soon as one filled its buffer while we were blocked on the other -
        # which rclone reliably triggers, since it logs progress to stderr throughout.
        def _drain_stderr() -> None:
            for raw in process.stderr:  # type: ignore[union-attr]
                line = raw.rstrip("\n")
                stderr_lines.append(line)
                if on_stderr_line is not None:
                    on_stderr_line(line)

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        # The timeout has to cover the read loop, not just the final wait(). Iterating
        # over stdout blocks indefinitely, so a process that stops producing output
        # would otherwise hang forever with a timeout set. A watchdog kills it instead,
        # which unblocks the read and lets us report the timeout.
        watchdog: threading.Timer | None = None
        if timeout is not None:

            def _on_deadline() -> None:
                if process.poll() is None:
                    timed_out.set()
                    process.kill()

            watchdog = threading.Timer(timeout, _on_deadline)
            watchdog.daemon = True
            watchdog.start()

        try:
            for raw in process.stdout:
                line = raw.rstrip("\n")
                stdout_lines.append(line)
                if on_line is not None:
                    on_line(line)
            process.wait()
        except BaseException:
            # Includes KeyboardInterrupt: don't leave an orphaned transfer running.
            process.kill()
            process.wait()
            raise
        finally:
            if watchdog is not None:
                watchdog.cancel()
            stderr_thread.join(timeout=5)

        if timed_out.is_set():
            raise ToolTimeoutError(resolved, timeout or 0.0)

        stderr = "\n".join(stderr_lines)

    result = CommandResult(
        argv=tuple(resolved),
        returncode=process.returncode,
        stdout="\n".join(stdout_lines),
        stderr=stderr,
    )
    if check and not result.ok:
        raise ToolFailedError(result.argv, result.returncode, result.stderr)
    return result
