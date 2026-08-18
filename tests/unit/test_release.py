"""Tests for wyeast.core.release — the family-release fingerprint/stamp/verify.

Covers every property the examiner-release-gate spec's T1 Verify block names:
the stamp trips on byte-moving and overlay verbs (incl. from-absent) but not on
the signoff's own append; a reverted byte-move leaves the stamp tripped yet
verify(live=True) still serves and does not re-walk; the fingerprint is stable
across a Person_NN renumber that preserves membership and changes on a real
re-cluster, a naming, or an edit to any serve-gating metadata index.
"""

import json
import os
import re
from pathlib import Path

import pytest

from wyeast.core.paths import CasePaths
from wyeast.core.moves import LEDGER_NAME
from wyeast.core import release


# ── a minimal on-disk case ───────────────────────────────────────────────────

def _write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def make_case(tmp_path: Path) -> CasePaths:
    """Build a tiny but structurally real delivered tree + serve-gating metadata."""
    paths = CasePaths.from_case_id("CASE_T", str(tmp_path))
    out = paths.output_dir
    arc = paths.archive_dir
    md = paths.metadata_dir
    (out).mkdir(parents=True, exist_ok=True)
    md.mkdir(parents=True, exist_ok=True)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)

    # archive/ — two physical originals.
    (arc / "_no_album").mkdir(parents=True, exist_ok=True)
    f1 = arc / "_no_album" / "a.jpg"
    f2 = arc / "_no_album" / "b.jpg"
    f1.write_bytes(b"AAAA")
    f2.write_bytes(b"BBBBBB")

    # by_person/Person_03 — symlink view into archive.
    person = out / "by_person" / "Person_03"
    person.mkdir(parents=True, exist_ok=True)
    os.symlink(os.path.relpath(f1, person), person / "a.jpg")
    os.symlink(os.path.relpath(f2, person), person / "b.jpg")

    # a delivered non-archive file.
    (out / "documents").mkdir(parents=True, exist_ok=True)
    (out / "documents" / "will.txt").write_text("last will")
    (out / "case_report.html").write_text("<html>report</html>")

    # serve-gating metadata (with wall-clock timestamps, as the real files carry).
    _write_json(md / release.ARCHIVE_MAP_FILE, {
        "timestamp": "2026-07-14T00:00:00",
        "entries": {str(f1): str(f1), str(f2): str(f2)}})
    _write_json(md / release.VIDEO_FRAME_MAP_FILE, {})
    _write_json(md / release.PERCEPTUAL_DUP_GROUPS_FILE, {
        "timestamp": "2026-07-14T00:00:00", "groups": []})
    _write_json(md / release.DUP_MEMBER_SCAN_FILE, {
        "generated_at": "2026-07-14T00:00:00", "members": {}})
    return paths


# ── fingerprint stability & sensitivity ──────────────────────────────────────

def test_fingerprint_stable_across_recompute(tmp_path):
    paths = make_case(tmp_path)
    assert release.fingerprint(paths) == release.fingerprint(paths)


def test_fingerprint_stable_across_person_renumber(tmp_path):
    """Person_03 -> Person_05 with identical membership and no name is a cosmetic
    relabel and MUST NOT change the fingerprint (no pointless wet re-sign)."""
    paths = make_case(tmp_path)
    before = release.fingerprint(paths)
    src = paths.output_dir / "by_person" / "Person_03"
    src.rename(paths.output_dir / "by_person" / "Person_05")
    assert release.fingerprint(paths) == before


def test_fingerprint_changes_on_naming_a_person(tmp_path):
    """Assigning a name (Person_03 -> Person_03_Jane_Harding) is a real change."""
    paths = make_case(tmp_path)
    before = release.fingerprint(paths)
    src = paths.output_dir / "by_person" / "Person_03"
    src.rename(paths.output_dir / "by_person" / "Person_03_Jane_Harding")
    assert release.fingerprint(paths) != before


def test_fingerprint_changes_on_recluster(tmp_path):
    """Different membership (a member removed from the cluster) re-signs."""
    paths = make_case(tmp_path)
    before = release.fingerprint(paths)
    (paths.output_dir / "by_person" / "Person_03" / "b.jpg").unlink()
    assert release.fingerprint(paths) != before


