"""
Unit tests for wyeast.core.moves — the custody-backed crash-safe move ledger.

These run with the stdlib only. Cross-device behaviour is simulated by
monkeypatching ``moves._same_device`` (force the copy+verify path) and
``moves._copy_file`` / ``os.replace`` (crash mid-operation) — no real
cross-filesystem mount is needed.
"""

import json
import os
import threading
from pathlib import Path

import pytest

from wyeast.core import moves
from wyeast.core.custody import ChainOfCustody, sha256_of
from wyeast.core.moves import (
    MoveLedger,
    MoveResult,
    UnresolvedMoveLoss,
    move_tracked,
    move_tracked_result,
    reconcile,
    resolve_collision_free,
    R_REDO,
    R_PROMOTED,
    R_DUPLICATE,
    R_PARTIAL,
    R_UNRESOLVED,
    R_DONE,
    STATUS_INTENT,
    STATUS_DONE,
)


# ── fixtures / helpers ────────────────────────────────────────────────────
@pytest.fixture
def metadata_dir(tmp_path):
    d = tmp_path / "output" / "metadata"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def custody(tmp_path):
    return ChainOfCustody(tmp_path / "logs" / "chain_of_custody.log")


def _make_src(tmp_path, name="photo.jpg", content=b"hello world"):
    src = tmp_path / "src" / name
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(content)
    return src


def _force_cross_device(monkeypatch):
    """Make move_tracked always take the copy+verify path."""
    monkeypatch.setattr(moves, "_same_device", lambda a, b: False)


# ── happy path ─────────────────────────────────────────────────────────────
def test_happy_path_same_device(tmp_path, metadata_dir, custody):
    src = _make_src(tmp_path)
    dst = tmp_path / "duplicates" / "photo.jpg"
    expected_sha = sha256_of(src)
    ledger = MoveLedger.for_metadata_dir(metadata_dir)

    out = move_tracked(src, dst, reason="exact_dupe", ledger=ledger, custody=custody)

    assert out == dst
    assert dst.exists()
    assert not src.exists()  # source gone
    assert sha256_of(dst) == expected_sha

    entries = ledger.load()
    assert [e["status"] for e in entries] == [STATUS_INTENT, STATUS_DONE]
    assert entries[0]["dst"] == str(dst)
    assert entries[0]["sha256"] == expected_sha
    assert entries[0]["reason"] == "exact_dupe"

    custody_lines = custody.log_path.read_text().splitlines()
    assert len(custody_lines) == 1
    assert custody_lines[0].startswith(expected_sha)
    assert str(dst) in custody_lines[0]


def test_happy_path_cross_device(tmp_path, metadata_dir, custody, monkeypatch):
    _force_cross_device(monkeypatch)
    src = _make_src(tmp_path, content=b"cross device bytes")
    dst = tmp_path / "dupes" / "photo.jpg"
    expected_sha = sha256_of(src)
    ledger = MoveLedger.for_metadata_dir(metadata_dir)

    out = move_tracked(src, dst, reason="perceptual_dupe", ledger=ledger, custody=custody)

    assert out == dst
    assert dst.read_bytes() == b"cross device bytes"
    assert not src.exists()
    # No leftover temp files.
    assert not list(dst.parent.glob("*.tmp.*"))
    assert sha256_of(dst) == expected_sha
    assert [e["status"] for e in ledger.load()] == [STATUS_INTENT, STATUS_DONE]


def test_collision_disambiguation_recorded_against_final(tmp_path, metadata_dir, custody):
    # Pre-existing dst forces name_1; intent must name the final path.
    src = _make_src(tmp_path, content=b"abc")
    dstdir = tmp_path / "dupes"
    dstdir.mkdir()
    (dstdir / "photo.jpg").write_bytes(b"already here")
    dst = dstdir / "photo.jpg"
    ledger = MoveLedger.for_metadata_dir(metadata_dir)

    out = move_tracked(src, dst, reason="dupe", ledger=ledger, custody=custody)

    assert out == dstdir / "photo_1.jpg"
    assert out.exists()
    entries = ledger.load()
    assert entries[0]["dst"] == str(dstdir / "photo_1.jpg")  # intent names final
    assert entries[1]["dst"] == str(dstdir / "photo_1.jpg")


