"""Command-line entry point.

The command surface is deliberately declared in full from the first release so the shape
of the pipeline is visible, even while individual stages are still being built. Stages
that are not implemented yet exit with a clear message rather than pretending to work.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console

from ditch_google import __version__

console = Console()
err_console = Console(stderr=True)

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
        console.print(f"ditch-google {__version__}")
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


@app.command()
def doctor() -> None:
    """Check that rclone, exiftool and proton-drive are installed and ready."""
    _not_yet("doctor")


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
def status() -> None:
    """Show migration progress from the state database."""
    _not_yet("status")


if __name__ == "__main__":  # pragma: no cover
    app()