def test_fingerprint_changes_on_archive_map_edit(tmp_path):
    """Remapping a src to a different canonical in archive_map (the master
    allowlist) changes the fingerprint even though no delivered byte moved."""
    paths = make_case(tmp_path)
    before = release.fingerprint(paths)
    md = paths.metadata_dir
    d = json.loads((md / release.ARCHIVE_MAP_FILE).read_text())
    stray = str(paths.case_dir / "extracted" / "stray.jpg")
    d["entries"][stray] = stray
    _write_json(md / release.ARCHIVE_MAP_FILE, d)
    assert release.fingerprint(paths) != before


def test_fingerprint_inert_to_archive_map_timestamp_churn(tmp_path):
    """A content-neutral --restart rewrites archive_map's wall-clock timestamp
    with no change to entries — the SLICE hash must ignore it (K14)."""
    paths = make_case(tmp_path)
    before = release.fingerprint(paths)
    md = paths.metadata_dir
    d = json.loads((md / release.ARCHIVE_MAP_FILE).read_text())
    d["timestamp"] = "2099-01-01T00:00:00"      # only the timestamp churns
    _write_json(md / release.ARCHIVE_MAP_FILE, d)
    assert release.fingerprint(paths) == before


def test_fingerprint_changes_on_dup_verdict(tmp_path):
    """A dup member's nudity_flag flipping (what resolve_dup_member_path gates on)
    changes the fingerprint; its scan timestamp/score alone does not."""
    paths = make_case(tmp_path)
    md = paths.metadata_dir
    base = release.fingerprint(paths)
    _write_json(md / release.DUP_MEMBER_SCAN_FILE, {
        "generated_at": "2030-01-01T00:00:00",     # timestamp churn: inert
        "members": {"/x/y.jpg": {"nudity_flag": False,
                                 "nudity_score": 0.1, "scanned_at": "t"}}})
    with_member = release.fingerprint(paths)
    assert with_member != base           # a new gated member is a real change
    _write_json(md / release.DUP_MEMBER_SCAN_FILE, {
        "generated_at": "2031-01-01T00:00:00",
        "members": {"/x/y.jpg": {"nudity_flag": True,
                                 "nudity_score": 0.9, "scanned_at": "t2"}}})
    assert release.fingerprint(paths) != with_member    # verdict flip re-signs


def test_fingerprint_is_version_scoped(tmp_path):
    """The digest is scoped by FINGERPRINT_VERSION, so a line-format change
    (v1 -> v2, when CSAM matching was removed) makes an old signature fail
    verify as "format changed, re-sign" rather than reading as tree tampering.
    Pins both that the version participates and that it is at v2.
    """
    paths = make_case(tmp_path)
    assert release.FINGERPRINT_VERSION == 2
    base = release.fingerprint(paths)
    orig = release.FINGERPRINT_VERSION
    try:
        release.FINGERPRINT_VERSION = orig + 1
        assert release.fingerprint(paths) != base
    finally:
        release.FINGERPRINT_VERSION = orig
    assert release.fingerprint(paths) == base


def test_deep_mode_differs_from_standard(tmp_path):
    paths = make_case(tmp_path)
    assert release.fingerprint(paths, "deep") != release.fingerprint(paths, "standard")


def test_deep_catches_same_size_swap(tmp_path):
    """standard mode is blind to a same-size content edit of an archive file;
    --deep catches it (the conceded threat-model boundary, made explicit)."""
    paths = make_case(tmp_path)
    f1 = paths.archive_dir / "_no_album" / "a.jpg"
    std_before = release.fingerprint(paths, "standard")
    deep_before = release.fingerprint(paths, "deep")
    f1.write_bytes(b"ZZZZ")                    # same size (4 bytes), new content
    assert release.fingerprint(paths, "standard") == std_before   # blind
    assert release.fingerprint(paths, "deep") != deep_before       # caught


# ── the visibility stamp ─────────────────────────────────────────────────────

def test_stamp_trips_on_move_ledger_from_absent(tmp_path):
    """A fresh case has no ledger; the first byte-moving verb CREATES it and the
    stamp must register that (the absent-file sentinel)."""
    paths = make_case(tmp_path)
    before = release.visibility_stamp(paths)
    ledger = paths.metadata_dir / LEDGER_NAME
    assert not ledger.exists()
    ledger.write_text('{"src":"x","dst":"y","status":"done"}\n')
    assert release.visibility_stamp(paths) != before


