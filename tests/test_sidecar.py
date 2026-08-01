"""Tests for sidecar matching.

Every naming variant documented in `sidecar.py` gets an explicit case here. If you have
seen a real Takeout filename this does not match, a test case alone is a valuable
contribution - see CONTRIBUTING.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ditch_google.constants import SIDECAR_FILENAME_BUDGET
from ditch_google.photos.sidecar import (
    MatchStrategy,
    candidate_sidecar_names,
    match_directory,
    match_media,
)


def index(*names: str) -> dict[str, Path]:
    """Build the lowercased sidecar index `match_media` expects."""
    return {name.lower(): Path(name) for name in names}


def match(media: str, *sidecars: str) -> tuple[str | None, MatchStrategy | None]:
    result = match_media(Path(media), index(*sidecars))
    return (result.sidecar.name if result.sidecar else None), result.strategy


# ------------------------------------------------------------------- the naming table


def test_modern_supplemental_metadata() -> None:
    assert match("IMG_1234.jpg", "IMG_1234.jpg.supplemental-metadata.json") == (
        "IMG_1234.jpg.supplemental-metadata.json",
        MatchStrategy.SUPPLEMENTAL,
    )


def test_legacy_full_name() -> None:
    assert match("IMG_1234.jpg", "IMG_1234.jpg.json") == (
        "IMG_1234.jpg.json",
        MatchStrategy.LEGACY,
    )


def test_stem_only() -> None:
    assert match("IMG_1234.jpg", "IMG_1234.json") == ("IMG_1234.json", MatchStrategy.STEM)


def test_supplemental_wins_over_legacy_when_both_exist() -> None:
    """Most specific first: the supplemental form is the one Google writes today."""
    sidecar, strategy = match(
        "IMG_1234.jpg",
        "IMG_1234.jpg.json",
        "IMG_1234.jpg.supplemental-metadata.json",
    )
    assert sidecar == "IMG_1234.jpg.supplemental-metadata.json"
    assert strategy is MatchStrategy.SUPPLEMENTAL


# ---------------------------------------------------------------------- truncation


def test_truncation_clips_to_the_budget_then_appends_json() -> None:
    """Google clips `<media>.supplemental-metadata` to 46 chars; `.json` is never clipped."""
    media = "a_really_long_photo_filename_from_a_phone_2019.jpg"
    expected = f"{(media + '.supplemental-metadata')[:SIDECAR_FILENAME_BUDGET]}.json"

    assert len(expected) == SIDECAR_FILENAME_BUDGET + len(".json")
    assert match(media, expected) == (expected, MatchStrategy.TRUNCATED)


def test_the_documented_double_dot_case() -> None:
    """A 45-character media name yields `<name>..json`.

    The 46th character is the dot that began `.supplemental-metadata`, so the clip leaves
    a trailing dot immediately before `.json`. It looks like a bug in Google's exporter,
    but it is what real archives contain.
    """
    media = "a" * 41 + ".jpg"  # 45 characters
    assert len(media) == 45

    expected = f"{media}..json"
    candidates = candidate_sidecar_names(media)
    assert expected in candidates
    assert match(media, expected) == (expected, MatchStrategy.TRUNCATED)


def test_a_name_exactly_at_the_budget_is_not_clipped() -> None:
    media = "b" * 20 + ".jpg"
    full = f"{media}.supplemental-metadata.json"
    # Short enough that the clip is a no-op only if under budget; assert we still match.
    assert match(media, full)[0] == full


def test_truncation_can_remove_the_whole_supplemental_suffix() -> None:
    """For long names the entire `.supplemental-metadata` portion disappears."""
    media = "c" * 60 + ".jpg"
    candidates = candidate_sidecar_names(media)
    clipped = next(c for c in candidates if len(c) == SIDECAR_FILENAME_BUDGET + len(".json"))
    assert "supplemental" not in clipped


# ----------------------------------------------------------------------- duplicates


def test_duplicate_marker_moves_to_the_end() -> None:
    """`IMG_1234(1).jpg` pairs with `IMG_1234.jpg.supplemental-metadata(1).json`."""
    assert match("IMG_1234(1).jpg", "IMG_1234.jpg.supplemental-metadata(1).json") == (
        "IMG_1234.jpg.supplemental-metadata(1).json",
        MatchStrategy.DUPLICATE,
    )


def test_duplicate_marker_legacy_form() -> None:
    assert match("IMG_1234(2).jpg", "IMG_1234.jpg(2).json")[0] == "IMG_1234.jpg(2).json"


def test_duplicate_with_its_own_supplemental_sidecar() -> None:
    """Some exports do name it the obvious way; that must still match."""
    assert (
        match("IMG_1234(1).jpg", "IMG_1234(1).jpg.supplemental-metadata.json")[0]
        == "IMG_1234(1).jpg.supplemental-metadata.json"
    )


def test_double_digit_duplicate_marker() -> None:
    assert match("IMG_1234(12).jpg", "IMG_1234.jpg.supplemental-metadata(12).json")[0] is not None


def test_parentheses_in_a_normal_name_are_not_a_duplicate_marker() -> None:
    """`Party (Copy).jpg` has parentheses but no numeric index."""
    sidecar, _ = match("Party (Copy).jpg", "Party (Copy).jpg.supplemental-metadata.json")
    assert sidecar == "Party (Copy).jpg.supplemental-metadata.json"


# -------------------------------------------------------------------------- edited


def test_edited_copy_shares_the_original_sidecar() -> None:
    assert match("IMG_1234-edited.jpg", "IMG_1234.jpg.supplemental-metadata.json") == (
        "IMG_1234.jpg.supplemental-metadata.json",
        MatchStrategy.EDITED,
    )


@pytest.mark.parametrize(
    "edited",
    [
        "IMG_1234-bearbeitet.jpg",
        "IMG_1234-modifié.jpg",
        "IMG_1234-editado.jpg",
        "IMG_1234-modificato.jpg",
        "IMG_1234-bewerkt.jpg",
    ],
)
def test_localised_edited_markers(edited: str) -> None:
    """Google localises the marker to the account language, not the file's origin."""
    assert match(edited, "IMG_1234.jpg.supplemental-metadata.json")[1] is MatchStrategy.EDITED


