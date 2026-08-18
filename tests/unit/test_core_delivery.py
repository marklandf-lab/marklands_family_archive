"""
Unit tests for wyeast.core.delivery: canonical archive path resolution
(album-only, multi-album, _no_album/<year> fallback) and the relative-symlink
delivery helpers (relocatable links, collision disambiguation, idempotent reuse).
"""

import json
import os
from pathlib import Path

import pytest

from wyeast.core import delivery
from wyeast.core import safe_names
from wyeast.core.errors import RequiredDataError


# ── safe_album_dirname now delegates to safe_names (review M9) ─────────────────

def test_safe_album_dirname_delegates_to_safe_names():
    # The duplicate is gone; delivery.safe_album_dirname == safe_component.
    for title in ("Cancun 2004", "My/Bad:Title", "  spaced  out  ", ""):
        assert delivery.safe_album_dirname(title) == \
            safe_names.safe_component(title, "album")


def test_safe_album_dirname_equivalent_for_existing_inputs():
    # Behaviour preserved for the ASCII album titles produced before M9.
    assert delivery.safe_album_dirname("Cancun 2004") == "Cancun_2004"
    assert delivery.safe_album_dirname("Vacation") == "Vacation"
    assert delivery.safe_album_dirname("") == "album"  # default fallback


# ── canonical_relpath ─────────────────────────────────────────────────────────

def test_canonical_relpath_album_only_uses_first_album_sorted():
    rec = {"album_membership": ["Vacation", "Cancun 2004"], "timestamp": "2004-07-01T10:00:00"}
    # first album alphabetically -> "Cancun 2004", sanitized
    assert delivery.canonical_relpath(rec, "IMG.jpg") == Path("Cancun_2004") / "IMG.jpg"


def test_canonical_relpath_no_album_falls_back_to_year():
    rec = {"album_membership": [], "timestamp": "2019-05-03T08:00:00",
           "timestamp_confidence": "high"}
    assert delivery.canonical_relpath(rec, "IMG.jpg") == Path("_no_album/2019/IMG.jpg")


def test_canonical_relpath_undated_when_no_usable_timestamp():
    assert delivery.canonical_relpath({"album_membership": []}, "IMG.jpg") == \
        Path("_no_album/undated/IMG.jpg")
    # an explicit "none" confidence is treated as undated too
    rec = {"timestamp": "2019-01-01T00:00:00", "timestamp_confidence": "none"}
    assert delivery.canonical_relpath(rec, "IMG.jpg") == Path("_no_album/undated/IMG.jpg")


def test_canonical_relpath_low_confidence_mtime_is_undated_not_mtime_year():
    # A low-confidence (legacy mtime-as-timestamp) record must NOT be filed under
    # its mtime year — only high/medium confidence yields a year bucket. This
    # keeps mtime-only photos out of a wrong year (e.g. the case-run year).
    rec = {"album_membership": [],
           "timestamp": "2026-06-28T00:00:00", "timestamp_confidence": "low"}
    assert delivery.canonical_relpath(rec, "IMG.jpg") == Path("_no_album/undated/IMG.jpg")
    # medium confidence (e.g. Google Takeout sidecar) still gets a year bucket
    rec_med = {"album_membership": [],
               "timestamp": "2019-01-01T00:00:00", "timestamp_confidence": "medium"}
    assert delivery.canonical_relpath(rec_med, "IMG.jpg") == Path("_no_album/2019/IMG.jpg")


def test_extra_album_relpaths_are_the_non_primary_albums():
    rec = {"album_membership": ["Vacation", "Cancun 2004", "Family"]}
    extras = delivery.extra_album_relpaths(rec, "IMG.jpg")
    # primary (first sorted) = "Cancun 2004"; extras = the rest, sorted
    assert extras == [Path("Family") / "IMG.jpg", Path("Vacation") / "IMG.jpg"]


def test_extra_album_relpaths_empty_for_single_album():
    assert delivery.extra_album_relpaths({"album_membership": ["Solo"]}, "x.jpg") == []


# ── relative_symlink / symlink_into ───────────────────────────────────────────

def test_relative_symlink_is_relocatable(tmp_path):
    target = tmp_path / "archive" / "a.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"img")
    link = tmp_path / "views" / "wedding" / "a.jpg"
    delivery.relative_symlink(link, target)

    assert link.is_symlink()
    # the stored target is RELATIVE (no leading slash) so the tree relocates
    assert not os.readlink(link).startswith("/")
    assert link.read_bytes() == b"img"

    # moving the whole tree keeps the link valid (same relative depth)
    moved = tmp_path.parent / (tmp_path.name + "_moved")
    os.rename(tmp_path, moved)
    assert (moved / "views" / "wedding" / "a.jpg").read_bytes() == b"img"


def test_symlink_into_disambiguates_distinct_targets(tmp_path):
    a = tmp_path / "src1" / "v.jpg"
    b = tmp_path / "src2" / "v.jpg"
    for p, c in ((a, b"AAA"), (b, b"BBB")):
        p.parent.mkdir(parents=True)
        p.write_bytes(c)
    view = tmp_path / "view"

    l1 = delivery.symlink_into(view, a)
    l2 = delivery.symlink_into(view, b)
    assert l1.name == "v.jpg" and l2.name == "v_1.jpg"
    assert l1.read_bytes() == b"AAA" and l2.read_bytes() == b"BBB"


def test_symlink_into_reuses_link_to_same_target(tmp_path):
    target = tmp_path / "a.jpg"
    target.write_bytes(b"img")
    view = tmp_path / "view"
    first = delivery.symlink_into(view, target)
    again = delivery.symlink_into(view, target)
    assert first == again  # idempotent: no v_1.jpg created
    assert list(view.iterdir()) == [first]


def test_load_archive_map_missing_returns_empty(tmp_path):
    assert delivery.load_archive_map(tmp_path) == {}


def test_load_archive_map_reads_entries(tmp_path):
    (tmp_path / delivery.ARCHIVE_MAP_NAME).write_text(
        json.dumps({"entries": {"/w/a.jpg": "/arch/a.jpg"}}))
    assert delivery.load_archive_map(tmp_path) == {"/w/a.jpg": "/arch/a.jpg"}


def test_load_archive_map_corrupt_raises(tmp_path):
    # Review H5/M5: a present-but-unparseable map must NOT silently return {}
    # (which would fail open and leave flagged media in the delivery tree). It
    # now raises the typed RequiredDataError (still a RuntimeError, so the s11
    # broad-except brander keeps catching it).
    (tmp_path / delivery.ARCHIVE_MAP_NAME).write_text("{ this is not json ")
    with pytest.raises(RequiredDataError):
        delivery.load_archive_map(tmp_path)


def test_load_archive_map_missing_distinguished_from_corrupt(tmp_path):
    # Missing -> {} (legitimate "nothing to map"); corrupt -> raises. The two
    # states must be distinguishable by the caller.
    assert delivery.load_archive_map(tmp_path) == {}      # missing
    (tmp_path / delivery.ARCHIVE_MAP_NAME).write_text("not json")
    with pytest.raises(RequiredDataError):                 # corrupt
        delivery.load_archive_map(tmp_path)