def test_stamp_trips_on_overlay_sidecar(tmp_path):
    paths = make_case(tmp_path)
    before = release.visibility_stamp(paths)
    _write_json(paths.metadata_dir / release.DECISIONS_FILE, {"removed_persons": {}})
    assert release.visibility_stamp(paths) != before


def test_stamp_trips_on_facecluster_same_slug_relabel(tmp_path):
    paths = make_case(tmp_path)
    _write_json(paths.metadata_dir / release.FACE_CLUSTERING_FILE, {"v": 1})
    before = release.visibility_stamp(paths)
    _write_json(paths.metadata_dir / release.FACE_CLUSTERING_FILE, {"v": 2})
    assert release.visibility_stamp(paths) != before


def test_stamp_trips_on_serve_gating_metadata(tmp_path):
    """The stamp covers every input the fingerprint slices, so an out-of-band edit
    to a serve-gating map trips the live tripwire (it no longer only flips the
    fingerprint while the stamp fast-path keeps serving)."""
    paths = make_case(tmp_path)
    md = paths.metadata_dir
    for fname in (release.ARCHIVE_MAP_FILE, release.VIDEO_FRAME_MAP_FILE,
                  release.PERCEPTUAL_DUP_GROUPS_FILE, release.DUP_MEMBER_SCAN_FILE,
                  release.CASE_SUMMARY_FILE):
        before = release.visibility_stamp(paths)
        p = md / fname
        p.write_text((p.read_text() if p.exists() else "{}").rstrip() + " ")  # touch
        assert release.visibility_stamp(paths) != before, fname


def test_verify_live_accepts_escalation_lock(tmp_path):
    """verify(live=True) works with a real threading.Lock and still caches (the
    single-flight guard for the threaded server)."""
    import threading
    paths = make_case(tmp_path)
    rec = _sign(paths)
    (paths.metadata_dir / LEDGER_NAME).write_text('{"status":"done"}\n')  # trip stamp
    lock, cache = threading.Lock(), {}
    calls = {"n": 0}
    real = release.fingerprint

    def counting(p, mode=release.MODE_STANDARD):
        calls["n"] += 1
        return real(p, mode)

    release.fingerprint = counting
    try:
        r1 = release.verify(paths, rec, live=True, escalation_cache=cache,
                            escalation_lock=lock)
        r2 = release.verify(paths, rec, live=True, escalation_cache=cache,
                            escalation_lock=lock)
    finally:
        release.fingerprint = real
    assert r1.ok and r2.ok and calls["n"] == 1


def test_stamp_does_not_trip_on_action_log(tmp_path):
    """The signoff's own append_action writes family_actions.ndjson; it is
    deliberately NOT in the stamp, so a fresh sign does not self-stale."""
    paths = make_case(tmp_path)
    before = release.visibility_stamp(paths)
    (paths.metadata_dir / "family_actions.ndjson").write_text('{"verb":"signoff"}\n')
    assert release.visibility_stamp(paths) == before


# ── verify(): the two tiers ──────────────────────────────────────────────────

def _sign(paths, mode="standard"):
    """Produce a valid record + the matching custody anchor (as T3 will)."""
    fp = release.fingerprint(paths, mode)
    stamp = release.visibility_stamp(paths)
    from wyeast.core.custody import ChainOfCustody
    ChainOfCustody(paths.custody_log).record_event("release", f"{fp} actor=Jane")
    rec = {"case_id": paths.case_id, "delivery_fingerprint": fp,
           "fingerprint_mode": mode,
           "fingerprint_version": release.FINGERPRINT_VERSION,
           "visibility_stamp": stamp, "revoked": False}
    return rec


def test_verify_authoritative_ok(tmp_path):
    paths = make_case(tmp_path)
    rec = _sign(paths)
    assert release.verify(paths, rec, live=False).ok


def test_verify_authoritative_detects_tree_change(tmp_path):
    paths = make_case(tmp_path)
    rec = _sign(paths)
    (paths.archive_dir / "_no_album" / "c.jpg").write_bytes(b"CCCC")
    r = release.verify(paths, rec, live=False)
    assert not r.ok and "changed" in r.reason


