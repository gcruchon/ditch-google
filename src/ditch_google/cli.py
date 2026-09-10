"""Command-line entry point.

`photos migrate` runs the whole pipeline; the individual stages are also exposed so a
migration can be driven step by step, inspected, or resumed from a particular point.

Human-facing output goes through rich; anything a script might parse goes through
`typer.echo`, and every command that can fail exits non-zero so it can gate a script.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table

from ditch_google import __version__, proc, protondrive
from ditch_google import doctor as doctor_module
from ditch_google.doctor import Check, Status
from ditch_google.photos import albums as albums_module
from ditch_google.photos import discover as discover_module
from ditch_google.photos import fetch as fetch_module
from ditch_google.photos import fix as fix_module
from ditch_google.photos import pipeline as pipeline_module
from ditch_google.photos import report as report_module
from ditch_google.photos import unpack as unpack_module
from ditch_google.photos import upload as upload_module
from ditch_google.photos import verify as verify_module
from ditch_google.photos.report import Report
from ditch_google.photos.unpack import UnsafeArchiveMemberError
from ditch_google.photos.verify import VerifyResult
from ditch_google.state import ArchiveStage, ItemStage, State, default_state_path

# `console` is for human-facing output only. Anything a script might parse - version
# strings, `--json` payloads - goes through `typer.echo`, which emits plain text. rich
# applies syntax highlighting to values like version numbers, which injects ANSI escapes
# into stdout whenever colour is forced (as it is on CI).
#
# `highlight=False` because rich's automatic highlighter marks up values it finds inside
# prose - numbers, paths, URLs - which both looks wrong mid-sentence (a cyan "1" inside a
# yellow warning) and splits the sentence with escape codes. Explicit markup still works.
console = Console(highlight=False)
err_console = Console(stderr=True, highlight=False)

app = typer.Typer(
    name="ditch-google",
    help="Migrate your Google Photos library to Proton Photos.",
    no_args_is_help=True,
    add_completion=True,
)

photos_app = typer.Typer(
    name="photos",
    help="Migrate a Google Photos library exported with Google Takeout.",
    no_args_is_help=True,
)
app.add_typer(photos_app)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"ditch-google {__version__}")
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    """Migrate your data out of Google and into Proton."""


#: Default staging directory, shared by every command that takes --work-dir.
DEFAULT_WORK_DIR = Path("./work")


def _human_bytes(count: int) -> str:
    """Format a byte count at a sensible scale.

    Always printing gigabytes turns a real number into a useless "0.0 GB" for anything
    smaller, which is exactly the case on a first trial run.
    """
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"  # pragma: no cover - unreachable, the loop always returns


_STATUS_MARK = {Status.OK: "[green]OK[/green]", Status.WARN: "[yellow]WARN[/yellow]"}


def _render_checks(checks: list[Check]) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")

    for check in checks:
        table.add_row(
            check.name,
            _STATUS_MARK.get(check.status, "[red]FAIL[/red]"),
            check.detail,
        )
    console.print(table)

    for check in checks:
        if check.remedy and check.status is not Status.OK:
            console.print(f"  [dim]{check.name}:[/dim] {check.remedy}")


@app.command()
def doctor(
    work_dir: Annotated[
        Path,
        typer.Option("--work-dir", help="Directory used to stage archives during migration."),
    ] = DEFAULT_WORK_DIR,
    source: Annotated[
        str | None,
        typer.Option("--source", help="Takeout location to verify, e.g. drive:Takeout."),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable results."),
    ] = False,
) -> None:
    """Check that rclone, exiftool and proton-drive are installed and ready."""
    checks = doctor_module.run_checks(work_dir=work_dir, source=source)

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "ok": not any(c.failed for c in checks),
                    "checks": [
                        {
                            "name": c.name,
                            "status": c.status.value,
                            "detail": c.detail,
                            "remedy": c.remedy,
                        }
                        for c in checks
                    ],
                },
                indent=2,
            )
        )
    else:
        _render_checks(checks)

    if any(check.failed for check in checks):
        raise typer.Exit(code=1)


def _render_report(report: Report, remote: VerifyResult | None) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("")
    table.add_column("Total", justify="right")
    table.add_column("Done", justify="right")
    table.add_column("Failed", justify="right")

    table.add_row(
        "archives",
        str(report.archives_total),
        str(report.archives_completed),
        str(report.archives_failed),
    )
    table.add_row(
        "photos", str(report.items_total), str(report.items_uploaded), str(report.items_failed)
    )
    table.add_row("albums", str(report.albums_total), "", str(report.albums_incomplete))
    console.print(table)

    if report.without_sidecar:
        console.print(
            f"[yellow]{report.without_sidecar} file(s) had no sidecar[/yellow] - they keep "
            "whatever dates they already carried."
        )
    if report.items_pending:
        console.print(f"[yellow]{report.items_pending} item(s) still pending.[/yellow]")

    for name, error in report.failures:
        console.print(f"  [red]{name}[/red]: {error}")

    if remote is not None:
        if not remote.checked:
            console.print("[yellow]Could not reach Proton, so nothing was verified.[/yellow]")
        elif remote.missing_capture_times:
            console.print(
                f"[red]{len(remote.missing_capture_times)} photo(s) are missing from the "
                "Proton timeline.[/red]"
            )
        else:
            console.print(
                f"[green]Matched {remote.matched_capture_times} photo(s) in the timeline"
                f"[/green] of {remote.timeline_total}."
            )

    console.print(
        f"[dim]migration {report.migration_id}, started {report.started_at}[/dim]",
    )
    console.print(
        "[green]Nothing was left behind.[/green]"
        if report.clean
        else "[yellow]Some items did not complete - see above.[/yellow]"
    )


@photos_app.command()
def migrate(
    source: Annotated[
        str, typer.Option("--source", help="Takeout location in Drive, e.g. drive:Takeout.")
    ],
    work_dir: Annotated[
        Path, typer.Option("--work-dir", help="Where to stage archives.")
    ] = DEFAULT_WORK_DIR,
    state_db: Annotated[Path | None, typer.Option("--state-db", help="Ledger location.")] = None,
    transfers: Annotated[int, typer.Option("--transfers", help="Parallel rclone transfers.")] = 4,
    conflict: Annotated[
        str, typer.Option("--conflict-strategy", help="skip or keep-both.")
    ] = "skip",
    keep_local: Annotated[
        bool,
        typer.Option(
            "--keep-local/--no-keep-local",
            help="Keep staged files instead of deleting each archive once it is uploaded.",
        ),
    ] = False,
    prefer_existing: Annotated[
        bool,
        typer.Option("--prefer-existing-exif", help="Only fill metadata gaps, never overwrite."),
    ] = False,
    marker: Annotated[
        bool, typer.Option("--marker-album/--no-marker-album", help="Group this run in an album.")
    ] = True,
    check_remote: Annotated[
        bool, typer.Option("--remote/--no-remote", help="Verify against the Proton timeline.")
    ] = True,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
) -> None:
    """Run the full migration: fetch, unpack, fix metadata, upload, restore albums."""
    if conflict not in protondrive.CONFLICT_STRATEGIES:
        err_console.print(
            f"[red]--conflict-strategy must be one of "
            f"{', '.join(protondrive.CONFLICT_STRATEGIES)}.[/red]"
        )
        raise typer.Exit(code=2)

    checks = doctor_module.run_checks(work_dir=work_dir, source=source)
    if any(check.failed for check in checks):
        err_console.print("[red]Preflight checks failed.[/red]")
        _render_checks(checks)
        raise typer.Exit(code=1)

    if not keep_local and not yes:
        console.print(
            "Each archive will be [bold]deleted locally[/bold] once its photos are in "
            "Proton, so peak disk stays near one archive rather than the whole library."
        )
        console.print("Pass [bold]--keep-local[/bold] to keep them instead.")
        if not typer.confirm("Continue?", default=True):
            raise typer.Abort

    with State.open(state_db or default_state_path()) as state:
        console.print(f"[dim]migration {state.migration_id}[/dim]")

        with Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task("starting", total=None)

            def hook(stage: str, archive: str, detail: str) -> None:
                label = f"[bold]{stage}[/bold] {archive}" if archive else f"[bold]{stage}[/bold]"
                progress.update(task, description=f"{label} {detail}".strip())

            result = pipeline_module.run_migration(
                state,
                source=source,
                work_dir=work_dir,
                conflict=conflict,
                transfers=transfers,
                prefer_existing=prefer_existing,
                keep_local=keep_local,
                marker=marker,
                check_remote=check_remote,
                hook=hook,
            )

        if result.bytes_reclaimed:
            console.print(f"Reclaimed {_human_bytes(result.bytes_reclaimed)} of staging space.")
        if marker and result.report is not None:
            name = albums_module.marker_album_name(state.migration_id, state.started_at_local)
            console.print(f"This run is grouped in [bold]{name}[/bold].")

        if result.report is not None:
            _render_report(result.report, None)
            if not result.report.clean:
                raise typer.Exit(code=1)


@photos_app.command()
def fetch(
    source: Annotated[
        str,
        typer.Option("--source", help="Takeout location in Drive, e.g. drive:Takeout."),
    ],
    work_dir: Annotated[
        Path, typer.Option("--work-dir", help="Where to stage archives.")
    ] = DEFAULT_WORK_DIR,
    state_db: Annotated[Path | None, typer.Option("--state-db", help="Ledger location.")] = None,
    transfers: Annotated[int, typer.Option("--transfers", help="Parallel rclone transfers.")] = 4,
) -> None:
    """Download the Takeout export from Google Drive."""
    with State.open(state_db or default_state_path()) as state:
        try:
            archives = fetch_module.list_archives(source)
        except proc.ToolError as exc:
            err_console.print(f"[red]Could not list {source}:[/red] {exc}")
            raise typer.Exit(code=1) from exc

        if not archives:
            err_console.print(
                f"[yellow]No .tgz or .zip archives found at {source}.[/yellow] "
                "Check that your Takeout export finished and was delivered to Drive."
            )
            raise typer.Exit(code=1)

        fetch_module.register_archives(state, archives)
        console.print(f"Found {len(archives)} archive(s) at {source}.")

        pending = [a for a in state.archives() if a.stage is ArchiveStage.PENDING]
        if not pending:
            console.print("[green]Every archive has already been downloaded.[/green]")
            return

        with Progress(
            TextColumn("[bold]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            for archive in pending:
                task = progress.add_task(archive.name, total=archive.size_bytes or None)
                try:
                    fetch_module.fetch_archive(
                        state,
                        archive.name,
                        work_dir,
                        transfers=transfers,
                        on_progress=lambda p, task=task: progress.update(  # type: ignore[misc]
                            task, completed=p.bytes_done, total=p.bytes_total or None
                        ),
                    )
                except proc.ToolError as exc:
                    err_console.print(f"[red]{archive.name} failed:[/red] {exc}")
                    raise typer.Exit(code=1) from exc

        console.print(f"[green]Downloaded {len(pending)} archive(s).[/green]")


@photos_app.command()
def unpack(
    work_dir: Annotated[
        Path, typer.Option("--work-dir", help="Where archives were staged.")
    ] = DEFAULT_WORK_DIR,
    state_db: Annotated[Path | None, typer.Option("--state-db", help="Ledger location.")] = None,
) -> None:
    """Extract the downloaded Takeout archives."""
    with State.open(state_db or default_state_path()) as state:
        ready = [a for a in state.archives() if a.stage is ArchiveStage.FETCHED]
        if not ready:
            console.print("Nothing to unpack. Run [bold]photos fetch[/bold] first.")
            return

        total_files = 0
        total_media = 0
        total_unmatched = 0
        for archive in ready:
            try:
                result = unpack_module.unpack_for_state(state, archive.name, work_dir)
            except UnsafeArchiveMemberError as exc:
                err_console.print(f"[red]{archive.name} is not safe to extract:[/red] {exc}")
                raise typer.Exit(code=1) from exc
            except (ValueError, OSError) as exc:
                err_console.print(f"[red]{archive.name} could not be extracted:[/red] {exc}")
                raise typer.Exit(code=1) from exc

            total_files += result.extracted
            for name, reason in result.skipped:
                console.print(f"  [yellow]skipped[/yellow] {name} ({reason})")

            # Catalogue what came out while it is on disk, so `status` is meaningful
            # immediately and the sidecar pairing is recorded before anything mutates.
            found = discover_module.discover_archive(state, archive.name, result.destination)
            total_media += found.media
            total_unmatched += found.unmatched
            console.print(
                f"{archive.name}: {result.extracted} file(s), "
                f"{found.media} media, {found.matched} with metadata"
            )

        console.print(
            f"[green]Extracted {total_files} file(s) from {len(ready)} archive(s).[/green]"
        )
        if total_unmatched:
            console.print(
                f"[yellow]{total_unmatched} of {total_media} media file(s) have no sidecar[/yellow]"
                " - they will keep whatever dates they already carry."
            )


@photos_app.command()
def fix(
    state_db: Annotated[Path | None, typer.Option("--state-db", help="Ledger location.")] = None,
    prefer_existing: Annotated[
        bool,
        typer.Option(
            "--prefer-existing-exif",
            help="Only fill gaps, never overwrite metadata already in the file.",
        ),
    ] = False,
) -> None:
    """Write metadata from the Takeout sidecar JSON back into the media files."""
    with State.open(state_db or default_state_path()) as state:
        pending = [i for i in state.pending_items() if i.stage is ItemStage.DISCOVERED]
        if not pending:
            console.print("Nothing to fix. Run [bold]photos unpack[/bold] first.")
            return

        try:
            with Progress(
                TextColumn("[bold]writing metadata"),
                BarColumn(),
                TaskProgressColumn(),
                TimeRemainingColumn(),
                console=console,
            ) as progress:
                task = progress.add_task("fix", total=len(pending))
                result = fix_module.fix_pending(
                    state,
                    prefer_existing=prefer_existing,
                    on_progress=lambda done, _total: progress.update(task, completed=done),
                )
        except proc.ToolNotFoundError as exc:
            err_console.print(f"[red]{exc}[/red] Run [bold]ditch-google doctor[/bold].")
            raise typer.Exit(code=1) from exc

        console.print(f"[green]Wrote metadata into {result.written} file(s).[/green]")
        if result.skipped_no_sidecar:
            console.print(
                f"{result.skipped_no_sidecar} file(s) had no sidecar and keep their existing dates."
            )
        if result.failed:
            err_console.print(
                f"[yellow]{result.failed} file(s) could not be written.[/yellow] "
                "See [bold]photos status[/bold]."
            )


@photos_app.command()
def upload(
    state_db: Annotated[Path | None, typer.Option("--state-db", help="Ledger location.")] = None,
    conflict: Annotated[
        str,
        typer.Option(
            "--conflict-strategy",
            help="What to do about photos already in Proton: skip or keep-both.",
        ),
    ] = "skip",
    batch_size: Annotated[
        int, typer.Option("--batch-size", help="Files per proton-drive invocation.")
    ] = upload_module.DEFAULT_BATCH_SIZE,
) -> None:
    """Upload media to the Proton Photos timeline."""
    if conflict not in protondrive.CONFLICT_STRATEGIES:
        err_console.print(
            f"[red]--conflict-strategy must be one of "
            f"{', '.join(protondrive.CONFLICT_STRATEGIES)}.[/red]"
        )
        raise typer.Exit(code=2)

    with State.open(state_db or default_state_path()) as state:
        pending = [i for i in state.pending_items() if i.stage is ItemStage.FIXED]
        if not pending:
            console.print("Nothing to upload. Run [bold]photos fix[/bold] first.")
            return

        try:
            with Progress(
                TextColumn("[bold]uploading to Proton"),
                BarColumn(),
                TaskProgressColumn(),
                TimeRemainingColumn(),
                console=console,
            ) as progress:
                task = progress.add_task("upload", total=len(pending))
                result = upload_module.upload_pending(
                    state,
                    conflict=conflict,
                    batch_size=batch_size,
                    on_progress=lambda done, _total: progress.update(task, completed=done),
                )
        except proc.ToolNotFoundError as exc:
            err_console.print(f"[red]{exc}[/red] Run [bold]ditch-google doctor[/bold].")
            raise typer.Exit(code=1) from exc
        except proc.ToolFailedError as exc:
            err_console.print(f"[red]Upload failed:[/red] {exc}")
            err_console.print("If you are not signed in, run: [bold]proton-drive auth login[/bold]")
            raise typer.Exit(code=1) from exc

        console.print(f"[green]Uploaded {result.uploaded} photo(s).[/green]")
        if result.already_present:
            console.print(f"{result.already_present} were already in Proton and were skipped.")
        if result.failed:
            err_console.print(
                f"[yellow]{result.failed} photo(s) failed.[/yellow] See [bold]photos status[/bold]."
            )
            raise typer.Exit(code=1)


@photos_app.command()
def albums(
    work_dir: Annotated[
        Path, typer.Option("--work-dir", help="Where archives were unpacked.")
    ] = DEFAULT_WORK_DIR,
    state_db: Annotated[Path | None, typer.Option("--state-db", help="Ledger location.")] = None,
    marker: Annotated[
        bool,
        typer.Option(
            "--marker-album/--no-marker-album",
            help="Also create one album holding everything this migration uploaded.",
        ),
    ] = True,
) -> None:
    """Recreate Takeout albums in Proton Photos."""
    with State.open(state_db or default_state_path()) as state:
        roots = [work_dir / "unpacked" / archive.name for archive in state.archives()]
        try:
            with Progress(
                TextColumn("[bold]restoring albums"),
                BarColumn(),
                TaskProgressColumn(),
                console=console,
            ) as progress:
                task = progress.add_task("albums", total=None)
                result = albums_module.restore_albums(
                    state,
                    roots,
                    marker=marker,
                    on_progress=lambda done, total: progress.update(
                        task, completed=done, total=total
                    ),
                )
        except proc.ToolError as exc:
            err_console.print(f"[red]Albums failed:[/red] {exc}")
            raise typer.Exit(code=1) from exc

        console.print(
            f"[green]{result.created} album(s) created, {result.reused} reused, "
            f"{result.photos_added} photo(s) added.[/green]"
        )
        if marker:
            name = albums_module.marker_album_name(state.migration_id, state.started_at_local)
            console.print(f"Everything this run uploaded is also in [bold]{name}[/bold].")
        for title, error in result.failed:
            err_console.print(f"  [yellow]{title}:[/yellow] {error}")
        if result.failed:
            raise typer.Exit(code=1)


@photos_app.command()
def verify(
    state_db: Annotated[Path | None, typer.Option("--state-db", help="Ledger location.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit machine-readable output.")] = False,
    check_remote: Annotated[
        bool,
        typer.Option("--remote/--no-remote", help="Also reconcile against the Proton timeline."),
    ] = True,
) -> None:
    """Reconcile what was uploaded against the local ledger."""
    with State.open(state_db or default_state_path()) as state:
        report = report_module.build_report(state)
        remote = verify_module.verify_uploads(state) if check_remote else None

        if as_json:
            payload = report.as_dict()
            if remote is not None:
                payload["remote"] = {
                    "checked": remote.checked,
                    "expected": remote.expected,
                    "timeline_total": remote.timeline_total,
                    "matched": remote.matched_capture_times,
                    "missing": remote.missing_capture_times,
                }
            typer.echo(json.dumps(payload, indent=2))
        else:
            _render_report(report, remote)

        if not report.clean or (remote is not None and not remote.ok):
            raise typer.Exit(code=1)


@photos_app.command()
def status(
    state_db: Annotated[
        Path | None,
        typer.Option("--state-db", help="Ledger location. Defaults to the XDG state directory."),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable results."),
    ] = False,
) -> None:
    """Show migration progress from the state database."""
    path = state_db or default_state_path()
    if not path.exists():
        if as_json:
            typer.echo(json.dumps({"started": False, "state_db": str(path)}, indent=2))
        else:
            console.print(f"No migration has been started yet ([dim]{path}[/dim] does not exist).")
        return

    with State.open(path) as state:
        items = state.item_counts()
        archives = state.archive_counts()
        complete = state.is_complete()
        unmatched = len(state.items_without_sidecar())

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "started": True,
                    "state_db": str(path),
                    "complete": complete,
                    "archives": archives,
                    "items": items,
                    "items_without_sidecar": unmatched,
                },
                indent=2,
            )
        )
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Stage")
    table.add_column("Archives", justify="right")
    table.add_column("Items", justify="right")
    for stage in sorted(set(archives) | set(items)):
        table.add_row(
            stage,
            str(archives.get(stage, "-")),
            str(items.get(stage, "-")),
        )
    console.print(table)

    if unmatched:
        console.print(f"[yellow]{unmatched} item(s) have no matching sidecar.[/yellow]")
    console.print("[green]Migration complete.[/green]" if complete else "Migration in progress.")


if __name__ == "__main__":  # pragma: no cover
    app()