def test_resolve_collision_free(tmp_path):
    d = tmp_path
    assert resolve_collision_free(d / "x.jpg") == d / "x.jpg"
    (d / "x.jpg").write_text("a")
    assert resolve_collision_free(d / "x.jpg") == d / "x_1.jpg"
    (d / "x_1.jpg").write_text("b")
    assert resolve_collision_free(d / "x.jpg") == d / "x_2.jpg"


# ── move_tracked_result: returned sha IS the destination content hash ─────
def test_move_tracked_result_sha_is_destination_hash_same_device(
        tmp_path, metadata_dir, custody):
    src = _make_src(tmp_path, content=b"payload bytes")
    dst = tmp_path / "duplicates" / "photo.jpg"
    expected_sha = sha256_of(src)

    res = move_tracked_result(src, dst, reason="collect",
                              ledger=MoveLedger.for_metadata_dir(metadata_dir),
                              custody=custody)

    assert isinstance(res, MoveResult)
    assert res.final == dst
    # The returned src-sha equals the destination's actual content hash.
    assert res.sha256 == expected_sha
    assert res.sha256 == sha256_of(res.final)


def test_move_tracked_result_sha_is_destination_hash_cross_device(
        tmp_path, metadata_dir, custody, monkeypatch):
    # Copy+verify path: the temp copy's hash is checked against the source
    # hash before os.replace, so the returned sha is the landed bytes' hash.
    _force_cross_device(monkeypatch)
    src = _make_src(tmp_path, content=b"cross-device payload")
    dst = tmp_path / "duplicates" / "photo.jpg"

    res = move_tracked_result(src, dst, reason="collect",
                              ledger=MoveLedger.for_metadata_dir(metadata_dir),
                              custody=custody)

    assert res.sha256 == sha256_of(res.final)
    assert not src.exists()


def test_move_tracked_wrapper_returns_only_final_path(
        tmp_path, metadata_dir, custody):
    # move_tracked is a thin wrapper over move_tracked_result: same behaviour,
    # bare Path return (the 20+ existing call sites are untouched).
    src = _make_src(tmp_path, content=b"wrapper")
    dst = tmp_path / "duplicates" / "photo.jpg"
    out = move_tracked(src, dst, reason="collect",
                       ledger=MoveLedger.for_metadata_dir(metadata_dir),
                       custody=custody)
    assert isinstance(out, Path) and not isinstance(out, tuple)
    assert out == dst and dst.exists() and not src.exists()


# ── crash between copy and replace → reconcile recovers ───────────────────
def test_crash_between_copy_and_replace_then_reconcile(
        tmp_path, metadata_dir, custody, monkeypatch):
    _force_cross_device(monkeypatch)
    src = _make_src(tmp_path, content=b"durable")
    dst = tmp_path / "dupes" / "photo.jpg"
    expected_sha = sha256_of(src)
    ledger = MoveLedger.for_metadata_dir(metadata_dir)

    # Simulate a crash AFTER os.replace lands the file but BEFORE unlink(src):
    # both src and dst exist with matching hash (duplicate-recovery row).
    real_remove = os.remove

    def crash_on_src_remove(path):
        if str(path) == str(src):
            raise KeyboardInterrupt("crash before source unlink")
        return real_remove(path)

    monkeypatch.setattr(os, "remove", crash_on_src_remove)
    with pytest.raises(KeyboardInterrupt):
        move_tracked(src, dst, reason="dupe", ledger=ledger, custody=custody)
    monkeypatch.setattr(os, "remove", real_remove)

    # Ledger has only the intent line; src AND dst now both exist with match.
    statuses = [e["status"] for e in ledger.load()]
    assert statuses == [STATUS_INTENT]
    assert src.exists() and dst.exists()

    summary = reconcile(ledger)
    assert len(summary[R_DUPLICATE]) == 1
    assert summary[R_DUPLICATE][0]["sha256"] == expected_sha