def test_verify_authoritative_detects_record_tamper(tmp_path):
    """Editing delivery_fingerprint in the record without a matching custody
    event is caught by the custody cross-check."""
    paths = make_case(tmp_path)
    rec = _sign(paths)
    rec["delivery_fingerprint"] = "deadbeef" * 8
    r = release.verify(paths, rec, live=False)
    assert not r.ok and "custody" in r.reason


def test_verify_attributes_a_format_bump_to_re_sign_not_tampering(tmp_path):
    """The whole point of versioning the digest: a pre-bump signature must be
    refused with a reason that says RE-SIGN, never the tamper reason.

    Versioning the digest without persisting+checking the version would be worse
    than not versioning it at all — every pre-bump case would report "tree changed
    since signing", so a genuine tamper would be indistinguishable from routine
    upgrade noise, training the examiner to sign straight past a real alert."""
    paths = make_case(tmp_path)
    rec = _sign(paths)
    assert release.verify(paths, rec, live=False).ok

    rec_v1 = dict(rec, fingerprint_version=release.FINGERPRINT_VERSION - 1)
    r = release.verify(paths, rec_v1, live=False)
    assert not r.ok
    assert "re-sign" in r.reason.lower()
    assert "NOT evidence the tree was altered" in r.reason
    assert "changed since signing" not in r.reason, "must not read as tampering"

    # A record predating the field at all is treated as v1, same refusal.
    rec_legacy = {k: v for k, v in rec.items() if k != "fingerprint_version"}
    r2 = release.verify(paths, rec_legacy, live=False)
    assert not r2.ok and "re-sign" in r2.reason.lower()


def test_version_gate_is_common_to_both_verify_tiers(tmp_path):
    """The gate sits BEFORE the live/non-live split so both tiers refuse alike.

    visibility_stamp carries no version, so a stale-version record would sail
    through the live tripwire's fast path — a long-lived family server would keep
    serving a tree that export already refuses, and whether the family could still
    see the archive would depend only on whether anyone restarted the server."""
    paths = make_case(tmp_path)
    rec_v1 = dict(_sign(paths), fingerprint_version=release.FINGERPRINT_VERSION - 1)
    # The stamp itself still matches — proving the refusal comes from the version
    # gate, not from tree drift.
    assert release.visibility_stamp(paths) == rec_v1["visibility_stamp"]
    for live in (False, True):
        r = release.verify(paths, rec_v1, live=live)
        assert not r.ok, f"live={live} must refuse a stale format version"
        assert "re-sign" in r.reason.lower()


def test_certificate_reprint_flags_an_earlier_screening_regime():
    """A reprint reproduces what was wet-signed. Re-rendering a v1 record with
    today's screening sentence would re-issue a signed legal instrument with
    materially different attested text, under the same signature block."""
    base = {
        "case_id": "C", "signed_at": "t", "actor": {"name": "J", "capacity": "atty"},
        "attestation": "att", "judgment": "j",
        "delivery_fingerprint": "abc", "fingerprint_mode": "standard",
        "dispositions": {}, "withheld": {},
        "machine_screen": {"scan_filters_enabled": []},
        "revoked": False,
    }
    current = release.render_certificate(
        dict(base, fingerprint_version=release.FINGERPRINT_VERSION), {})
    assert "Reprint notice" not in current

    legacy = release.render_certificate(base, {})          # no version == v1
    assert "Reprint notice" in legacy
    assert "wet-signed printout remains" in legacy


def test_verify_rejects_revoked(tmp_path):
    paths = make_case(tmp_path)
    rec = _sign(paths)
    rec["revoked"] = True
    assert not release.verify(paths, rec, live=False).ok
    assert not release.verify(paths, rec, live=True).ok


def test_verify_rejects_wrong_case(tmp_path):
    paths = make_case(tmp_path)
    rec = _sign(paths)
    rec["case_id"] = "SOMEONE_ELSE"
    assert not release.verify(paths, rec, live=True).ok


def test_verify_live_fast_path(tmp_path):
    paths = make_case(tmp_path)
    rec = _sign(paths)
    r = release.verify(paths, rec, live=True)
    assert r.ok and not r.escalated       # stamp matched: no fingerprint walk


