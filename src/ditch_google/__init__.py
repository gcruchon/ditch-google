"""Migrate a Google Photos library to Proton Photos with metadata and albums intact."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ditch-google")
except PackageNotFoundError:  # pragma: no cover - only when running from a source tree
    __version__ = "0.0.0.dev0"

__all__ = ["__version__"]
