"""A long-lived ``exiftool`` process, driven over its ``-stay_open`` protocol.

exiftool takes roughly a fifth of a second to start, because it loads a large Perl
library every time. Forking it once per photo would cost over five hours on a
100,000-photo library before doing a single byte of useful work. ``-stay_open`` keeps one
process alive and feeds it commands, which turns that into minutes.

This is the one place that does not go through :mod:`ditch_google.proc`. ``run`` and
``stream`` both model a command that starts, produces output and exits; an interactive
session that outlives many commands cannot be expressed that way. The binary is still
resolved through ``proc.resolve`` and failures are still raised as ``proc.ToolError``
subclasses, so callers see one consistent error hierarchy.

The protocol itself:

* arguments are written to stdin one per line
* ``-execute{n}`` runs the accumulated arguments
* exiftool answers with output followed by a line reading ``{ready{n}}``
* ``-stay_open`` / ``False`` shuts it down

The ``{n}`` matters. Without it, a command that produced unexpected output would leave
the reader out of step with the writer for the rest of the run, silently attributing one
photo's result to another.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import Self

from ditch_google import proc

__all__ = ["ExiftoolError", "ExiftoolSession"]

#: exiftool's own summary line for a write that failed. Printed to stdout, ordered
#: before the ready sentinel, which is what makes it safe to key error handling off.
_FAILED_RE = re.compile(r"(\d+) files? weren't updated due to errors")


class ExiftoolError(proc.ToolError):
    """exiftool reported an error for a command."""

    def __init__(self, message: str, *, args: Sequence[str] | None = None) -> None:
        self.command_args = list(args or [])
        super().__init__(message)


class ExiftoolSession:
    """One ``exiftool -stay_open`` process, reused across many files.

    Use as a context manager::

        with ExiftoolSession() as session:
            session.execute(["-DateTimeOriginal=2019:06:08 13:20:00", str(path)])
    """

    def __init__(self, executable: Path | None = None) -> None:
        self._executable = executable or proc.resolve("exiftool")
        self._process: subprocess.Popen[str] | None = None
        self._counter = 0
        self._stderr: list[str] = []
        self._stderr_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._process is not None:
            return

        self._process = subprocess.Popen(
            [
                str(self._executable),
                "-stay_open",
                "True",
                "-@",
                "-",
                "-common_args",
                "-charset",
                "filename=utf8",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )

        # Drained on a thread so a chatty command cannot fill the stderr pipe and
        # deadlock us while we are blocked reading stdout.
        def _drain() -> None:
            assert self._process is not None  # noqa: S101
            assert self._process.stderr is not None  # noqa: S101
            for line in self._process.stderr:
                self._stderr.append(line.rstrip("\n"))

        self._stderr_thread = threading.Thread(target=_drain, daemon=True)
        self._stderr_thread.start()

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        self._process = None

        try:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.write("-stay_open\nFalse\n")
                process.stdin.flush()
                process.stdin.close()
            process.wait(timeout=10)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            process.kill()
            process.wait()
        finally:
            if self._stderr_thread is not None:
                self._stderr_thread.join(timeout=5)

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -------------------------------------------------------------------- commands

    def execute(self, args: Sequence[str]) -> str:
        """Run one exiftool command and return its stdout.

        Raises:
            ExiftoolError: The session is not running, exiftool exited, or it reported
                an error for this command.
        """
        with self._lock:
            return self._execute_locked(args)

    def _execute_locked(self, args: Sequence[str]) -> str:
        if self._process is None:
            raise ExiftoolError("exiftool session is not running")
        if self._process.poll() is not None:
            raise ExiftoolError(
                f"exiftool exited unexpectedly: {self._recent_errors() or 'no error output'}"
            )

        assert self._process.stdin is not None  # noqa: S101
        assert self._process.stdout is not None  # noqa: S101

        self._counter += 1
        token = self._counter
        before = len(self._stderr)

        payload = "".join(f"{arg}\n" for arg in args) + f"-execute{token}\n"
        try:
            self._process.stdin.write(payload)
            self._process.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise ExiftoolError(f"exiftool stopped accepting input: {exc}", args=args) from exc

        sentinel = f"{{ready{token}}}"
        collected: list[str] = []
        while True:
            line = self._process.stdout.readline()
            if not line:
                raise ExiftoolError(
                    f"exiftool closed its output: {self._recent_errors(before) or 'no output'}",
                    args=args,
                )
            stripped = line.rstrip("\n")
            if stripped == sentinel:
                break
            collected.append(stripped)

        output = "\n".join(collected)

        # Decide on stdout, not stderr. exiftool prints its summary to stdout *before*
        # the ready sentinel, so it is ordered with respect to our read. The matching
        # "Error: ..." line goes to stderr, which our drain thread picks up whenever it
        # gets scheduled - often after we have already moved on. Trusting stderr here
        # meant a failed write was silently counted as a success.
        failed = _FAILED_RE.search(output)
        if failed is not None and int(failed.group(1)) > 0:
            raise ExiftoolError(self._await_error(before), args=args)

        return output

    def _await_error(self, since: int) -> str:
        """Best-effort detail for a failure stdout has already confirmed.

        Gives the stderr drain a moment to catch up so the message names the actual
        problem. The failure itself is never contingent on this arriving.
        """
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            errors = [line for line in self._stderr[since:] if line.lower().startswith("error")]
            if errors:
                return "; ".join(errors)
            time.sleep(0.02)
        return "exiftool could not write the file"

    def _recent_errors(self, since: int = 0) -> str:
        return "; ".join(self._stderr[since:][-5:])