def test_verify_live_refuses_after_release_verb(tmp_path):
    """A post-sign byte-move that re-exposes content: stamp trips AND fingerprint
    differs → refuse (the rev-3 BLOCKING hole)."""
    paths = make_case(tmp_path)
    rec = _sign(paths)
    # simulate verb_release surfacing a new delivered file + ledger line
    (paths.archive_dir / "_no_album" / "released.jpg").write_bytes(b"NEW")
    (paths.metadata_dir / LEDGER_NAME).write_text('{"status":"done"}\n')
    r = release.verify(paths, rec, live=True)
    assert not r.ok and r.escalated


def test_verify_live_serves_reverted_move_and_caches(tmp_path):
    """A content-neutral byte-move (release then re-banish) leaves the append-only
    ledger changed → stamp tripped forever — but the fingerprint is unchanged, so
    verify serves. And the escalation verdict is cached against the stamp value:
    the second request must NOT re-walk the tree."""
    paths = make_case(tmp_path)
    rec = _sign(paths)
    # ledger grew (append-only) but no family-visible content changed
    (paths.metadata_dir / LEDGER_NAME).write_text(
        '{"status":"done","reason":"release"}\n'
        '{"status":"done","reason":"rebanish"}\n')

    calls = {"n": 0}
    real_fp = release.fingerprint

    def counting_fp(p, mode=release.MODE_STANDARD):
        calls["n"] += 1
        return real_fp(p, mode)

    cache = {}
    release.fingerprint = counting_fp
    try:
        r1 = release.verify(paths, rec, live=True, escalation_cache=cache)
        r2 = release.verify(paths, rec, live=True, escalation_cache=cache)
    finally:
        release.fingerprint = real_fp

    assert r1.ok and r1.escalated          # stamp tripped, fingerprint unchanged → serve
    assert r2.ok                           # still serves
    assert calls["n"] == 1                 # walked once, second request hit the cache


def test_load_release_absent_is_none(tmp_path):
    paths = make_case(tmp_path)
    assert release.load_release(paths) is None


def test_load_release_corrupt_raises(tmp_path):
    paths = make_case(tmp_path)
    release.release_path(paths).write_text("{not json")
    with pytest.raises(release.ReleaseError):
        release.load_release(paths)


def test_load_release_non_object_raises(tmp_path):
    """Valid JSON that is not an object (a list/scalar) is still a corrupt record —
    fail closed, never treated as absent."""
    paths = make_case(tmp_path)
    release.release_path(paths).write_text("[1, 2, 3]")
    with pytest.raises(release.ReleaseError):
        release.load_release(paths)


def test_fingerprint_covers_perceptual_dup_groups(tmp_path):
    """The dup-gating slice folds perceptual_dup_groups' keeper→member relations,
    not just dup_member_scan verdicts — a changed group re-signs."""
    paths = make_case(tmp_path)
    md = paths.metadata_dir
    _write_json(md / release.PERCEPTUAL_DUP_GROUPS_FILE, {
        "timestamp": "2026-07-14T00:00:00",
        "groups": [{"group_id": 1, "keeper": "extracted/photos/k.jpg",
                    "members": [{"file": "m1.jpg", "moved": True}]}]})
    before = release.fingerprint(paths)
    # add a member to the group → the serve-gating relation changes → re-sign
    _write_json(md / release.PERCEPTUAL_DUP_GROUPS_FILE, {
        "timestamp": "2099-01-01T00:00:00",       # timestamp churn stays inert
        "groups": [{"group_id": 1, "keeper": "extracted/photos/k.jpg",
                    "members": [{"file": "m1.jpg"}, {"file": "m2.jpg"}]}]})
    assert release.fingerprint(paths) != before


# ── pinning: the mirrored constants must track their source of truth ──────────

def test_delivered_top_pinned_to_export_delivery():
    import tools.export_delivery as ed
    assert set(release.DELIVERED_TOP) == set(ed.INCLUDE_TOP)


def test_view_dirs_pinned_to_family_archive():
    src = (Path(__file__).resolve().parents[2] / "tools" / "family_archive.py").read_text()
    m = re.search(r"VIEW_DIRS\s*=\s*\(([^)]*)\)", src)
    assert m
    names = tuple(re.findall(r'"([^"]+)"', m.group(1)))
    assert names == release.VIEW_DIRS