def test_crash_during_copy_leaves_source_intact(
        tmp_path, metadata_dir, custody, monkeypatch):
    _force_cross_device(monkeypatch)
    src = _make_src(tmp_path, content=b"intact")
    dst = tmp_path / "dupes" / "photo.jpg"
    ledger = MoveLedger.for_metadata_dir(metadata_dir)

    def crash_copy(s, d, chunk_size=1 << 20):
        # Write a partial/garbage temp then crash, mimicking a half-copy.
        d.write_bytes(b"part")
        raise KeyboardInterrupt("crash mid-copy")

    monkeypatch.setattr(moves, "_copy_file", crash_copy)
    with pytest.raises(KeyboardInterrupt):
        move_tracked(src, dst, reason="dupe", ledger=ledger, custody=custody)

    # Source untouched; ledger shows intent only → reconcile says redo.
    assert src.exists()
    assert [e["status"] for e in ledger.load()] == [STATUS_INTENT]
    summary = reconcile(ledger)
    assert len(summary[R_REDO]) == 1


def test_verify_mismatch_aborts_and_cleans_temp(
        tmp_path, metadata_dir, custody, monkeypatch):
    _force_cross_device(monkeypatch)
    src = _make_src(tmp_path, content=b"real bytes")
    dst = tmp_path / "dupes" / "photo.jpg"
    ledger = MoveLedger.for_metadata_dir(metadata_dir)

    def corrupt_copy(s, d, chunk_size=1 << 20):
        d.write_bytes(b"CORRUPTED")  # different bytes → hash mismatch

    monkeypatch.setattr(moves, "_copy_file", corrupt_copy)
    with pytest.raises(IOError):
        move_tracked(src, dst, reason="dupe", ledger=ledger, custody=custody)

    assert src.exists()                       # source never destroyed
    assert not dst.exists()                   # final not created
    assert not list(dst.parent.glob("*.tmp.*"))  # temp cleaned
    assert [e["status"] for e in ledger.load()] == [STATUS_INTENT]


# ── each of the 6 reconcile rows ───────────────────────────────────────────
def _write_ledger(metadata_dir, entries):
    led = MoveLedger.for_metadata_dir(metadata_dir)
    for e in entries:
        led.record(e["src"], e["dst"], e["sha256"], e.get("reason", "r"), e["status"])
    return led


def test_reconcile_row_src_exists_dst_absent_redo(tmp_path, metadata_dir):
    src = _make_src(tmp_path, content=b"x")
    dst = tmp_path / "d" / "photo.jpg"
    led = _write_ledger(metadata_dir, [{
        "src": str(src), "dst": str(dst), "sha256": sha256_of(src),
        "status": STATUS_INTENT}])
    summary = reconcile(led)
    assert len(summary[R_REDO]) == 1


def test_reconcile_row_src_absent_dst_match_promote(tmp_path, metadata_dir):
    dst = tmp_path / "d" / "photo.jpg"
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b"landed")
    src = tmp_path / "src" / "photo.jpg"  # never created
    led = _write_ledger(metadata_dir, [{
        "src": str(src), "dst": str(dst), "sha256": sha256_of(dst),
        "status": STATUS_INTENT}])
    summary = reconcile(led)
    assert len(summary[R_PROMOTED]) == 1
    # A `done` line was appended (append-only promotion).
    statuses = [e["status"] for e in led.load()]
    assert statuses == [STATUS_INTENT, STATUS_DONE]
    # Re-running reconcile must not re-promote (latest status is done now).
    summary2 = reconcile(led)
    assert len(summary2[R_PROMOTED]) == 0
    assert len(summary2[R_DONE]) == 1


def test_reconcile_row_both_exist_match_duplicate(tmp_path, metadata_dir):
    content = b"same"
    src = _make_src(tmp_path, content=content)
    dst = tmp_path / "d" / "photo.jpg"
    dst.parent.mkdir(parents=True)
    dst.write_bytes(content)
    led = _write_ledger(metadata_dir, [{
        "src": str(src), "dst": str(dst), "sha256": sha256_of(src),
        "status": STATUS_INTENT}])
    summary = reconcile(led)
    assert len(summary[R_DUPLICATE]) == 1
    assert src.exists() and dst.exists()  # no double-move; both preserved


