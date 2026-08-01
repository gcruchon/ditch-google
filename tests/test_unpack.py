"""Tests for archive extraction.

The security cases build genuinely hostile archives rather than mocking the check, because
the thing worth proving is that a real crafted `.tgz` or `.zip` cannot write outside the
destination directory.
"""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from ditch_google.photos.unpack import (
    UnsafeArchiveMemberError,
    is_safe_member,
    unpack_archive,
)

# ------------------------------------------------------------------- building archives


def make_tar(path: Path, members: dict[str, bytes]) -> Path:
    with tarfile.open(path, "w:gz") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return path


def make_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return path


# ----------------------------------------------------------------------- name checking


@pytest.mark.parametrize(
    "name",
    [
        "Takeout/Google Photos/IMG_1.jpg",
        "IMG_1.jpg",
        "a/b/c/d.json",
        "Takeout/Photos from 2019/IMG (1).jpg",
    ],
)
def test_ordinary_names_are_safe(name: str) -> None:
    ok, reason = is_safe_member(name)
    assert ok, reason


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("../escape.jpg", "path traversal"),
        ("Takeout/../../escape.jpg", "path traversal"),
        ("/etc/passwd", "absolute path"),
        ("//etc/passwd", "absolute path"),
        ("..", "empty or dot path"),
        ("", "empty or dot path"),
        ("C:/Windows/system32/evil.dll", "drive-letter absolute path"),
        ("..\\..\\escape.jpg", "path traversal"),
    ],
)
def test_hostile_names_are_refused(name: str, expected: str) -> None:
    ok, reason = is_safe_member(name)
    assert not ok
    assert reason == expected


# --------------------------------------------------------------------- happy path


def test_unpack_tar(tmp_path: Path) -> None:
    source = make_tar(
        tmp_path / "a.tgz",
        {
            "Takeout/Google Photos/IMG_1.jpg": b"jpegdata",
            "Takeout/Google Photos/IMG_1.jpg.json": b"{}",
        },
    )
    result = unpack_archive(source, tmp_path / "out")

    assert result.extracted == 2
    assert (
        tmp_path / "out" / "Takeout" / "Google Photos" / "IMG_1.jpg"
    ).read_bytes() == b"jpegdata"


def test_unpack_zip(tmp_path: Path) -> None:
    source = make_zip(tmp_path / "a.zip", {"Takeout/IMG_1.jpg": b"jpegdata"})
    result = unpack_archive(source, tmp_path / "out")

    assert result.extracted == 1
    assert (tmp_path / "out" / "Takeout" / "IMG_1.jpg").read_bytes() == b"jpegdata"


def test_unpack_creates_the_destination(tmp_path: Path) -> None:
    source = make_tar(tmp_path / "a.tgz", {"IMG_1.jpg": b"x"})
    unpack_archive(source, tmp_path / "deeply" / "nested" / "out")
    assert (tmp_path / "deeply" / "nested" / "out" / "IMG_1.jpg").exists()


def test_unrecognised_format_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("this is not an archive")
    with pytest.raises(ValueError, match=r"not a \.tgz or \.zip"):
        unpack_archive(source, tmp_path / "out")


# ------------------------------------------------------------------------- security


def test_tar_path_traversal_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    """CVE-2007-4559. The canary must not exist afterwards."""
    outside = tmp_path / "outside.txt"
    source = make_tar(tmp_path / "evil.tgz", {"../outside.txt": b"pwned"})

    with pytest.raises(UnsafeArchiveMemberError, match="path traversal"):
        unpack_archive(source, tmp_path / "out")

    assert not outside.exists()


def test_zip_slip_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    source = make_zip(tmp_path / "evil.zip", {"../outside.txt": b"pwned"})

    with pytest.raises(UnsafeArchiveMemberError, match="path traversal"):
        unpack_archive(source, tmp_path / "out")

    assert not outside.exists()


def test_absolute_path_member_is_refused(tmp_path: Path) -> None:
    # S108: an absolute /tmp path is the point of this test - it is the hostile member
    # name stored inside the archive, never a path we write to.
    source = make_tar(tmp_path / "evil.tgz", {"/tmp/pwned.txt": b"x"})  # noqa: S108
    with pytest.raises(UnsafeArchiveMemberError):
        unpack_archive(source, tmp_path / "out")


def test_a_hostile_member_stops_the_whole_archive(tmp_path: Path) -> None:
    """One hostile entry discredits the archive; we don't extract the rest of it."""
    source = make_tar(
        tmp_path / "evil.tgz",
        {"good.jpg": b"fine", "../escape.jpg": b"pwned"},
    )
    with pytest.raises(UnsafeArchiveMemberError):
        unpack_archive(source, tmp_path / "out")

    assert not (tmp_path / "escape.jpg").exists()


def test_symlinks_are_skipped_not_followed(tmp_path: Path) -> None:
    """A symlink to /etc/passwd would turn a later write into an arbitrary-file write."""
    source = tmp_path / "links.tgz"
    with tarfile.open(source, "w:gz") as archive:
        link = tarfile.TarInfo("passwd-link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)

        content = b"real"
        info = tarfile.TarInfo("IMG_1.jpg")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))

    result = unpack_archive(source, tmp_path / "out")

    assert result.extracted == 1
    assert not (tmp_path / "out" / "passwd-link").exists()
    assert ("passwd-link", "link") in result.skipped


def test_hard_links_are_skipped(tmp_path: Path) -> None:
    source = tmp_path / "links.tgz"
    with tarfile.open(source, "w:gz") as archive:
        content = b"real"
        info = tarfile.TarInfo("IMG_1.jpg")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))

        link = tarfile.TarInfo("hard")
        link.type = tarfile.LNKTYPE
        link.linkname = "IMG_1.jpg"
        archive.addfile(link)

    result = unpack_archive(source, tmp_path / "out")
    assert ("hard", "link") in result.skipped


def test_special_files_are_skipped(tmp_path: Path) -> None:
    """A photo archive has no legitimate reason to contain a device node."""
    source = tmp_path / "special.tgz"
    with tarfile.open(source, "w:gz") as archive:
        node = tarfile.TarInfo("dev/null")
        node.type = tarfile.CHRTYPE
        node.devmajor = 1
        node.devminor = 3
        archive.addfile(node)

    result = unpack_archive(source, tmp_path / "out")
    assert result.extracted == 0
    assert ("dev/null", "special file") in result.skipped


def test_zip_symlink_entry_is_skipped(tmp_path: Path) -> None:
    source = tmp_path / "links.zip"
    with zipfile.ZipFile(source, "w") as archive:
        info = zipfile.ZipInfo("passwd-link")
        info.external_attr = (0xA1FF) << 16  # S_IFLNK | 0777
        archive.writestr(info, "/etc/passwd")
        archive.writestr("IMG_1.jpg", b"real")

    result = unpack_archive(source, tmp_path / "out")

    assert result.extracted == 1
    assert not (tmp_path / "out" / "passwd-link").exists()
    assert ("passwd-link", "link") in result.skipped
