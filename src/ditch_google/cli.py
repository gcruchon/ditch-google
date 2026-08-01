"""Command-line entry point.

The command surface is deliberately declared in full from the first release so the shape
of the pipeline is visible, even while individual stages are still being built. Stages
that are not implemented yet exit with a clear message rather than pretending to work.
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
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table

from ditch_google import __version__, proc
from ditch_google import doctor as doctor_module
from ditch_google.doctor import Check, Status
from ditch_google.photos import fetch as fetch_module
from ditch_google.photos import unpack as unpack_module
from ditch_google.photos.unpack import UnsafeArchiveMemberError
from ditch_google.state import ArchiveStage, State, default_state_path

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


def _not_yet(stage: str) -> None:
    """Exit cleanly for a stage that exists in the CLI but is not built yet."""
    err_console.print(f"[yellow]The '{stage}' stage is not implemented yet.[/yellow]")
    raise typer.Exit(code=2)


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    """Migrate your data out of Google and into Proton."""


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
    ] = Path("./work"),
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


@photos_app.command()
def migrate() -> None:
    """Run the full migration: fetch, unpack, fix metadata, upload, restore albums."""
    _not_yet("migrate")


@photos_app.command()
def fetch(
    source: Annotated[
        str,
        typer.Option("--source", help="Takeout location in Drive, e.g. drive:Takeout."),
    ],
    work_dir: Annotated[Path, typer.Option("--work-dir", help="Where to stage archives.")] = Path(
        "./work"
    ),
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
    ] = Path("./work"),
    state_db: Annotated[Path | None, typer.Option("--state-db", help="Ledger location.")] = None,
) -> None:
    """Extract the downloaded Takeout archives."""
    with State.open(state_db or default_state_path()) as state:
        ready = [a for a in state.archives() if a.stage is ArchiveStage.FETCHED]
        if not ready:
            console.print("Nothing to unpack. Run [bold]photos fetch[/bold] first.")
            return

        total_files = 0
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
            console.print(f"{archive.name}: {result.extracted} file(s)")
            for name, reason in result.skipped:
                console.print(f"  [yellow]skipped[/yellow] {name} ({reason})")

        console.print(
            f"[green]Extracted {total_files} file(s) from {len(ready)} archive(s).[/green]"
        )


@photos_app.command()
def fix() -> None:
    """Write metadata from the Takeout sidecar JSON back into the media files."""
    _not_yet("fix")


@photos_app.command()
def upload() -> None:
    """Upload media to the Proton Photos timeline."""
    _not_yet("upload")


@photos_app.command()
def albums() -> None:
    """Recreate Takeout albums in Proton Photos."""
    _not_yet("albums")


@photos_app.command()
def verify() -> None:
    """Reconcile what was uploaded against the local ledger."""
    _not_yet("verify")


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