def test_reconcile_row_both_exist_mismatch_partial(tmp_path, metadata_dir):
    src = _make_src(tmp_path, content=b"source")
    dst = tmp_path / "d" / "photo.jpg"
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b"DIFFERENT")  # partial / wrong dest
    led = _write_ledger(metadata_dir, [{
        "src": str(src), "dst": str(dst), "sha256": sha256_of(src),
        "status": STATUS_INTENT}])
    summary = reconcile(led)
    assert len(summary[R_PARTIAL]) == 1
    assert summary[R_PARTIAL][0]["_partial_policy"] == "quarantine"


def test_reconcile_row_dst_absent_when_src_absent_is_unresolved(
        tmp_path, metadata_dir):
    src = tmp_path / "src" / "gone.jpg"
    dst = tmp_path / "d" / "gone.jpg"
    led = _write_ledger(metadata_dir, [{
        "src": str(src), "dst": str(dst), "sha256": "0" * 64,
        "status": STATUS_INTENT}])
    with pytest.raises(UnresolvedMoveLoss):
        reconcile(led)
    report = metadata_dir / "_move_ledger_unresolved.json"
    assert report.exists()
    doc = json.loads(report.read_text())
    assert doc["unresolved_count"] == 1
    assert doc["entries"][0]["src"] == str(src)


def test_reconcile_row_done_skipped(tmp_path, metadata_dir):
    # status=done is never re-evaluated even if files are absent.
    led = _write_ledger(metadata_dir, [{
        "src": str(tmp_path / "x"), "dst": str(tmp_path / "y"),
        "sha256": "0" * 64, "status": STATUS_DONE}])
    summary = reconcile(led)
    assert len(summary[R_DONE]) == 1
    assert summary[R_UNRESOLVED] == []


def test_reconcile_unresolved_does_not_silently_skip(tmp_path, metadata_dir):
    # A loss entry mixed with a healthy redo must still RAISE (not swallow).
    src_ok = _make_src(tmp_path, content=b"ok")
    led = MoveLedger.for_metadata_dir(metadata_dir)
    led.record(str(src_ok), str(tmp_path / "d" / "ok.jpg"), sha256_of(src_ok),
               "r", STATUS_INTENT)
    led.record(str(tmp_path / "gone"), str(tmp_path / "gone2"), "0" * 64,
               "r", STATUS_INTENT)
    with pytest.raises(UnresolvedMoveLoss):
        reconcile(led)


# ── concurrency ────────────────────────────────────────────────────────────
def test_concurrent_appenders_lose_no_entries(tmp_path, metadata_dir):
    ledger = MoveLedger.for_metadata_dir(metadata_dir)
    n_threads = 8
    per_thread = 25

    def worker(tid):
        for i in range(per_thread):
            ledger.record(
                f"/src/{tid}/{i}", f"/dst/{tid}/{i}",
                f"{tid:02d}{i:02d}".ljust(64, "0"), "concurrent", STATUS_DONE)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    entries = ledger.load()
    assert len(entries) == n_threads * per_thread
    # Every line is a complete, parseable JSON object (no interleaving torn
    # writes) and every (tid,i) pair is present exactly once.
    seen = {(e["src"], e["dst"]) for e in entries}
    assert len(seen) == n_threads * per_thread
    raw_lines = ledger.path.read_text().splitlines()
    for line in raw_lines:
        json.loads(line)  # would raise if a write was torn


