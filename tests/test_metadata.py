"""Tests for sidecar parsing and EXIF writing.

The parsing tests are pure and always run. The writing tests use a real exiftool and are
marked `requires_exiftool`, because asserting that we build the right argument list proves
much less than asserting the bytes on disk actually changed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ditch_google.exiftool import ExiftoolSession
from ditch_google.photos.metadata import (
    SidecarData,
    build_exiftool_args,
    load_sidecar,
    parse_sidecar,
    write_metadata,
)

HAS_EXIFTOOL = shutil.which("exiftool") is not None
needs_exiftool = pytest.mark.skipif(not HAS_EXIFTOOL, reason="exiftool is not installed")


@pytest.fixture
def jpeg(tmp_path: Path) -> Path:
    """A real JPEG on disk, with no metadata of its own to start with."""
    return make_jpeg(tmp_path / "IMG_1234.jpg")


def make_jpeg(path: Path) -> Path:
    from PIL import Image

    Image.new("RGB", (8, 8), (120, 90, 60)).save(path, "JPEG")
    return path


def read_tags(path: Path, *tags: str) -> dict[str, str]:
    """Read tags back with a fresh exiftool, so we test the file, not our session."""
    output = subprocess.run(
        [shutil.which("exiftool") or "exiftool", "-json", "-n", *tags, str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    parsed: dict[str, str] = json.loads(output.stdout)[0]
    return parsed


# ------------------------------------------------------------------------- parsing


def test_parse_full_sidecar() -> None:
    data = parse_sidecar(
        {
            "photoTakenTime": {"timestamp": "1560000000"},
            "geoData": {"latitude": 38.7071, "longitude": -9.1355, "altitude": 12.0},
            "description": "Sunset over the bridge",
            "people": [{"name": "Ana"}, {"name": "Bruno"}],
        }
    )

    assert data.taken_at == datetime(2019, 6, 8, 13, 20, tzinfo=UTC)
    assert data.latitude == pytest.approx(38.7071)
    assert data.description == "Sunset over the bridge"
    assert data.people == ("Ana", "Bruno")


def test_zeroed_geodata_falls_back_to_geodataexif() -> None:
    """Google zeroes geoData when location history was off, rather than omitting it."""
    data = parse_sidecar(
        {
            "geoData": {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0},
            "geoDataExif": {"latitude": 38.7071, "longitude": -9.1355, "altitude": 12.0},
        }
    )
    assert data.latitude == pytest.approx(38.7071)
    assert data.longitude == pytest.approx(-9.1355)


def test_both_geo_blocks_zeroed_yields_no_location() -> None:
    """Writing 0,0 would put the photo in the Gulf of Guinea."""
    data = parse_sidecar(
        {
            "geoData": {"latitude": 0.0, "longitude": 0.0},
            "geoDataExif": {"latitude": 0.0, "longitude": 0.0},
        }
    )
    assert not data.has_location
    assert data.latitude is None


def test_geodata_is_preferred_over_geodataexif_when_both_are_real() -> None:
    data = parse_sidecar(
        {
            "geoData": {"latitude": 1.0, "longitude": 2.0},
            "geoDataExif": {"latitude": 3.0, "longitude": 4.0},
        }
    )
    assert data.latitude == pytest.approx(1.0)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"photoTakenTime": {}},
        {"photoTakenTime": {"timestamp": "not-a-number"}},
        {"photoTakenTime": "nonsense"},
        {"geoData": "nonsense"},
        {"people": "nonsense"},
        {"description": "   "},
        "not a dict",
        None,
    ],
)
def test_malformed_sidecars_do_not_raise(payload: object) -> None:
    """A broken field should cost that field, not the whole photo."""
    assert isinstance(parse_sidecar(payload), SidecarData)


def test_a_broken_geo_block_still_yields_the_timestamp() -> None:
    data = parse_sidecar(
        {"photoTakenTime": {"timestamp": "1560000000"}, "geoData": {"latitude": "abc"}}
    )
    assert data.taken_at is not None
    assert not data.has_location


def test_load_sidecar_reads_a_file(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"photoTakenTime": {"timestamp": "1560000000"}}))
    assert load_sidecar(path).taken_at is not None


def test_load_sidecar_survives_a_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text("{ this is not json")
    assert load_sidecar(path).is_empty


def test_load_sidecar_survives_a_missing_file(tmp_path: Path) -> None:
    assert load_sidecar(tmp_path / "absent.json").is_empty


# ---------------------------------------------------------------- argument building


def test_no_args_when_there_is_nothing_to_write() -> None:
    assert build_exiftool_args(SidecarData(), Path("IMG.jpg")) == []


def test_timestamp_args_include_an_explicit_offset() -> None:
    """Takeout gives an instant, not a wall-clock time; the offset removes the ambiguity."""
    args = build_exiftool_args(
        SidecarData(taken_at=datetime(2019, 6, 8, 13, 20, tzinfo=UTC)), Path("IMG.jpg")
    )
    assert "-EXIF:DateTimeOriginal=2019:06:08 13:20:00" in args
    assert "-EXIF:OffsetTimeOriginal=+00:00" in args


def test_video_gets_quicktime_tags_too() -> None:
    """EXIF tags are not read from most video containers; QuickTime atoms are."""
    args = build_exiftool_args(
        SidecarData(taken_at=datetime(2019, 6, 8, 13, 20, tzinfo=UTC)), Path("VID.mp4")
    )
    assert any(arg.startswith("-QuickTime:CreateDate=") for arg in args)


def test_stills_do_not_get_quicktime_tags() -> None:
    args = build_exiftool_args(
        SidecarData(taken_at=datetime(2019, 6, 8, 13, 20, tzinfo=UTC)), Path("IMG.jpg")
    )
    assert not any(arg.startswith("-QuickTime:") for arg in args)


@pytest.mark.parametrize(
    ("latitude", "longitude", "lat_ref", "lon_ref"),
    [
        (38.7071, -9.1355, "N", "W"),
        (-33.8688, 151.2093, "S", "E"),
    ],
)
def test_gps_hemisphere_refs(latitude: float, longitude: float, lat_ref: str, lon_ref: str) -> None:
    """exiftool needs magnitudes plus a hemisphere; a signed value alone is wrong."""
    args = build_exiftool_args(SidecarData(latitude=latitude, longitude=longitude), Path("IMG.jpg"))
    assert f"-EXIF:GPSLatitudeRef={lat_ref}" in args
    assert f"-EXIF:GPSLongitudeRef={lon_ref}" in args
    assert f"-EXIF:GPSLatitude={abs(latitude)}" in args


def test_prefer_existing_adds_create_only_write_mode() -> None:
    args = build_exiftool_args(SidecarData(description="x"), Path("IMG.jpg"), prefer_existing=True)
    assert "-wm" in args
    assert "cg" in args


def test_default_overwrites_existing_tags() -> None:
    """The sidecar is authoritative by default - it is what Google Photos itself shows."""
    args = build_exiftool_args(SidecarData(description="x"), Path("IMG.jpg"))
    assert "-wm" not in args


def test_writes_in_place_to_avoid_doubling_disk_use() -> None:
    args = build_exiftool_args(SidecarData(description="x"), Path("IMG.jpg"))
    assert "-overwrite_original_in_place" in args


def test_people_become_repeatable_xmp_tags() -> None:
    args = build_exiftool_args(SidecarData(people=("Ana", "Bruno")), Path("IMG.jpg"))
    assert args.count("-XMP:PersonInImage+=Ana") == 1
    assert args.count("-XMP:PersonInImage+=Bruno") == 1


# --------------------------------------------------------- writing to a real file


@needs_exiftool
def test_session_round_trip(jpeg: Path) -> None:
    with ExiftoolSession() as session:
        session.execute(["-EXIF:ImageDescription=hello", "-overwrite_original", str(jpeg)])
    assert read_tags(jpeg, "-ImageDescription")["ImageDescription"] == "hello"


@needs_exiftool
def test_session_reuses_one_process_across_commands(jpeg: Path, tmp_path: Path) -> None:
    """The whole point of -stay_open: many commands, one process."""
    second = tmp_path / "IMG_5678.jpg"
    second.write_bytes(jpeg.read_bytes())

    with ExiftoolSession() as session:
        session.execute(["-EXIF:ImageDescription=first", "-overwrite_original", str(jpeg)])
        session.execute(["-EXIF:ImageDescription=second", "-overwrite_original", str(second)])

    assert read_tags(jpeg, "-ImageDescription")["ImageDescription"] == "first"
    assert read_tags(second, "-ImageDescription")["ImageDescription"] == "second"


@needs_exiftool
def test_write_metadata_sets_date_and_location(jpeg: Path) -> None:
    data = SidecarData(
        taken_at=datetime(2019, 6, 8, 13, 20, tzinfo=UTC),
        latitude=38.7071,
        longitude=-9.1355,
        description="Sunset over the bridge",
    )

    with ExiftoolSession() as session:
        assert write_metadata(session, jpeg, data)

    tags = read_tags(
        jpeg, "-DateTimeOriginal", "-GPSLatitude", "-GPSLongitude", "-ImageDescription"
    )
    assert tags["DateTimeOriginal"] == "2019:06:08 13:20:00"
    assert float(tags["GPSLatitude"]) == pytest.approx(38.7071, abs=1e-4)
    assert float(tags["GPSLongitude"]) == pytest.approx(-9.1355, abs=1e-4)
    assert tags["ImageDescription"] == "Sunset over the bridge"


@needs_exiftool
def test_southern_western_coordinates_round_trip_negative(jpeg: Path) -> None:
    """A sign error here would put Sydney in the North Atlantic."""
    data = SidecarData(latitude=-33.8688, longitude=151.2093)
    with ExiftoolSession() as session:
        write_metadata(session, jpeg, data)

    tags = read_tags(jpeg, "-GPSLatitude", "-GPSLongitude")
    assert float(tags["GPSLatitude"]) == pytest.approx(-33.8688, abs=1e-4)
    assert float(tags["GPSLongitude"]) == pytest.approx(151.2093, abs=1e-4)


@needs_exiftool
def test_write_metadata_sets_the_file_mtime(jpeg: Path) -> None:
    taken = datetime(2019, 6, 8, 13, 20, tzinfo=UTC)
    with ExiftoolSession() as session:
        write_metadata(session, jpeg, SidecarData(taken_at=taken))

    assert jpeg.stat().st_mtime == pytest.approx(taken.timestamp(), abs=1)


@needs_exiftool
def test_prefer_existing_does_not_clobber_a_good_date(jpeg: Path) -> None:
    with ExiftoolSession() as session:
        session.execute(
            ["-EXIF:DateTimeOriginal=2001:01:01 00:00:00", "-overwrite_original", str(jpeg)]
        )
        write_metadata(
            session,
            jpeg,
            SidecarData(taken_at=datetime(2019, 6, 8, 13, 20, tzinfo=UTC)),
            prefer_existing=True,
        )

    assert read_tags(jpeg, "-DateTimeOriginal")["DateTimeOriginal"] == "2001:01:01 00:00:00"


@needs_exiftool
def test_default_does_clobber_an_existing_date(jpeg: Path) -> None:
    """Google sometimes overwrote the original EXIF date with the upload date."""
    with ExiftoolSession() as session:
        session.execute(
            ["-EXIF:DateTimeOriginal=2001:01:01 00:00:00", "-overwrite_original", str(jpeg)]
        )
        write_metadata(
            session, jpeg, SidecarData(taken_at=datetime(2019, 6, 8, 13, 20, tzinfo=UTC))
        )

    assert read_tags(jpeg, "-DateTimeOriginal")["DateTimeOriginal"] == "2019:06:08 13:20:00"


@needs_exiftool
def test_write_metadata_returns_false_for_an_empty_sidecar(jpeg: Path) -> None:
    with ExiftoolSession() as session:
        assert write_metadata(session, jpeg, SidecarData()) is False


@needs_exiftool
def test_a_failed_write_raises_rather_than_reporting_success(tmp_path: Path) -> None:
    """Regression: a failed write was silently counted as a success.

    exiftool announces the failure on stdout, ordered before its ready sentinel, but puts
    the explanatory `Error:` line on stderr, which arrives asynchronously. Keying the
    decision off stderr meant we usually checked before the line landed.
    """
    from ditch_google.exiftool import ExiftoolError

    not_an_image = tmp_path / "broken.jpg"
    not_an_image.write_text("this is definitely not a jpeg")

    with ExiftoolSession() as session, pytest.raises(ExiftoolError) as caught:
        write_metadata(
            session, not_an_image, SidecarData(taken_at=datetime(2019, 6, 8, 13, 20, tzinfo=UTC))
        )

    assert "not a valid jpg" in str(caught.value).lower()


@needs_exiftool
def test_failures_are_detected_consistently_across_many_writes(tmp_path: Path) -> None:
    """The race was timing-dependent, so prove it holds over repeated attempts."""
    from ditch_google.exiftool import ExiftoolError

    data = SidecarData(taken_at=datetime(2019, 6, 8, 13, 20, tzinfo=UTC))
    failures = 0

    with ExiftoolSession() as session:
        for n in range(10):
            broken = tmp_path / f"broken_{n}.jpg"
            broken.write_text("not a jpeg")
            try:
                write_metadata(session, broken, data)
            except ExiftoolError:
                failures += 1

    assert failures == 10