def test_edited_copy_prefers_its_own_sidecar_when_one_exists() -> None:
    sidecar, strategy = match(
        "IMG_1234-edited.jpg",
        "IMG_1234.jpg.supplemental-metadata.json",
        "IMG_1234-edited.jpg.supplemental-metadata.json",
    )
    assert sidecar == "IMG_1234-edited.jpg.supplemental-metadata.json"
    assert strategy is MatchStrategy.SUPPLEMENTAL


# ---------------------------------------------------------------------- live photos


def test_live_photo_video_inherits_the_still_sidecar() -> None:
    """Takeout writes one JSON for the still only, so the video half has none of its own."""
    assert match("IMG_1234.MP4", "IMG_1234.HEIC.supplemental-metadata.json") == (
        "IMG_1234.HEIC.supplemental-metadata.json",
        MatchStrategy.LIVE_PHOTO,
    )


def test_motion_photo_mp_extension() -> None:
    assert match("IMG_1234.MP", "IMG_1234.jpg.supplemental-metadata.json")[1] is (
        MatchStrategy.LIVE_PHOTO
    )


def test_a_plain_video_with_its_own_sidecar_is_not_a_live_photo() -> None:
    assert match("VID_9999.mp4", "VID_9999.mp4.supplemental-metadata.json") == (
        "VID_9999.mp4.supplemental-metadata.json",
        MatchStrategy.SUPPLEMENTAL,
    )


# ------------------------------------------------------------------------ case rules


def test_matching_is_case_insensitive() -> None:
    """Takeout is inconsistent about extension case, and the archive may be unpacked on
    a case-insensitive filesystem."""
    assert match("IMG_1234.JPG", "img_1234.jpg.supplemental-metadata.json")[0] is not None


# ---------------------------------------------------------------------- no match


def test_unmatched_media_is_reported_not_dropped() -> None:
    """The number of unmatched files is the headline figure in the final report."""
    result = match_media(Path("IMG_9999.jpg"), index("IMG_1234.jpg.json"))
    assert not result.matched
    assert result.sidecar is None
    assert result.strategy is None


def test_empty_index_matches_nothing() -> None:
    assert not match_media(Path("IMG_1.jpg"), {}).matched


# ------------------------------------------------------------------ whole directory


def test_match_directory_pairs_and_reports() -> None:
    media = [Path("IMG_1.jpg"), Path("IMG_2.jpg"), Path("orphan.jpg")]
    sidecars = [
        Path("IMG_1.jpg.supplemental-metadata.json"),
        Path("IMG_2.jpg.json"),
    ]

    results = match_directory(media, sidecars)

    assert [r.matched for r in results] == [True, True, False]
    assert [r.strategy for r in results] == [
        MatchStrategy.SUPPLEMENTAL,
        MatchStrategy.LEGACY,
        None,
    ]


def test_match_directory_on_a_realistic_mixed_export() -> None:
    """One directory containing most of the awkward cases at once."""
    media = [
        Path("IMG_1234.jpg"),
        Path("IMG_1234-edited.jpg"),
        Path("IMG_1234.MP4"),
        Path("IMG_5678(1).jpg"),
        Path("VID_0001.mp4"),
        Path("mystery.png"),
    ]
    sidecars = [
        Path("IMG_1234.jpg.supplemental-metadata.json"),
        Path("IMG_5678.jpg.supplemental-metadata(1).json"),
        Path("VID_0001.mp4.json"),
    ]

    results = {r.media.name: r for r in match_directory(media, sidecars)}

    assert results["IMG_1234.jpg"].strategy is MatchStrategy.SUPPLEMENTAL
    assert results["IMG_1234-edited.jpg"].strategy is MatchStrategy.EDITED
    assert results["IMG_1234.MP4"].strategy is MatchStrategy.LIVE_PHOTO
    assert results["IMG_5678(1).jpg"].strategy is MatchStrategy.DUPLICATE
    assert results["VID_0001.mp4"].strategy is MatchStrategy.LEGACY
    assert not results["mystery.png"].matched


# ------------------------------------------------------------------ candidate order


def test_candidates_are_deduplicated() -> None:
    candidates = candidate_sidecar_names("IMG_1234.jpg")
    assert len(candidates) == len(set(candidates))


def test_candidates_are_most_specific_first() -> None:
    candidates = candidate_sidecar_names("IMG_1234.jpg")
    assert candidates[0] == "IMG_1234.jpg.supplemental-metadata.json"
