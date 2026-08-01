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
from rich.table import Table

from ditch_google import __version__
from ditch_google import doctor as doctor_module
from ditch_google.doctor import Check, Status
from ditch_google.state import State, default_state_path

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
def fetch() -> None:
    """Download the Takeout export from Google Drive."""
    _not_yet("fetch")


@photos_app.command()
def unpack() -> None:
    """Extract the downloaded Takeout archives."""
    _not_yet("unpack")


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