def test_concurrent_move_tracked_interleaved_reconcile(
        tmp_path, metadata_dir, custody):
    """Full moves (interleaved intent/done pairs) from N threads through ONE
    ledger: every entry lands, latest-status collapses each move identity to
    `done`, and reconcile classifies everything clean — the substrate the
    collect_dedup thread pool relies on."""
    ledger = MoveLedger.for_metadata_dir(metadata_dir)
    dest_dir = tmp_path / "dupes"
    n_threads, per_thread = 8, 10
    failures: list = []

    def worker(tid):
        try:
            for i in range(per_thread):
                src = _make_src(tmp_path, name=f"t{tid}_{i}.jpg",
                                content=f"{tid}:{i}".encode())
                move_tracked(src, dest_dir / src.name, reason="concurrent",
                             ledger=ledger, custody=custody)
        except Exception as e:  # noqa: BLE001 — surfaced via the assert below
            failures.append(e)

    threads = [threading.Thread(target=worker, args=(t,))
               for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert failures == []
    total = n_threads * per_thread
    entries = ledger.load()
    assert len(entries) == 2 * total          # one intent + one done per move
    latest = ledger.latest_status()
    assert len(latest) == total
    assert all(e["status"] == STATUS_DONE for e in latest.values())

    summary = reconcile(ledger)
    assert len(summary[R_DONE]) == total
    for code in (R_REDO, R_PROMOTED, R_DUPLICATE, R_PARTIAL, R_UNRESOLVED):
        assert summary[code] == []
    # Every file actually landed with intact bytes.
    landed = list(dest_dir.glob("*.jpg"))
    assert len(landed) == total
    assert {p.read_bytes() for p in landed} == {
        f"{t}:{i}".encode() for t in range(n_threads) for i in range(per_thread)}


def test_iter_skips_trailing_partial_line(tmp_path, metadata_dir):
    ledger = MoveLedger.for_metadata_dir(metadata_dir)
    ledger.record("/a", "/b", "0" * 64, "r", STATUS_DONE)
    # Simulate a crash mid-append: a truncated final line.
    with open(ledger.path, "a") as f:
        f.write('{"src": "/c", "dst": "/d", "sha256": "incomp')
    entries = ledger.load()
    assert len(entries) == 1
    assert entries[0]["src"] == "/a"


# ── done_dst_index: O(1) resume lookup ────────────────────────────────────
# Regression cover for the quadratic resume scan found on case dbdoc: callers
# resolving "was this source already moved?" once per file used latest_status(),
# which re-opens and re-parses the whole ledger on every call.

def test_done_dst_index_maps_done_src_to_dst(tmp_path, metadata_dir):
    ledger = MoveLedger.for_metadata_dir(metadata_dir)
    ledger.record_intent(tmp_path / "a.jpg", tmp_path / "out" / "a.jpg", "sha-a", "collect")
    ledger.record_done(tmp_path / "a.jpg", tmp_path / "out" / "a.jpg", "sha-a", "collect")
    ledger.record_intent(tmp_path / "b.jpg", tmp_path / "out" / "b.jpg", "sha-b", "collect")

    idx = ledger.done_dst_index()
    # only the completed move is indexed; a bare intent is not a resume point
    assert idx == {str(tmp_path / "a.jpg"): str(tmp_path / "out" / "a.jpg")}


def test_done_dst_index_picks_up_a_prior_runs_ledger(tmp_path, metadata_dir):
    """A ledger written by an earlier (crashed) process is read on first use."""
    writer = MoveLedger.for_metadata_dir(metadata_dir)
    writer.record_done(tmp_path / "x.jpg", tmp_path / "out" / "x.jpg", "sha-x", "collect")

    fresh = MoveLedger.for_metadata_dir(metadata_dir)   # separate instance, cold cache
    assert fresh.done_dst_index()[str(tmp_path / "x.jpg")] == str(tmp_path / "out" / "x.jpg")


def test_done_dst_index_reflects_appends_made_after_it_was_built(tmp_path, metadata_dir):
    ledger = MoveLedger.for_metadata_dir(metadata_dir)
    assert ledger.done_dst_index() == {}                # build cold, empty

    ledger.record_done(tmp_path / "late.jpg", tmp_path / "out" / "late.jpg", "sha-l", "collect")
    assert ledger.done_dst_index()[str(tmp_path / "late.jpg")] == str(tmp_path / "out" / "late.jpg")


def test_done_dst_index_rebuilds_when_another_writer_appends(tmp_path, metadata_dir):
    """Staleness guard: an append by a *different* instance is still seen."""
    reader = MoveLedger.for_metadata_dir(metadata_dir)
    assert reader.done_dst_index() == {}

    other = MoveLedger.for_metadata_dir(metadata_dir)
    other.record_done(tmp_path / "o.jpg", tmp_path / "out" / "o.jpg", "sha-o", "collect")

    # reader's cache is stale by size; it must notice and rebuild
    assert reader.done_dst_index()[str(tmp_path / "o.jpg")] == str(tmp_path / "out" / "o.jpg")


def test_done_dst_index_does_not_rescan_the_ledger_per_lookup(tmp_path, metadata_dir):
    """The load-bearing property: N lookups must not cost N full ledger reads.

    Without this, collect() over N sources against an L-entry ledger costs N×L
    JSON parses — ~460M on a 31k-file case, pinning one core for ~30 minutes.
    """
    ledger = MoveLedger.for_metadata_dir(metadata_dir)
    for i in range(50):
        ledger.record_done(tmp_path / f"f{i}.jpg", tmp_path / "out" / f"f{i}.jpg",
                           f"sha-{i}", "collect")

    # __iter__ opens the ledger; count how many times 200 lookups trigger it
    import builtins
    opens = 0
    real_builtin_open = builtins.open

    def counting_builtin_open(file, *a, **kw):
        nonlocal opens
        if str(file) == str(ledger.path):
            opens += 1
        return real_builtin_open(file, *a, **kw)

    builtins.open = counting_builtin_open
    try:
        for i in range(200):
            ledger.done_dst_index().get(str(tmp_path / f"f{i % 50}.jpg"))
    finally:
        builtins.open = real_builtin_open

    # index already warm from the record_done() calls → zero re-reads.
    # Allow 1 for an implementation that builds lazily on first lookup.
    assert opens <= 1, f"ledger re-read {opens} times across 200 lookups"


# ── cross-device moves must not rewrite mode or mtime ──────────────────────
# The same-device branch is an os.replace, which keeps the inode and therefore
# mode/mtime for free. The cross-device branch builds a NEW inode with
# open/write, so without an explicit replay the destination silently takes the
# umask (typically 0644) and the wall-clock time of the copy — and which branch
# runs depends only on where the case tree is mounted.

import stat as _stat  # noqa: E402


@pytest.fixture(autouse=False)
def _not_root():
    if os.geteuid() == 0:
        pytest.skip("mode assertions are meaningless as root")


def test_cross_device_move_preserves_mode(tmp_path, metadata_dir, custody, monkeypatch):
    """REGRESSION: a read-only file used to land at 0644 after a cross-device move."""
    if os.geteuid() == 0:
        pytest.skip("mode assertions are meaningless as root")
    _force_cross_device(monkeypatch)
    src = _make_src(tmp_path, content=b"read-only bytes")
    os.chmod(src, 0o444)
    ledger = MoveLedger.for_metadata_dir(metadata_dir)

    dst = move_tracked(src, tmp_path / "suspense" / "photo.jpg",
                       reason="suspense", ledger=ledger, custody=custody)

    assert _stat.S_IMODE(os.stat(dst).st_mode) == 0o444


def test_cross_device_move_preserves_mtime(tmp_path, metadata_dir, custody, monkeypatch):
    """mtime is fs_mtime in metadata_index, ocr's page order, and build_archive's
    copy-verification signal — replacing it with the copy time loses real evidence."""
    _force_cross_device(monkeypatch)
    src = _make_src(tmp_path, content=b"dated bytes")
    # a distinctive past timestamp with sub-second precision
    want_ns = 1_262_349_045_123_456_789
    os.utime(src, ns=(want_ns, want_ns))
    ledger = MoveLedger.for_metadata_dir(metadata_dir)

    dst = move_tracked(src, tmp_path / "dupes" / "photo.jpg",
                       reason="dupe", ledger=ledger, custody=custody)

    assert os.stat(dst).st_mtime_ns == want_ns


def test_cross_device_matches_same_device_metadata(tmp_path, metadata_dir, custody,
                                                   monkeypatch):
    """The two branches must be indistinguishable in what they leave behind."""
    if os.geteuid() == 0:
        pytest.skip("mode assertions are meaningless as root")
    want_ns = 1_262_349_045_123_456_789
    ledger = MoveLedger.for_metadata_dir(metadata_dir)

    same = _make_src(tmp_path, name="same.jpg", content=b"x")
    os.chmod(same, 0o640)
    os.utime(same, ns=(want_ns, want_ns))
    same_dst = move_tracked(same, tmp_path / "out" / "same.jpg", reason="r",
                            ledger=ledger, custody=custody)
    same_st = os.stat(same_dst)

    _force_cross_device(monkeypatch)
    cross = _make_src(tmp_path, name="cross.jpg", content=b"x")
    os.chmod(cross, 0o640)
    os.utime(cross, ns=(want_ns, want_ns))
    cross_dst = move_tracked(cross, tmp_path / "out" / "cross.jpg", reason="r",
                             ledger=ledger, custody=custody)
    cross_st = os.stat(cross_dst)

    assert _stat.S_IMODE(cross_st.st_mode) == _stat.S_IMODE(same_st.st_mode)
    assert cross_st.st_mtime_ns == same_st.st_mtime_ns


def test_cross_device_preserves_a_restrictive_readable_mode(tmp_path, metadata_dir,
                                                            custody, monkeypatch):
    """0400 (owner-read-only) must land as 0400, not widened to the umask."""
    if os.geteuid() == 0:
        pytest.skip("mode assertions are meaningless as root")
    _force_cross_device(monkeypatch)
    src = _make_src(tmp_path, content=b"locked bytes")
    ledger = MoveLedger.for_metadata_dir(metadata_dir)
    os.chmod(src, 0o400)

    dst = move_tracked(src, tmp_path / "suspense" / "photo.jpg",
                       reason="suspense", ledger=ledger, custody=custody)

    assert _stat.S_IMODE(os.stat(dst).st_mode) == 0o400
    assert not src.exists(), "source should have been unlinked after the replace"


def test_unreadable_source_is_refused_before_anything_moves(tmp_path, metadata_dir,
                                                            custody, monkeypatch):
    """Documents a real boundary: move_tracked hashes the SOURCE first.

    A mode-000 file therefore never reaches the copy — the move raises and the
    source is left intact with the ledger at `intent` for reconcile. That is the
    correct conservative behaviour, and it is why collect_dedup widens through
    perms.borrow_read BEFORE calling move_tracked rather than expecting the move
    to cope on its own.
    """
    if os.geteuid() == 0:
        pytest.skip("permission assertions are meaningless as root")
    _force_cross_device(monkeypatch)
    src = _make_src(tmp_path, content=b"unreadable bytes")
    ledger = MoveLedger.for_metadata_dir(metadata_dir)
    os.chmod(src, 0o000)
    try:
        with pytest.raises(PermissionError):
            move_tracked(src, tmp_path / "suspense" / "photo.jpg",
                         reason="suspense", ledger=ledger, custody=custody)
        assert src.exists(), "source must be untouched when the move is refused"
    finally:
        os.chmod(src, 0o644)


def test_metadata_preservation_never_fails_a_verified_move(tmp_path, metadata_dir,
                                                            custody, monkeypatch):
    """The bytes and the ledger are the guarantees; metadata is best-effort."""
    _force_cross_device(monkeypatch)

    def boom(*a, **k):
        raise OSError(1, "operation not permitted")
    monkeypatch.setattr(moves.os, "chmod", boom)
    monkeypatch.setattr(moves.os, "utime", boom)

    src = _make_src(tmp_path, content=b"still must land")
    ledger = MoveLedger.for_metadata_dir(metadata_dir)
    dst = move_tracked(src, tmp_path / "out" / "photo.jpg", reason="r",
                       ledger=ledger, custody=custody)
    assert dst.read_bytes() == b"still must land"
    assert not src.exists()


def test_preserve_metadata_tolerates_a_missing_stat(tmp_path):
    """_lstat_or_none returning None must be a no-op, not a crash."""
    target = tmp_path / "f"
    target.write_bytes(b"x")
    moves._preserve_metadata(None, target)      # must not raise
    assert target.read_bytes() == b"x"


def test_lstat_or_none_returns_none_for_missing_path(tmp_path):
    assert moves._lstat_or_none(tmp_path / "nope") is None
    real = tmp_path / "yes"
    real.write_bytes(b"x")
    assert moves._lstat_or_none(real) is not None
