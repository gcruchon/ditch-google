"""Tests for the Proton upload stage."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from ditch_google import protondrive
from ditch_google.photos.upload import batch_items, upload_pending
from ditch_google.state import Item, ItemStage, State
from tests.conftest import FakeTool


@pytest.fixture
def state(tmp_path: Path) -> Iterator[State]:
    with State.open(tmp_path / "photos.db") as opened:
        opened.add_archive("a.tgz", "remote:a.tgz")
        yield opened


def summary_json(
    transferred: int = 0,
    skipped: int = 0,
    failures: list[dict[str, str]] | None = None,
) -> str:
    return json.dumps(
        {
            "transferredItems": transferred,
            "transferredBytes": transferred * 1000,
            "skippedItems": skipped,
            "failedItems": len(failures or []),
            "failures": failures or [],
        }
    )


def add_items(state: State, *names: str, stage: ItemStage = ItemStage.FIXED) -> list[str]:
    paths = []
    for name in names:
        path = f"/work/{name}"
        state.add_item("a.tgz", path)
        state.set_item_stage(path, stage)
        paths.append(path)
    return paths


# ------------------------------------------------------------------- summary parsing


def test_parse_upload_summary() -> None:
    parsed = protondrive.parse_upload_summary(
        summary_json(transferred=3, skipped=1, failures=[{"name": "bad.jpg", "error": "boom"}])
    )
    assert parsed.transferred == 3
    assert parsed.skipped == 1
    assert parsed.failed == 1
    assert parsed.failures == {"bad.jpg": "boom"}
    assert parsed.succeeded == 4


def test_summary_is_found_after_other_output() -> None:
    """The CLI logs before printing its summary; the summary is the last JSON object."""
    noisy = "Uploading...\nsome log line\n" + summary_json(transferred=2)
    assert protondrive.parse_upload_summary(noisy).transferred == 2


@pytest.mark.parametrize("stdout", ["", "no json here", "{}", '{"unrelated": 1}'])
def test_unparseable_summary_yields_zeroes(stdout: str) -> None:
    assert protondrive.parse_upload_summary(stdout) == protondrive.UploadSummary()


# ------------------------------------------------------------------------- the CLI call


def test_conflict_strategy_is_always_passed(fake_tool: FakeTool, tmp_path: Path) -> None:
    """Without it the CLI prompts on stdin and an unattended run hangs forever."""
    argv_log = tmp_path / "argv.txt"
    fake_tool(
        "proton-drive",
        script=f'printf "%s\\n" "$@" > {argv_log}\nprintf \'{summary_json(transferred=1)}\\n\'',
    )

    protondrive.upload_photos([Path("/work/a.jpg")])

    recorded = argv_log.read_text().splitlines()
    assert "--conflict-strategy" in recorded
    assert "skip" in recorded
    assert "--json" in recorded


def test_invalid_conflict_strategy_is_rejected() -> None:
    with pytest.raises(ValueError, match="conflict must be one of"):
        protondrive.upload_photos([Path("/work/a.jpg")], conflict="clobber")


def test_no_paths_makes_no_call(fake_tool: FakeTool) -> None:
    fake_tool("proton-drive", exit_code=1)
    assert protondrive.upload_photos([]) == protondrive.UploadSummary()


def test_partial_failure_still_returns_the_summary(fake_tool: FakeTool) -> None:
    """A batch with any failure exits non-zero. Raising would turn a partial success
    into a reported total loss."""
    payload = summary_json(transferred=2, failures=[{"name": "bad.jpg", "error": "boom"}])
    fake_tool("proton-drive", script=f"printf '{payload}\\n'\nexit 1")

    summary = protondrive.upload_photos([Path("/work/a.jpg")])
    assert summary.transferred == 2
    assert summary.failures == {"bad.jpg": "boom"}


def test_hard_failure_with_no_summary_raises(fake_tool: FakeTool) -> None:
    from ditch_google import proc

    fake_tool("proton-drive", stderr="You need to login first", exit_code=1)
    with pytest.raises(proc.ToolFailedError):
        protondrive.upload_photos([Path("/work/a.jpg")])


# ----------------------------------------------------------------------------- batching


def test_batches_respect_the_size_limit() -> None:
    items = [
        Item(i, "a", f"/w/{i}.jpg", None, None, ItemStage.FIXED, None, None, "") for i in range(7)
    ]
    assert [len(b) for b in batch_items(items, size=3)] == [3, 3, 1]


def test_repeated_basenames_are_split_across_batches() -> None:
    """Failures are reported by basename only, so a repeat inside one batch would make
    attribution impossible."""
    items = [
        Item(1, "a", "/w/one/IMG_1.jpg", None, None, ItemStage.FIXED, None, None, ""),
        Item(2, "a", "/w/two/IMG_1.jpg", None, None, ItemStage.FIXED, None, None, ""),
    ]
    batches = list(batch_items(items, size=50))
    assert [len(b) for b in batches] == [1, 1]


# -------------------------------------------------------------------------- the pass


def test_upload_marks_items_uploaded(state: State, fake_tool: FakeTool) -> None:
    add_items(state, "a.jpg", "b.jpg")
    fake_tool("proton-drive", script=f"printf '{summary_json(transferred=2)}\\n'")

    result = upload_pending(state)

    assert result.uploaded == 2
    assert result.failed == 0
    assert all(i.stage is ItemStage.UPLOADED for i in state.items())


def test_named_failures_are_attributed(state: State, fake_tool: FakeTool) -> None:
    add_items(state, "good.jpg", "bad.jpg")
    payload = summary_json(transferred=1, failures=[{"name": "bad.jpg", "error": "too large"}])
    fake_tool("proton-drive", script=f"printf '{payload}\\n'\nexit 1")

    result = upload_pending(state)

    assert result.failed == 1
    good = state.get_item("/work/good.jpg")
    bad = state.get_item("/work/bad.jpg")
    assert good is not None and good.stage is ItemStage.UPLOADED
    assert bad is not None and bad.stage is ItemStage.FAILED
    assert bad.error == "too large"


def test_server_side_duplicates_count_as_present_not_uploaded(
    state: State, fake_tool: FakeTool
) -> None:
    """Proton de-duplicates on name plus SHA1, which is what makes resume idempotent."""
    add_items(state, "a.jpg")
    fake_tool("proton-drive", script=f"printf '{summary_json(skipped=1)}\\n'")

    result = upload_pending(state)

    assert result.already_present == 1
    assert result.uploaded == 0
    item = state.get_item("/work/a.jpg")
    assert item is not None and item.stage is ItemStage.UPLOADED


def test_unaccounted_files_are_retried_individually_then_failed(
    state: State, fake_tool: FakeTool, tmp_path: Path
) -> None:
    """The CLI silently ignores non image/video files.

    A batch whose counts don't add up cannot be attributed, so it is retried one file at
    a time. Claiming those reached Proton would be the worst possible outcome.
    """
    add_items(state, "a.jpg", "weird.bin")
    calls = tmp_path / "calls.txt"
    # Always report a single transfer, whatever the batch size: for the 2-file batch the
    # counts won't add up, forcing the per-file retry.
    fake_tool(
        "proton-drive",
        script=f"echo call >> {calls}\nprintf '{summary_json(transferred=1)}\\n'",
    )

    result = upload_pending(state)

    # One batch call, then one call per file.
    assert len(calls.read_text().splitlines()) == 3
    assert result.failed == 0
    assert all(i.stage is ItemStage.UPLOADED for i in state.items())


def test_a_single_ignored_file_is_recorded_as_failed(state: State, fake_tool: FakeTool) -> None:
    add_items(state, "weird.bin")
    fake_tool("proton-drive", script=f"printf '{summary_json()}\\n'")

    result = upload_pending(state)

    assert result.failed == 1
    item = state.get_item("/work/weird.bin")
    assert item is not None
    assert item.stage is ItemStage.FAILED
    assert item.error is not None
    assert "ignored" in item.error


def test_only_fixed_items_are_uploaded(state: State, fake_tool: FakeTool) -> None:
    add_items(state, "notfixed.jpg", stage=ItemStage.DISCOVERED)
    fake_tool("proton-drive", exit_code=1)
    assert upload_pending(state).total == 0


def test_upload_is_resumable(state: State, fake_tool: FakeTool) -> None:
    add_items(state, "a.jpg")
    fake_tool("proton-drive", script=f"printf '{summary_json(transferred=1)}\\n'")

    assert upload_pending(state).uploaded == 1
    assert upload_pending(state).total == 0


def test_progress_is_reported(state: State, fake_tool: FakeTool) -> None:
    add_items(state, "a.jpg", "b.jpg", "c.jpg")
    fake_tool("proton-drive", script=f"printf '{summary_json(transferred=2)}\\n'")

    seen: list[tuple[int, int]] = []
    upload_pending(state, batch_size=2, on_progress=lambda d, t: seen.append((d, t)))

    assert seen[-1] == (3, 3)
