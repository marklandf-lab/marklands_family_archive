"""Poster regeneration (regen_video_posters.py).

The stills the Videos grid uses as tile images live in the pipeline's WORKING
tree, which a delivery does not carry — so on a delivered case every poster 404s.
This tool rebuilds them from the delivered videos via macOS Quick Look.

The planning half is what these tests cover: which posters get regenerated, which
are left alone, and which are honestly reported as unfixable. The Quick Look call
itself is injected, so nothing here spawns a subprocess or depends on the host's
video codecs.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import regen_video_posters as rp  # noqa: E402


def frame_map(*entries):
    """{frame path: {source_video, frame_index, frame_offset_seconds}} in the
    given ORDER — order is load-bearing, see test_poster_is_the_first_frame."""
    return {f: {"source_video": s, "frame_index": i, "frame_offset_seconds": o}
            for f, s, i, o in entries}


def test_poster_is_the_first_frame_each_video_contributes():
    """Mirrors video_rows' pick. A video with several stills posters on the first
    one IN FILE ORDER — not the lowest frame_index — because that is what the
    grid asks for."""
    fm = frame_map(
        ("/w/photos/a_f000002.jpg", "/w/videos/a.mov", 2, 30),
        ("/w/photos/a_f000001.jpg", "/w/videos/a.mov", 1, 0),
        ("/w/photos/b_f000001.jpg", "/w/videos/b.mov", 1, 0),
    )
    assert rp.poster_frames(fm) == {
        "/w/videos/a.mov": "/w/photos/a_f000002.jpg",
        "/w/videos/b.mov": "/w/photos/b_f000001.jpg",
    }


def test_later_offset_frames_are_counted_not_silently_dropped():
    """Quick Look cannot seek, so the 30s/60s stills stay missing. The count is
    reported — a tool that fixes 80% of a problem must not read as fixing it."""
    fm = frame_map(
        ("/w/photos/a_f000001.jpg", "/w/videos/a.mov", 1, 0),
        ("/w/photos/a_f000002.jpg", "/w/videos/a.mov", 2, 30),
        ("/w/photos/a_f000003.jpg", "/w/videos/a.mov", 3, 60),
    )
    assert rp.orphan_frame_count(fm) == 2


def test_plan_targets_the_delivered_video_not_the_working_path(tmp_path):
    """The map names a working path the delivery never carried; the bytes are at
    the archive canonical. The plan must read from the canonical."""
    canonical = tmp_path / "archive" / "a.mov"
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(b"\x00\x00\x00\x20ftypqt  ")
    fm = frame_map(("/w/photos/a_f000001.jpg", "/w/videos/a.mov", 1, 0))
    todo, skipped, unresolved = rp.build_plan(fm, {"/w/videos/a.mov": str(canonical)})
    assert todo == [(str(canonical), "/w/photos/a_f000001.jpg")]
    assert not skipped and not unresolved


def test_an_existing_still_is_never_overwritten(tmp_path):
    """If the real workstation stills are re-delivered later they must win. A
    still that is already on disk is skipped, not regenerated over."""
    dest = tmp_path / "photos" / "a_f000001.jpg"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"the real still")
    canonical = tmp_path / "a.mov"
    canonical.write_bytes(b"movie")
    fm = frame_map((str(dest), "/w/videos/a.mov", 1, 0))
    todo, skipped, _unresolved = rp.build_plan(fm, {"/w/videos/a.mov": str(canonical)})
    assert todo == [] and skipped == [str(dest)]
    assert dest.read_bytes() == b"the real still"


def test_undelivered_source_is_reported_not_attempted(tmp_path):
    """A quarantined or moved-out video has no tile in the grid, so a poster for
    it would serve nobody — and there are no bytes to make one from."""
    fm = frame_map(
        ("/w/photos/gone_f000001.jpg", "/w/videos/gone.mov", 1, 0),   # not mapped
        ("/w/photos/moved_f000001.jpg", "/w/videos/moved.mov", 1, 0),  # mapped, absent
    )
    todo, _skipped, unresolved = rp.build_plan(
        fm, {"/w/videos/moved.mov": str(tmp_path / "not-here.mov")})
    assert todo == []
    assert sorted(unresolved) == ["/w/videos/gone.mov", "/w/videos/moved.mov"]


def test_dry_run_writes_nothing(tmp_path):
    dest = tmp_path / "photos" / "a_f000001.jpg"
    called = []

    def grabber(video, box):          # must never run
        called.append(video)
        return b"jpeg", "640x480"

    written, failed = rp.run([("/v/a.mov", str(dest))], apply=False, grabber=grabber)
    assert (written, failed, called) == ([], [], [])
    assert not dest.exists()


def test_apply_writes_the_still_where_the_map_expects_it(tmp_path):
    dest = tmp_path / "photos" / "a_f000001.jpg"
    written, failed = rp.run([("/v/a.mov", str(dest))], apply=True, jobs=1,
                             grabber=lambda video, box: (b"jpegbytes", "640x480"))
    assert written == [str(dest)] and failed == []
    assert dest.read_bytes() == b"jpegbytes"


def test_a_failed_grab_leaves_no_file_behind(tmp_path):
    """Quick Look falling back to a generic icon must produce nothing at all —
    a placeholder written into the gallery is worse than a missing tile, because
    it looks like the archive lost the video."""
    dest = tmp_path / "photos" / "a_f000001.jpg"
    written, failed = rp.run([("/v/a.mov", str(dest))], apply=True, jobs=1,
                             grabber=lambda video, box: (None, "fallback icon (32x32)"))
    assert written == []
    assert failed == [(str(dest), "fallback icon (32x32)")]
    assert not dest.exists()


def test_write_is_atomic_and_leaves_no_temp(tmp_path):
    dest = tmp_path / "deep" / "nested" / "a.jpg"
    rp.write_atomically(dest, b"data")
    assert dest.read_bytes() == b"data"
    assert [p.name for p in dest.parent.iterdir()] == ["a.jpg"]


@pytest.mark.skipif(not Path(rp.QLMANAGE).exists(), reason="macOS-only")
def test_qlmanage_rejects_a_file_it_cannot_thumbnail(tmp_path):
    """The real grabber, on something that is not a movie: no bytes, a reason —
    never a half-written or icon-shaped 'poster'.

    qlmanage does not fail on a corrupt movie, it HANGS — verified, indefinitely.
    That is why grab_poster passes a timeout at all, and why this test uses a
    deliberately tiny one: the assertion is that a stuck Quick Look degrades to a
    reported failure instead of wedging a 451-video run."""
    junk = tmp_path / "not-a-movie.mov"
    junk.write_bytes(b"definitely not a quicktime container")
    data, reason = rp.grab_poster(junk, box=256, timeout=2)
    assert data is None
    assert "timed out" in reason or "no thumbnail" in reason
