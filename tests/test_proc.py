"""Tests for the shared subprocess runner."""

from __future__ import annotations

from pathlib import Path

import pytest

from ditch_google import proc
from tests.conftest import FakeTool


def test_resolve_returns_absolute_path(fake_tool: FakeTool) -> None:
    created = fake_tool("widget")
    assert proc.resolve("widget") == created


def test_resolve_raises_for_missing_tool(fake_bin: Path) -> None:
    with pytest.raises(proc.ToolNotFoundError) as exc_info:
        proc.resolve("definitely-not-installed")
    assert exc_info.value.tool == "definitely-not-installed"
    assert "definitely-not-installed" in str(exc_info.value)


def test_run_captures_stdout_and_stderr(fake_tool: FakeTool) -> None:
    fake_tool("widget", stdout="hello", stderr="a warning")
    result = proc.run(["widget"])
    assert result.ok
    assert result.stdout.strip() == "hello"
    assert result.stderr.strip() == "a warning"


def test_run_raises_on_nonzero_exit(fake_tool: FakeTool) -> None:
    fake_tool("widget", stderr="it broke", exit_code=3)
    with pytest.raises(proc.ToolFailedError) as exc_info:
        proc.run(["widget"])
    assert exc_info.value.returncode == 3
    assert "it broke" in str(exc_info.value)


def test_run_without_check_returns_failure(fake_tool: FakeTool) -> None:
    fake_tool("widget", exit_code=3)
    result = proc.run(["widget"], check=False)
    assert not result.ok
    assert result.returncode == 3


def test_run_times_out(fake_tool: FakeTool) -> None:
    fake_tool("widget", script="sleep 5")
    with pytest.raises(proc.ToolTimeoutError):
        proc.run(["widget"], timeout=0.3)


def test_run_rejects_empty_argv() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        proc.run([])


def test_run_passes_extra_environment(fake_tool: FakeTool) -> None:
    fake_tool("widget", script='printf "%s" "$DITCH_TEST_VALUE"')
    result = proc.run(["widget"], env={"DITCH_TEST_VALUE": "present"})
    assert result.stdout == "present"


def test_run_disables_colour_in_children(fake_tool: FakeTool) -> None:
    """Child output is parsed, so it must not arrive wrapped in ANSI escapes."""
    fake_tool("widget", script='printf "%s" "$NO_COLOR"')
    assert proc.run(["widget"]).stdout == "1"


def test_run_json_parses_output(fake_tool: FakeTool) -> None:
    fake_tool("widget", stdout='{"albums": [{"name": "Lisbon"}]}')
    assert proc.run_json(["widget"]) == {"albums": [{"name": "Lisbon"}]}


def test_run_json_raises_on_non_json_output(fake_tool: FakeTool) -> None:
    """A tool printing a human message where JSON was expected must not pass silently."""
    fake_tool("widget", stdout="Not signed in.")
    with pytest.raises(proc.ToolFailedError, match="could not parse it"):
        proc.run_json(["widget"])


def test_stream_invokes_callback_per_line(fake_tool: FakeTool) -> None:
    fake_tool("widget", script='printf "one\\ntwo\\nthree\\n"')
    seen: list[str] = []
    result = proc.stream(["widget"], on_line=seen.append)
    assert seen == ["one", "two", "three"]
    assert result.stdout == "one\ntwo\nthree"


def test_stream_raises_on_failure_with_stderr(fake_tool: FakeTool) -> None:
    fake_tool("widget", script='printf "partial\\n"\necho "upload failed" >&2\nexit 1')
    with pytest.raises(proc.ToolFailedError, match="upload failed"):
        proc.stream(["widget"], on_line=lambda _: None)


def test_stream_times_out_while_blocked_on_output(fake_tool: FakeTool) -> None:
    """A process that emits a line then goes silent must still hit its timeout.

    Regression: the timeout originally only wrapped the final `wait()`, while iterating
    over stdout blocked indefinitely - so a stalled transfer would hang forever despite
    a timeout being set.
    """
    fake_tool("widget", script='printf "start\\n"\nsleep 5')
    with pytest.raises(proc.ToolTimeoutError):
        proc.stream(["widget"], on_line=lambda _: None, timeout=0.3)


def test_stream_without_timeout_runs_to_completion(fake_tool: FakeTool) -> None:
    """The default is no limit: a real upload can legitimately take hours."""
    fake_tool("widget", script='printf "done\\n"')
    result = proc.stream(["widget"], on_line=lambda _: None)
    assert result.ok
    assert result.stdout == "done"
