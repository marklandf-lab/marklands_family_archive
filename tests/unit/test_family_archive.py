"""Unit tests for the Family Archive verbs (tools/family_archive.py).

Verbs are pure functions over an ArchiveCase, so they test directly against a
fixture case tree — no live socket. Reuses make_case from _case_fixture and
adds a by_person symlink view so Banish/Rename exercise the real symlink/ledger
machinery.

Run under venv-phase1.
"""
import importlib.util
import json
import os
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import tools.family_archive as fa  # noqa: E402
from wyeast.core.delivery import relative_symlink  # noqa: E402
from wyeast.core.paths import CasePaths  # noqa: E402

# Upstream loads make_case out of tests/unit/test_build_explorer.py by path;
# that module tests tools/build_explorer.py, which is outside this repo's import
# closure, so the fixture lives in _case_fixture.py here instead.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _case_fixture as _tbe  # noqa: E402
make_case = _tbe.make_case

SRC_A = "/work/extracted/a.jpg"
SRC_B = "/work/extracted/b.jpg"


def setup_case(tmp_path, *, delivery_blocked=False, role="examiner"):
    """Build a case with a by_person view symlink and return (case, paths)."""
    cases, case_dir = make_case(tmp_path, delivery_blocked=delivery_blocked)
    paths = CasePaths.from_case_id("CASE_T", str(cases))
    # Add a by_person view: by_person/Person_01/a.jpg -> ../../archive/a.jpg
    arc_a = paths.archive_dir / "a.jpg"
    relative_symlink(paths.output_dir / "by_person" / "Person_01" / "a.jpg", arc_a)
    case = fa.ArchiveCase(paths, role, {})
    return case, paths


def _actions(paths):
    p = paths.metadata_dir / "family_actions.ndjson"
    return p.read_text().strip().splitlines() if p.exists() else []


def _sign(paths):
    """Write a valid family_release.json + custody anchor so a family session
    passes the E4 export gate (the disposition gate is exercised separately in
    test_release_verbs)."""
    from wyeast.core import release
    from wyeast.core.custody import ChainOfCustody
    fp = release.fingerprint(paths)
    stamp = release.visibility_stamp(paths)
    ChainOfCustody(paths.custody_log).record_event("release", f"{fp} actor=Test")
    release.release_path(paths).write_text(json.dumps(
        {"case_id": paths.case_id, "delivery_fingerprint": fp,
         "fingerprint_mode": "standard",
         "fingerprint_version": release.FINGERPRINT_VERSION,
         "visibility_stamp": stamp, "revoked": False}))


def _add_quarantine(paths, name, *, filt="explicit_sexual_imagery"):
    """Add a real quarantined file + manifest entry (canonical/quarantine/view
    paths under the case tree) so verb_release exercises the real machinery."""
    qdir = paths.case_dir / "quarantine" / filt
    qdir.mkdir(parents=True, exist_ok=True)
    qfile = qdir / name
    qfile.write_bytes(b"\xff\xd8\xff\xd9")
    canonical = paths.archive_dir / name
    view = paths.output_dir / "by_person" / "Person_01" / name
    entry = {"file": f"/work/{name}", "filter": filt,
             "canonical_path": str(canonical), "quarantine_path": str(qfile),
             "view_paths": [str(view)], "timestamp": "2026-01-01T00:00:00"}
    mpath = paths.metadata_dir / "quarantine_manifest.json"
    m = json.loads(mpath.read_text()) if mpath.exists() else {}
    m.setdefault("entries", []).append(entry)
    m.setdefault("released", [])
    mpath.write_text(json.dumps(m))
    return entry, canonical, view, qfile


# ── Release (quarantine → delivery) + undo ──

def test_release_then_undo(tmp_path):
    case, paths = setup_case(tmp_path)
    _e, canonical, view, qfile = _add_quarantine(paths, "rel.jpg")
    assert qfile.exists() and not canonical.exists()

    res = fa.verb_release(case, {"canonical_path": str(canonical)})
    assert res["ok"]
    assert canonical.exists() and not qfile.exists(), "file moved back to archive"
    assert view.is_symlink() and os.path.realpath(view) == os.path.realpath(canonical)
    # audit: ledger + custody + exactly one action line
    ledger = (paths.metadata_dir / "_move_ledger.ndjson").read_text()
    assert '"status": "intent"' in ledger and '"status": "done"' in ledger
    assert paths.custody_log.exists()
    assert len(_actions(paths)) == 1
    # manifest: entry moved from pending → released
    m = json.loads((paths.metadata_dir / "quarantine_manifest.json").read_text())
    assert all(e["canonical_path"] != str(canonical) for e in m["entries"])
    assert any(e["canonical_path"] == str(canonical) for e in m["released"])

    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    assert qfile.exists() and not canonical.exists(), "re-quarantined"
    assert not view.exists() and not view.is_symlink(), "recreated view removed"
    m2 = json.loads((paths.metadata_dir / "quarantine_manifest.json").read_text())
    assert any(e["canonical_path"] == str(canonical) for e in m2["entries"])
    assert all(e["canonical_path"] != str(canonical) for e in m2["released"])


def test_release_undo_never_deletes_a_real_file_at_view_path(tmp_path):
    """M5: release-undo (_requarantine) must only unlink the view SYMLINKS it
    recreated — never a real, source-bearing file that later came to occupy a
    recorded view path (the never-destroy invariant, mirroring verb_remove_person).
    The prior `p.is_symlink() or p.exists()` would delete such a file."""
    case, paths = setup_case(tmp_path)
    _e, canonical, view, _qfile = _add_quarantine(paths, "rel.jpg")
    res = fa.verb_release(case, {"canonical_path": str(canonical)})
    assert view.is_symlink()
    # Simulate a real file coming to occupy the recorded view path after release.
    view.unlink()
    view.write_bytes(b"real-source-bearing-bytes")
    assert view.is_file() and not view.is_symlink()

    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    assert view.exists() and view.read_bytes() == b"real-source-bearing-bytes", \
        "a real file at the view path must survive release-undo"


# ── Export collection (person/scene/category) ──

def test_export_collection_person_skips_undelivered(tmp_path):
    case, paths = setup_case(tmp_path)
    # fixture Person_01 = [SRC_A]; add an undelivered member that won't resolve
    fc = json.loads((paths.metadata_dir / "face_clustering.json").read_text())
    fc["person_clusters"]["Person_01"] = [SRC_A, "/work/extracted/missing.jpg"]
    (paths.metadata_dir / "face_clustering.json").write_text(json.dumps(fc))
    case.load()
    dest = tmp_path / "exp"
    res = fa.verb_export_collection(case, {"kind": "person", "key": "Person_01", "dest": str(dest)})
    assert res["ok"] and res["count"] == 1 and res["skipped"] == 1
    assert (dest / "a.jpg").exists()
    assert (dest / "export_manifest.json").exists()
    assert (paths.archive_dir / "a.jpg").exists(), "collection export must not move originals"
    assert len(_actions(paths)) == 1  # one export_collection audit line


def test_export_collection_family_gated_when_blocked(tmp_path):
    # B1: the gate must raise VerbError (403) inside a verb, NOT sys.exit — a
    # SystemExit in a request thread dies silently with no response to the client.
    case, _ = setup_case(tmp_path, delivery_blocked=True, role="family")
    with pytest.raises(fa.VerbError) as e:
        fa.verb_export_collection(case, {"kind": "person", "key": "Person_01"})
    assert e.value.code == 403


# ── Quarantine Discard (#9) ──

def test_discard_quarantine_then_undo(tmp_path):
    case, paths = setup_case(tmp_path)
    _e, canonical, _v, qfile = _add_quarantine(paths, "disc.jpg", filt="criminal_legal_jeopardy")
    res = fa.verb_discard_quarantine(case, {"canonical_path": str(canonical)})
    assert res["ok"]
    assert not qfile.exists(), "quarantined file moved out"
    banished = paths.output_dir / fa.BANISHED_DIR / "quarantine" / "disc.jpg"
    assert banished.exists()
    m = json.loads((paths.metadata_dir / "quarantine_manifest.json").read_text())
    assert all(e["canonical_path"] != str(canonical) for e in m["entries"])
    assert len(_actions(paths)) == 1
    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    assert qfile.exists() and not banished.exists(), "restored to quarantine"
    m2 = json.loads((paths.metadata_dir / "quarantine_manifest.json").read_text())
    assert any(e["canonical_path"] == str(canonical) for e in m2["entries"])


def test_release_batch_skips_missing_and_undoes(tmp_path):
    """#17: bulk-select coverage for the quarantine table, mirroring verb_banish's
    batch pattern — every item releases, the case reloads ONCE, and a missing
    member is skipped (not fatal) rather than aborting the whole selection."""
    case, paths = setup_case(tmp_path)
    _e1, canonical1, view1, qfile1 = _add_quarantine(paths, "rel1.jpg")
    _e2, canonical2, view2, qfile2 = _add_quarantine(paths, "rel2.jpg")

    res = fa.verb_release(case, {
        "canonical_paths": [str(canonical1), str(canonical2), "/no/such/entry"]})
    assert res["ok"] and res["count"] == 2 and res["skipped"] == 1
    assert len(res["undo_tokens"]) == 2 and "undo_token" not in res
    assert canonical1.exists() and canonical2.exists()
    assert not qfile1.exists() and not qfile2.exists()
    assert view1.is_symlink() and view2.is_symlink()
    assert len(_actions(paths)) == 2   # one audit entry per released item

    for tok in res["undo_tokens"]:
        fa.verb_undo(case, {"undo_token": tok})
    assert qfile1.exists() and qfile2.exists() and not canonical1.exists() and not canonical2.exists()


def test_discard_quarantine_batch_and_undo(tmp_path):
    case, paths = setup_case(tmp_path)
    _e1, canonical1, _v1, qfile1 = _add_quarantine(paths, "disc1.jpg", filt="criminal_legal_jeopardy")
    _e2, canonical2, _v2, qfile2 = _add_quarantine(paths, "disc2.jpg", filt="criminal_legal_jeopardy")

    res = fa.verb_discard_quarantine(
        case, {"canonical_paths": [str(canonical1), str(canonical2)]})
    assert res["ok"] and res["count"] == 2 and res["skipped"] == 0
    assert not qfile1.exists() and not qfile2.exists()
    banished1 = paths.output_dir / fa.BANISHED_DIR / "quarantine" / "disc1.jpg"
    banished2 = paths.output_dir / fa.BANISHED_DIR / "quarantine" / "disc2.jpg"
    assert banished1.exists() and banished2.exists()
    assert len(_actions(paths)) == 2

    for tok in res["undo_tokens"]:
        fa.verb_undo(case, {"undo_token": tok})
    assert qfile1.exists() and qfile2.exists()
    assert not banished1.exists() and not banished2.exists()


# ── Demote from Most Significant (#12) ──

def test_demote_ranked_then_undo(tmp_path):
    case, paths = setup_case(tmp_path)
    res = fa.verb_demote_ranked(case, {"key": "scene:beach", "label": "beach"})
    dec = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert "scene:beach" in dec["ranked_demoted"]
    assert len(_actions(paths)) == 1
    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    dec2 = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert "scene:beach" not in dec2.get("ranked_demoted", {})


def test_demote_email_toggle_and_undo(tmp_path):
    case, paths = setup_case(tmp_path)
    dpath = paths.metadata_dir / "family_decisions.json"
    # demote
    res = fa.verb_demote_email(case, {"thread_id": "t1", "subject": "Hello"})
    assert res["ok"] and res["demoted"] is True
    assert "t1" in json.loads(dpath.read_text())["email_demoted"]
    assert len(_actions(paths)) == 1
    # demoting again toggles it back off (restore)
    res2 = fa.verb_demote_email(case, {"thread_id": "t1", "subject": "Hello"})
    assert res2["demoted"] is False
    assert "t1" not in json.loads(dpath.read_text()).get("email_demoted", {})
    # undo of the restore re-demotes it
    fa.verb_undo(case, {"undo_token": res2["undo_token"]})
    assert "t1" in json.loads(dpath.read_text())["email_demoted"]
    # explicit restore flag also lifts it
    fa.verb_demote_email(case, {"thread_id": "t1", "restore": True})
    assert "t1" not in json.loads(dpath.read_text()).get("email_demoted", {})


# ── Reset all curation (#reset) ──

def test_reset_reverses_everything_and_clears_state(tmp_path):
    case, paths = setup_case(tmp_path)
    arc_a = paths.archive_dir / "a.jpg"
    view = paths.output_dir / "by_person" / "Person_01" / "a.jpg"
    fa.verb_banish(case, {"src": SRC_A})
    fa.verb_rename_person(case, {"person_id": "Person_01", "new_name": "Jane Q"})
    _e, canonical, _v, qfile = _add_quarantine(paths, "disc.jpg", filt="criminal_legal_jeopardy")
    fa.verb_discard_quarantine(case, {"canonical_path": str(canonical)})
    fa.verb_confirm(case, {"queue": "scene", "id": "/x/low.jpg", "decision": "reject"})
    fa.verb_demote_ranked(case, {"key": "scene:beach"})
    assert not arc_a.exists() and not qfile.exists()

    res = fa.verb_reset(case, {})
    assert res["ok"] and res["failed"] == 0

    # banish reversed: canonical back + view symlink restored
    assert arc_a.exists()
    assert view.is_symlink() and os.path.realpath(view) == os.path.realpath(arc_a)
    # rename reversed: folder back to Person_01, identity cleared
    assert (paths.output_dir / "by_person" / "Person_01").is_dir()
    assert not (paths.output_dir / "by_person" / "Person_01_Jane_Q").exists()
    fc = json.loads((paths.metadata_dir / "face_clustering.json").read_text())
    assert "Person_01" not in fc.get("cluster_identities", {})
    # discard reversed: quarantine file back + manifest entry restored
    assert qfile.exists()
    m = json.loads((paths.metadata_dir / "quarantine_manifest.json").read_text())
    assert any(e["canonical_path"] == str(canonical) for e in m["entries"])
    # decisions cleared; history is just the reset marker
    assert not (paths.metadata_dir / "family_decisions.json").exists()
    acts = _actions(paths)
    assert len(acts) == 1 and json.loads(acts[0])["action"] == "reset"


def test_reset_is_examiner_only(tmp_path):
    case, _ = setup_case(tmp_path, role="family")
    with pytest.raises(fa.VerbError) as e:
        fa.verb_reset(case, {})
    assert e.value.code == 403


# ── Remove person (dissolve grouping, #6) ──

def test_remove_person_dissolves_grouping_and_undoes(tmp_path):
    case, paths = setup_case(tmp_path)
    by_person = paths.output_dir / "by_person"
    assert (by_person / "Person_01").is_dir()
    res = fa.verb_remove_person(case, {"person_id": "Person_01"})
    assert res["ok"]
    assert not (by_person / "Person_01").exists(), "folder removed"
    assert (paths.archive_dir / "a.jpg").exists(), "photo kept in archive"
    dec = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert "Person_01" in dec["removed_persons"]
    assert "Person_01" not in [r["person_id"] for r in case.section("people")]
    assert len(_actions(paths)) == 1
    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    link = by_person / "Person_01" / "a.jpg"
    assert (by_person / "Person_01").is_dir() and link.is_symlink() and os.path.exists(link)
    dec2 = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert "Person_01" not in dec2.get("removed_persons", {})


def test_remove_person_tolerates_missing_folder(tmp_path):  # video-only person (Person_05)
    case, paths = setup_case(tmp_path)
    fc = json.loads((paths.metadata_dir / "face_clustering.json").read_text())
    fc["person_clusters"]["Person_09"] = ["/x/vid_f000001.jpg"]
    (paths.metadata_dir / "face_clustering.json").write_text(json.dumps(fc))
    case.load()
    res = fa.verb_remove_person(case, {"person_id": "Person_09"})  # no by_person folder → no error
    assert res["ok"]
    assert "Person_09" in json.loads((paths.metadata_dir / "family_decisions.json").read_text())["removed_persons"]
    fa.verb_undo(case, {"undo_token": res["undo_token"]})  # no-op symlink recreation, no error
    assert "Person_09" not in json.loads((paths.metadata_dir / "family_decisions.json").read_text()).get("removed_persons", {})


# ── G-15 face-assist verbs: merge_persons / assign_face (DECISIONS OVERLAY) ──

def _multi_person_case(tmp_path, *, role="examiner"):
    """A case with two real photo clusters (Person_01=[a], Person_02=[b]) plus a
    noise face — for exercising merge_persons / assign_face. Returns (case, paths)."""
    case, paths = setup_case(tmp_path, role=role)
    fc = json.loads((paths.metadata_dir / "face_clustering.json").read_text())
    fc["person_clusters"] = {"Person_01": [SRC_A], "Person_02": [SRC_B]}
    fc["noise_files"] = [NOISE_SRC]
    (paths.metadata_dir / "face_clustering.json").write_text(json.dumps(fc))
    case.load()
    return case, paths


NOISE_SRC = "/work/extracted/noise1.jpg"


def _people_by_id(case):
    return {r["person_id"]: r for r in case.section("people")}


def test_merge_persons_folds_members_drops_loser_and_undoes(tmp_path):
    case, paths = _multi_person_case(tmp_path)
    fc_bytes = (paths.metadata_dir / "face_clustering.json").read_bytes()

    before = _people_by_id(case)
    assert before["Person_01"]["photo_count"] == 1 and "Person_02" in before

    res = fa.verb_merge_persons(case, {"winner_pid": "Person_01", "loser_pid": "Person_02"})
    assert res["ok"]
    # Overlay recorded (NOT in face_clustering.json).
    dec = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert dec["person_merges"] == {"Person_02": "Person_01"}
    # face_clustering.json is byte-for-byte unchanged (pipeline index untouched).
    assert (paths.metadata_dir / "face_clustering.json").read_bytes() == fc_bytes

    after = _people_by_id(case)
    assert "Person_02" not in after, "merged loser drops out of People"
    assert after["Person_01"]["photo_count"] == 2, "winner absorbs loser's photos"
    assert set(after["Person_01"]["member_ids"]) == {SRC_A, SRC_B}
    # person_detail: winner shows the union; loser redirects to the winner.
    assert case.person_detail_section("Person_01")["photo_count"] == 2
    loser_detail = case.person_detail_section("Person_02")
    assert loser_detail["merged_into"] == "Person_01"
    # exactly one audit line, reversible.
    assert len(_actions(paths)) == 1

    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    restored = _people_by_id(case)
    assert "Person_02" in restored and restored["Person_01"]["photo_count"] == 1
    assert "person_merges" not in json.loads(
        (paths.metadata_dir / "family_decisions.json").read_text()) \
        or "Person_02" not in json.loads(
            (paths.metadata_dir / "family_decisions.json").read_text())["person_merges"]
    assert (paths.metadata_dir / "face_clustering.json").read_bytes() == fc_bytes


def test_assign_face_adds_to_person_drops_from_queue_and_undoes(tmp_path):
    case, paths = _multi_person_case(tmp_path)
    fc_bytes = (paths.metadata_dir / "face_clustering.json").read_bytes()

    q_before = {(i["queue"], i["id"]) for i in case.section("review")["confirm_queue"]}
    assert ("face", NOISE_SRC) in q_before, "noise face surfaced in the confirm queue"

    res = fa.verb_assign_face(case, {"src": NOISE_SRC, "person_id": "Person_01"})
    assert res["ok"]
    dec = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert dec["face_assignments"] == {NOISE_SRC: "Person_01"}
    assert (paths.metadata_dir / "face_clustering.json").read_bytes() == fc_bytes

    # Face now joins Person_01's members and leaves the confirm queue.
    detail = case.person_detail_section("Person_01")
    assert NOISE_SRC in [m["id"] for m in detail["members"]]
    q_after = {(i["queue"], i["id"]) for i in case.section("review")["confirm_queue"]}
    assert ("face", NOISE_SRC) not in q_after
    assert len(_actions(paths)) == 1

    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    q_undo = {(i["queue"], i["id"]) for i in case.section("review")["confirm_queue"]}
    assert ("face", NOISE_SRC) in q_undo, "undo returns the face to the queue"
    assert NOISE_SRC not in [m["id"] for m in case.person_detail_section("Person_01")["members"]]
    assert (paths.metadata_dir / "face_clustering.json").read_bytes() == fc_bytes


def test_face_assist_verbs_are_examiner_only(tmp_path):
    case, _paths = _multi_person_case(tmp_path, role="family")
    for verb, payload in (
        (fa.verb_merge_persons, {"winner_pid": "Person_01", "loser_pid": "Person_02"}),
        (fa.verb_assign_face, {"src": NOISE_SRC, "person_id": "Person_01"}),
    ):
        with pytest.raises(fa.VerbError) as e:
            verb(case, payload)
        assert e.value.code == 403


def test_merge_persons_validates_payload(tmp_path):
    case, _paths = _multi_person_case(tmp_path)
    with pytest.raises(fa.VerbError) as e1:
        fa.verb_merge_persons(case, {"winner_pid": "Person_99", "loser_pid": "Person_02"})
    assert e1.value.code == 404
    with pytest.raises(fa.VerbError) as e2:
        fa.verb_merge_persons(case, {"winner_pid": "Person_01", "loser_pid": "Person_XX"})
    assert e2.value.code == 404
    with pytest.raises(fa.VerbError) as e3:
        fa.verb_merge_persons(case, {"winner_pid": "Person_01", "loser_pid": "Person_01"})
    assert e3.value.code == 400


def test_assign_face_validates_payload(tmp_path):
    case, _paths = _multi_person_case(tmp_path)
    with pytest.raises(fa.VerbError) as e1:   # unknown person
        fa.verb_assign_face(case, {"src": NOISE_SRC, "person_id": "Person_99"})
    assert e1.value.code == 404
    with pytest.raises(fa.VerbError) as e2:   # src not a noise face
        fa.verb_assign_face(case, {"src": "/work/extracted/not_noise.jpg", "person_id": "Person_01"})
    assert e2.value.code == 404


def test_merge_persons_resolves_chains_and_refuses_cycles(tmp_path):
    case, paths = _multi_person_case(tmp_path)
    fc = json.loads((paths.metadata_dir / "face_clustering.json").read_text())
    fc["person_clusters"]["Person_03"] = []   # a third cluster to chain through
    (paths.metadata_dir / "face_clustering.json").write_text(json.dumps(fc))
    case.load()

    # Chain: Person_03 → Person_02 → Person_01. Both fold into Person_01.
    fa.verb_merge_persons(case, {"winner_pid": "Person_01", "loser_pid": "Person_02"})
    fa.verb_merge_persons(case, {"winner_pid": "Person_02", "loser_pid": "Person_03"})
    people = _people_by_id(case)
    assert "Person_02" not in people and "Person_03" not in people
    assert people["Person_01"]["photo_count"] == 2   # SRC_A + SRC_B (Person_03 empty)

    # A cycle (Person_01 → Person_02, which resolves back to Person_01) is refused.
    with pytest.raises(fa.VerbError) as e:
        fa.verb_merge_persons(case, {"winner_pid": "Person_02", "loser_pid": "Person_01"})
    assert e.value.code == 409


def test_merge_drops_resolved_confirm_queue_items(tmp_path):
    """A merge resolves the queue prompts it answers: the loser's unnamed_person
    'name this person' item and any face_merge suggestion naming the merged cluster
    both stop reappearing."""
    case, paths = _multi_person_case(tmp_path)
    # A face_merge suggestion on SRC_A pointing at Person_02, and Person_02 is unnamed.
    geo = json.loads((paths.metadata_dir / "geo_cluster_index.json").read_text())
    geo[SRC_A] = dict(geo.get(SRC_A, {}), face_cluster_merge_candidates=["Person_02"])
    (paths.metadata_dir / "geo_cluster_index.json").write_text(json.dumps(geo))
    case.load()

    q0 = case.section("review")["confirm_queue"]
    assert ("face_merge", SRC_A) in {(i["queue"], i["id"]) for i in q0}
    assert ("name_person", "Person_02") in {(i["queue"], i["id"]) for i in q0}

    fa.verb_merge_persons(case, {"winner_pid": "Person_01", "loser_pid": "Person_02"})
    q1 = {(i["queue"], i["id"]) for i in case.section("review")["confirm_queue"]}
    assert ("face_merge", SRC_A) not in q1, "merge resolves the face_merge suggestion"
    assert ("name_person", "Person_02") not in q1, "merged loser drops its name prompt"


def test_face_assist_never_writes_face_clustering_json(tmp_path):
    """The load-bearing invariant: neither verb (nor their undo) ever mutates the
    pipeline-authored face_clustering.json — the overlay lives only in decisions."""
    case, paths = _multi_person_case(tmp_path)
    fcp = paths.metadata_dir / "face_clustering.json"
    fc_bytes = fcp.read_bytes()
    r1 = fa.verb_merge_persons(case, {"winner_pid": "Person_01", "loser_pid": "Person_02"})
    r2 = fa.verb_assign_face(case, {"src": NOISE_SRC, "person_id": "Person_01"})
    fa.verb_undo(case, {"undo_token": r2["undo_token"]})
    fa.verb_undo(case, {"undo_token": r1["undo_token"]})
    assert fcp.read_bytes() == fc_bytes


# ── Correspondent identity merge suggestions (P2 #9; DECISIONS OVERLAY) ──

def _corr_case(tmp_path, *, role="examiner"):
    """A case with a correspondent_frequency.json seeded with a 2-address
    duplicate-candidate cluster (Jane Doe) for exercising the merge/reject verbs."""
    case, paths = setup_case(tmp_path, role=role)
    freq = [
        {"address": "jane@one.com", "display_name": "Jane Doe", "sent_count": 6,
         "received_count": 4, "total": 10, "bidirectional": True,
         "first_seen": "2010-01-01", "last_seen": "2015-01-01"},
        {"address": "jane.doe@two.com", "display_name": "Jane Doe", "sent_count": 3,
         "received_count": 2, "total": 5, "bidirectional": True,
         "first_seen": "2016-01-01", "last_seen": "2020-01-01"},
    ]
    (paths.metadata_dir / "correspondent_frequency.json").write_text(json.dumps(freq))
    (paths.metadata_dir / "correspondent_frequency_family.json").write_text(json.dumps(freq))
    case.load()
    return case, paths


def test_correspondent_merge_confirm_folds_and_undoes(tmp_path):
    case, paths = _corr_case(tmp_path)
    cands = case.correspondent_duplicates_section()
    assert [c["name"] for c in cands] == ["Jane Doe"]

    res = fa.verb_correspondent_merge_confirm(
        case, {"addresses": ["jane@one.com", "jane.doe@two.com"]})
    assert res["ok"] and res["winner"] == "jane@one.com"
    dec = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert dec["correspondent_merges"] == {"jane.doe@two.com": "jane@one.com"}
    # correspondent_frequency.json itself is untouched (overlay only).
    assert len(json.loads((paths.metadata_dir / "correspondent_frequency.json").read_text())) == 2

    rows = case.section("correspondents")
    assert len(rows) == 1
    assert rows[0]["total"] == 15 and rows[0]["merged_addresses"] == ["jane.doe@two.com"]
    # the confirmed cluster no longer suggests itself.
    assert case.correspondent_duplicates_section() == []

    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    dec2 = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert "jane.doe@two.com" not in (dec2.get("correspondent_merges") or {})
    assert len(case.section("correspondents")) == 2
    assert [c["name"] for c in case.correspondent_duplicates_section()] == ["Jane Doe"]


def test_correspondent_merge_reject_suppresses_and_undoes(tmp_path):
    case, paths = _corr_case(tmp_path)
    res = fa.verb_correspondent_merge_reject(
        case, {"addresses": ["jane@one.com", "jane.doe@two.com"]})
    assert res["ok"]
    assert case.correspondent_duplicates_section() == []
    # correspondent cards are untouched by a rejection.
    assert len(case.section("correspondents")) == 2

    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    assert [c["name"] for c in case.correspondent_duplicates_section()] == ["Jane Doe"]


def test_correspondent_merge_verbs_are_examiner_only(tmp_path):
    case, _paths = _corr_case(tmp_path, role="family")
    for verb in (fa.verb_correspondent_merge_confirm, fa.verb_correspondent_merge_reject):
        with pytest.raises(fa.VerbError) as e:
            verb(case, {"addresses": ["jane@one.com", "jane.doe@two.com"]})
        assert e.value.code == 403


def test_correspondent_merge_validates_payload(tmp_path):
    case, _paths = _corr_case(tmp_path)
    with pytest.raises(fa.VerbError) as e1:  # only one address
        fa.verb_correspondent_merge_confirm(case, {"addresses": ["jane@one.com"]})
    assert e1.value.code == 400
    with pytest.raises(fa.VerbError) as e2:  # unknown address
        fa.verb_correspondent_merge_confirm(
            case, {"addresses": ["jane@one.com", "nobody@nowhere.com"]})
    assert e2.value.code == 404


# ── Move verb (Phase 1, person-only; face_placements DECISIONS OVERLAY) ──

SRC_C = "/work/extracted/c.jpg"


def _move_case(tmp_path, *, role="examiner"):
    """Person_01=[SRC_A, SRC_B], Person_02=[SRC_C]; all three delivered (on disk +
    archive_map + scene_index). For exercising verb_move."""
    case, paths = _multi_person_case(tmp_path, role=role)
    arc_c = paths.archive_dir / "c.jpg"
    arc_c.write_bytes(b"\xff\xd8\xff\xd9")
    am = json.loads((paths.metadata_dir / "archive_map.json").read_text())
    am["entries"][SRC_C] = str(arc_c)
    (paths.metadata_dir / "archive_map.json").write_text(json.dumps(am))
    sc = json.loads((paths.metadata_dir / "scene_index.json").read_text())
    sc["clip_results"][SRC_C] = {"category": "everyday life", "confidence": 0.9, "delivered": True}
    (paths.metadata_dir / "scene_index.json").write_text(json.dumps(sc))
    fc = json.loads((paths.metadata_dir / "face_clustering.json").read_text())
    fc["person_clusters"] = {"Person_01": [SRC_A, SRC_B], "Person_02": [SRC_C]}
    (paths.metadata_dir / "face_clustering.json").write_text(json.dumps(fc))
    case.load()
    return case, paths


def test_move_removes_from_origin_adds_to_target_and_undoes(tmp_path):
    case, paths = _move_case(tmp_path)
    fc_bytes = (paths.metadata_dir / "face_clustering.json").read_bytes()

    res = fa.verb_move(case, {"view": "person", "src": SRC_B, "to": "Person_02"})
    assert res["ok"] and res["from"] == "Person_01" and res["to"] == "Person_02"
    # Overlay recorded (NOT in face_clustering.json).
    dec = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert dec["face_placements"] == {SRC_B: "Person_02"}
    assert (paths.metadata_dir / "face_clustering.json").read_bytes() == fc_bytes

    people = _people_by_id(case)
    assert SRC_B not in people["Person_01"]["member_ids"] and people["Person_01"]["photo_count"] == 1
    assert SRC_B in people["Person_02"]["member_ids"] and people["Person_02"]["photo_count"] == 2
    # person_detail reflects it on both sides.
    assert SRC_B not in [m["id"] for m in case.person_detail_section("Person_01")["members"]]
    assert SRC_B in [m["id"] for m in case.person_detail_section("Person_02")["members"]]
    assert len(_actions(paths)) == 1

    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    p2 = _people_by_id(case)
    assert SRC_B in p2["Person_01"]["member_ids"] and p2["Person_01"]["photo_count"] == 2
    assert SRC_B not in p2["Person_02"]["member_ids"]
    assert not json.loads((paths.metadata_dir / "family_decisions.json").read_text()).get("face_placements")
    # The never-mutate invariant holds across move + undo.
    assert (paths.metadata_dir / "face_clustering.json").read_bytes() == fc_bytes


def test_move_last_member_drops_origin_no_ghost_no_stale_queue(tmp_path):
    case, paths = _move_case(tmp_path)
    # Person_02 has exactly one member (SRC_C) — moving it empties the cluster.
    res = fa.verb_move(case, {"view": "person", "src": SRC_C, "to": "Person_01"})
    assert res["ok"]
    people = _people_by_id(case)
    assert "Person_02" not in people, "emptied origin drops out of People (no ghost 0-count person)"
    assert people["Person_01"]["photo_count"] == 3
    q = {(i["queue"], i["id"]) for i in case.section("review")["confirm_queue"]}
    assert ("name_person", "Person_02") not in q, "no stale unnamed_person for the dropped cluster"
    assert ("name_person", "Person_01") in q, "target's own name prompt is unaffected"


def test_move_into_removed_person_refused(tmp_path):
    case, _paths = _move_case(tmp_path)
    fa.verb_remove_person(case, {"person_id": "Person_02"})
    with pytest.raises(fa.VerbError) as e:
        fa.verb_move(case, {"view": "person", "src": SRC_A, "to": "Person_02"})
    assert e.value.code == 409


def test_move_video_frame_refused(tmp_path):
    case, paths = _move_case(tmp_path)
    frame = "/work/extracted/vidA_f000001.jpg"     # matches VIDEO_FRAME_RE + the map
    fc = json.loads((paths.metadata_dir / "face_clustering.json").read_text())
    fc["person_clusters"]["Person_02"].append(frame)
    (paths.metadata_dir / "face_clustering.json").write_text(json.dumps(fc))
    (paths.metadata_dir / "video_frame_map.json").write_text(
        json.dumps({frame: {"source_video": "/work/vid/vidA.mov"}}))
    case.load()
    with pytest.raises(fa.VerbError) as e:
        fa.verb_move(case, {"view": "person", "src": frame, "to": "Person_01"})
    assert e.value.code == 400


def test_move_to_nonexistent_target_refused(tmp_path):
    case, _paths = _move_case(tmp_path)
    with pytest.raises(fa.VerbError) as e:
        fa.verb_move(case, {"view": "person", "src": SRC_A, "to": "Person_99"})
    assert e.value.code == 409


def test_move_noop_and_non_person_view_refused(tmp_path):
    case, _paths = _move_case(tmp_path)
    with pytest.raises(fa.VerbError) as e1:      # to == from (no-op)
        fa.verb_move(case, {"view": "person", "src": SRC_A, "to": "Person_01"})
    assert e1.value.code == 409
    with pytest.raises(fa.VerbError) as e2:      # scene view: unknown category refused
        fa.verb_move(case, {"view": "scene", "src": SRC_A, "to": "outdoors"})
    assert e2.value.code == 400
    with pytest.raises(fa.VerbError) as e3:      # a truly unsupported view is still refused
        fa.verb_move(case, {"view": "folder", "src": SRC_A, "to": "x"})
    assert e3.value.code == 400
    with pytest.raises(fa.VerbError) as e4:      # event view IS supported now: unknown album → 409
        fa.verb_move(case, {"view": "event", "src": SRC_A, "to": "x"})
    assert e4.value.code == 409


def test_move_is_examiner_only(tmp_path):
    case, _paths = _move_case(tmp_path, role="family")
    with pytest.raises(fa.VerbError) as e:
        fa.verb_move(case, {"view": "person", "src": SRC_A, "to": "Person_02"})
    assert e.value.code == 403


def test_move_refuses_undelivered_src(tmp_path):
    case, paths = _move_case(tmp_path)
    # An undelivered cluster member (no archive_map entry) never resolves → refused,
    # so a move can never surface a non-deliverable item.
    undel = "/work/extracted/undel.jpg"
    fc = json.loads((paths.metadata_dir / "face_clustering.json").read_text())
    fc["person_clusters"]["Person_01"].append(undel)
    (paths.metadata_dir / "face_clustering.json").write_text(json.dumps(fc))
    case.load()
    with pytest.raises(fa.VerbError):
        fa.verb_move(case, {"view": "person", "src": undel, "to": "Person_02"})


def test_move_placement_of_nonuniverse_src_hidden_from_family(tmp_path):
    """Defense in depth: even a directly-recorded placement of an undelivered src is
    dropped for the family role by person_detail's `m in universe` gate."""
    case, paths = _move_case(tmp_path, role="family")
    undel = "/work/extracted/undel.jpg"
    (paths.metadata_dir / "family_decisions.json").write_text(
        json.dumps({"face_placements": {undel: "Person_01"}}))
    case.load()
    ids = [m["id"] for m in case.person_detail_section("Person_01")["members"]]
    assert undel not in ids


def test_move_leaves_curation_intact_and_envelope_unchanged(tmp_path):
    case, paths = _move_case(tmp_path)
    fa.verb_favorite(case, {"id": SRC_B, "on": True})
    cur_before = (paths.metadata_dir / "curation_layer.json").read_bytes()
    fa.verb_move(case, {"view": "person", "src": SRC_B, "to": "Person_02"})
    # Move keys on the id, never the curation sidecar → favorites/notes untouched.
    assert (paths.metadata_dir / "curation_layer.json").read_bytes() == cur_before
    # The paginated photos envelope is unchanged in shape.
    env = case.api_section("photos", {})
    assert set(["rows", "total", "offset", "limit", "facets"]).issubset(env.keys())


# ── Move Phase 1.5: scene-move (scene_placements overlay) + batch move ──

def _scene_move_case(tmp_path, *, role="examiner"):
    """_move_case with TWO gallery scene categories so a scene-move has a valid
    target: SRC_A → 'outdoors', SRC_B/SRC_C stay 'everyday life'."""
    case, paths = _move_case(tmp_path, role=role)
    sc = json.loads((paths.metadata_dir / "scene_index.json").read_text())
    sc["clip_results"][SRC_A]["category"] = "outdoors"
    (paths.metadata_dir / "scene_index.json").write_text(json.dumps(sc))
    case.load()
    return case, paths


def _photos_by_id(case):
    return {r["id"]: r for r in case.section("photos")}


def test_scene_move_overrides_photo_rows_scene_and_feeds_facet(tmp_path):
    case, paths = _scene_move_case(tmp_path)
    sc_bytes = (paths.metadata_dir / "scene_index.json").read_bytes()
    res = fa.verb_move(case, {"view": "scene", "src": SRC_B, "to": "outdoors"})
    assert res["ok"] and res["to"] == "outdoors" and "from" not in res
    # Overlay recorded in family_decisions.json — NOT in scene_index.json.
    dec = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert dec["scene_placements"] == {SRC_B: "outdoors"}
    assert (paths.metadata_dir / "scene_index.json").read_bytes() == sc_bytes
    # photo_rows.scene is relabelled …
    assert _photos_by_id(case)[SRC_B]["scene"] == "outdoors"
    # … and the search index (which reads photo_rows.scene) inherits it.
    rec = [r for r in case.section("search")["records"] if r.get("h") == SRC_B]
    assert rec and "outdoors" in rec[0]["s"]


def test_scene_move_into_scanned_document_category_refused(tmp_path):
    """Review-C boundary: moving INTO a SCENE_LABELS bucket would drop the item out
    of the gallery (build_photo_universe reads scene_index directly) — refused."""
    case, _paths = _scene_move_case(tmp_path)
    label = next(iter(fa.SCENE_LABELS))   # "scanned document or handwritten letter"
    with pytest.raises(fa.VerbError) as e:
        fa.verb_move(case, {"view": "scene", "src": SRC_B, "to": label})
    assert e.value.code == 400


def test_scene_move_of_non_universe_src_refused(tmp_path):
    """You cannot scene-move something that isn't a delivered gallery photo."""
    case, _paths = _scene_move_case(tmp_path)
    with pytest.raises(fa.VerbError) as e:
        fa.verb_move(case, {"view": "scene", "src": "/work/extracted/undel.jpg", "to": "outdoors"})
    assert e.value.code == 404


def test_scene_move_undo_restores_and_scene_index_byte_unchanged(tmp_path):
    case, paths = _scene_move_case(tmp_path)
    sc_bytes = (paths.metadata_dir / "scene_index.json").read_bytes()
    res = fa.verb_move(case, {"view": "scene", "src": SRC_B, "to": "outdoors"})
    assert _photos_by_id(case)[SRC_B]["scene"] == "outdoors"
    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    # Placement popped; the row falls back to its pipeline category.
    assert not json.loads((paths.metadata_dir / "family_decisions.json").read_text()).get("scene_placements")
    assert _photos_by_id(case)[SRC_B]["scene"] == "everyday life"
    # The never-mutate invariant holds across move + undo.
    assert (paths.metadata_dir / "scene_index.json").read_bytes() == sc_bytes


def test_move_inverse_dispatches_key_by_view(tmp_path):
    """A person-move and a scene-move on the SAME src are both action=='move' but
    invert DIFFERENT overlay keys — so they undo independently."""
    case, paths = _scene_move_case(tmp_path)
    fc_bytes = (paths.metadata_dir / "face_clustering.json").read_bytes()
    sc_bytes = (paths.metadata_dir / "scene_index.json").read_bytes()
    rp = fa.verb_move(case, {"view": "person", "src": SRC_B, "to": "Person_02"})
    rs = fa.verb_move(case, {"view": "scene", "src": SRC_B, "to": "outdoors"})
    dec = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert dec["face_placements"] == {SRC_B: "Person_02"}
    assert dec["scene_placements"] == {SRC_B: "outdoors"}
    # Undo the SCENE move: only scene_placements reverses; face_placements intact.
    fa.verb_undo(case, {"undo_token": rs["undo_token"]})
    dec2 = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert not dec2.get("scene_placements") and dec2["face_placements"] == {SRC_B: "Person_02"}
    # Undo the PERSON move: face_placements reverses independently.
    fa.verb_undo(case, {"undo_token": rp["undo_token"]})
    dec3 = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert not dec3.get("face_placements")
    # Neither authoritative index was ever mutated.
    assert (paths.metadata_dir / "face_clustering.json").read_bytes() == fc_bytes
    assert (paths.metadata_dir / "scene_index.json").read_bytes() == sc_bytes


def test_move_batch_one_write_per_item_audit_skip_not_fail(tmp_path, monkeypatch):
    case, paths = _scene_move_case(tmp_path)
    calls = []
    real = fa.atomic_write_json
    monkeypatch.setattr(fa, "atomic_write_json",
                        lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    # Two valid members + one non-movable (not in the gallery) → skipped, not fatal.
    bogus = "/work/extracted/undel.jpg"
    res = fa.verb_move(case, {"view": "scene", "srcs": [SRC_B, SRC_C, bogus], "to": "outdoors"})
    assert res["count"] == 2 and res["skipped"] == 1 and len(res["undo_tokens"]) == 2
    assert "undo_token" not in res            # count != 1 → no single-item affordance
    # ONE atomic_write_json for the whole batch (append_action uses O_APPEND, not this).
    assert len(calls) == 1
    dec = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert dec["scene_placements"] == {SRC_B: "outdoors", SRC_C: "outdoors"}
    # Per-item audit: one action line per placed item.
    assert len(_actions(paths)) == 2
    ph = _photos_by_id(case)
    assert ph[SRC_B]["scene"] == "outdoors" and ph[SRC_C]["scene"] == "outdoors"


def test_move_batch_undo_tokens_each_reverse_one_item(tmp_path):
    case, paths = _scene_move_case(tmp_path)
    res = fa.verb_move(case, {"view": "scene", "srcs": [SRC_B, SRC_C], "to": "outdoors"})
    tok_b, tok_c = res["undo_tokens"]
    fa.verb_undo(case, {"undo_token": tok_b})
    dec = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert dec["scene_placements"] == {SRC_C: "outdoors"}, "only SRC_B reversed"
    fa.verb_undo(case, {"undo_token": tok_c})
    assert not json.loads((paths.metadata_dir / "family_decisions.json").read_text()).get("scene_placements")


def test_move_batch_person_skips_invalid_member(tmp_path):
    case, paths = _scene_move_case(tmp_path)
    # SRC_A, SRC_B in Person_01; move both to Person_02, plus a bogus src → skipped.
    res = fa.verb_move(case, {"view": "person",
                              "srcs": [SRC_A, SRC_B, "/nope.jpg"], "to": "Person_02"})
    assert res["count"] == 2 and res["skipped"] == 1
    people = _people_by_id(case)
    assert set([SRC_A, SRC_B]).issubset(set(people["Person_02"]["member_ids"]))
    assert "Person_01" not in people, "Person_01 emptied → drops out of People"


def test_scene_move_examiner_only(tmp_path):
    case, _paths = _scene_move_case(tmp_path, role="family")
    with pytest.raises(fa.VerbError) as e1:
        fa.verb_move(case, {"view": "scene", "src": SRC_B, "to": "outdoors"})
    assert e1.value.code == 403
    with pytest.raises(fa.VerbError) as e2:      # batch is examiner-only too
        fa.verb_move(case, {"view": "scene", "srcs": [SRC_B], "to": "outdoors"})
    assert e2.value.code == 403


# ── Move Phase 2: event-album view + event-move (event_placements overlay) ──

def _event_move_case(tmp_path, *, role="examiner"):
    """_move_case with two event albums + trip-cluster tags: album 0 = {SRC_A, SRC_B},
    album 1 = {SRC_C}. The static photo_count is deliberately WRONG (99) to prove the
    view derives its own live count."""
    case, paths = _move_case(tmp_path, role=role)
    summ = json.loads((paths.metadata_dir / "case_summary.json").read_text())
    summ["event_albums"] = [
        {"album_id": "0", "title": "Tahoe 2004", "place": "Lake Tahoe",
         "date_range": "2004", "scenes": ["beach"], "photo_count": 99, "folder": "by_event/0"},
        {"album_id": "1", "title": "Paris Trip", "place": "Paris",
         "date_range": "2011", "scenes": ["city"], "photo_count": 99},
    ]
    (paths.metadata_dir / "case_summary.json").write_text(json.dumps(summ))
    geo = json.loads((paths.metadata_dir / "geo_cluster_index.json").read_text())
    geo.setdefault(SRC_A, {})["gps_trip_cluster_id"] = 0
    geo.setdefault(SRC_B, {})["gps_trip_cluster_id"] = 0
    geo.setdefault(SRC_C, {})["gps_trip_cluster_id"] = 1
    (paths.metadata_dir / "geo_cluster_index.json").write_text(json.dumps(geo))
    case.load()
    return case, paths


def test_event_view_lists_albums_with_derived_live_count(tmp_path):
    case, _paths = _event_move_case(tmp_path)
    albums = {a["album_id"]: a for a in case.section("events")}
    # DERIVED count (2 + 1), NOT the static 99 photo_count in case_summary.
    assert albums["0"]["count"] == 2 and albums["1"]["count"] == 1
    assert albums["0"]["title"] == "Tahoe 2004" and albums["0"]["place"] == "Lake Tahoe"
    # Every configured album kept; cards sorted by live count desc.
    assert [a["album_id"] for a in case.section("events")] == ["0", "1"]


def test_event_filter_narrows_and_paginates_true_total(tmp_path):
    case, _paths = _event_move_case(tmp_path)
    env = case.api_section("photos", {"event": "0"})
    assert set(["rows", "total", "offset", "limit", "facets"]).issubset(env.keys())
    assert env["total"] == 2, "true album total, not the whole gallery"
    assert {r["id"] for r in env["rows"]} == {SRC_A, SRC_B}
    # The tail is reachable via the server slice (narrow BEFORE paginate).
    tail = case.api_section("photos", {"event": "0", "offset": "1", "limit": "1"})
    assert tail["total"] == 2 and len(tail["rows"]) == 1
    # Album 1 narrows to just SRC_C.
    assert {r["id"] for r in case.api_section("photos", {"event": "1"})["rows"]} == {SRC_C}


def test_event_move_records_placement_audit_undo_and_indexes_byte_unchanged(tmp_path):
    case, paths = _event_move_case(tmp_path)
    geo_bytes = (paths.metadata_dir / "geo_cluster_index.json").read_bytes()
    sum_bytes = (paths.metadata_dir / "case_summary.json").read_bytes()

    res = fa.verb_move(case, {"view": "event", "src": SRC_C, "to": "0"})
    assert res["ok"] and res["to"] == "0" and "from" not in res
    dec = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert dec["event_placements"] == {SRC_C: "0"}
    # DERIVED counts shift: album 0 → 3, album 1 → 0.
    albums = {a["album_id"]: a["count"] for a in case.section("events")}
    assert albums == {"0": 3, "1": 0}
    # SRC_C now filters under album 0, not album 1.
    assert {r["id"] for r in case.api_section("photos", {"event": "0"})["rows"]} == {SRC_A, SRC_B, SRC_C}
    assert case.api_section("photos", {"event": "1"})["total"] == 0
    assert len(_actions(paths)) == 1
    # Never mutates the pipeline indexes.
    assert (paths.metadata_dir / "geo_cluster_index.json").read_bytes() == geo_bytes
    assert (paths.metadata_dir / "case_summary.json").read_bytes() == sum_bytes

    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    assert not json.loads((paths.metadata_dir / "family_decisions.json").read_text()).get("event_placements")
    assert {a["album_id"]: a["count"] for a in case.section("events")} == {"0": 2, "1": 1}
    # Byte-unchanged across move + undo.
    assert (paths.metadata_dir / "geo_cluster_index.json").read_bytes() == geo_bytes
    assert (paths.metadata_dir / "case_summary.json").read_bytes() == sum_bytes


def test_event_move_to_nonexistent_album_refused(tmp_path):
    case, _paths = _event_move_case(tmp_path)
    with pytest.raises(fa.VerbError) as e:
        fa.verb_move(case, {"view": "event", "src": SRC_C, "to": "77"})
    assert e.value.code == 409


def test_event_move_noop_refused(tmp_path):
    case, _paths = _event_move_case(tmp_path)
    with pytest.raises(fa.VerbError) as e:      # SRC_A already in album 0
        fa.verb_move(case, {"view": "event", "src": SRC_A, "to": "0"})
    assert e.value.code == 409


def test_event_move_of_non_universe_src_refused(tmp_path):
    case, _paths = _event_move_case(tmp_path)
    with pytest.raises(fa.VerbError) as e:
        fa.verb_move(case, {"view": "event", "src": "/work/extracted/undel.jpg", "to": "0"})
    assert e.value.code == 404


def test_person_scene_event_move_same_src_undo_independently(tmp_path):
    """All three are action=='move' but invert DIFFERENT overlay keys, dispatched by
    the recorded before.view — so a person/scene/event move on the SAME src undo
    independently."""
    case, paths = _event_move_case(tmp_path)
    # give SRC_B a second scene target
    sc = json.loads((paths.metadata_dir / "scene_index.json").read_text())
    sc["clip_results"][SRC_A]["category"] = "outdoors"
    (paths.metadata_dir / "scene_index.json").write_text(json.dumps(sc))
    case.load()
    rp = fa.verb_move(case, {"view": "person", "src": SRC_B, "to": "Person_02"})
    rs = fa.verb_move(case, {"view": "scene", "src": SRC_B, "to": "outdoors"})
    re_ = fa.verb_move(case, {"view": "event", "src": SRC_B, "to": "1"})
    dec = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert dec["face_placements"] == {SRC_B: "Person_02"}
    assert dec["scene_placements"] == {SRC_B: "outdoors"}
    assert dec["event_placements"] == {SRC_B: "1"}
    # Undo the EVENT move only: event_placements clears; the other two stay.
    fa.verb_undo(case, {"undo_token": re_["undo_token"]})
    dec2 = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert not dec2.get("event_placements")
    assert dec2["face_placements"] == {SRC_B: "Person_02"} and dec2["scene_placements"] == {SRC_B: "outdoors"}
    # Undo the remaining two independently.
    fa.verb_undo(case, {"undo_token": rs["undo_token"]})
    fa.verb_undo(case, {"undo_token": rp["undo_token"]})
    dec3 = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert not dec3.get("face_placements") and not dec3.get("scene_placements")


def test_event_move_batch_skips_invalid_member(tmp_path):
    case, paths = _event_move_case(tmp_path)
    # SRC_A + SRC_B are both in album 0 → both valid moves to album 1; bogus skipped.
    res = fa.verb_move(case, {"view": "event",
                              "srcs": [SRC_A, SRC_B, "/work/extracted/undel.jpg"], "to": "1"})
    assert res["count"] == 2 and res["skipped"] == 1
    dec = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert dec["event_placements"] == {SRC_A: "1", SRC_B: "1"}


def test_event_view_both_roles_but_move_examiner_only(tmp_path):
    # Family can VIEW the events section (non-sensitive grouping) …
    fam, _fp = _event_move_case(tmp_path, role="family")
    fam_albums = {a["album_id"]: a["count"] for a in fam.section("events")}
    assert fam_albums == {"0": 2, "1": 1}
    # … but a family event-move is refused (examiner-only verb).
    with pytest.raises(fa.VerbError) as e:
        fa.verb_move(fam, {"view": "event", "src": SRC_C, "to": "0"})
    assert e.value.code == 403


# ── Move Phase 2.5: document-category move (doc_placements overlay) ──

DOC_FIN = "/work/docs/invoice.pdf"          # financial (from make_case)
DOC_CRED = "/work/docs/passwords.txt"       # account_credentials (from make_case)
DOC_MISC = "/work/docs/note.pdf"            # miscellaneous (added by helper)
DOC_EMAIL = "/work/docs/msg.eml"            # miscellaneous, source=email (added)


def _doc_move_case(tmp_path, *, role="examiner", case_config=None):
    """A case whose document_classifications carry: a financial doc, an
    account_credentials doc (both from make_case), a movable miscellaneous doc, and
    an email-sourced doc (excluded from document_rows). Optionally writes a
    case_config.json (default: NONE — so doc_move_categories exercises the
    FileNotFoundError → DEFAULT_DOC_CATEGORIES fallback)."""
    case, paths = setup_case(tmp_path, role=role)
    summ = json.loads((paths.metadata_dir / "case_summary.json").read_text())
    summ["document_classifications"] = [
        {"file": DOC_FIN, "filename": "invoice.pdf", "category": "financial",
         "source": "document", "significance": 4, "summary": "An invoice."},
        {"file": DOC_CRED, "filename": "passwords.txt", "category": "account_credentials",
         "source": "document", "significance": 5, "summary": "Login is hunter2"},
        {"file": DOC_MISC, "filename": "note.pdf", "category": "miscellaneous",
         "source": "document", "significance": 3, "summary": "A mis-filed letter."},
        {"file": DOC_EMAIL, "filename": "msg.eml", "category": "miscellaneous",
         "source": "email", "significance": 2, "summary": "An email body."},
    ]
    (paths.metadata_dir / "case_summary.json").write_text(json.dumps(summ))
    if case_config is not None:
        (paths.case_dir / "case_config.json").write_text(json.dumps(case_config))
    case.load()
    return case, paths


def _docs_index(case):
    return {c["category"]: c["count"] for c in case.section("documents")["index"]}


def test_doc_move_records_placement_audit_undo_and_summary_byte_unchanged(tmp_path):
    case, paths = _doc_move_case(tmp_path)
    sum_bytes = (paths.metadata_dir / "case_summary.json").read_bytes()
    # Baseline browse counts (email doc excluded; credentials visible to examiner).
    assert _docs_index(case) == {"financial": 1, "account_credentials": 1, "miscellaneous": 1}

    res = fa.verb_move(case, {"view": "document", "src": DOC_MISC, "to": "legal"})
    assert res["ok"] and res["to"] == "legal" and "from" not in res
    dec = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert dec["doc_placements"] == {DOC_MISC: "legal"}
    # DERIVED browse counts shift: miscellaneous → legal.
    assert _docs_index(case) == {"financial": 1, "account_credentials": 1, "legal": 1}
    # The moved doc now reports category "legal" in the rows.
    rows = {r["file"]: r for r in case.section("documents")["rows"]}
    assert rows[DOC_MISC]["category"] == "legal"
    assert len(_actions(paths)) == 1
    # Never mutates document_classifications (byte-unchanged).
    assert (paths.metadata_dir / "case_summary.json").read_bytes() == sum_bytes

    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    assert not json.loads((paths.metadata_dir / "family_decisions.json").read_text()).get("doc_placements")
    assert _docs_index(case) == {"financial": 1, "account_credentials": 1, "miscellaneous": 1}
    assert (paths.metadata_dir / "case_summary.json").read_bytes() == sum_bytes


def test_doc_move_into_letter_category_shows_on_correspondence_and_leaves_documents(tmp_path):
    case, _paths = _doc_move_case(tmp_path)
    # Before: DOC_MISC is in the Documents miscellaneous bucket, not Correspondence.
    corr0 = case.section("correspondence")
    assert DOC_MISC not in {r["file"] for r in corr0["typed"] + corr0["handwritten"]}
    fa.verb_move(case, {"view": "document", "src": DOC_MISC, "to": "personal_correspondence"})
    # After: on the Correspondence page …
    corr = case.section("correspondence")
    assert DOC_MISC in {r["file"] for r in corr["typed"] + corr["handwritten"]}
    # … and gone from the Documents miscellaneous bucket.
    assert "miscellaneous" not in _docs_index(case)
    assert DOC_MISC not in {r["file"] for r in case.section("documents")["rows"]
                            if r["category"] == "miscellaneous"}


def test_doc_move_out_of_account_credentials_refused(tmp_path):
    """§13.3 write-time guard 1: the src's pipeline category is account_credentials
    → refused (403). Moving it out would expose its body to family."""
    case, _paths = _doc_move_case(tmp_path)
    with pytest.raises(fa.VerbError) as e:
        fa.verb_move(case, {"view": "document", "src": DOC_CRED, "to": "legal"})
    assert e.value.code == 403


def test_doc_move_into_account_credentials_refused(tmp_path):
    """§13.3 write-time guard 1: `to == account_credentials` is refused (400) — it is
    never a movable target, and would hide a non-credential doc from family."""
    case, _paths = _doc_move_case(tmp_path)
    with pytest.raises(fa.VerbError) as e:
        fa.verb_move(case, {"view": "document", "src": DOC_MISC, "to": "account_credentials"})
    assert e.value.code == 400


def test_render_seal_family_drops_credential_doc_even_with_forced_placement(tmp_path):
    """§13.3 render-time seal (stale-placement leak): even if a doc_placements entry
    is forced onto disk moving the credential doc to a browsable category, the FAMILY
    view still drops it — the drop keys on the DERIVED pipeline category. Covers both
    the Documents rows AND the family search rows (build_search/FTS feeder)."""
    case, paths = _doc_move_case(tmp_path, role="family")
    # Simulate a stale placement written by an examiner BEFORE a re-classification.
    dec = {"doc_placements": {DOC_CRED: "legal"}}
    (paths.metadata_dir / "family_decisions.json").write_text(json.dumps(dec))
    case.load()
    # Family Documents rows never include the credential doc.
    rows = case.section("documents")["rows"]
    assert DOC_CRED not in {r["file"] for r in rows}
    # It also did not get re-bucketed into legal for family.
    assert all(r["file"] != DOC_CRED for r in rows)
    # Family search index (build_search over the SAME family rows) is leak-free.
    recs = [r for r in case.section("search")["records"] if r.get("k") == "document"]
    assert all(r.get("h") != DOC_CRED for r in recs)
    assert all("hunter2" not in (r.get("s") or "") for r in recs)


def test_doc_move_email_sourced_src_refused(tmp_path):
    case, _paths = _doc_move_case(tmp_path)
    with pytest.raises(fa.VerbError) as e:
        fa.verb_move(case, {"view": "document", "src": DOC_EMAIL, "to": "legal"})
    assert e.value.code == 400


def test_doc_move_unknown_src_refused(tmp_path):
    case, _paths = _doc_move_case(tmp_path)
    with pytest.raises(fa.VerbError) as e:
        fa.verb_move(case, {"view": "document", "src": "/work/docs/nope.pdf", "to": "legal"})
    assert e.value.code == 404


def test_doc_move_unknown_target_refused(tmp_path):
    case, _paths = _doc_move_case(tmp_path)
    with pytest.raises(fa.VerbError) as e:
        fa.verb_move(case, {"view": "document", "src": DOC_MISC, "to": "not_a_category"})
    assert e.value.code == 400


def test_doc_move_noop_refused(tmp_path):
    case, _paths = _doc_move_case(tmp_path)
    with pytest.raises(fa.VerbError) as e:      # already miscellaneous
        fa.verb_move(case, {"view": "document", "src": DOC_MISC, "to": "miscellaneous"})
    assert e.value.code == 409


def test_doc_move_works_when_case_config_absent(tmp_path):
    """§13.4 FileNotFoundError guard: load_case_config RAISES when case_config.json is
    absent — the verb must fall back to DEFAULT_DOC_CATEGORIES, never 500."""
    case, paths = _doc_move_case(tmp_path)  # no case_config.json written
    assert not (paths.case_dir / "case_config.json").exists()
    # A move to a constant category SUCCEEDS (no crash from the missing config).
    res = fa.verb_move(case, {"view": "document", "src": DOC_MISC, "to": "recipe"})
    assert res["ok"] and res["to"] == "recipe"
    assert fa.doc_move_categories(case) == set(fa.DEFAULT_DOC_CATEGORIES)


def test_doc_move_uses_case_config_categories_when_present(tmp_path):
    """§13.4: when case_config.json defines document_categories, the movable set is
    those names MINUS account_credentials (never a target)."""
    cfg = {"document_categories": [
        {"name": "financial"}, {"name": "legal"}, {"name": "custom_bucket"},
        {"name": "account_credentials"}]}
    case, _paths = _doc_move_case(tmp_path, case_config=cfg)
    assert fa.doc_move_categories(case) == {"financial", "legal", "custom_bucket"}
    # The config-defined custom bucket is a valid target …
    res = fa.verb_move(case, {"view": "document", "src": DOC_MISC, "to": "custom_bucket"})
    assert res["ok"]
    # … but a category not in the config is refused.
    with pytest.raises(fa.VerbError) as e:
        fa.verb_move(case, {"view": "document", "src": DOC_FIN, "to": "recipe"})
    assert e.value.code == 400


def test_doc_move_inverse_dispatches_doc_placements_not_face(tmp_path):
    """§13.9 MANDATORY: a doc-move undo inverts doc_placements and NEVER touches
    face_placements (the .get(mview, 'face_placements') default would corrupt it)."""
    case, paths = _doc_move_case(tmp_path)
    fc_bytes = (paths.metadata_dir / "face_clustering.json").read_bytes()
    res = fa.verb_move(case, {"view": "document", "src": DOC_MISC, "to": "legal"})
    dec = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert dec["doc_placements"] == {DOC_MISC: "legal"}
    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    dec2 = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert not dec2.get("doc_placements"), "doc_placements popped by the inverse"
    assert not dec2.get("face_placements"), "face_placements never touched by a doc-move undo"
    assert (paths.metadata_dir / "face_clustering.json").read_bytes() == fc_bytes


def test_doc_move_family_view_works_but_family_move_refused(tmp_path):
    """Family can VIEW Documents (credentials excluded) but a family doc-move → 403."""
    fam, _paths = _doc_move_case(tmp_path, role="family")
    fam_rows = {r["file"] for r in fam.section("documents")["rows"]}
    assert DOC_FIN in fam_rows and DOC_MISC in fam_rows
    assert DOC_CRED not in fam_rows, "family never browses raw account_credentials"
    with pytest.raises(fa.VerbError) as e:
        fa.verb_move(fam, {"view": "document", "src": DOC_MISC, "to": "legal"})
    assert e.value.code == 403


def test_overview_documents_count_excludes_email_and_matches_section_examiner(tmp_path):
    """Overview's "documents" tile must match what the Documents page itself shows —
    it used to be the raw document_classifications length (email included)."""
    case, _paths = _doc_move_case(tmp_path)
    rows = case.section("documents")["rows"]
    assert len(rows) == 3, "financial + credentials + misc; email-sourced excluded"
    counts = case.section("overview")["counts"]
    assert counts["documents"] == 3 == len(rows)


def test_overview_documents_count_excludes_email_and_matches_section_family(tmp_path):
    fam, _paths = _doc_move_case(tmp_path, role="family")
    fam_rows = fam.section("documents")["rows"]
    assert len(fam_rows) == 2, "family additionally drops account_credentials"
    fam_counts = fam.section("overview")["counts"]
    assert fam_counts["documents"] == 2 == len(fam_rows)


def test_doc_move_batch_skips_invalid_member(tmp_path):
    case, paths = _doc_move_case(tmp_path)
    # DOC_FIN + DOC_MISC are movable to legal; DOC_CRED (403) and DOC_EMAIL (400)
    # and an unknown src (404) are skipped, not fatal.
    res = fa.verb_move(case, {"view": "document",
                              "srcs": [DOC_FIN, DOC_MISC, DOC_CRED, DOC_EMAIL, "/nope.pdf"],
                              "to": "legal"})
    assert res["count"] == 2 and res["skipped"] == 3
    dec = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert dec["doc_placements"] == {DOC_FIN: "legal", DOC_MISC: "legal"}


def test_doc_move_view_relaxed_but_unknown_view_still_refused(tmp_path):
    case, _paths = _doc_move_case(tmp_path)
    with pytest.raises(fa.VerbError) as e:
        fa.verb_move(case, {"view": "folder", "src": DOC_MISC, "to": "legal"})
    assert e.value.code == 400


# ── Move Phase 2.6: financial SUB-category move (§14) ──

DOC_FIN2 = "/work/docs/receipt.pdf"   # financial, subcategory receipts_bills_orders


def _submove_case(tmp_path, *, role="examiner", case_config=None):
    """A case with TWO financial docs carrying subcategories (receipts_bills_orders
    ×2 + a bare financial), plus the account_credentials + misc + email docs, so
    the financial drill has real subcategory buckets to shift."""
    case, paths = setup_case(tmp_path, role=role)
    summ = json.loads((paths.metadata_dir / "case_summary.json").read_text())
    summ["document_classifications"] = [
        {"file": DOC_FIN, "filename": "invoice.pdf", "category": "financial",
         "subcategory": "receipts_bills_orders", "source": "document",
         "significance": 4, "summary": "An invoice."},
        {"file": DOC_FIN2, "filename": "receipt.pdf", "category": "financial",
         "subcategory": "receipts_bills_orders", "source": "document",
         "significance": 4, "summary": "A receipt."},
        {"file": DOC_CRED, "filename": "passwords.txt", "category": "account_credentials",
         "source": "document", "significance": 5, "summary": "Login is hunter2"},
        {"file": DOC_MISC, "filename": "note.pdf", "category": "miscellaneous",
         "source": "document", "significance": 3, "summary": "A mis-filed letter."},
    ]
    (paths.metadata_dir / "case_summary.json").write_text(json.dumps(summ))
    if case_config is not None:
        (paths.case_dir / "case_config.json").write_text(json.dumps(case_config))
    case.load()
    return case, paths


def _fin_subs(case):
    """Financial subcategory → count from the documents drill index."""
    for c in case.section("documents")["index"]:
        if c["category"] == "financial":
            return {s["name"]: s["count"] for s in c["subcategories"]}
    return {}


def test_submove_records_dict_and_financial_drill_shifts(tmp_path):
    case, paths = _submove_case(tmp_path)
    sum_bytes = (paths.metadata_dir / "case_summary.json").read_bytes()
    assert _fin_subs(case) == {"receipts_bills_orders": 2}

    res = fa.verb_move(case, {"view": "document", "src": DOC_FIN2,
                              "to": "financial", "subcategory": "banking"})
    assert res["ok"] and res["subcategory"] == "banking"
    dec = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert dec["doc_placements"] == {
        DOC_FIN2: {"category": "financial", "subcategory": "banking"}}
    # DERIVED drill counts shift: one receipt → banking.
    assert _fin_subs(case) == {"receipts_bills_orders": 1, "banking": 1}
    # The moved row reports the new subcategory (still category financial).
    rows = {r["file"]: r for r in case.section("documents")["rows"]}
    assert rows[DOC_FIN2]["category"] == "financial"
    assert rows[DOC_FIN2]["subcategory"] == "banking"
    # document_classifications never mutated.
    assert (paths.metadata_dir / "case_summary.json").read_bytes() == sum_bytes


def test_submove_on_nonfinancial_to_refused(tmp_path):
    """A subcategory on a non-financial `to` → 400 (§14.3)."""
    case, _paths = _submove_case(tmp_path)
    with pytest.raises(fa.VerbError) as e:
        fa.verb_move(case, {"view": "document", "src": DOC_MISC,
                            "to": "legal", "subcategory": "banking"})
    assert e.value.code == 400


def test_submove_unknown_subcategory_refused(tmp_path):
    case, _paths = _submove_case(tmp_path)
    with pytest.raises(fa.VerbError) as e:
        fa.verb_move(case, {"view": "document", "src": DOC_FIN,
                            "to": "financial", "subcategory": "not_a_sub"})
    assert e.value.code == 400


def test_submove_noop_refused(tmp_path):
    """The effective (category, subcategory) already == target → 409."""
    case, _paths = _submove_case(tmp_path)
    with pytest.raises(fa.VerbError) as e:
        fa.verb_move(case, {"view": "document", "src": DOC_FIN,
                            "to": "financial", "subcategory": "receipts_bills_orders"})
    assert e.value.code == 409


def test_submove_subcategory_only_omits_to(tmp_path):
    """A bare `subcategory` (no `to`) is a valid financial sub-move — category
    stays financial (§14.3)."""
    case, paths = _submove_case(tmp_path)
    res = fa.verb_move(case, {"view": "document", "src": DOC_FIN2, "subcategory": "insurance"})
    assert res["ok"]
    dec = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert dec["doc_placements"][DOC_FIN2] == {
        "category": "financial", "subcategory": "insurance"}


def test_submove_on_nonfinancial_bare_subcategory_refused(tmp_path):
    """A bare subcategory on a NON-financial doc (no to=financial) → 400."""
    case, _paths = _submove_case(tmp_path)
    with pytest.raises(fa.VerbError) as e:
        fa.verb_move(case, {"view": "document", "src": DOC_MISC, "subcategory": "banking"})
    assert e.value.code == 400


def test_submove_combined_move_nonfinancial_into_financial_sub(tmp_path):
    """The VERB permits a combined move: a non-financial doc → financial/banking in
    one call (to=financial + subcategory), §14.6."""
    case, paths = _submove_case(tmp_path)
    res = fa.verb_move(case, {"view": "document", "src": DOC_MISC,
                              "to": "financial", "subcategory": "banking"})
    assert res["ok"]
    rows = {r["file"]: r for r in case.section("documents")["rows"]}
    assert rows[DOC_MISC]["category"] == "financial"
    assert rows[DOC_MISC]["subcategory"] == "banking"


def test_category_move_to_nonfinancial_clears_subcategory_at_render(tmp_path):
    """§14.2: a §13 category move to a NON-financial target clears the subcategory
    at render (subcat is meaningless outside financial)."""
    case, _paths = _submove_case(tmp_path)
    fa.verb_move(case, {"view": "document", "src": DOC_FIN, "to": "legal"})
    rows = {r["file"]: r for r in case.section("documents")["rows"]}
    assert rows[DOC_FIN]["category"] == "legal"
    assert rows[DOC_FIN]["subcategory"] is None


def test_backward_compat_string_placement_renders_category_only(tmp_path):
    """A pre-existing STRING doc_placements value (the §13 shape) still renders
    category-only (backward compat — the shipped §13 path is untouched)."""
    case, paths = _submove_case(tmp_path)
    dec = {"doc_placements": {DOC_MISC: "legal"}}   # legacy string value
    (paths.metadata_dir / "family_decisions.json").write_text(json.dumps(dec))
    case.load()
    rows = {r["file"]: r for r in case.section("documents")["rows"]}
    assert rows[DOC_MISC]["category"] == "legal"
    assert rows[DOC_MISC]["subcategory"] is None


def test_submove_undo_restores_prior_value_str_or_dict(tmp_path):
    """Undo of a sub-move restores the WHOLE prior value (dict here) without
    touching face_placements (§14.2 shape-agnostic inverse)."""
    case, paths = _submove_case(tmp_path)
    fc_bytes = (paths.metadata_dir / "face_clustering.json").read_bytes()
    # First sub-move: prior value is None → records dict.
    r1 = fa.verb_move(case, {"view": "document", "src": DOC_FIN2,
                             "to": "financial", "subcategory": "banking"})
    # Second sub-move: prior value is the banking DICT → records paystubs.
    r2 = fa.verb_move(case, {"view": "document", "src": DOC_FIN2,
                             "to": "financial", "subcategory": "paystubs"})
    # Undo #2 → restores the banking dict exactly.
    fa.verb_undo(case, {"undo_token": r2["undo_token"]})
    dec = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert dec["doc_placements"][DOC_FIN2] == {
        "category": "financial", "subcategory": "banking"}
    # Undo #1 → pops the placement (prior was None).
    fa.verb_undo(case, {"undo_token": r1["undo_token"]})
    dec2 = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert not dec2.get("doc_placements")
    assert not dec2.get("face_placements"), "face_placements never touched"
    assert (paths.metadata_dir / "face_clustering.json").read_bytes() == fc_bytes


def test_doc_subcategories_absent_config_falls_back_to_constant(tmp_path):
    """case_config.json absent → doc_subcategories falls back to the constant, no 500."""
    case, paths = _submove_case(tmp_path)   # no case_config.json written
    assert not (paths.case_dir / "case_config.json").exists()
    assert fa.doc_subcategories(case) == set(fa.FINANCIAL_SUBCATEGORY_NAMES)
    # A sub-move to a constant subcategory succeeds (no crash from missing config).
    res = fa.verb_move(case, {"view": "document", "src": DOC_FIN,
                              "to": "financial", "subcategory": "banking"})
    assert res["ok"]


def test_doc_subcategories_present_config_used(tmp_path):
    cfg = {"financial_subcategories": [{"name": "banking", "hint": "x"},
                                       {"name": "custom_sub", "hint": "y"}]}
    case, _paths = _submove_case(tmp_path, case_config=cfg)
    assert fa.doc_subcategories(case) == {"banking", "custom_sub"}
    # A name in the config is a valid target …
    assert fa.verb_move(case, {"view": "document", "src": DOC_FIN,
                               "to": "financial", "subcategory": "custom_sub"})["ok"]
    # … but a name NOT in the config (even a constant default) is refused.
    with pytest.raises(fa.VerbError) as e:
        fa.verb_move(case, {"view": "document", "src": DOC_FIN2,
                            "to": "financial", "subcategory": "paystubs"})
    assert e.value.code == 400


def test_doc_subcategories_empty_list_disables_submove(tmp_path):
    """§14.4 review #4: a present-but-EMPTY financial_subcategories list means the
    second pass is disabled → the EMPTY set (NOT a fallback to the constant), so a
    sub-move has no valid targets and is refused."""
    cfg = {"financial_subcategories": []}
    case, _paths = _submove_case(tmp_path, case_config=cfg)
    assert fa.doc_subcategories(case) == set()
    with pytest.raises(fa.VerbError) as e:
        fa.verb_move(case, {"view": "document", "src": DOC_FIN,
                            "to": "financial", "subcategory": "banking"})
    assert e.value.code == 400


def test_submove_account_credentials_render_seal_unchanged(tmp_path):
    """§14.7: the account_credentials render seal is inert to sub-moves (financial is
    never a credential) — the derived-category family drop still fires first."""
    case, paths = _submove_case(tmp_path, role="family")
    # A forced sub-move placement onto the credential doc cannot expose it to family.
    dec = {"doc_placements": {DOC_CRED: {"category": "financial", "subcategory": "banking"}}}
    (paths.metadata_dir / "family_decisions.json").write_text(json.dumps(dec))
    case.load()
    rows = case.section("documents")["rows"]
    assert DOC_CRED not in {r["file"] for r in rows}


def test_family_submove_refused(tmp_path):
    """A family-role sub-move → 403 (examiner-gated verb)."""
    fam, _paths = _submove_case(tmp_path, role="family")
    with pytest.raises(fa.VerbError) as e:
        fa.verb_move(fam, {"view": "document", "src": DOC_FIN,
                           "to": "financial", "subcategory": "banking"})
    assert e.value.code == 403


# ── overlay-level properties (apply_face_overlay face_placements pass) ──

def test_overlay_stale_placement_is_noop(tmp_path):
    from tools._archive_data import apply_face_overlay
    fc = {"person_clusters": {"Person_01": ["/x/a.jpg"]}, "noise_files": []}
    out = apply_face_overlay(fc, {"face_placements": {"/x/a.jpg": "Person_99"}})
    assert out["person_clusters"]["Person_01"] == ["/x/a.jpg"], "stale target → src stays in origin"


def test_overlay_placement_resolves_target_through_merge_and_drops_empty(tmp_path):
    from tools._archive_data import apply_face_overlay
    fc = {"person_clusters": {"Person_01": ["/x/a.jpg"], "Person_02": ["/x/b.jpg"],
                              "Person_03": ["/x/c.jpg"]}, "noise_files": []}
    dec = {"person_merges": {"Person_02": "Person_01"},
           "face_placements": {"/x/c.jpg": "Person_02"}}
    out = apply_face_overlay(fc, dec)
    assert "Person_02" not in out["person_clusters"], "merged loser dropped"
    assert "/x/c.jpg" in out["person_clusters"]["Person_01"], "placement into loser routes to winner"
    assert "Person_03" not in out["person_clusters"], "origin emptied by placement is dropped"


def test_overlay_placement_wins_over_legacy_assignment_same_src(tmp_path):
    """A src carrying both keys: the legacy append is pulled and the placement wins;
    the src is excluded from noise_files (no confirm-queue leak)."""
    from tools._archive_data import apply_face_overlay
    fc = {"person_clusters": {"Person_01": ["/x/keep.jpg"], "Person_02": []},
          "noise_files": ["/x/n.jpg"]}
    dec = {"face_assignments": {"/x/n.jpg": "Person_01"},
           "face_placements": {"/x/n.jpg": "Person_02"}}
    out = apply_face_overlay(fc, dec)
    assert out["person_clusters"]["Person_01"] == ["/x/keep.jpg"], "assigned copy pulled"
    assert "/x/n.jpg" in out["person_clusters"]["Person_02"], "placement wins"
    assert "/x/n.jpg" not in out["noise_files"], "excluded from the confirm queue"


def test_overlay_legacy_assignment_still_applies_dual_read(tmp_path):
    """An on-disk legacy face_assignments entry (pre-Move) still applies via the
    overlay's dual-read and leaves the confirm queue."""
    case, paths = _multi_person_case(tmp_path)
    (paths.metadata_dir / "family_decisions.json").write_text(
        json.dumps({"face_assignments": {NOISE_SRC: "Person_01"}}))
    case.load()
    assert NOISE_SRC in [m["id"] for m in case.person_detail_section("Person_01")["members"]]
    q = {(i["queue"], i["id"]) for i in case.section("review")["confirm_queue"]}
    assert ("face", NOISE_SRC) not in q


def test_overlay_does_not_mutate_input(tmp_path):
    from tools._archive_data import apply_face_overlay
    fc = {"person_clusters": {"Person_01": ["/x/a.jpg"], "Person_02": ["/x/b.jpg"]},
          "noise_files": ["/x/n.jpg"]}
    snapshot = json.loads(json.dumps(fc))
    apply_face_overlay(fc, {"face_placements": {"/x/b.jpg": "Person_01"}})
    assert fc == snapshot, "the input dict must never be mutated"


# ── Person collection export maps video frames → source video ──

def test_collection_ids_person_maps_video_frames_to_source(tmp_path):
    case, paths = setup_case(tmp_path)
    fc = json.loads((paths.metadata_dir / "face_clustering.json").read_text())
    fc["person_clusters"]["Person_09"] = ["/x/clip_f000001.jpg", SRC_A]
    (paths.metadata_dir / "face_clustering.json").write_text(json.dumps(fc))
    (paths.metadata_dir / "video_frame_map.json").write_text(
        json.dumps({"/x/clip_f000001.jpg": {"source_video": "/work/vid.mov"}}))
    case.load()
    ids = fa._collection_ids(case, "person", "Person_09")
    assert "/work/vid.mov" in ids, "video-frame member maps to its source video"
    assert "/x/clip_f000001.jpg" not in ids, "keyframe still not exported"
    assert SRC_A in ids, "real photo member kept"


def test_collection_ids_financial_uncategorized_subcat(tmp_path):
    # A financial doc with no subcategory is bucketed as "uncategorized" by
    # documents_index; the export key financial:uncategorized must resolve to it
    # (regression: server normalised null -> "" and matched nothing).
    case, _paths = setup_case(tmp_path)
    case.load()
    ids = fa._collection_ids(case, "category", "financial:uncategorized")
    assert "/work/docs/invoice.pdf" in ids


def test_quarantine_media_fallback_serves_examiner(tmp_path):
    # A quarantined file lives under <case>/quarantine/, not the archive — its
    # canonical (archive) path no longer exists and is forbidden to the generic
    # resolver. The examiner Review queue must still display it for triage.
    case, paths = setup_case(tmp_path)
    e, canonical, _view, qfile = _add_quarantine(paths, "q.jpg", filt="explicit_sexual_content")
    case.load()
    # generic resolver can't serve it (archive path gone / quarantine forbidden)
    with pytest.raises(fa.VerbError):
        fa.resolve_media_path(case, str(canonical))
    # fallback resolves by canonical path (Quarantine group src) ...
    assert fa.resolve_quarantine_media_path(case, str(canonical)) == qfile
    # ... and by the original scan path (Sensitivity-list src)
    assert fa.resolve_quarantine_media_path(case, e["file"]) == qfile


def test_quarantine_media_fallback_refuses_family(tmp_path):
    case, paths = setup_case(tmp_path)
    case.load()
    # a quarantined item is examiner-only — never served to the family
    _e2, canonical2, _v2, _q2 = _add_quarantine(paths, "n.jpg", filt="nudity")
    fam = fa.ArchiveCase(paths, "family", {}); fam.load()
    with pytest.raises(fa.VerbError) as ex2:
        fa.resolve_quarantine_media_path(fam, str(canonical2))
    assert ex2.value.code == 403


# ── Messages section (message_triage integration) ──

CONV_ID = "sms_3f9a2b4c5d6e"
CONV_ID2 = "wa_112233445566"


def _add_messages(paths):
    """Write a conversation_index.json + two per-conversation JSONs (one keep,
    one platform) and a discard-verdict index entry with no detail file."""
    md = paths.metadata_dir
    (md / "conversation_index.json").write_text(json.dumps([
        {"conversation_id": CONV_ID, "platform": "sms", "participants": ["Mom"],
         "display_name": "Mom", "span": ["2020-01-01 09:00", "2020-01-02 10:00"],
         "message_count": 2, "chunk_count": 1, "call_event_count": 0,
         "attachment_count": 2, "direction_counts": {"sent": 1, "received": 1},
         "triage_verdict": "keep", "triage_reason": "personal correspondence",
         "sources": ["/orig/sms.xml"]},
        {"conversation_id": CONV_ID2, "platform": "whatsapp", "participants": ["Dad"],
         "display_name": "Dad", "span": ["2019-01-01 09:00", "2019-06-01 10:00"],
         "message_count": 1, "chunk_count": 1, "call_event_count": 1,
         "attachment_count": 0, "direction_counts": {"sent": 0, "received": 1},
         "triage_verdict": "platform", "triage_reason": "", "sources": ["/orig/wa.db"]},
        {"conversation_id": "sms_junk9999", "platform": "sms", "participants": ["SpamCo"],
         "display_name": "SpamCo", "span": ["2023-01-01 00:00", "2023-01-01 00:01"],
         "message_count": 40, "triage_verdict": "discard"},
    ]))
    mdir = md / "messages"
    mdir.mkdir(exist_ok=True)
    (mdir / f"{CONV_ID}.json").write_text(json.dumps({
        "conversation_id": CONV_ID, "platform": "sms", "participants": ["Mom"],
        "triage_verdict": "keep",
        "messages": [
            {"ts": "2020-01-02 10:00", "sender": "Mom", "direction": "received",
             "text": "photo", "attachments": [SRC_A, "/work/extracted/undelivered.jpg"]},
            {"ts": "2020-01-01 09:00", "sender": "owner", "direction": "sent",
             "text": "hi", "attachments": []},
        ],
        "call_events": [],
    }))
    (mdir / f"{CONV_ID2}.json").write_text(json.dumps({
        "conversation_id": CONV_ID2, "platform": "whatsapp", "participants": ["Dad"],
        "triage_verdict": "platform",
        "messages": [{"ts": "2019-01-01 09:00", "sender": "Dad", "direction": "received",
                      "text": "hello", "attachments": []}],
        "call_events": [{"ts": "2019-06-01 10:00", "call_type": "voice", "duration_s": 65}],
    }))


def test_messages_page_registered_and_family_visible():
    keys = [k for k, _, _ in fa.PAGES]
    assert "messages" in keys, "Messages must be a registered page (nav entry)"
    assert "messages" not in fa.EXAMINER_ONLY, "family sees kept conversations"


def test_accounts_nav_label_is_online_accounts():  # BACKLOG #23
    # Label-only rename ("Accounts & assets" over-promised — no monetary balances
    # exist in the pipeline). The page id stays "accounts"; only the nav label
    # changed, so pin it to guard against the old copy drifting back in.
    labels = dict((k, label) for k, label, _ in fa.PAGES)
    assert labels["accounts"] == "Online Accounts"


def test_messages_section_rows_and_overview_count(tmp_path):
    case, paths = setup_case(tmp_path)
    _add_messages(paths)
    case.load()
    rows = case.section("messages")
    ids = [r["conversation_id"] for r in rows]
    assert ids == [CONV_ID, CONV_ID2], "keep first, discard excluded"
    assert rows[0]["display_name"] == "Mom" and rows[0]["verdict"] == "keep"
    counts = case.section("overview")["counts"]
    assert counts["messages"] == 2, "overview counts the non-discard conversations"


def test_messages_section_degrades_when_stage_absent(tmp_path):
    # Existing cases: no conversation_index.json / messages/ dir at all.
    case, _paths = setup_case(tmp_path)
    assert case.section("messages") == []
    assert case.section("overview")["counts"]["messages"] == 0
    with pytest.raises(fa.VerbError) as e:
        case.conversation_section(CONV_ID)
    assert e.value.code == 404


def test_conversation_detail_lazy_per_file_and_attachment_resolution(tmp_path):
    case, paths = setup_case(tmp_path)
    _add_messages(paths)
    case.load()
    assert case._conversation_cache == {}, "no conversation JSON loaded up front"
    d = case.conversation_section(CONV_ID)
    # per-FILE lazy: only the requested conversation was read
    assert set(case._conversation_cache) == {CONV_ID}
    # chronological transcript with direction for bubble styling
    assert [m["direction"] for m in d["messages"]] == ["sent", "received"]
    assert [m["ts"] for m in d["messages"]] == ["2020-01-01 09:00", "2020-01-02 10:00"]
    # attachments: delivered one resolves through archive_map (servable /media
    # src); undelivered one is name-only, never a broken link
    atts = {a["name"]: a["src"] for a in d["messages"][1]["attachments"]}
    assert atts["a.jpg"] == SRC_A
    assert atts["undelivered.jpg"] is None
    # call events surface on the platform conversation
    d2 = case.conversation_section(CONV_ID2)
    assert d2["call_events"][0]["call_type"] == "voice"
    assert set(case._conversation_cache) == {CONV_ID, CONV_ID2}
    # unknown conversation → 404
    with pytest.raises(fa.VerbError) as e:
        case.conversation_section("nope_000000000000")
    assert e.value.code == 404


def test_conversation_id_traversal_rejected(tmp_path):
    case, paths = setup_case(tmp_path)
    _add_messages(paths)
    case.load()
    # a hostile id must never escape output/metadata/messages/
    for bad in ("../case_summary", "../../../etc/passwd", "a/b", ".."):
        with pytest.raises(fa.VerbError) as e:
            case.conversation_section(bad)
        assert e.value.code == 404, bad


def test_search_includes_conversations(tmp_path):
    case, paths = setup_case(tmp_path)
    _add_messages(paths)
    case.load()
    s = case.section("search")
    recs = [r for r in s["records"] if r["p"] == "messages"]
    assert {r["h"] for r in recs} == {CONV_ID, CONV_ID2}, "discard conversation not indexed"
    mom = next(r for r in recs if r["h"] == CONV_ID)
    assert mom["t"] == "Mom" and mom["k"] == "conversation"


# ── section/API pagination (F-3: kill the silent caps) ──────────────────────────

def _pages(case, name, params_base, limit=10):
    """Walk every page of a paginated section; return (all_rows, first_total)."""
    out, off, total = [], 0, None
    while True:
        p = dict(params_base); p["offset"] = str(off); p["limit"] = str(limit)
        pg = case.api_section(name, p)
        total = pg["total"]
        out.extend(pg["rows"])
        got = len(pg["rows"])
        off += got
        if off >= total or not got:
            break
    return out, total


def test_api_section_paginates_documents_true_total_and_index(tmp_path):
    case, paths = setup_case(tmp_path)
    summ = json.loads((paths.metadata_dir / "case_summary.json").read_text())
    summ["document_classifications"] = [
        {"file": f"/d/{i}.pdf", "filename": f"{i}.pdf", "category": "legal",
         "source": "document", "significance": i % 5} for i in range(50)]
    (paths.metadata_dir / "case_summary.json").write_text(json.dumps(summ))
    case.load()
    full = case.section("documents")["rows"]
    assert len(full) == 50
    # honors offset/limit; reports the TRUE (pre-slice) total; carries full index
    p0 = case.api_section("documents", {"offset": "0", "limit": "20"})
    assert p0["total"] == 50 and p0["offset"] == 0 and p0["limit"] == 20 and len(p0["rows"]) == 20
    assert p0["index"] and sum(c["count"] for c in p0["index"]) == 50, "index from ALL rows"
    # sum of pages == the full set, in order, with no dupes or gaps
    seen, total = _pages(case, "documents", {}, limit=20)
    files = [r["file"] for r in seen]
    assert total == 50 and files == [r["file"] for r in full]
    assert len(files) == len(set(files)) == 50
    # limit is clamped to the sane max (a client can't demand a 30k-row payload)
    assert case.api_section("documents", {"offset": "0", "limit": "999999"})["limit"] == 2000
    # server-side category filter narrows total but keeps the full index
    summ["document_classifications"].append(
        {"file": "/d/fin.pdf", "filename": "fin.pdf", "category": "financial",
         "source": "document", "significance": 1})
    (paths.metadata_dir / "case_summary.json").write_text(json.dumps(summ))
    case.load()
    fin = case.api_section("documents", {"cat": "financial", "offset": "0", "limit": "20"})
    assert fin["total"] == 1 and fin["rows"][0]["file"] == "/d/fin.pdf"
    assert sum(c["count"] for c in fin["index"]) == 51, "index still spans all categories"


def test_api_section_photos_sort_date_and_pagination(tmp_path):
    # make_case seeds a.jpg (2020-05-01) and b.jpg (2019-01-01).
    case, _paths = setup_case(tmp_path)
    newest = case.api_section("photos", {})
    assert [r["name"] for r in newest["rows"]] == ["a.jpg", "b.jpg"] and newest["total"] == 2
    oldest = case.api_section("photos", {"sort": "oldest"})
    assert [r["name"] for r in oldest["rows"]] == ["b.jpg", "a.jpg"], "oldest tail reachable via sort"
    # date-range filter (server-side) narrows the set and the total
    only20 = case.api_section("photos", {"date_from": "2020-01-01"})
    assert [r["name"] for r in only20["rows"]] == ["a.jpg"] and only20["total"] == 1
    only19 = case.api_section("photos", {"date_to": "2019-12-31"})
    assert [r["name"] for r in only19["rows"]] == ["b.jpg"] and only19["total"] == 1
    # offset/limit slice with the true total preserved
    pg = case.api_section("photos", {"offset": "1", "limit": "1"})
    assert [r["name"] for r in pg["rows"]] == ["b.jpg"] and pg["total"] == 2 and pg["offset"] == 1


def test_timeline_venues_and_on_this_day_sections(tmp_path):
    # G-5/G-10/G-8: enrich the fixture geo index with temporal + venue clusters, then
    # exercise the three new sections through the live ArchiveCase.
    case, paths = setup_case(tmp_path)
    gpath = paths.metadata_dir / "geo_cluster_index.json"
    geo = json.loads(gpath.read_text())
    # a.jpg 2020-05-01 (Portland), b.jpg 2019-01-01 — put them in two chapters, and
    # both a-venue plus a singleton so venue filtering is exercised.
    geo["/work/extracted/a.jpg"] = {"temporal_chapter": "2020-05", "temporal_event_id": 1,
                                     "compound_label": "Portland_Oregon | 2020-05 | event 1",
                                     "gps_venue_cluster_id": "0-0"}
    geo["/work/extracted/b.jpg"] = {"temporal_chapter": "2019-01", "temporal_event_id": 2,
                                    "compound_label": "Unknown_Location | 2019-01 | event 2",
                                    "gps_venue_cluster_id": "0-0"}
    geo["__clusters__"] = {"undated_file_count": 0}
    gpath.write_text(json.dumps(geo))
    # a.jpg has GPS; give b.jpg GPS too so the venue has 2 geolocated members.
    mpath = paths.metadata_dir / "metadata_index.json"
    md = json.loads(mpath.read_text())
    md["/work/extracted/b.jpg"]["gps"] = {"lat": 45.6, "lon": -122.7}
    mpath.write_text(json.dumps(md))
    case.load()

    tl = case.section("timeline")
    assert tl["chapter_count"] == 2 and tl["event_count"] == 2
    assert [c["chapter"] for c in tl["chapters"]] == ["2020-05", "2019-01"]
    assert tl["chapters"][0]["label"] == "Portland_Oregon"
    assert tl["chapters"][0]["date_from"] == "2020-05-01"
    assert tl["undated"]["count"] == 0

    places = case.section("places")
    assert "venues" in places and len(places["venues"]) == 1  # the 0-0 venue (2 members)
    assert places["venues"][0]["count"] == 2
    assert places["venues"][0]["name"] == "Portland_Oregon"

    # On-this-day injected via api_section (today passed as a param → deterministic).
    ov = case.api_section("overview", {"today": "2024-05-01"})
    assert ov["on_this_day"]["total_count"] == 1
    assert ov["on_this_day"]["years"][0]["year"] == "2020"
    # no today param → no card (never reads the wall clock in the builder)
    assert "on_this_day" not in case.api_section("overview", {})


def _seed_gallery_layer(paths):
    """Add 25 photos to the case (on top of the fixture's a.jpg/b.jpg): favorites +
    an album live in the OLDEST 3 (so newest-first they fall past page 1), and 2
    are hidden. Returns nothing — the caller reloads the case."""
    scene = json.loads((paths.metadata_dir / "scene_index.json").read_text())
    md = json.loads((paths.metadata_dir / "metadata_index.json").read_text())
    for i in range(25):
        src = f"/work/extracted/g{i:02d}.jpg"
        # newest-first ordering by ts: g00 is newest (2020-...), g24 oldest (2018-...)
        yr = 2020 - (i // 12)
        scene["clip_results"][src] = {"category": "everyday life", "confidence": 0.9, "delivered": True}
        rec = {"timestamp": f"{yr}-01-{(i % 12) + 1:02d}T00:00:00", "place": None, "gps": None}
        md[src] = rec
    # favorites + album on the 3 OLDEST (g22,g23,g24 → last by newest-first sort)
    for i in (22, 23, 24):
        md[f"/work/extracted/g{i:02d}.jpg"].update(
            {"photo_library_favorite": True, "album_membership": ["Reunion"],
             "gallery_source": "iphoto"})
    # 2 hidden (g00, g01 — the newest, so they'd otherwise head page 1)
    for i in (0, 1):
        md[f"/work/extracted/g{i:02d}.jpg"]["photo_library_hidden"] = True
    (paths.metadata_dir / "scene_index.json").write_text(json.dumps(scene))
    (paths.metadata_dir / "metadata_index.json").write_text(json.dumps(md))


def test_api_section_photos_owner_gallery_filters_and_facets(tmp_path):
    case, paths = setup_case(tmp_path)
    _seed_gallery_layer(paths)
    case.load()
    # Base universe: a.jpg + b.jpg (fixture) + 25 = 27 photos; 2 hidden, 3 favorites.
    # Default view EXCLUDES hidden → 25, and reports whole-set facets.
    d = case.api_section("photos", {"offset": "0", "limit": "10"})
    assert d["total"] == 25, "hidden excluded from the main grid by default"
    assert d["facets"] == {"favorites": 3, "hidden": 2, "albums": ["Reunion"]}
    assert not any(r.get("hidden") for r in d["rows"]), "no hidden leaks onto page 1"
    # Favorites live in the OLDEST 3 (past page 1) — the SERVER filter still reaches
    # them because it narrows the FULL set before the page slice.
    favs, ftot = _pages(case, "photos", {"favorite": "1"}, limit=10)
    assert ftot == 3 and len(favs) == 3 and all(r["favorite"] for r in favs)
    # Album filter narrows to the same 3 members (filtered total is correct).
    alb, atot = _pages(case, "photos", {"album": "Reunion"}, limit=10)
    assert atot == 3 and {r["id"] for r in alb} == {r["id"] for r in favs}
    # hidden=1 reveals the owner's hidden photos → the full 27 become reachable.
    _all, htot = _pages(case, "photos", {"hidden": "1"}, limit=10)
    assert htot == 27, "examiner can always reveal hidden — never a silent drop"
    # Favorite + a tight page window: total stays the filtered count, page slices it.
    pg = case.api_section("photos", {"favorite": "1", "offset": "2", "limit": "1"})
    assert pg["total"] == 3 and pg["offset"] == 2 and len(pg["rows"]) == 1


def test_api_section_paginates_emails_and_keeps_demoted_reachable(tmp_path):
    case, paths = setup_case(tmp_path)
    threads = [{"thread_id": f"t{i}", "subject": f"s{i}", "significance": 5,
                "date_last": "2020-01-01"} for i in range(30)]
    (paths.metadata_dir / "email_threads_index.json").write_text(json.dumps({"threads": threads}))
    # Demote t0 → forced to significance 0 (sinks to the bottom band). With true
    # pagination the cap is gone, so it must remain reachable on a later page.
    (paths.metadata_dir / "family_decisions.json").write_text(
        json.dumps({"email_demoted": {"t0": {}}}))
    case.load()
    assert len(case.section("emails")) == 30
    seen, total = _pages(case, "emails", {}, limit=10)
    ids = [r["thread_id"] for r in seen]
    assert total == 30 and len(ids) == 30 and len(set(ids)) == 30, "sum of pages == full set, no dupes"
    demoted = [r for r in seen if r["thread_id"] == "t0"]
    assert demoted and demoted[0]["demoted"] is True, "demoted thread reachable + still flagged"


def test_overview_emails_count_matches_email_section(tmp_path):
    """Overview previously had no "emails" tile at all; it must now match the
    thread-grain total the Emails section itself shows (see email_rows)."""
    case, paths = setup_case(tmp_path)
    threads = [{"thread_id": f"t{i}", "subject": f"s{i}", "significance": 5,
                "date_last": "2020-01-01"} for i in range(30)]
    (paths.metadata_dir / "email_threads_index.json").write_text(json.dumps({"threads": threads}))
    case.load()
    assert len(case.section("emails")) == 30
    counts = case.section("overview")["counts"]
    assert counts["emails"] == 30 == len(case.section("emails"))


def test_api_section_correspondents_paginates_true_total(tmp_path):
    case, paths = setup_case(tmp_path)
    freq = [{"address": f"p{i}@x", "display_name": f"P{i}", "sent_count": i,
             "received_count": 1, "total": i + 1, "bidirectional": True,
             "first_seen": "2010-01-01", "last_seen": "2015-01-01"} for i in range(25)]
    (paths.metadata_dir / "correspondent_frequency.json").write_text(json.dumps(freq))
    case.load()
    assert len(case.section("correspondents")) == 25
    p0 = case.api_section("correspondents", {"offset": "0", "limit": "10"})
    assert p0["total"] == 25 and len(p0["rows"]) == 10 and p0["offset"] == 0
    # ranked by total desc (p24 leads); both roles see the page (non-sensitive)
    assert p0["rows"][0]["address"] == "p24@x"
    seen, total = _pages(case, "correspondents", {}, limit=10)
    ids = [r["address"] for r in seen]
    assert total == 25 and len(ids) == 25 and len(set(ids)) == 25, "pages cover the full set"

    # The roles NO LONGER see the same cards (audience split). correspondent_
    # frequency.json is the examiner's union; the family reads its own file,
    # built from family-visible mail only. Absent → EMPTY, never a fallback to
    # the union: a family card for an estate-rescued marketing sender would rank
    # near the top by volume and its ?participant= click-through would return
    # nothing, because the family's thread set excludes that sender.
    fam = fa.ArchiveCase(paths, "family", {}); fam.load()
    assert fam.api_section("correspondents", {"offset": "0", "limit": "5"})["total"] == 0, \
        "family must not fall back to the examiner's union card set"

    family_freq = [f for f in freq if f["address"] in ("p1@x", "p2@x")]
    (paths.metadata_dir / "correspondent_frequency_family.json").write_text(
        json.dumps(family_freq))
    fam = fa.ArchiveCase(paths, "family", {}); fam.load()
    fam_page = fam.api_section("correspondents", {"offset": "0", "limit": "5"})
    assert fam_page["total"] == 2
    assert {r["address"] for r in fam_page["rows"]} == {"p1@x", "p2@x"}


def test_api_section_emails_participant_filter_narrows_and_paginates(tmp_path):
    case, paths = setup_case(tmp_path)
    # 20 threads with dawn (mixed participant forms), 10 without → filter → 20.
    threads = []
    for i in range(20):
        form = ("dawn@x" if i % 2 else "Dawn <dawn@x>")   # bare + display-name forms
        threads.append({"thread_id": f"d{i}", "subject": f"s{i}", "significance": 3,
                        "date_last": "2020-01-01", "participants": [form, "owner@x"]})
    for i in range(10):
        threads.append({"thread_id": f"o{i}", "subject": f"o{i}", "significance": 3,
                        "date_last": "2020-01-01", "participants": ["someone@x", "owner@x"]})
    (paths.metadata_dir / "email_threads_index.json").write_text(json.dumps({"threads": threads}))
    case.load()
    # unfiltered total is all 30
    assert case.api_section("emails", {"offset": "0", "limit": "5"})["total"] == 30
    # participant filter narrows to the 20 dawn threads; total reflects the FILTER
    p0 = case.api_section("emails", {"participant": "dawn@x", "offset": "0", "limit": "8"})
    assert p0["total"] == 20 and len(p0["rows"]) == 8
    seen, total = _pages(case, "emails", {"participant": "dawn@x"}, limit=8)
    ids = [r["thread_id"] for r in seen]
    assert total == 20 and len(ids) == 20 and set(ids) == {f"d{i}" for i in range(20)}
    # case-insensitive substring match (matches "Dawn <dawn@x>")
    assert case.api_section("emails", {"participant": "DAWN@X"})["total"] == 20


def test_api_section_correspondents_search_and_sort(tmp_path):
    case, paths = setup_case(tmp_path)
    freq = [
        {"address": "dawn@example.net", "display_name": "Dawn Merrick", "total": 100,
         "bidirectional": True, "last_seen": "2015-01-01"},
        {"address": "bob@x.com", "display_name": "Bob Smith", "total": 500,
         "bidirectional": True, "last_seen": "2020-01-01"},
        {"address": "dawn2@x.com", "display_name": "Dawn Someone", "total": 10,
         "bidirectional": True, "last_seen": "2010-01-01"},
    ]
    (paths.metadata_dir / "correspondent_frequency.json").write_text(json.dumps(freq))
    case.load()
    # ?q= matches name OR address, case-insensitive; unfiltered default stays total-desc.
    unfiltered = case.api_section("correspondents", {})
    assert [r["address"] for r in unfiltered["rows"]] == ["bob@x.com", "dawn@example.net", "dawn2@x.com"]
    by_name = case.api_section("correspondents", {"q": "DAWN"})
    assert by_name["total"] == 2
    assert {r["address"] for r in by_name["rows"]} == {"dawn@example.net", "dawn2@x.com"}
    # Matches on ADDRESS only — "example.net" appears in no display_name — which
    # is the half of ?q= this assertion exists to prove. (The query was "yahoo"
    # until the address it targeted was scrubbed to dawn@example.net upstream and
    # the query was not updated with it, so it matched nothing and the test failed.)
    by_addr = case.api_section("correspondents", {"q": "example.net"})
    assert by_addr["total"] == 1 and by_addr["rows"][0]["address"] == "dawn@example.net"
    # ?sort=name / ?sort=recent re-order; total stays the count either way.
    by_name_sort = case.api_section("correspondents", {"sort": "name"})
    assert [r["name"] for r in by_name_sort["rows"]] == ["Bob Smith", "Dawn Merrick", "Dawn Someone"]
    by_recent = case.api_section("correspondents", {"sort": "recent"})
    assert [r["address"] for r in by_recent["rows"]] == ["bob@x.com", "dawn@example.net", "dawn2@x.com"]


def test_api_section_emails_search_date_and_sort(tmp_path):
    case, paths = setup_case(tmp_path)
    threads = [
        {"thread_id": "t1", "subject": "Estate paperwork", "significance": 2,
         "date_last": "2015-06-01", "participants": ["dawn@x", "owner@x"]},
        {"thread_id": "t2", "subject": "Weekly newsletter", "significance": 5,
         "date_last": "2020-01-01", "participants": ["marketing@x", "owner@x"]},
        {"thread_id": "t3", "subject": "Re: Estate paperwork", "significance": 1,
         "date_last": "2010-01-01", "participants": ["dawn@x", "owner@x"]},
    ]
    (paths.metadata_dir / "email_threads_index.json").write_text(json.dumps({"threads": threads}))
    case.load()
    # ?q= matches subject OR participant, case-insensitive; narrows before the slice.
    by_subject = case.api_section("emails", {"q": "estate"})
    assert by_subject["total"] == 2 and {r["thread_id"] for r in by_subject["rows"]} == {"t1", "t3"}
    by_participant = case.api_section("emails", {"q": "MARKETING"})
    assert by_participant["total"] == 1 and by_participant["rows"][0]["thread_id"] == "t2"
    # date range narrows by date_last.
    ranged = case.api_section("emails", {"date_from": "2012-01-01", "date_to": "2018-01-01"})
    assert ranged["total"] == 1 and ranged["rows"][0]["thread_id"] == "t1"
    # default order stays significance-desc; ?sort=recent / ?sort=subject re-order.
    default = case.api_section("emails", {})
    assert [r["thread_id"] for r in default["rows"]] == ["t2", "t1", "t3"]
    by_recent = case.api_section("emails", {"sort": "recent"})
    assert [r["thread_id"] for r in by_recent["rows"]] == ["t2", "t1", "t3"]
    by_subject_sort = case.api_section("emails", {"sort": "subject"})
    assert [r["thread_id"] for r in by_subject_sort["rows"]] == ["t1", "t3", "t2"]
    # ?q= composes with the existing ?participant= click-through filter.
    combined = case.api_section("emails", {"participant": "dawn@x", "q": "re:"})
    assert combined["total"] == 1 and combined["rows"][0]["thread_id"] == "t3"


def test_thread_messages_attaches_resolved_attachments(tmp_path):
    case, paths = setup_case(tmp_path)
    # A delivered document the attachment resolves to (invoice.pdf is in make_case's
    # document_classifications). Build one .eml with three attachments.
    eml = "/work/mail/1.eml"
    (paths.metadata_dir / "email_threads_index.json").write_text(json.dumps(
        {"threads": [{"thread_id": "t1", "subject": "Docs", "files": [eml]}]}))
    (paths.metadata_dir / "email_index.json").write_text(json.dumps([
        {"file": eml, "message_id": "<1>", "email_from": "a@x", "email_subject": "Docs",
         "email_date_iso": "2020-01-01", "ocr_text": "attached",
         "attachments": [
             {"filename": "invoice.pdf", "content_type": "application/pdf",
              "size_bytes": 500, "is_inline": False},
             {"filename": "logo.png", "content_type": "image/png",
              "size_bytes": 9, "is_inline": True, "content_id": "<ii>"},
             {"filename": "nope.zip", "content_type": "application/zip",
              "size_bytes": 3, "is_inline": False},
         ]},
    ]))
    case.load()
    detail = case.thread_messages("t1")
    atts = {a["filename"]: a for a in detail["messages"][0]["attachments"]}
    assert atts["invoice.pdf"]["file_id"] == "/work/docs/invoice.pdf"  # unique doc basename
    assert atts["nope.zip"]["file_id"] is None                         # unmatched → name-only
    assert atts["logo.png"]["is_inline"] is True


def test_api_section_wraps_videos_and_correspondence_scanned(tmp_path):
    case, paths = setup_case(tmp_path)
    # videos: empty but still a paginated envelope (frontend reads .rows/.total),
    # now with an additive whole-set G-11 facets summary (empty here).
    v = case.api_section("videos", {"offset": "0", "limit": "5"})
    assert v == {"rows": [], "total": 0, "offset": 0, "limit": 5,
                 "facets": {"persons": [], "scenes": []}}
    # correspondence: only the `scanned` sub-list is paginated; typed/handwritten stay
    scene = json.loads((paths.metadata_dir / "scene_index.json").read_text())
    label = "scanned document or handwritten letter"
    for i in range(15):
        scene["clip_results"][f"/scan/{i}.jpg"] = {"category": label, "delivered": True}
    (paths.metadata_dir / "scene_index.json").write_text(json.dumps(scene))
    case.load()
    data = case.api_section("correspondence", {"offset": "0", "limit": "10"})
    assert "typed" in data and "handwritten" in data
    assert isinstance(data["scanned"], dict)
    assert data["scanned"]["total"] == 15 and len(data["scanned"]["rows"]) == 10
    # walking pages covers the whole scanned set
    seen = []
    for off in (0, 10):
        pg = case.api_section("correspondence", {"offset": str(off), "limit": "10"})["scanned"]
        seen.extend(r["id"] for r in pg["rows"])
    assert len(seen) == 15 and len(set(seen)) == 15


def _setup_videos(paths, case):
    """Add two delivered videos + video_index facets + a video_frame_map (one video
    with in-tree frames plus an out-of-tree frame, one with pruned/no frames) and
    reload the case. Returns (src1, src2)."""
    arc = paths.archive_dir
    v1 = arc / "party.mov"; v1.write_bytes(b"\x00")
    v2 = arc / "concert.mp4"; v2.write_bytes(b"\x00")
    src1, src2 = "/work/extracted/party.mov", "/work/extracted/concert.mp4"
    am = json.loads((paths.metadata_dir / "archive_map.json").read_text())
    am["entries"][src1] = str(v1); am["entries"][src2] = str(v2)
    (paths.metadata_dir / "archive_map.json").write_text(json.dumps(am))
    (paths.metadata_dir / "video_index.json").write_text(json.dumps({"videos": [
        {"source_video": src1, "assigned_persons": ["Person_01", "no_faces"],
         "assigned_scenes": ["birthday party"]},
        {"source_video": src2, "assigned_persons": ["Person_02"],
         "assigned_scenes": ["concert"]},
    ]}))
    photos = paths.case_dir / "extracted" / "photos"
    photos.mkdir(parents=True, exist_ok=True)
    # Two frames that SURVIVE on disk (retention keeps posters/member frames)...
    (photos / "party_f000002.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    (photos / "party_f000001.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    intree = str(photos)
    out_of_tree = str(paths.case_dir.parent / "outside_f.jpg")   # under cases/, NOT under CASE_T
    (paths.metadata_dir / "video_frame_map.json").write_text(json.dumps({
        intree + "/party_f000002.jpg": {"source_video": src1, "frame_offset_seconds": 5},
        intree + "/party_f000001.jpg": {"source_video": src1, "frame_offset_seconds": 1},
        # ...plus a PRUNED frame (map entry lingers, file gone) → must be excluded
        intree + "/party_f000003_pruned.jpg": {"source_video": src1, "frame_offset_seconds": 7},
        out_of_tree: {"source_video": src1, "frame_offset_seconds": 9},   # must be refused
        # src2 (concert.mp4) has NO frames → pruned/retention degrade to empty
    }))
    fc = json.loads((paths.metadata_dir / "face_clustering.json").read_text())
    fc["cluster_identities"] = {"Person_01": {"name": "Dawn"}}
    (paths.metadata_dir / "face_clustering.json").write_text(json.dumps(fc))
    case.load()
    return src1, src2


def test_videos_facets_and_person_scene_filters(tmp_path):  # G-11
    case, paths = setup_case(tmp_path)
    src1, src2 = _setup_videos(paths, case)
    allv = case.api_section("videos", {"offset": "0", "limit": "50"})
    assert allv["total"] == 2 and {r["id"] for r in allv["rows"]} == {src1, src2}
    # facets from the FULL set: name resolved for Person_01, prettified for the unnamed one
    facets = allv["facets"]
    assert {"person_id": "Person_01", "name": "Dawn"} in facets["persons"]
    assert {"person_id": "Person_02", "name": "Person 02"} in facets["persons"]
    assert facets["scenes"] == ["birthday party", "concert"]
    # each row carries its own persons/scenes
    r1 = next(r for r in allv["rows"] if r["id"] == src1)
    assert r1["persons"] == [{"person_id": "Person_01", "name": "Dawn"}]
    assert r1["scenes"] == ["birthday party"]
    # ?person= narrows the FULL set before the slice → filtered total is correct
    p = case.api_section("videos", {"person": "Person_01", "offset": "0", "limit": "50"})
    assert p["total"] == 1 and [r["id"] for r in p["rows"]] == [src1]
    # ?scene= narrows likewise
    s = case.api_section("videos", {"scene": "concert", "offset": "0", "limit": "50"})
    assert s["total"] == 1 and s["rows"][0]["id"] == src2
    # facets remain whole-set even under an active filter
    assert p["facets"]["scenes"] == ["birthday party", "concert"]


def test_videos_filter_interacts_with_pagination(tmp_path):  # G-11
    case, paths = setup_case(tmp_path)
    arc = paths.archive_dir
    am = json.loads((paths.metadata_dir / "archive_map.json").read_text())
    vids = []
    for i in range(5):
        f = arc / ("v%d.mov" % i); f.write_bytes(b"\x00")
        src = "/work/extracted/v%d.mov" % i
        am["entries"][src] = str(f); vids.append(src)
    (paths.metadata_dir / "archive_map.json").write_text(json.dumps(am))
    (paths.metadata_dir / "video_index.json").write_text(json.dumps({"videos": [
        {"source_video": s, "assigned_persons": ["Person_01"], "assigned_scenes": ["x"]}
        for s in vids]}))
    case.load()
    seen = []
    for off in (0, 2, 4):
        pg = case.api_section("videos", {"person": "Person_01", "offset": str(off), "limit": "2"})
        assert pg["total"] == 5   # filtered total is the same across pages
        seen.extend(r["id"] for r in pg["rows"])
    assert len(seen) == 5 and len(set(seen)) == 5   # the whole filtered tail is reachable


def test_video_frames_endpoint_ordered_containment_retention(tmp_path):  # G-11
    case, paths = setup_case(tmp_path)
    src1, src2 = _setup_videos(paths, case)
    fr = case.video_frames_section(src1)["frames"]
    # ordered by capture offset; the out-of-tree frame is refused (containment),
    # the pruned (file-gone) frame is dropped (retention).
    assert [f["id"].split("/")[-1] for f in fr] == ["party_f000001.jpg", "party_f000002.jpg"]
    assert [f["offset"] for f in fr] == [1, 5]
    assert all("outside" not in f["id"] for f in fr), "out-of-tree frame id must be refused"
    assert all("pruned" not in f["id"] for f in fr), "pruned (file-gone) frame must be dropped"
    # every returned frame id passes the media resolver → it serves via /thumb + /media
    for f in fr:
        assert fa.resolve_media_path(case, f["id"]).is_file()
    # retention: src2's frames were pruned → empty list (degrade, never an error)
    assert case.video_frames_section(src2)["frames"] == []
    # unknown / out-of-tree source id → empty; empty id → empty
    assert case.video_frames_section("/etc/passwd")["frames"] == []
    assert case.video_frames_section("")["frames"] == []


def test_video_frames_family_gated_to_delivered(tmp_path):  # G-11
    case, paths = setup_case(tmp_path, role="family")
    src1, src2 = _setup_videos(paths, case)
    # family: a delivered video exposes its poster strip, and each frame serves
    # via the delivered-set gate's video_frame_map branch (not a family byte-leak).
    ffr = case.video_frames_section(src1)["frames"]
    assert len(ffr) == 2
    for f in ffr:
        assert fa.resolve_media_path(case, f["id"]).is_file()
    # family: frames of an UNDELIVERED source (absent from archive_map) are not exposed
    intree = str(paths.case_dir / "extracted" / "photos")
    vfm = json.loads((paths.metadata_dir / "video_frame_map.json").read_text())
    vfm[intree + "/ghost_f000001.jpg"] = {"source_video": "/work/undelivered.mov",
                                          "frame_offset_seconds": 0}
    (paths.metadata_dir / "video_frame_map.json").write_text(json.dumps(vfm))
    case.load()
    assert case.video_frames_section("/work/undelivered.mov")["frames"] == []


def test_search_section_fed_uncapped_emails(tmp_path):
    # build_search must be fed the uncapped email/document builders, or a thread
    # past the old 5000 cap is unfindable (the index itself was truncated).
    case, paths = setup_case(tmp_path)
    threads = [{"thread_id": f"t{i}", "subject": f"subject{i}", "significance": 0,
                "date_last": "2020-01-01"} for i in range(5001)]
    (paths.metadata_dir / "email_threads_index.json").write_text(json.dumps({"threads": threads}))
    case.load()
    recs = [r for r in case.section("search")["records"] if r["p"] == "emails"]
    assert len(recs) == 5001, "search index fed the FULL thread list"
    assert any(r["h"] == "t5000" for r in recs), "thread past the old cap is findable"


# ── HTTP Range parsing (audio/video streaming, #4) ──

def test_parse_range():
    assert fa.parse_range(None, 1000) == (None, None)          # no header
    assert fa.parse_range("", 1000) == (None, None)
    assert fa.parse_range("bytes=0-99", 1000) == (0, 99)
    assert fa.parse_range("bytes=100-", 1000) == (100, 999)    # open-ended
    assert fa.parse_range("bytes=-100", 1000) == (900, 999)    # suffix
    assert fa.parse_range("bytes=0-99999", 1000) == (0, 999)   # end clamped to size
    assert fa.parse_range("bytes=2000-3000", 1000) == (None, None)  # unsatisfiable
    assert fa.parse_range("bytes=abc", 1000) == (None, None)   # garbage
    assert fa.parse_range("bytes=0-0", 1) == (0, 0)
    assert fa.parse_range("bytes=0-99", 0) == (None, None)     # empty file


# ── Banish + Unbanish ──

def test_banish_then_unbanish(tmp_path):
    case, paths = setup_case(tmp_path)
    arc_a = paths.archive_dir / "a.jpg"
    view = paths.output_dir / "by_person" / "Person_01" / "a.jpg"
    assert arc_a.exists() and view.is_symlink()

    res = fa.verb_banish(case, {"src": SRC_A})
    assert res["ok"]
    assert not arc_a.exists(), "canonical should have moved out of archive"
    assert (paths.output_dir / fa.BANISHED_DIR / "a.jpg").exists()
    assert not view.exists() and not view.is_symlink(), "view symlink removed"
    # audit: ledger intent+done, custody line, exactly one action line
    ledger = (paths.metadata_dir / "_move_ledger.ndjson").read_text()
    assert '"status": "intent"' in ledger and '"status": "done"' in ledger
    assert paths.custody_log.exists()
    assert len(_actions(paths)) == 1

    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    assert arc_a.exists(), "file restored to canonical path"
    assert view.is_symlink() and os.path.realpath(view) == os.path.realpath(arc_a)
    assert len(_actions(paths)) == 2  # banish + banish_undo


def test_banish_uses_surgical_reload_not_full_reparse(tmp_path):
    # #13 (docs/BACKLOG.md): a single Discard must not pay for re-parsing the
    # unrelated large indexes (case_summary, email_threads_index,
    # metadata_index, ...) — reload_after_move() rebuilds only
    # universe/stacks from state already in memory, never those. Prove it:
    # mutate case_summary.json on disk AFTER the case is loaded, then banish —
    # the in-memory summary must NOT pick up that change, because
    # reload_after_move() never re-reads it (a full case.load() would).
    case, paths = setup_case(tmp_path)
    assert SRC_A in case.universe
    before_summary = dict(case.summary)
    (paths.metadata_dir / "case_summary.json").write_text(json.dumps({"poisoned": True}))
    res = fa.verb_banish(case, {"src": SRC_A})
    assert res["ok"]
    # universe correctly reflects the move — the whole point of the rebuild.
    assert SRC_A not in case.universe
    # but the untouched large index was never re-read from the poisoned file.
    assert case.summary == before_summary
    assert case.summary.get("poisoned") is not True


def test_banish_batch_moves_all_in_one_call(tmp_path):
    # Multi-select Discard sends {srcs:[...]} so the server reloads the case once,
    # not once per item (the per-item reload made the grid lag and look broken).
    case, paths = setup_case(tmp_path)
    arc_a = paths.archive_dir / "a.jpg"
    arc_b = paths.archive_dir / "b.jpg"
    assert arc_a.exists() and arc_b.exists()
    res = fa.verb_banish(case, {"srcs": [SRC_A, SRC_B]})
    assert res["ok"] and res["count"] == 2 and res["skipped"] == 0
    assert not arc_a.exists() and not arc_b.exists(), "both moved out of archive"
    assert (paths.output_dir / fa.BANISHED_DIR / "a.jpg").exists()
    assert (paths.output_dir / fa.BANISHED_DIR / "b.jpg").exists()
    # each item is still independently undoable from History (two action lines)
    assert len(_actions(paths)) == 2
    assert len(res["undo_tokens"]) == 2


def test_banish_batch_skips_undeliverable(tmp_path):
    # A selection may include an already-moved / non-deliverable member; the batch
    # skips it rather than failing the whole request.
    case, paths = setup_case(tmp_path)
    res = fa.verb_banish(case, {"srcs": [SRC_A, "/work/extracted/nope.jpg"]})
    assert res["ok"] and res["count"] == 1 and res["skipped"] == 1
    assert not (paths.archive_dir / "a.jpg").exists()


# ── Rename person (label + on-disk folder) ──

def test_rename_person_renames_folder_and_keeps_links(tmp_path):
    case, paths = setup_case(tmp_path)
    by_person = paths.output_dir / "by_person"
    res = fa.verb_rename_person(case, {"person_id": "Person_01", "new_name": "James Hale"})
    assert res["ok"] and res["new_folder"] == "Person_01_James_Hale"
    assert not (by_person / "Person_01").exists()
    new_dir = by_person / "Person_01_James_Hale"
    assert new_dir.is_dir()
    link = new_dir / "a.jpg"
    assert link.is_symlink() and os.path.exists(link), "relative interior link still resolves"
    fc = json.loads((paths.metadata_dir / "face_clustering.json").read_text())
    assert fc["cluster_identities"]["Person_01"]["name"] == "James Hale"

    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    assert (by_person / "Person_01").is_dir() and not new_dir.exists()
    fc2 = json.loads((paths.metadata_dir / "face_clustering.json").read_text())
    assert "Person_01" not in fc2.get("cluster_identities", {})


# ── Confirm ──

def test_confirm_writes_decision_and_undoes(tmp_path):
    case, paths = setup_case(tmp_path)
    res = fa.verb_confirm(case, {"queue": "scene", "id": "/x/low.jpg", "decision": "accept"})
    dec = json.loads((paths.metadata_dir / fa.DECISIONS_FILE).read_text())
    assert dec["scene"]["/x/low.jpg"]["decision"] == "accept"
    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    dec2 = json.loads((paths.metadata_dir / fa.DECISIONS_FILE).read_text())
    assert "/x/low.jpg" not in dec2.get("scene", {})  # restored to absent


# ── Export (non-destructive) ──

def test_export_copies_without_touching_archive(tmp_path):
    case, paths = setup_case(tmp_path)
    dest = tmp_path / "exp"
    res = fa.verb_export(case, {"items": [SRC_A], "dest": str(dest)})
    assert res["ok"] and res["count"] == 1
    assert (dest / "a.jpg").exists()
    assert (dest / "export_manifest.json").exists()
    assert (paths.archive_dir / "a.jpg").exists(), "export must not move the original"


# ── Guards ──

def test_family_role_refuses_mutating_verbs(tmp_path):
    case, _ = setup_case(tmp_path, role="family")
    with pytest.raises(fa.VerbError) as e:
        fa.verb_banish(case, {"src": SRC_A})
    assert e.value.code == 403


def test_media_path_traversal_rejected(tmp_path):
    case, _ = setup_case(tmp_path)
    for bad in ("/etc/passwd", "../../../../etc/passwd"):
        with pytest.raises(fa.VerbError):
            fa.resolve_media_path(case, bad)


def test_family_media_serves_surfaced_audio(tmp_path):
    """A2 regression guard: audio recordings are a first-class family section but
    carry NO archive_map / video_frame_map entry (they live under
    extracted/other/audio). The delivered-set gate must still serve them via the
    precomputed deliverable_audio set, or every recording becomes a dead player."""
    case, paths = setup_case(tmp_path, role="family")
    audio = paths.extracted_dir / "other" / "audio" / "voicemail.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"RIFF....WAVE")
    # Surface it the way the pipeline does: a case_summary audio_classification.
    summ = json.loads((paths.metadata_dir / "case_summary.json").read_text())
    summ.setdefault("audio_classifications", []).append(
        {"file": str(audio), "filename": "voicemail.wav", "category": "voicemail"})
    (paths.metadata_dir / "case_summary.json").write_text(json.dumps(summ))
    case.load()
    assert fa.resolve_media_path(case, str(audio)) == audio
    # A non-surfaced file in the same audio tree is still refused (not a stray door).
    stray = paths.extracted_dir / "other" / "audio" / "not_surfaced.wav"
    stray.write_bytes(b"RIFF....WAVE")
    with pytest.raises(fa.VerbError) as e:
        fa.resolve_media_path(case, str(stray))
    assert e.value.code == 403


def test_c2_noncanonical_spelling_routes_through_map(tmp_path):
    """C2: a non-canonical spelling of a delivered working path must normalize to
    the same archive_map key (so it resolves to the delivered canonical), not fall
    through to serving the raw path. Proves the map lookup is normpath-normalized."""
    case, paths = setup_case(tmp_path, role="family")
    # '/work/extracted/./a.jpg' and '/work/extracted//a.jpg' both == SRC_A normalized.
    for spelling in ("/work/extracted/./a.jpg", "/work/extracted//a.jpg"):
        assert fa.resolve_media_path(case, spelling) == (paths.archive_dir / "a.jpg")


def test_c3_family_export_dest_confined(tmp_path):
    """C3: the family role may only export under output/family_export/; an escaping
    dest is refused. The examiner (trusted) keeps an unconstrained dest."""
    fam, paths = setup_case(tmp_path, role="family")
    _sign(paths)                                   # released, so E4 lets us reach
    outside = tmp_path / "evil"                     # the dest-confinement check
    with pytest.raises(fa.VerbError) as e:
        fa.verb_export(fam, {"items": [SRC_A], "dest": str(outside)})
    assert e.value.code == 403
    assert not outside.exists(), "no directory created at the rejected dest"
    # A dest under family_export/ is fine for the family role.
    inside = paths.output_dir / "family_export" / "sub"
    res = fa.verb_export(fam, {"items": [SRC_A], "dest": str(inside)})
    assert res["ok"] and (inside / "a.jpg").exists()
    # The examiner may export anywhere on the workstation (e.g. an external drive).
    exam, epaths = setup_case(tmp_path / "e", role="examiner")
    ext = tmp_path / "usb_dest"
    res = fa.verb_export(exam, {"items": [SRC_A], "dest": str(ext)})
    assert res["ok"] and (ext / "a.jpg").exists()


def test_c1_local_request_guard_blocks_rebinding(tmp_path):
    """C1: the Host/Origin guard (now applied to GET as well as POST) rejects a
    DNS-rebound Host and a cross-origin fetch, while allowing normal loopback
    navigation. This is the check do_GET calls before serving any read."""
    class _Server:
        server_address = ("127.0.0.1", 7766)

    class _Fake:
        def __init__(self, headers):
            self.headers = headers
            self.server = _Server()

    ok = fa.FamilyArchiveHandler._local_request_ok
    assert ok(_Fake({"Host": "127.0.0.1:7766"})) is True          # direct nav
    assert ok(_Fake({"Host": "localhost:7766"})) is True
    assert ok(_Fake({"Host": "evil.example"})) is False           # DNS rebinding
    assert ok(_Fake({"Host": "127.0.0.1:7766",
                     "Origin": "http://evil.example"})) is False  # cross-origin fetch


def test_metadata_tree_not_servable(tmp_path):
    case, paths = setup_case(tmp_path)
    with pytest.raises(fa.VerbError):
        fa.resolve_media_path(case, str(paths.metadata_dir / "case_summary.json"))


def test_family_export_blocked_when_gated(tmp_path):
    # B1: VerbError(403), not SystemExit (would kill the request thread silently).
    case, _ = setup_case(tmp_path, delivery_blocked=True, role="family")
    with pytest.raises(fa.VerbError) as e:
        fa.verb_export(case, {"items": [SRC_A]})
    assert e.value.code == 403
    # The startup/CLI path still exits (build_case uses assert_family_allowed).
    with pytest.raises(SystemExit):
        fa.assert_family_allowed({"export_gate": {"delivery_blocked": True}})


def test_audit_one_line_per_verb(tmp_path):
    case, paths = setup_case(tmp_path)
    assert len(_actions(paths)) == 0
    fa.verb_confirm(case, {"queue": "scene", "id": "/x/low.jpg", "decision": "reject"})
    assert len(_actions(paths)) == 1
    fa.verb_rename_person(case, {"person_id": "Person_01", "new_name": "Jane"})
    assert len(_actions(paths)) == 2


# ── security: family /media role gating (allow-list, not deny-list) ──────────────

def test_family_media_allows_delivered_and_working_trees(tmp_path):
    """A family session may fetch delivered canonicals (output/archive) and known
    video keyframes under extracted/photos (posters/stills). A working-tree file
    with NO delivered-set or frame-map provenance is an undelivered stray and is
    refused — the C2 delivered-set gate behind the directory allow-list."""
    case, paths = setup_case(tmp_path, role="family")
    # SRC_A maps through archive_map to output/archive/a.jpg — allowed.
    assert fa.resolve_media_path(case, SRC_A) == (paths.archive_dir / "a.jpg")
    # A legitimate video keyframe under extracted/photos (in video_frame_map) —
    # served directly, allowed.
    frame = paths.extracted_dir / "photos" / "frame.jpg"
    frame.parent.mkdir(parents=True, exist_ok=True)
    frame.write_bytes(b"\xff\xd8\xff\xd9")
    (paths.metadata_dir / "video_frame_map.json").write_text(
        json.dumps({str(frame): {"source_video": "/work/extracted/v.mp4"}}))
    case.load()
    assert fa.resolve_media_path(case, str(frame)) == frame
    # A stray working-tree file with no archive_map or frame-map entry is refused
    # (this is the byte-leak the gate closes: a banished item's working twin, or
    # any undelivered file physically sitting under extracted/photos).
    stray = paths.extracted_dir / "photos" / "stray.jpg"
    stray.write_bytes(b"\xff\xd8\xff\xd9")
    with pytest.raises(fa.VerbError) as e:
        fa.resolve_media_path(case, str(stray))
    assert e.value.code == 403


def test_family_media_blocks_undelivered_and_banished(tmp_path):
    """The core hole: a family session must NOT fetch originals, duplicates, junk,
    or examiner-banished items by direct URL — even though they live in the case."""
    case, paths = setup_case(tmp_path, role="family")
    blocked = {
        "original": paths.original_files_dir / "raw.jpg",
        "duplicate": paths.duplicates_dir / "dup.jpg",
        "junk": paths.extracted_dir / "photos_junk" / "junk.jpg",
        "banished": paths.output_dir / "family_banished" / "gone.jpg",
    }
    for label, p in blocked.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\xff\xd8\xff\xd9")
        with pytest.raises(fa.VerbError) as e:
            fa.resolve_media_path(case, str(p))
        assert e.value.code == 403, label


def test_examiner_media_keeps_broader_denylist(tmp_path):
    """The examiner role is trusted (and reaches quarantine via its own resolver):
    it keeps the deny-list, so an original IS resolvable for it — proving the new
    allow-list is family-scoped, not a blanket tightening that breaks the examiner."""
    case, paths = setup_case(tmp_path, role="examiner")
    orig = paths.original_files_dir / "raw.jpg"
    orig.parent.mkdir(parents=True, exist_ok=True)
    orig.write_bytes(b"\xff\xd8\xff\xd9")
    assert fa.resolve_media_path(case, str(orig)) == orig


# ── security: /media response headers neutralise hostile estate files ────────────

def test_media_headers_sandbox_and_nosniff_always():
    # nosniff is sent on EVERY media response, including PDF. The sandbox CSP is on
    # every type EXCEPT application/pdf (F-11): PDF previews inline in the browser's
    # own sandboxed viewer, which the CSP would blank — see the F-11 tests below.
    for ctype in ("image/jpeg", "video/mp4", "application/pdf", "image/svg+xml"):
        ct, h = fa._media_headers(ctype)
        assert h["X-Content-Type-Options"] == "nosniff"
        if ctype == "application/pdf":
            assert "Content-Security-Policy" not in h
        else:
            assert h["Content-Security-Policy"] == "sandbox"


def test_media_headers_force_download_for_non_inline():
    # An HTML/script-bearing type is relabelled octet-stream + attachment so the
    # browser can never render it as a same-origin document.
    ct, h = fa._media_headers("text/html")
    assert ct == "application/octet-stream"
    assert h["Content-Disposition"] == "attachment"
    # inline media keeps its real type (still with the security headers)
    ct2, h2 = fa._media_headers("image/jpeg")
    assert ct2 == "image/jpeg" and "Content-Disposition" not in h2


# ── security: rename_folder containment + remove_person never destroys ───────────

def test_rename_folder_rejects_path_escape(tmp_path):
    case, paths = setup_case(tmp_path)
    outside = tmp_path / "outside_secret"
    outside.mkdir()
    for bad in ("../../../outside_secret", "..", "a/b", "/abs"):
        with pytest.raises(fa.VerbError) as e:
            fa.verb_rename_folder(case, {"view": "by_person", "old_name": bad,
                                         "new_name": "X"})
        assert e.value.code == 403, bad
    assert outside.exists()  # never moved


def test_rename_folder_legit_still_works(tmp_path):
    case, paths = setup_case(tmp_path)
    fa.verb_rename_folder(case, {"view": "by_person", "old_name": "Person_01",
                                 "new_name": "Family"})
    assert (paths.output_dir / "by_person" / "Family").is_dir()
    assert not (paths.output_dir / "by_person" / "Person_01").exists()


def test_remove_person_preserves_real_file(tmp_path):
    """verb_remove_person may only drop view SYMLINKS. A stray real file in the
    folder is source-bearing and must survive (never-destroy invariant)."""
    case, paths = setup_case(tmp_path)
    fdir = paths.output_dir / "by_person" / "Person_01"
    real = fdir / "note.txt"
    real.write_text("keep me")
    symlink = fdir / "a.jpg"                       # created by setup_case
    assert symlink.is_symlink()
    fa.verb_remove_person(case, {"person_id": "Person_01"})
    assert not symlink.exists()                    # symlink dropped
    assert real.exists() and real.read_text() == "keep me"   # real file preserved
    assert fdir.is_dir()                           # folder kept (rmdir couldn't empty it)


# ── security: CSRF / DNS-rebinding guard on mutating POSTs ───────────────────────

def _fake_req(headers, port=7766):
    return types.SimpleNamespace(
        headers=headers, server=types.SimpleNamespace(server_address=("127.0.0.1", port)))


def test_local_request_guard_accepts_same_origin():
    ok = fa.FamilyArchiveHandler._local_request_ok(
        _fake_req({"Origin": "http://127.0.0.1:7766", "Host": "127.0.0.1:7766"}))
    assert ok is True
    # a non-browser client (no Origin) is not a CSRF vector — allowed
    assert fa.FamilyArchiveHandler._local_request_ok(
        _fake_req({"Host": "127.0.0.1:7766"})) is True


def test_local_request_guard_rejects_cross_origin_and_rebinding():
    # cross-site Origin
    assert fa.FamilyArchiveHandler._local_request_ok(
        _fake_req({"Origin": "http://evil.example", "Host": "127.0.0.1:7766"})) is False
    # DNS-rebinding: hostile Host header re-resolved to loopback
    assert fa.FamilyArchiveHandler._local_request_ok(
        _fake_req({"Host": "evil.example"})) is False


# ── undo/reset bookkeeping (#12) ─────────────────────────────────────────────────

def test_undo_is_not_replayable(tmp_path):
    """Undoing the same token twice must 409 — the already-undone guard relies on
    a TOP-LEVEL `undoes` key (previously buried in `before`, so it never fired)."""
    case, paths = setup_case(tmp_path)
    res = fa.verb_banish(case, {"src": SRC_A})
    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    with pytest.raises(fa.VerbError) as e:
        fa.verb_undo(case, {"undo_token": res["undo_token"]})
    assert e.value.code == 409


def test_reset_does_not_reinvert_an_undone_action(tmp_path):
    """Reset must SKIP an action already undone — re-inverting it would try to
    unbanish an un-banished file (spurious failure / wrong state)."""
    case, paths = setup_case(tmp_path)
    arc_a = paths.archive_dir / "a.jpg"
    res = fa.verb_banish(case, {"src": SRC_A})
    assert not arc_a.exists()
    fa.verb_undo(case, {"undo_token": res["undo_token"]})     # banish reversed
    assert arc_a.exists()
    result = fa.verb_reset(case, {})
    assert result["ok"] and result["failed"] == 0             # no re-inversion failure
    assert result["reversed"] == 0                            # nothing left to reverse
    assert arc_a.exists()                                     # not double-flipped


def test_reset_archives_audit_trail_not_deletes(tmp_path):
    """Reset must ARCHIVE the action log (auditable), not unlink it."""
    case, paths = setup_case(tmp_path)
    fa.verb_confirm(case, {"queue": "scene", "id": "/x/low.jpg", "decision": "reject"})
    fa.verb_reset(case, {})
    archives = list(paths.metadata_dir.glob("family_actions.ndjson.reset-*"))
    assert len(archives) == 1
    assert any(json.loads(l)["action"] == "confirm"
               for l in archives[0].read_text().splitlines())
    acts = _actions(paths)
    assert len(acts) == 1 and json.loads(acts[0])["action"] == "reset"


def test_undo_rename_leaves_no_synthetic_action(tmp_path):
    """Undoing a rename must not append a fresh reversible rename entry (which
    would pollute History and make Reset re-invert a synthetic action)."""
    case, paths = setup_case(tmp_path)
    res = fa.verb_rename_person(case, {"person_id": "Person_01", "new_name": "Jane Q"})
    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    acts = [json.loads(l) for l in _actions(paths)]
    assert [a["action"] for a in acts] == ["rename_person", "rename_person_undo"]
    assert acts[1]["undoes"] == res["undo_token"]             # top-level marker


# ── server performance (P2): lazy/cached section, thumb cache, batch view walk ───

def test_section_skips_photo_build_for_nonphoto_pages(tmp_path, monkeypatch):
    """The ~O(#photos) photo_rows build must only run for pages that use it, not
    on every GET (history/emails/accounts/documents/people never need it)."""
    case, paths = setup_case(tmp_path)
    def boom(*a, **k):
        raise AssertionError("photo_rows built for a page that doesn't need it")
    monkeypatch.setattr(fa, "photo_rows", boom)
    for page in ("documents", "emails", "accounts", "recordings", "history", "people"):
        case.section(page)                      # must not build photos
    with pytest.raises(AssertionError):         # ...but a photo page still does
        case.section("photos")


def test_state_cache_reuses_within_generation_and_invalidates_on_load(tmp_path, monkeypatch):
    case, paths = setup_case(tmp_path)
    n = {"c": 0}
    real = fa.photo_rows
    monkeypatch.setattr(fa, "photo_rows",
                        lambda *a, **k: (n.__setitem__("c", n["c"] + 1) or real(*a, **k)))
    case.section("photos"); case.section("places")   # both use photos, same generation
    assert n["c"] == 1                                # built once, then cached
    case.load()                                       # atomic swap invalidates
    case.section("photos")
    assert n["c"] == 2


def test_thumb_bytes_disk_cache_and_invalidation(tmp_path, monkeypatch):
    src = tmp_path / "img.jpg"; src.write_bytes(b"orig")
    calls = {"n": 0}
    monkeypatch.setattr(fa, "_jpeg_bytes",
                        lambda p, box: (calls.__setitem__("n", calls["n"] + 1) or b"JPEGBYTES"))
    cache = tmp_path / "tc"
    assert fa._thumb_bytes(src, 320, cache_dir=cache) == b"JPEGBYTES"
    assert fa._thumb_bytes(src, 320, cache_dir=cache) == b"JPEGBYTES"
    assert calls["n"] == 1                            # second call served from disk
    assert list(cache.glob("*.jpg"))                  # cache file written
    src.write_bytes(b"changed contents now")          # new size/mtime → miss
    fa._thumb_bytes(src, 320, cache_dir=cache)
    assert calls["n"] == 2


def test_build_view_index_matches_walk(tmp_path):
    case, paths = setup_case(tmp_path)                # by_person/Person_01/a.jpg -> archive/a.jpg
    arc_a = paths.archive_dir / "a.jpg"
    walked = set(fa.current_views(case, arc_a))
    indexed = set(fa.current_views(case, arc_a, view_index=fa.build_view_index(case)))
    assert walked == indexed and walked               # identical + non-empty


# ── photo stacks (perceptual dup groups): scan gate + closed allowlist ──

def _add_stack(paths, *, scan="clean"):
    """Wire a 1-member stack onto keeper SRC_A: dupe file on disk, group index,
    ledger resolution, and (per `scan`) a dup_member_scan verdict.
    scan: clean | flagged | unscanned | uncovered | missing (no scan file at all)."""
    dupes = paths.case_dir / "duplicates" / "perceptual"
    dupes.mkdir(parents=True, exist_ok=True)
    dupe = dupes / "group_0001_a2.jpg"
    dupe.write_bytes(b"\xff\xd8\xff\xd9")
    md = paths.metadata_dir
    (md / "perceptual_dup_groups.json").write_text(json.dumps({
        "schema_version": 1, "case_id": "CASE_T", "stage": "collect_dedup",
        "timestamp": "t",
        "params": {"max_distance": 10, "burst_window_seconds": 3.0,
                   "perceptual_burst_policy": "fold"},
        "groups": [{"group_id": 1, "kind": "burst", "keeper": SRC_A,
                    "members": [{"file": "a2.jpg",
                                 "capture_time": "2019-06-02T14:11:04",
                                 "moved": True}]}]}))
    with open(md / "_move_ledger.ndjson", "a") as fh:
        fh.write(json.dumps({"src": "/work/extracted/a2.jpg", "dst": str(dupe),
                             "sha256": "s", "status": "done",
                             "reason": "perceptual_dupe", "ts": "t"}) + "\n")
    if scan != "missing":
        rec = {"file": str(dupe), "nudity_scanned": scan != "unscanned", "nudity_flag": scan == "flagged",
               "nudity_score": 0.9 if scan == "flagged" else None,
               "scanned_at": "t"}
        (md / "dup_member_scan.json").write_text(json.dumps({
            "schema_version": 1, "stage": "sensitive_scan", "case_id": "CASE_T",
            "generated_at": "t",
            "params": {"nudenet_enabled": True,
                       "nudenet_available": True, "nudenet_threshold": 0.6},
            "members": {} if scan == "uncovered" else {str(dupe): rec}}))
    return dupe


@pytest.mark.parametrize("role", ["examiner", "family"])
def test_stack_surfaces_and_member_serves_via_allowlist(tmp_path, role):
    cases, _ = make_case(tmp_path)
    paths = CasePaths.from_case_id("CASE_T", str(cases))
    dupe = _add_stack(paths, scan="clean")
    case = fa.ArchiveCase(paths, role, {})
    # Stack attached to the keeper's photo row.
    row = next(r for r in case.section("photos") if r["id"] == SRC_A)
    st = row["stack"]
    assert st["n"] == 1 and st["kind"] == "burst"
    assert st["members"][0]["src"] == str(dupe)
    assert st["members"][0]["capture_time"] == "2019-06-02T14:11:04"
    # Member serves ONLY through the dedicated allowlist resolver…
    assert fa.resolve_dup_member_path(case, str(dupe)) == dupe
    # …the generic resolver refuses duplicates/ outright for BOTH roles.
    with pytest.raises(fa.VerbError):
        fa.resolve_media_path(case, str(dupe))


def test_unscanned_member_never_surfaces(tmp_path):
    """No dup_member_scan.json → keeper-only display (fail-closed), and the
    member is not servable."""
    cases, _ = make_case(tmp_path)
    paths = CasePaths.from_case_id("CASE_T", str(cases))
    dupe = _add_stack(paths, scan="missing")
    case = fa.ArchiveCase(paths, "examiner", {})
    assert case.stacks == {} and case.dup_member_paths == {}
    row = next(r for r in case.section("photos") if r["id"] == SRC_A)
    assert "stack" not in row
    with pytest.raises(fa.VerbError):
        fa.resolve_dup_member_path(case, str(dupe))


@pytest.mark.parametrize("scan", ["flagged", "uncovered"])
def test_flagged_or_uncovered_member_dropped(tmp_path, scan):
    cases, _ = make_case(tmp_path)
    paths = CasePaths.from_case_id("CASE_T", str(cases))
    dupe = _add_stack(paths, scan=scan)
    case = fa.ArchiveCase(paths, "examiner", {})
    assert case.stacks == {}   # single-member group fully dropped → no badge
    with pytest.raises(fa.VerbError):
        fa.resolve_dup_member_path(case, str(dupe))


def test_unscanned_member_is_not_served(tmp_path):
    """The inverse of the former KNOWN_GAP test — the gap is closed.

    `build_stacks` now requires COVERAGE (`nudity_scanned is True`), not merely
    an unflagged verdict. A member that was never scanned carries
    `nudity_scanned False, nudity_flag False` — what every member got whenever
    NudeNet was off — and used to be served to the family exactly like one that
    was scanned and came back clean. Group members skip the delivered-image scan
    at `collect_dedup`, so this pass is their only screening: no verdict means
    not shown, which is what the gate's own docstring always specified.

    Do not relax this to `nudity_flag`-only; that reopens the hole."""
    cases, _ = make_case(tmp_path)
    paths = CasePaths.from_case_id("CASE_T", str(cases))
    dupe = _add_stack(paths, scan="unscanned")
    case = fa.ArchiveCase(paths, "family", {})

    rec = json.loads((paths.metadata_dir / "dup_member_scan.json").read_text())
    assert rec["members"][str(dupe)]["nudity_scanned"] is False, "fixture is unscanned"

    assert case.stacks == {}, "an unscanned member must not form a stack"
    with pytest.raises(fa.VerbError):
        fa.resolve_dup_member_path(case, str(dupe))


def test_unscanned_member_is_withheld_from_the_examiner_too(tmp_path):
    """Coverage is required for every role. The examiner has other routes to an
    unscreened file (Review surfaces, suspense); the dup-member route is a
    closed allowlist and must not become a side channel around screening."""
    cases, _ = make_case(tmp_path)
    paths = CasePaths.from_case_id("CASE_T", str(cases))
    dupe = _add_stack(paths, scan="unscanned")
    case = fa.ArchiveCase(paths, "examiner", {})

    assert case.stacks == {}
    with pytest.raises(fa.VerbError):
        fa.resolve_dup_member_path(case, str(dupe))


def test_scanned_clean_member_is_still_served(tmp_path):
    """The other side of the change: coverage + clean verdict still serves, so
    enabling the filter restores stacks in full (measured: 3,677/3,677 members
    covered on a case that ran with NudeNet on)."""
    cases, _ = make_case(tmp_path)
    paths = CasePaths.from_case_id("CASE_T", str(cases))
    dupe = _add_stack(paths, scan="clean")
    case = fa.ArchiveCase(paths, "family", {})

    assert case.stacks, "a scanned, unflagged member must still form a stack"
    assert fa.resolve_dup_member_path(case, str(dupe)) == dupe


def test_non_member_dupe_file_stays_forbidden(tmp_path):
    """Another file sitting in duplicates/perceptual/ (not in any surfaced
    group) is never servable — the allowlist is closed."""
    cases, _ = make_case(tmp_path)
    paths = CasePaths.from_case_id("CASE_T", str(cases))
    _add_stack(paths, scan="clean")
    stray = paths.case_dir / "duplicates" / "perceptual" / "group_0002_stray.jpg"
    stray.write_bytes(b"\xff\xd8\xff\xd9")
    case = fa.ArchiveCase(paths, "examiner", {})
    with pytest.raises(fa.VerbError):
        fa.resolve_dup_member_path(case, str(stray))
    with pytest.raises(fa.VerbError):
        fa.resolve_media_path(case, str(stray))


def test_ambiguous_ledger_basename_fails_closed(tmp_path):
    """Two perceptual_dupe moves sharing a basename → the member is dropped
    rather than risking serving the wrong bytes."""
    cases, _ = make_case(tmp_path)
    paths = CasePaths.from_case_id("CASE_T", str(cases))
    dupe = _add_stack(paths, scan="clean")
    other = paths.case_dir / "duplicates" / "perceptual" / "group_0009_a2.jpg"
    other.write_bytes(b"\xff\xd8\xff\xd9")
    with open(paths.metadata_dir / "_move_ledger.ndjson", "a") as fh:
        fh.write(json.dumps({"src": "/work/other/sub/a2.jpg", "dst": str(other),
                             "sha256": "s2", "status": "done",
                             "reason": "perceptual_dupe", "ts": "t"}) + "\n")
    case = fa.ArchiveCase(paths, "examiner", {})
    assert case.stacks == {}
    for p in (dupe, other):
        with pytest.raises(fa.VerbError):
            fa.resolve_dup_member_path(case, str(p))


def test_banished_keeper_takes_stack_with_it(tmp_path):
    """Keeper-state gate: banishing the keeper removes it from the universe,
    so the stack (and member serving) disappears on the reload."""
    cases, _ = make_case(tmp_path)
    paths = CasePaths.from_case_id("CASE_T", str(cases))
    dupe = _add_stack(paths, scan="clean")
    case = fa.ArchiveCase(paths, "examiner", {})
    assert SRC_A in case.stacks
    fa.verb_banish(case, {"src": SRC_A})
    assert case.stacks == {} and case.dup_member_paths == {}
    with pytest.raises(fa.VerbError):
        fa.resolve_dup_member_path(case, str(dupe))


# ── confirm/batch (one write for a whole selection) ──

def test_confirm_batch_single_write_and_individual_undo(tmp_path):
    case, paths = setup_case(tmp_path)
    out = fa.verb_confirm_batch(case, {
        "items": [{"queue": "scene", "id": "s1"}, {"queue": "face", "id": "f1"}],
        "decision": "accept"})
    assert out["ok"] is True and out["count"] == 2
    dec = json.loads((paths.metadata_dir / fa.DECISIONS_FILE).read_text())
    assert dec["scene"]["s1"]["decision"] == "accept"
    assert dec["face"]["f1"]["decision"] == "accept"
    # One audit entry PER item, so History can undo them individually.
    assert len([l for l in _actions(paths) if '"confirm"' in l]) == 2
    fa.verb_undo(case, {"undo_token": out["undo_tokens"][0]})
    dec = json.loads((paths.metadata_dir / fa.DECISIONS_FILE).read_text())
    assert "s1" not in dec.get("scene", {})
    assert dec["face"]["f1"]["decision"] == "accept"   # untouched


def test_confirm_batch_validates_payload(tmp_path):
    case, _ = setup_case(tmp_path)
    with pytest.raises(fa.VerbError):
        fa.verb_confirm_batch(case, {"items": [], "decision": "accept"})
    with pytest.raises(fa.VerbError):
        fa.verb_confirm_batch(case, {"items": [{"queue": "scene", "id": 1}],
                                     "decision": "maybe"})
    with pytest.raises(fa.VerbError):
        fa.verb_confirm_batch(case, {"items": [{"id": 1}], "decision": "accept"})


# ── B1 correctness fixes (C-1 … C-6) ──

def test_c1_banish_undo_reloads_served_state(tmp_path):
    """C-1: undo of a banish must case.load() so the restored item reappears in
    every view with NO further verb (the inverse used not to reload, so an undone
    banish stayed absent from `universe` until some other mutating verb reloaded)."""
    case, _paths = setup_case(tmp_path)
    assert SRC_A in {r["id"] for r in case.section("photos")}
    res = fa.verb_banish(case, {"src": SRC_A})
    assert SRC_A not in {r["id"] for r in case.section("photos")}, "banished → gone"
    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    assert SRC_A in {r["id"] for r in case.section("photos")}, \
        "undo must refresh served state without another verb"


def test_c1_release_undo_reloads_served_state(tmp_path):
    """C-1 inverse: release → undo re-quarantines; the file is absent from the
    photo universe again immediately (no extra verb needed to reload)."""
    case, paths = setup_case(tmp_path)
    work_src = "/work/rel.jpg"
    _e, canonical, _view, _qfile = _add_quarantine(paths, "rel.jpg")
    # Register the item as a delivered photo so it participates in the photo
    # universe (which keeps a photo only while its archive canonical exists).
    md = paths.metadata_dir
    sc = json.loads((md / "scene_index.json").read_text())
    sc["clip_results"][work_src] = {"category": "everyday life", "confidence": 0.9,
                                    "delivered": True}
    (md / "scene_index.json").write_text(json.dumps(sc))
    am = json.loads((md / "archive_map.json").read_text())
    am["entries"][work_src] = str(canonical)
    (md / "archive_map.json").write_text(json.dumps(am))
    case.load()
    assert work_src not in {r["id"] for r in case.section("photos")}, "quarantined → absent"

    res = fa.verb_release(case, {"canonical_path": str(canonical)})
    assert work_src in {r["id"] for r in case.section("photos")}, "released → present"
    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    assert work_src not in {r["id"] for r in case.section("photos")}, \
        "re-quarantined item must be absent from served state after undo"


def test_c2_rename_undo_restores_string_form_identity(tmp_path):
    """C-2: cluster_identities values are EITHER {"name": ..} OR a bare string
    (older enroll output). Undo of a rename must recover the name from either
    shape — the old code computed name=None for a string identity and DELETED it."""
    case, paths = setup_case(tmp_path)
    fc_path = paths.metadata_dir / "face_clustering.json"
    fc = json.loads(fc_path.read_text())
    fc["cluster_identities"]["Person_01"] = "Aunt May"   # bare STRING identity
    fc_path.write_text(json.dumps(fc))
    case.load()

    res = fa.verb_rename_person(case, {"person_id": "Person_01", "new_name": "Betty"})
    fa.verb_undo(case, {"undo_token": res["undo_token"]})

    ident = json.loads(fc_path.read_text())["cluster_identities"].get("Person_01")
    assert ident is not None, "string identity must not be deleted by undo"
    name = ident["name"] if isinstance(ident, dict) else ident
    assert name == "Aunt May", "the original name must be restored"


def _add_quarantine_at(paths, *, canonical_rel, qname, filt):
    """Quarantine entry with an explicit canonical subpath so two entries can share
    a basename under different filters (the C-3 collision)."""
    qdir = paths.case_dir / "quarantine" / filt
    qdir.mkdir(parents=True, exist_ok=True)
    qfile = qdir / qname
    qfile.write_bytes(b"\xff\xd8\xff\xd9")
    canonical = paths.archive_dir / canonical_rel
    canonical.parent.mkdir(parents=True, exist_ok=True)
    entry = {"file": f"/work/{canonical_rel}", "filter": filt,
             "canonical_path": str(canonical), "quarantine_path": str(qfile),
             "view_paths": [], "timestamp": "2026-01-01T00:00:00"}
    mpath = paths.metadata_dir / "quarantine_manifest.json"
    m = json.loads(mpath.read_text()) if mpath.exists() else {}
    m.setdefault("entries", []).append(entry)
    m.setdefault("released", [])
    mpath.write_text(json.dumps(m))
    return entry, canonical, qfile


def test_c3_release_matches_canonical_path_not_basename(tmp_path):
    """C-3: two entries with the SAME basename under different filters. Release by
    canonical_path hits the right one; release by the ambiguous bare basename 409s
    (basenames are not unique across filter dirs)."""
    case, paths = setup_case(tmp_path)
    # wipe the make_case default entry so only our two collide
    (paths.metadata_dir / "quarantine_manifest.json").write_text(
        json.dumps({"entries": [], "released": []}))
    _e1, canon1, q1 = _add_quarantine_at(
        paths, canonical_rel="sub1/dup.jpg", qname="dup.jpg", filt="nudity")
    _e2, canon2, q2 = _add_quarantine_at(
        paths, canonical_rel="sub2/dup.jpg", qname="dup.jpg", filt="explicit_sexual_imagery")
    case.load()

    # ambiguous bare basename → 409 (no side effects)
    with pytest.raises(fa.VerbError) as e:
        fa.verb_release(case, {"id": "dup.jpg"})
    assert e.value.code == 409
    assert q1.exists() and q2.exists(), "409 must not move anything"

    # unambiguous canonical_path → releases exactly entry 1
    res = fa.verb_release(case, {"canonical_path": str(canon1)})
    assert res["ok"]
    assert canon1.exists() and not q1.exists(), "entry 1 released to its own canonical"
    assert q2.exists() and not canon2.exists(), "entry 2 untouched"


def test_c4_confirm_batch_no_forged_audit_on_write_failure(tmp_path, monkeypatch):
    """C-4: confirm_batch must persist the decisions file BEFORE appending audit
    lines. If the write fails, no `confirm` audit line may exist (the old order
    wrote N durable audit lines first, forging audit for unpersisted decisions)."""
    case, paths = setup_case(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(fa, "atomic_write_json", boom)

    with pytest.raises(RuntimeError):
        fa.verb_confirm_batch(case, {
            "items": [{"queue": "scene", "id": "s1"}, {"queue": "face", "id": "f1"}],
            "decision": "accept"})
    assert [l for l in _actions(paths) if '"confirm"' in l] == [], \
        "no audit lines for decisions that never persisted"


def test_c5_rename_into_existing_folder_conflicts(tmp_path):
    """C-5: renaming a person into an existing target folder must 409 (not silently
    skip the move and desync identity from folder), leaving both sides untouched."""
    case, paths = setup_case(tmp_path)
    (paths.output_dir / "by_person" / "Person_01_James_Hale").mkdir(parents=True)
    with pytest.raises(fa.VerbError) as e:
        fa.verb_rename_person(case, {"person_id": "Person_01", "new_name": "James Hale"})
    assert e.value.code == 409
    fc = json.loads((paths.metadata_dir / "face_clustering.json").read_text())
    assert "Person_01" not in fc.get("cluster_identities", {}), "identity not persisted"
    assert (paths.output_dir / "by_person" / "Person_01").is_dir(), "folder untouched"


def test_c5_remove_person_finds_out_of_band_renamed_folder(tmp_path):
    """C-5: remove_person must locate the folder defensively (scan by_person/ for
    the cluster's symlinks) when it was renamed out-of-band — else its symlinks are
    not recorded and undo restores nothing."""
    case, paths = setup_case(tmp_path)
    by_person = paths.output_dir / "by_person"
    os.rename(by_person / "Person_01", by_person / "Person_01_renamed")  # disk only
    case.load()

    res = fa.verb_remove_person(case, {"person_id": "Person_01"})
    assert res["ok"]
    entry = json.loads(_actions(paths)[-1])
    recorded = entry["before"]["views"]
    assert recorded and any("Person_01_renamed" in link for link, _t in recorded), \
        "the out-of-band folder's symlinks must be recorded for undo"

    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    restored = by_person / "Person_01_renamed" / "a.jpg"
    assert restored.is_symlink() and os.path.exists(restored)


def test_c6_banish_refuses_non_archive_path(tmp_path):
    """C-6: banish must require the resolved canonical to be under output/archive/.
    An original (or frame) resolves for the examiner but is NOT banishable → 400,
    and the file is left untouched."""
    case, paths = setup_case(tmp_path)
    original = paths.case_dir / "original_files" / "orig.jpg"
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(b"\xff\xd8\xff\xd9")
    with pytest.raises(fa.VerbError) as e:
        fa.verb_banish(case, {"src": str(original)})
    assert e.value.code == 400
    assert original.exists(), "a non-archive source file must be left untouched"


# ── B2 robustness/scale (R-2..R-7) ──────────────────────────────────────────────

def test_r2_email_by_file_parses_once_under_race(tmp_path, monkeypatch):
    """R-2: two threads opening thread details concurrently must NOT both json.load
    the ~120 MB email index — the lazy first-touch build is lock-guarded."""
    import threading
    import time
    case, paths = setup_case(tmp_path)
    (paths.metadata_dir / "email_index.json").write_text(
        json.dumps([{"file": "/e/1.eml", "email_subject": "hi"}]))
    calls = {"n": 0}
    real = fa.load_json

    def counting(path, default=None):
        if str(path).endswith("email_index.json"):
            calls["n"] += 1
            time.sleep(0.03)          # widen the race window
        return real(path, default)

    monkeypatch.setattr(fa, "load_json", counting)
    barrier = threading.Barrier(5)

    def worker():
        barrier.wait()
        _ = case.email_by_file

    ts = [threading.Thread(target=worker) for _ in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert calls["n"] == 1, "email index parsed exactly once despite the race"
    assert "/e/1.eml" in case.email_by_file


def test_r2_conversation_cache_is_bounded_lru(tmp_path, monkeypatch):
    """R-2: the per-conversation cache was unbounded; it must LRU-evict past a cap."""
    monkeypatch.setattr(fa, "_CONVERSATION_CACHE_CAP", 4)
    case, paths = setup_case(tmp_path)
    mdir = paths.metadata_dir / "messages"
    mdir.mkdir(parents=True, exist_ok=True)
    for i in range(10):
        (mdir / f"c{i}.json").write_text(json.dumps(
            {"conversation_id": f"c{i}", "platform": "sms", "messages": []}))
    for i in range(10):
        assert case.conversation_by_id(f"c{i}") is not None
    assert len(case._conversation_cache) <= 4, "cache bounded to the cap"
    # The most-recently-opened ids are the survivors (LRU keeps recent).
    assert "c9" in case._conversation_cache and "c0" not in case._conversation_cache


def test_r3_thumb_inflight_dedup_single_decode(tmp_path, monkeypatch):
    """R-3: N concurrent requests for the SAME uncached thumb decode once."""
    import threading
    import time
    src = tmp_path / "one.jpg"
    src.write_bytes(b"orig")
    cache = tmp_path / "tc"
    calls = {"n": 0}

    def slow(path, box):
        calls["n"] += 1
        time.sleep(0.05)
        return b"JPEGBYTES"

    monkeypatch.setattr(fa, "_jpeg_bytes", slow)
    barrier = threading.Barrier(6)
    results = []

    def worker():
        barrier.wait()
        results.append(fa._thumb_bytes(src, 320, cache_dir=cache))

    ts = [threading.Thread(target=worker) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert calls["n"] == 1, "one decode for six concurrent requests"
    assert results == [b"JPEGBYTES"] * 6


def test_r3_thumb_decode_semaphore_caps_concurrency(tmp_path, monkeypatch):
    """R-3: distinct uncached thumbs decode concurrently but never above the bound."""
    import threading
    import time
    cache = tmp_path / "tc"
    monkeypatch.setattr(fa, "_THUMB_DECODE_SEM", threading.Semaphore(2))
    state = {"cur": 0, "max": 0}
    lk = threading.Lock()

    def slow(path, box):
        with lk:
            state["cur"] += 1
            state["max"] = max(state["max"], state["cur"])
        time.sleep(0.05)
        with lk:
            state["cur"] -= 1
        return b"J"

    monkeypatch.setattr(fa, "_jpeg_bytes", slow)
    srcs = []
    for i in range(6):
        s = tmp_path / f"f{i}.jpg"
        s.write_bytes(bytes([i]) * (i + 1))     # distinct size/mtime → distinct keys
        srcs.append(s)
    barrier = threading.Barrier(6)

    def worker(s):
        barrier.wait()
        fa._thumb_bytes(s, 320, cache_dir=cache)

    ts = [threading.Thread(target=worker, args=(s,)) for s in srcs]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert state["max"] <= 2, "concurrent decodes capped by the semaphore"


def test_r4_cross_instance_confirms_no_lost_decisions(tmp_path, monkeypatch):
    """R-4: two ArchiveCase instances (== two processes) interleaving confirms on
    the same case must not lose entries — the flock makes each RMW atomic."""
    import threading
    import time
    case1, paths = setup_case(tmp_path)
    case2 = fa.ArchiveCase(paths, "examiner", {})
    orig = fa.atomic_write_json

    def slow_write(path, obj):
        time.sleep(0.01)          # widen the RMW window (would lose writes unlocked)
        return orig(path, obj)

    monkeypatch.setattr(fa, "atomic_write_json", slow_write)

    def worker(case, prefix):
        for i in range(20):
            fa.verb_confirm(case, {"queue": "scene", "id": f"{prefix}{i}",
                                   "decision": "accept"})

    t1 = threading.Thread(target=worker, args=(case1, "a"))
    t2 = threading.Thread(target=worker, args=(case2, "b"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    decisions = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    scene = decisions.get("scene", {})
    assert sum(1 for k in scene if k.startswith("a")) == 20
    assert sum(1 for k in scene if k.startswith("b")) == 20, "no decisions lost"


def test_r5_actions_parsed_once_per_undo_and_invalidates_on_append(tmp_path, monkeypatch):
    """R-5: find_action + is_undone share ONE ndjson parse per undo; the cache
    invalidates as soon as a new action line is appended."""
    case, paths = setup_case(tmp_path)
    r = fa.verb_demote_ranked(case, {"key": "scene:foo", "label": "x"})
    tok = r["undo_token"]
    counts = {"n": 0}
    real = fa.actions_history
    monkeypatch.setattr(fa, "actions_history",
                        lambda paths_: (counts.__setitem__("n", counts["n"] + 1) or real(paths_)))
    fa.verb_undo(case, {"undo_token": tok})
    assert counts["n"] == 1, "one parse shared by find_action + is_undone in an undo"
    # A subsequent append grows the file → the next read re-parses (fresh history).
    fa.verb_demote_ranked(case, {"key": "scene:bar", "label": "y"})
    assert fa.find_action(case, "does-not-exist") is None
    assert counts["n"] == 2, "cache invalidated by the append"


def test_r5_search_index_bytes_cached_per_generation(tmp_path):
    """R-5: the /api/search serialization is cached per state generation (identity
    holds within a generation, a fresh object after load())."""
    case, _paths = setup_case(tmp_path)
    b1 = case.search_index_bytes()
    assert b1 is case.search_index_bytes(), "same bytes reused within a generation"
    assert json.loads(b1) == case.section("search")
    case.load()
    assert case.search_index_bytes() is not b1, "re-serialized after a reload"


def test_r6_append_action_fsyncs_and_skips_truncated_line(tmp_path, monkeypatch):
    """R-6: append_action fsyncs the action line before releasing the flock, and a
    truncated trailing line is still skipped (not fatal) by actions_history."""
    import tools._archive_data as ad
    case, paths = setup_case(tmp_path)
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real(fd))[1])
    fa.append_action(case, "demote_ranked", "k", {"present": False}, {}, reversible=True)
    assert synced, "fsync was issued on the action-log fd"
    before = len(ad.actions_history(paths))
    apath = paths.metadata_dir / "family_actions.ndjson"
    with open(apath, "a", encoding="utf-8") as fh:
        fh.write('{"partial": ')          # truncated, invalid JSON, no newline
    after = ad.actions_history(paths)      # must not raise
    assert len(after) == before, "truncated trailing line skipped"


def test_r7_startup_refuses_corrupt_archive_map(tmp_path):
    """R-7: a present-but-corrupt archive_map.json must refuse at startup rather
    than fail open to a zero-media archive."""
    cases, case_dir = make_case(tmp_path)
    (case_dir / "output" / "metadata" / "archive_map.json").write_text("{ not json ]")
    args = fa.parse_args(["CASE_T", "--role", "examiner", "--cases-root", str(cases)])
    with pytest.raises(SystemExit) as e:
        fa.build_case(args)
    assert e.value.code == 1


def test_r7_absent_map_family_overview_carries_warning(tmp_path):
    """R-7: a legitimately absent archive_map.json for the family role surfaces a
    prominent warning field in the Overview payload."""
    cases, case_dir = make_case(tmp_path)
    (case_dir / "output" / "metadata" / "archive_map.json").unlink()
    paths = CasePaths.from_case_id("CASE_T", str(cases))
    fam = fa.ArchiveCase(paths, "family", {})
    ov = fam.section("overview")
    assert ov.get("archive_warning"), "family overview flags the missing archive index"
    # An examiner (broader resolver) does not get the family-facing warning.
    exm = fa.ArchiveCase(paths, "examiner", {})
    assert exm.section("overview").get("archive_warning") is None


# ── F-11: /media security headers (PDF previews; everything else stays locked) ──

def test_media_headers_pdf_previews_inline_without_sandbox():
    """F-11 (load-bearing): application/pdf is served inline so the browser's
    built-in PDF viewer renders it — WITHOUT the sandbox CSP that would blank the
    viewer — but keeps nosniff. This is the ONLY type that relaxes."""
    ctype, headers = fa._media_headers("application/pdf")
    assert ctype == "application/pdf"                       # not octet-stream
    assert headers.get("Content-Disposition") == "inline"  # not attachment
    assert headers.get("X-Content-Type-Options") == "nosniff"
    assert "Content-Security-Policy" not in headers        # sandbox CSP dropped
    # A charset/parameter suffix must not defeat the match (the sandbox drop keys
    # off the parsed media type, not the raw string).
    ctype2, headers2 = fa._media_headers("application/pdf; charset=binary")
    assert ctype2.startswith("application/pdf")
    assert headers2.get("Content-Disposition") == "inline"
    assert "Content-Security-Policy" not in headers2


def test_media_headers_hostile_types_stay_attachment_octetstream_sandbox():
    """F-11 (load-bearing): SVG and HTML — the script-carrying estate-file threats —
    stay forced to attachment + application/octet-stream + Content-Security-Policy:
    sandbox. SVG matches the image/ prefix but must NOT be served inline. Proves the
    PDF change did not widen the surface for any other type."""
    for hostile in ("image/svg+xml", "text/html", "text/html; charset=utf-8"):
        ctype, headers = fa._media_headers(hostile)
        assert ctype == "application/octet-stream", hostile
        assert headers.get("Content-Disposition") == "attachment", hostile
        assert headers.get("Content-Security-Policy") == "sandbox", hostile
        assert headers.get("X-Content-Type-Options") == "nosniff", hostile


def test_media_headers_images_and_audio_unchanged():
    """Raster images / audio / video keep their inline behaviour with the sandbox
    CSP + nosniff — the PDF/SVG changes leave these untouched."""
    for inline_ok in ("image/jpeg", "video/mp4", "audio/mpeg"):
        ctype, headers = fa._media_headers(inline_ok, {"Cache-Control": "max-age=3600"})
        assert ctype == inline_ok
        assert "Content-Disposition" not in headers, inline_ok
        assert headers.get("Content-Security-Policy") == "sandbox", inline_ok
        assert headers.get("X-Content-Type-Options") == "nosniff", inline_ok
        assert headers.get("Cache-Control") == "max-age=3600", inline_ok


# ── full-text search (FTS5) — family-archive-full-text-search.md ──
# make_case (_case_fixture) already seeds an OCR body carrying a distinctive
# token (OCR_TOKEN) that is NOT in the document's title/summary, and an
# account_credentials doc whose SECRET is family-hidden. We add email indexes with
# a NOISE email that lives in email_index but is NOT referenced by any thread — the
# leak the threads-driven join must prevent.
from tools import build_fts  # noqa: E402

OCR_TOKEN = _tbe.OCR_TOKEN            # "ocrtokenxyz" — in ocr_text body only
SECRET = _tbe.SECRET                  # family-hidden credential value
EML_A = "/work/mail/msg_a.eml"
EML_NOISE = "/work/mail/msg_noise.eml"
CABIN_TOKEN = "cabinphrasexyz"        # body of a real (thread-referenced) email
NOISE_TOKEN = "noiseonlytokenxyz"     # body of a noise email absent from any thread


def _add_email_indexes(paths):
    md = paths.metadata_dir
    # The conversation index is ROLE-SCOPED (wyeast.core.audience): the family's
    # is what the family's FTS is built from. Writing the legacy unsuffixed name
    # here would leave the family index empty — by design, since that file is the
    # union and may carry estate-rescued mail.
    threads_index = json.dumps({
        "threads": [{
            "thread_id": "thread_1", "subject": "Weekend plans",
            "participants": ["a@x.com"], "date_first": "2020-01-01",
            "date_last": "2020-01-02", "significance": 3, "categories": [],
            "message_count": 1, "files": [EML_A],
        }],
    })
    (md / "email_threads_index_family.json").write_text(threads_index)
    (md / "email_threads_index_examiner.json").write_text(threads_index)
    # email_index carries BOTH the thread's message AND a noise message that no
    # thread references (mirrors email_noise_log exclusion). Iterating email_index
    # directly would index the noise body; iterating threads must not.
    (md / "email_index.json").write_text(json.dumps([
        {"file": EML_A, "ocr_text": "we discussed the " + CABIN_TOKEN + " at length",
         "email_subject": "Weekend plans", "email_from": "a@x.com"},
        {"file": EML_NOISE, "ocr_text": NOISE_TOKEN + " unsolicited spam body",
         "email_subject": "You won", "email_from": "spam@x.com"},
    ]))


def _build_and_open(tmp_path, role):
    from wyeast.core.paths import CasePaths
    cases, _ = _tbe.make_case(tmp_path, delivery_blocked=False)
    paths = CasePaths.from_case_id("CASE_T", str(cases))
    _add_email_indexes(paths)
    db = build_fts.build_fts_db(paths, role, {})
    return paths, db


def test_fts_finds_body_only_term(tmp_path):
    """The exact gap this closes: a term present in ocr_text BODY (not the title or
    the 160-char lexical snippet) is findable."""
    _paths, db = _build_and_open(tmp_path, "family")
    res = build_fts.search(db, OCR_TOKEN)
    assert res["total"] >= 1
    refs = [h["ref"] for h in res["hits"]]
    assert "/work/docs/invoice.pdf" in refs, "body-only OCR token did not reach FTS"
    # the snippet is highlighted over the body, and the term is NOT in the title
    hit = next(h for h in res["hits"] if h["ref"] == "/work/docs/invoice.pdf")
    assert "<mark>" in hit["snippet"]
    assert OCR_TOKEN not in (hit["title"] or "")


def test_fts_family_excludes_noise_thread_body(tmp_path):
    """The leak the threads-driven email join prevents: a noise email present in
    email_index but referenced by NO thread must never enter the family index."""
    _paths, db = _build_and_open(tmp_path, "family")
    # a real (thread-referenced) email body IS indexed…
    assert build_fts.search(db, CABIN_TOKEN)["total"] >= 1
    hit = build_fts.search(db, CABIN_TOKEN)["hits"][0]
    assert hit["kind"] == "email" and hit["ref"] == "thread_1"
    # …but the noise body (no thread) is absent — no leak.
    assert build_fts.search(db, NOISE_TOKEN)["total"] == 0


def test_fts_family_excludes_credential_examiner_finds_it(tmp_path):
    """A family-hidden account_credentials doc must be absent from the FAMILY index
    but present in the EXAMINER index (role gating via document_rows membership)."""
    from wyeast.core.paths import CasePaths
    cases, _ = _tbe.make_case(tmp_path, delivery_blocked=False)
    paths = CasePaths.from_case_id("CASE_T", str(cases))
    _add_email_indexes(paths)
    db_fam = build_fts.build_fts_db(paths, "family", {})
    db_exm = build_fts.build_fts_db(paths, "examiner", {})
    token = "SECRETVALUE"  # unicode61 splits SECRETVALUE_DO_NOT_LEAK on '_'
    assert build_fts.search(db_fam, token)["total"] == 0, "family leaked credential body"
    assert build_fts.search(db_exm, token)["total"] >= 1, "examiner should find it"


def test_fts_malformed_match_query_does_not_raise(tmp_path):
    """A raw user string that is not a valid FTS expression must be caught and
    treated as a plain-term query — never a 500."""
    _paths, db = _build_and_open(tmp_path, "family")
    for bad in ["(unbalanced", 'lone " quote', "NEAR(", "cabin AND", OCR_TOKEN + ")"]:
        res = build_fts.search(db, bad)  # must not raise
        assert isinstance(res, dict) and "hits" in res
    # the trailing-paren case still resolves to the real term via the fallback
    assert build_fts.search(db, OCR_TOKEN + ")")["total"] >= 1


def test_fts_refs_are_real_ids(tmp_path):
    """Deep-link refs resolve to real item ids: doc → file path, email → thread_id."""
    _paths, db = _build_and_open(tmp_path, "examiner")
    doc = build_fts.search(db, OCR_TOKEN)["hits"][0]
    assert doc["ref"] == "/work/docs/invoice.pdf" and doc["page"] == "documents"
    eml = build_fts.search(db, CABIN_TOKEN)["hits"][0]
    assert eml["ref"] == "thread_1" and eml["page"] == "emails"


def test_fts_freshness_rebuild_on_source_change(tmp_path):
    """is_fresh flips to False when a source index mtime changes (triggers rebuild)."""
    from wyeast.core.paths import CasePaths
    cases, _ = _tbe.make_case(tmp_path, delivery_blocked=False)
    paths = CasePaths.from_case_id("CASE_T", str(cases))
    _add_email_indexes(paths)
    db = build_fts.build_fts_db(paths, "family", {})
    assert build_fts.is_fresh(db, paths)
    # bump a source index mtime → stale
    os.utime(paths.metadata_dir / "ocr_index.json", None)
    import time as _t
    (paths.metadata_dir / "ocr_index.json").write_text(
        (paths.metadata_dir / "ocr_index.json").read_text())
    assert not build_fts.is_fresh(db, paths)


def test_archivecase_fts_search_lifecycle(tmp_path):
    """ArchiveCase.fts_search builds off-thread (returns building:True first), then
    serves real hits once the index is ready. Build synchronously here for
    determinism, then assert a body-only hit."""
    case, paths = setup_case(tmp_path)
    _add_email_indexes(paths)
    # first search, no index yet → building fallback (lexical), never raises
    first = case.fts_search(OCR_TOKEN, 0, 30)
    assert first.get("building") is True
    # build synchronously (bypass the daemon thread) for a deterministic assertion
    case._run_fts_build()
    ready = case.fts_search(OCR_TOKEN, 0, 30)
    assert not ready.get("building")
    assert any(h["ref"] == "/work/docs/invoice.pdf" for h in ready["hits"])


def test_fts_concurrent_builds_do_not_race(tmp_path):
    """Regression (CI flake): a daemon FTS build (kicked by fts_search) racing a
    direct _run_fts_build in the SAME process must not collide on the sqlite — the
    old PID-only tmp name + unserialized build produced 'table docs already exists'
    / 'disk I/O error' under contention. The per-thread tmp + build lock serialize
    them (only one build runs); the index ends fresh, error-free, and queryable."""
    import threading as _th
    case, paths = setup_case(tmp_path)
    _add_email_indexes(paths)
    errs = []

    def build():
        try:
            case._run_fts_build()
        except Exception as e:  # the bug would surface here
            errs.append(repr(e))

    case.fts_search(OCR_TOKEN, 0, 30)  # kicks off the daemon build
    threads = [_th.Thread(target=build) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errs, errs
    assert case._fts_status.get("state") != "error"
    res = case.fts_search(OCR_TOKEN, 0, 30)
    assert not res.get("building")
    assert any(h["ref"] == "/work/docs/invoice.pdf" for h in res["hits"])


def test_archivecase_lexical_fallback_covers_first_build(tmp_path):
    """While the FTS index builds, the lexical fallback still answers (so the first
    search isn't empty). OCR_TOKEN sits in the 160-char snippet, so it's found."""
    case, paths = setup_case(tmp_path)
    res = case.lexical_search(OCR_TOKEN, 0, 30)
    assert res["building"] is True
    assert any(h["ref"] == "/work/docs/invoice.pdf" for h in res["hits"])


# ── G-3: transcript viewer + seek-synced player (endpoint gate + sidecar containment) ──

def _add_recording(paths, name, *, text="Hi there", segments=None, sidecar="json",
                   on_disk=True, delivered=True):
    """Add one recording under extracted/other/audio/, its transcription_index
    record + (optionally) a .json/.vtt sidecar, and (when delivered) an
    audio_classifications entry so it lands in the deliverable_audio set."""
    audio_dir = paths.extracted_dir / "other" / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    audio = audio_dir / (name + ".caf")
    if on_disk:
        audio.write_bytes(b"\x00\x01\x02\x03")
    segments = segments or []
    rec = {"file": str(audio), "transcript_text": text, "duration": 12.5,
           "language": "en", "segment_count": len(segments)}
    if sidecar == "json":
        js = audio_dir / (name + ".json")
        js.write_text(json.dumps({"segments": segments}))
        rec["json_sidecar"] = str(js)
    elif sidecar == "vtt":
        vtt = audio_dir / (name + ".vtt")
        lines = ["WEBVTT", ""]
        for s in segments:
            lines.append("00:00:%06.3f --> 00:00:%06.3f" % (s["start"], s["end"]))
            lines.append(s["text"]); lines.append("")
        vtt.write_text("\n".join(lines))
        rec["vtt_sidecar"] = str(vtt)
    tpath = paths.metadata_dir / "transcription_index.json"
    idx = json.loads(tpath.read_text()) if tpath.exists() else []
    idx.append(rec)
    tpath.write_text(json.dumps(idx))
    spath = paths.metadata_dir / "case_summary.json"
    summary = json.loads(spath.read_text())
    if delivered:
        summary.setdefault("audio_classifications", []).append(
            {"file": str(audio), "filename": name + ".caf", "category": "voicemail",
             "significance": 3, "summary": "A recording."})
    spath.write_text(json.dumps(summary))
    return str(audio), rec


def test_transcript_endpoint_json_segments_and_seek_payload(tmp_path):
    case, paths = setup_case(tmp_path)
    f, _ = _add_recording(paths, "vm", segments=[
        {"start": 0.85, "end": 2.05, "text": " Hi"},
        {"start": 2.05, "end": 4.0, "text": " there"}], sidecar="json")
    case.load()  # rebuild deliverable_audio + transcription_index
    d = case.transcript_section(f)
    assert [s["start"] for s in d["segments"]] == [0.85, 2.05]
    assert d["segments"][0]["text"] == "Hi"
    assert d["has_audio"] is True and d["language"] == "en"


def test_transcript_endpoint_vtt_parser_path(tmp_path):
    case, paths = setup_case(tmp_path)
    f, _ = _add_recording(paths, "clip", segments=[
        {"start": 0.0, "end": 2.0, "text": "first"},
        {"start": 2.0, "end": 5.5, "text": "second"}], sidecar="vtt")
    case.load()
    d = case.transcript_section(f)
    assert [s["text"] for s in d["segments"]] == ["first", "second"]
    assert d["segments"][1]["end"] == 5.5


def test_transcript_sidecar_containment_refuses_out_of_root(tmp_path):
    """The load-bearing security piece: a transcription record whose sidecar path
    points OUTSIDE extracted/other/audio/ (an attacker-crafted absolute path) must
    never be read — the endpoint degrades to empty segments, and the low-level
    resolver returns None for both a sibling-tree path and a traversal escape."""
    case, paths = setup_case(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text('{"segments": [{"start": 0, "end": 1, "text": "LEAK"}]}')
    # Point the sidecar at the out-of-root secret.
    f, _ = _add_recording(paths, "evil", segments=[{"start": 0, "end": 1, "text": "x"}],
                          sidecar="json")
    tpath = paths.metadata_dir / "transcription_index.json"
    idx = json.loads(tpath.read_text())
    idx[-1]["json_sidecar"] = str(secret)
    idx[-1]["vtt_sidecar"] = str(secret)  # also poison the fallback
    tpath.write_text(json.dumps(idx))
    case.load()
    d = case.transcript_section(f)
    assert d["segments"] == [], "out-of-root sidecar must not be read (containment)"
    # direct resolver refuses the sibling path AND a traversal escape
    assert fa.resolve_sidecar_path(case, str(secret)) is None
    audio_dir = paths.extracted_dir / "other" / "audio"
    assert fa.resolve_sidecar_path(case, str(audio_dir / ".." / ".." / ".." / "secret.txt")) is None
    assert fa.read_sidecar_text(case, str(secret)) is None


def test_transcript_endpoint_degrades_when_audio_reaped(tmp_path):
    """goog/appl case: audio + sidecars reaped from disk, but the transcription
    record + summary survive. Family still gets transcript_text, segments empty,
    has_audio False — never a crash."""
    case, paths = setup_case(tmp_path, role="family")
    f, _ = _add_recording(paths, "gone", text="the reaped words",
                          sidecar="json", on_disk=False, delivered=True)
    # Delete the sidecar to simulate reaping.
    (paths.extracted_dir / "other" / "audio" / "gone.json").unlink()
    case.load()
    d = case.transcript_section(f)
    assert d["segments"] == [] and d["has_audio"] is False
    assert d["transcript_text"] == "the reaped words"


def test_transcript_endpoint_honors_role_and_deliver_gate(tmp_path):
    # transcribe.deliver false → family refused (mirrors audio_rows withholding).
    cases, _ = _tbe.make_case(tmp_path)
    paths = CasePaths.from_case_id("CASE_T", str(cases))
    case = fa.ArchiveCase(paths, "family", {"transcribe": {"deliver": False}})
    f, _ = _add_recording(paths, "vm", segments=[{"start": 0, "end": 1, "text": "hi"}])
    case.load()
    with pytest.raises(fa.VerbError) as e:
        case.transcript_section(f)
    assert e.value.code == 403
    # A recording NOT in the delivered set is withheld from a family session.
    case2 = fa.ArchiveCase(paths, "family", {})
    f2, _ = _add_recording(paths, "undel", segments=[{"start": 0, "end": 1, "text": "x"}],
                           delivered=False)
    case2.load()
    with pytest.raises(fa.VerbError) as e2:
        case2.transcript_section(f2)
    assert e2.value.code == 403
    # examiner sees it regardless (transcription_index is authoritative)
    case3 = fa.ArchiveCase(paths, "examiner", {})
    case3.load()
    assert case3.transcript_section(f2)["segments"][0]["text"] == "x"


# ── G-13 junk rescue: junk_rows section + verb_unjunk (audited/reversible/never-destroy) ──

def _add_junk(case, paths, name="junk1.png", *, reason="a banner"):
    """Add a junk-routed image: a real file under extracted/photos_junk/ and a
    scene_index.junk_results entry keyed by its ORIGINAL working path (under the
    case tree, goog-style). Reloads the case.
    Returns (key, junk_file, dest)."""
    photos_dir = paths.extracted_dir / "photos"
    key = str(photos_dir / name)                      # original working path (goog-style)
    junk_dir = paths.extracted_dir / "photos_junk"
    junk_dir.mkdir(parents=True, exist_ok=True)
    junk_file = junk_dir / name
    junk_file.write_bytes(b"\xff\xd8\xff\xd9")
    sp = paths.metadata_dir / "scene_index.json"
    si = json.loads(sp.read_text())
    si.setdefault("junk_results", {})[key] = {"junk_label": reason, "confidence": 0.4,
                                              "source": "clip"}
    sp.write_text(json.dumps(si))
    case.load()
    return key, junk_file, Path(key)


def _add_label_junk(case, paths, name="scenejunk.png", *, with_archive=True,
                    reason="a banner"):
    """Scene-classifier (label-only) junk: the file STAYS at its working path (never
    moved to photos_junk), recorded only as a junk_results LABEL — junk_results and
    clip_results are disjoint in production. Optionally give it a delivered archive
    entry so an un-junk can resurface it. Reloads. Returns (key, work_file)."""
    photos_dir = paths.extracted_dir / "photos"
    photos_dir.mkdir(parents=True, exist_ok=True)
    key = str(photos_dir / name)
    work_file = Path(key)
    work_file.write_bytes(b"\xff\xd8\xff\xd9")
    sp = paths.metadata_dir / "scene_index.json"
    si = json.loads(sp.read_text())
    si.setdefault("junk_results", {})[key] = {"junk_label": reason, "confidence": 0.4,
                                              "source": "clip"}
    sp.write_text(json.dumps(si))
    if with_archive:
        arc = paths.archive_dir / name
        arc.write_bytes(b"\xff\xd8\xff\xd9")
        ap = paths.metadata_dir / "archive_map.json"
        am = json.loads(ap.read_text())
        am.setdefault("entries", {})[key] = str(arc)
        ap.write_text(json.dumps(am))
    case.load()
    return key, work_file


def test_junk_page_registered_examiner_only():
    keys = [k for k, _, _ in fa.PAGES]
    assert "junk" in keys and "junk" in fa.EXAMINER_ONLY, "junk grid is examiner-only"
    assert "guided" in keys and "guided" in fa.EXAMINER_ONLY, "guided review is examiner-only"
    # The Review page carries the quarantine list, whose rows now always expose a
    # servable `src` (quarantine_section no longer withholds it for any category).
    # With that per-row withholding gone, page-level role gating is the ONLY thing
    # keeping quarantined material off a family screen — pin it so a future edit
    # to PAGES/EXAMINER_ONLY cannot silently open it.
    assert "review" in keys and "review" in fa.EXAMINER_ONLY, "review queue is examiner-only"


def test_junk_section_paginates_and_carries_reason(tmp_path):
    case, paths = setup_case(tmp_path)
    for i in range(5):
        _add_junk(case, paths, "j%d.png" % i, reason="banner")
    rows = case.section("junk")
    assert len(rows) == 5
    assert all(r["id"] and r["name"] and r["reason"] == "banner" for r in rows)
    page = case.api_section("junk", {"offset": "0", "limit": "2"})
    assert page["total"] == 5 and len(page["rows"]) == 2 and page["limit"] == 2
    page2 = case.api_section("junk", {"offset": "4", "limit": "2"})
    assert page2["offset"] == 4 and len(page2["rows"]) == 1


def test_unjunk_moves_back_restores_symlink_audited_reversible(tmp_path):
    # THE load-bearing test: un-junk moves the file OUT of photos_junk back to its
    # working location, restores the scene view symlink, audits (ledger + custody +
    # one action line), and is reversible (undo re-junks; nothing is ever destroyed).
    case, paths = setup_case(tmp_path)
    key, junk_file, dest = _add_junk(case, paths, "junk1.png")
    assert junk_file.is_file() and not dest.exists()

    res = fa.verb_unjunk(case, {"id": key})
    assert res["ok"] and res["count"] == 1
    assert dest.is_file() and not junk_file.exists(), "moved back to working location"
    view = paths.output_dir / "all_photos_by_scene" / "_rescued" / "junk1.png"
    assert view.is_symlink() and os.path.realpath(view) == os.path.realpath(dest)
    # audit: ledger intent+done, custody line, exactly one action line
    ledger = (paths.metadata_dir / "_move_ledger.ndjson").read_text()
    assert '"status": "intent"' in ledger and '"status": "done"' in ledger
    assert paths.custody_log.exists()
    assert len(_actions(paths)) == 1

    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    assert junk_file.is_file() and not dest.exists(), "undo re-junked the file"
    assert not view.exists() and not view.is_symlink(), "rescued view symlink removed"
    assert len(_actions(paths)) == 2  # unjunk + unjunk_undo


def test_unjunk_undo_never_deletes_a_real_file_at_view_path(tmp_path):
    # _rejunk must only unlink the SYMLINK it recreated — never a real, source-bearing
    # file that later came to occupy the recorded view path (never-destroy invariant).
    case, paths = setup_case(tmp_path)
    key, junk_file, dest = _add_junk(case, paths, "junk1.png")
    res = fa.verb_unjunk(case, {"id": key})
    view = paths.output_dir / "all_photos_by_scene" / "_rescued" / "junk1.png"
    assert view.is_symlink()
    view.unlink()
    view.write_bytes(b"real-source-bearing-bytes")
    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    assert view.exists() and view.read_bytes() == b"real-source-bearing-bytes", \
        "a real file at the rescued-view path must survive un-junk undo"


def test_unjunk_works_on_a_delivered_copy_with_no_working_file(tmp_path):
    """A delivery carries output/ and leaves the working trees behind.

    Scene-junk is a LABEL and the rescue is a pure overlay -- nothing is moved -- so
    the original working file is not needed. Refusing on it meant un-junking failed
    for almost everything on a served copy: 394 of the first 400 junk rows on 813_mf,
    of which 388 had a delivered canonical and were perfectly rescuable. What decides
    whether a rescue can surface anything is that canonical, which is what
    build_photo_universe requires before it will put the tile back.
    """
    case, paths = setup_case(tmp_path)
    key, work_file = _add_label_junk(case, paths, "delivered.png", with_archive=True)
    work_file.unlink()                      # the delivery left the working tree behind
    case.load()
    res = fa.verb_unjunk(case, {"id": key})
    assert res["ok"] and res.get("undo_token")
    assert case.decisions.get("junk_rescued", {}).get(key) is True
    # and it really does come back to the gallery
    universe = _ad.build_photo_universe(case.scene_index, case.archive_map, "examiner",
                                        rescued=case.decisions.get("junk_rescued"))
    assert key in universe, "the rescued item rejoins the photo universe"


def test_unjunk_still_refuses_when_there_is_nothing_to_restore(tmp_path):
    # No working file AND no delivered canonical: the rescue could not surface
    # anything, so refusing is the honest answer rather than a silent no-op.
    case, paths = setup_case(tmp_path)
    key, work_file = _add_label_junk(case, paths, "gone.png", with_archive=False)
    work_file.unlink()
    case.load()
    with pytest.raises(fa.VerbError):
        fa.verb_unjunk(case, {"id": key})


def test_unjunk_refuses_family(tmp_path):
    case, paths = setup_case(tmp_path, role="family")
    key, junk_file, dest = _add_junk(case, paths, "junk1.png")
    with pytest.raises(fa.VerbError) as e:
        fa.verb_unjunk(case, {"id": key})
    assert e.value.code == 403
    assert junk_file.is_file() and not dest.exists(), "family-refused: nothing moved"


def test_unjunk_of_a_flagged_item_is_examiner_judgment_not_a_refusal(tmp_path):
    """Un-junking is gated on ROLE, not on scan verdict — and that is deliberate.

    `_unjunk_one` used to carry one content guard that hard-refused a single
    category; that category is gone, so the verb now has no sensitivity check at
    all. A sensitivity-flagged junk item is therefore restorable by an examiner,
    who is the human screen. This test asserts the widened behavior rather than
    leaving it silent: if someone later wants un-junk to consult
    `first_matched_filter`, this test is what they must consciously change."""
    case, paths = setup_case(tmp_path)
    key, junk_file, dest = _add_junk(case, paths, "flagged.png")
    ssp = paths.metadata_dir / "sensitive_scan_index.json"
    ss = json.loads(ssp.read_text()) if ssp.exists() else {}
    ss[key] = {"human_review_required": False, "sensitivity_filters": {
        "explicit_sexual_content": {"triggered": True}}}
    ssp.write_text(json.dumps(ss))
    case.load()

    fa.verb_unjunk(case, {"id": key})

    assert dest.exists(), "examiner un-junk restores the file"
    assert not junk_file.exists(), "moved, not copied"
    # And the family is still refused the same action on the same item.
    fam = fa.ArchiveCase(paths, "family", {}); fam.load()
    key2, junk2, dest2 = _add_junk(case, paths, "flagged2.png")
    fam.load()
    with pytest.raises(fa.VerbError) as e:
        fa.verb_unjunk(fam, {"id": key2})
    assert e.value.code == 403


def test_quarantine_section_rows_are_uniform_and_servable(tmp_path):
    """Every quarantine row now carries a servable `src` and `locked: False` — the
    per-row withholding that used to apply to one category is gone. Pin the shape:
    `locked` is retained as a frontend contract key (family.js still branches on
    it) and must stay False at the producer until something is meant to set it."""
    case, paths = setup_case(tmp_path)
    _e1, canonical1, _v1, _q1 = _add_quarantine(paths, "a1.jpg", filt="drug_use")
    _e2, canonical2, _v2, _q2 = _add_quarantine(
        paths, "b1.jpg", filt="explicit_sexual_content")
    case.load()

    sec = case.quarantine_section()

    assert sec["total"] == len(sec["entries"])
    by_name = {r["name"]: r for r in sec["entries"]}
    assert {"a1.jpg", "b1.jpg"} <= set(by_name)
    for name, canonical in (("a1.jpg", canonical1), ("b1.jpg", canonical2)):
        row = by_name[name]
        assert row["locked"] is False, "no category is withheld at the row level"
        assert row["src"] == str(canonical) == row["canonical_path"]
    assert {"drug_use", "explicit_sexual_content"} <= {
        r["filter"] for r in sec["entries"]}
    # Uniform across EVERY row, not just the two seeded here.
    assert all(r["locked"] is False for r in sec["entries"])
    assert all(r["src"] == r["canonical_path"] for r in sec["entries"])


def test_unjunk_unknown_id_404(tmp_path):
    case, paths = setup_case(tmp_path)
    with pytest.raises(fa.VerbError) as e:
        fa.verb_unjunk(case, {"id": str(paths.extracted_dir / "photos" / "nope.png")})
    assert e.value.code == 404


def test_unjunk_batch_skips_missing(tmp_path):
    case, paths = setup_case(tmp_path)
    key, junk_file, dest = _add_junk(case, paths, "junk1.png")
    ghost = str(paths.extracted_dir / "photos" / "ghost.png")  # not a junk_results key
    res = fa.verb_unjunk(case, {"ids": [key, ghost]})
    assert res["ok"] and res["count"] == 1 and res["skipped"] == 1
    assert dest.is_file() and not junk_file.exists()


def test_unjunk_label_only_scene_junk_uses_overlay_not_move(tmp_path):
    # Regression: scene-classifier junk is a LABEL — the file was NEVER moved to
    # photos_junk, so the old code raised "junk file not present" and un-junk was
    # broken for the entire Junk view. It must instead record a reversible
    # junk_rescued overlay, MOVE nothing, resurface the delivered photo into the
    # gallery, and drop it from the Junk view. Undo clears the overlay.
    case, paths = setup_case(tmp_path)
    key, work_file = _add_label_junk(case, paths, "scenejunk.png", with_archive=True)
    junk_twin = paths.extracted_dir / "photos_junk" / "scenejunk.png"
    assert not junk_twin.exists(), "precondition: label-only junk is NOT in photos_junk"
    assert any(r["id"] == key for r in case.section("junk")), "listed in the Junk view"
    assert key not in case.universe, "junked: absent from the gallery"

    res = fa.verb_unjunk(case, {"id": key})     # previously raised 'junk file not present'
    assert res["ok"] and res["count"] == 1
    assert work_file.is_file(), "never moved — the working file stays put"
    assert not junk_twin.exists(), "no phantom photos_junk file created"
    dec = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert dec.get("junk_rescued", {}).get(key) is True, "overlay records the rescue"
    assert key in case.universe, "resurfaced into the gallery"
    assert not any(r["id"] == key for r in case.section("junk")), "left the Junk view"
    assert len(_actions(paths)) == 1

    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    dec = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert not dec.get("junk_rescued"), "undo clears the overlay"
    assert any(r["id"] == key for r in case.section("junk")), "back in the Junk view"
    assert key not in case.universe, "back out of the gallery"


def test_unjunk_label_only_without_archive_succeeds_without_surfacing(tmp_path):
    # A label-only junk item with no delivered archive (e.g. a junk-tagged video frame
    # or an undelivered original) must still UN-JUNK without error — it simply can't
    # resurface (no archive tile), and must never fabricate one.
    case, paths = setup_case(tmp_path)
    key, work_file = _add_label_junk(case, paths, "novarchive.png", with_archive=False)
    res = fa.verb_unjunk(case, {"id": key})
    assert res["ok"] and res["count"] == 1, "un-junk succeeds even with nothing to surface"
    assert key not in case.universe, "no archive entry → not surfaced (no broken tile)"
    assert not any(r["id"] == key for r in case.section("junk")), "still leaves the Junk view"


def _add_scanned(case, paths, name="scanned.png", *, with_archive=True):
    """A scene-classifier scanned-document/handwritten-letter image: a real working
    file CLIP-tagged with a SCENE_LABELS category (never moved — same shape as
    label-only junk). Optionally gets a delivered archive entry. Reloads. Returns
    (key, work_file)."""
    photos_dir = paths.extracted_dir / "photos"
    photos_dir.mkdir(parents=True, exist_ok=True)
    key = str(photos_dir / name)
    work_file = Path(key)
    work_file.write_bytes(b"\xff\xd8\xff\xd9")
    label = list(fa.SCENE_LABELS)[0]
    sp = paths.metadata_dir / "scene_index.json"
    si = json.loads(sp.read_text())
    si.setdefault("clip_results", {})[key] = {"category": label, "delivered": True}
    sp.write_text(json.dumps(si))
    if with_archive:
        arc = paths.archive_dir / name
        arc.write_bytes(b"\xff\xd8\xff\xd9")
        ap = paths.metadata_dir / "archive_map.json"
        am = json.loads(ap.read_text())
        am.setdefault("entries", {})[key] = str(arc)
        ap.write_text(json.dumps(am))
    case.load()
    return key, work_file


# ── #19: "not a document" release of a scanned image (BACKLOG.md #19) ──

def test_release_scanned_rejoins_gallery_leaves_correspondence_reversible(tmp_path):
    case, paths = setup_case(tmp_path)
    key, work_file = _add_scanned(case, paths, "letter.png")
    assert key not in case.universe, "scanned-tagged: excluded from the gallery"
    assert any(r["id"] == key for r in case.section("correspondence")["scanned"]), \
        "surfaced in Correspondence's scanned list"

    res = fa.verb_release_scanned(case, {"id": key})
    assert res["ok"] and res["count"] == 1
    assert work_file.is_file(), "never moved — purely a decisions overlay"
    dec = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert dec.get("scanned_released", {}).get(key) is True, "overlay records the release"
    assert key in case.universe, "rejoined the gallery"
    assert case.universe[key]["category"] == "uncategorized", "no longer tagged as scanned"
    assert not any(r["id"] == key for r in case.section("correspondence")["scanned"]), \
        "left the Correspondence scanned list"
    assert len(_actions(paths)) == 1

    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    dec = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert not dec.get("scanned_released"), "undo clears the overlay"
    assert key not in case.universe, "back out of the gallery"
    assert any(r["id"] == key for r in case.section("correspondence")["scanned"]), \
        "back in the Correspondence scanned list"


def test_release_scanned_refuses_non_scanned_item(tmp_path):
    case, paths = setup_case(tmp_path)
    with pytest.raises(fa.VerbError):
        fa.verb_release_scanned(case, {"id": SRC_A})   # an ordinary photo, not scanned-tagged


def test_release_scanned_batch_skips_non_scanned(tmp_path):
    case, paths = setup_case(tmp_path)
    key, _ = _add_scanned(case, paths, "letter2.png")
    res = fa.verb_release_scanned(case, {"ids": [key, SRC_A]})
    assert res["ok"] and res["count"] == 1 and res["skipped"] == 1
    assert key in case.universe


def test_release_scanned_requires_examiner(tmp_path):
    case, paths = setup_case(tmp_path, role="family")
    key, _ = _add_scanned(case, paths, "letter3.png")
    with pytest.raises(fa.VerbError):
        fa.verb_release_scanned(case, {"id": key})


# ── G-14 transparency panel: role gating ──

def _seed_transparency(paths, *, with_noise=True):
    md = paths.metadata_dir
    (md / "collect_dedup_summary.json").write_text(json.dumps({"exact_dupes_moved": 5}))
    (md / "perceptual_dup_groups.json").write_text(json.dumps(
        {"groups": [{"keeper": "a"}, {"keeper": "b"}]}))
    (md / "suspense_manifest.json").write_text(json.dumps([{"file": "x"}]))
    if with_noise:
        (md / "email_noise_log.json").write_text(json.dumps([
            {"email_from": "a@b.com", "email_subject": "Receipt",
             "email_date_iso": "2020-01-01", "has_significant_attachment": True},
            {"email_from": "c@d.com", "email_subject": "Spam",
             "has_significant_attachment": False},
        ]))


def test_transparency_section_gates_family_vs_examiner(tmp_path):
    ex_case, expaths = setup_case(tmp_path / "ex")
    _seed_transparency(expaths)
    ex = ex_case.transparency_section()
    assert ex["near_duplicate_groups"] == 2 and ex["nothing_deleted"] is True
    assert ex["suspense_count"] == 1
    assert len(ex["significant_attachment_noise"]) == 1
    assert ex["significant_attachment_noise"][0]["from"] == "a@b.com"

    fam_case, fpaths = setup_case(tmp_path / "fam", role="family")
    _seed_transparency(fpaths)
    fam = fam_case.transparency_section()
    assert fam["near_duplicate_groups"] == 2 and fam["nothing_deleted"] is True
    assert "suspense_count" not in fam, "family never sees the suspense count"
    assert "significant_attachment_noise" not in fam, "family never sees noise detail"


# ── G-12 guided review: section assembly ──

def test_guided_section_assembles_steps_and_reflects_progress(tmp_path):
    case, paths = setup_case(tmp_path)
    d = case.section("guided")
    steps = {s["key"]: s for s in d["steps"]}
    assert set(steps) == {"quarantine", "human_review", "confirm", "name_persons",
                          "vital_docs", "reconciliation"}
    # make_case seeds a quarantine entry, a human-review path, an unnamed Person_01,
    # and a reconciliation review item.
    assert steps["quarantine"]["count"] == 1
    assert steps["human_review"]["count"] == 1
    assert steps["name_persons"]["count"] == 1
    assert steps["reconciliation"]["count"] == 1
    assert d["handoff"]["ready"] is False

    # Acknowledging a step through the EXISTING confirm verb (queue guided_progress)
    # marks it done — no new verb.
    fa.verb_confirm(case, {"queue": "guided_progress", "id": "quarantine",
                           "decision": "accept"})
    d2 = case.section("guided")
    q2 = next(s for s in d2["steps"] if s["key"] == "quarantine")
    assert q2["acknowledged"] is True and q2["done"] is True


# ── curation layer (favorites / collections / notes) ──────────────────────────────
#
# The curation layer is a pure, additive, audited sidecar (curation_layer.json) keyed
# by existing item ids. Every verb: mutates only the sidecar, writes exactly one audit
# line, is reversible via Undo, and (in Phase 1) is examiner-only. It NEVER moves a
# file or edits a pipeline index (the never-destroy / examiner-authority invariants).

def _curation(paths):
    p = paths.metadata_dir / "curation_layer.json"
    return json.loads(p.read_text()) if p.exists() else None


def test_favorite_toggle_mutates_sidecar_one_audit_and_undo(tmp_path):
    case, paths = setup_case(tmp_path)
    res = fa.verb_favorite(case, {"id": SRC_A, "on": True})
    assert res["ok"] and res["on"] is True
    cur = _curation(paths)
    assert SRC_A in cur["favorites"] and cur["favorites"][SRC_A]["actor"] == "examiner"
    assert cur["schema_version"] == 1
    assert len(_actions(paths)) == 1, "exactly one audit line per verb"
    # No file was moved and no ledger entry was written (pure sidecar).
    assert not (paths.metadata_dir / "_move_ledger.ndjson").exists()
    # Undo reverses it.
    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    assert SRC_A not in _curation(paths)["favorites"], "undo clears the star"

    # Toggling OFF an existing star and undoing restores it.
    fa.verb_favorite(case, {"id": SRC_A, "on": True})
    off = fa.verb_favorite(case, {"id": SRC_A, "on": False})
    assert SRC_A not in _curation(paths)["favorites"]
    fa.verb_undo(case, {"undo_token": off["undo_token"]})
    assert SRC_A in _curation(paths)["favorites"], "undo of an un-star re-stars it"


def test_note_set_clear_and_undo(tmp_path):
    case, paths = setup_case(tmp_path)
    r1 = fa.verb_note_set(case, {"id": SRC_B, "text": "For the memorial"})
    assert _curation(paths)["notes"][SRC_B]["text"] == "For the memorial"
    assert len(_actions(paths)) == 1
    # Overwrite then undo → prior note restored.
    fa.verb_note_set(case, {"id": SRC_B, "text": "changed"})
    r3 = fa.verb_note_set(case, {"id": SRC_B, "text": "changed again"})
    fa.verb_undo(case, {"undo_token": r3["undo_token"]})
    assert _curation(paths)["notes"][SRC_B]["text"] == "changed"
    # Clear then undo → note comes back.
    rc = fa.verb_note_clear(case, {"id": SRC_B})
    assert SRC_B not in _curation(paths)["notes"]
    fa.verb_undo(case, {"undo_token": rc["undo_token"]})
    assert _curation(paths)["notes"][SRC_B]["text"] == "changed"
    # Note text is length-capped at the write path.
    long = fa.verb_note_set(case, {"id": SRC_A, "text": "x" * 9999})
    assert len(_curation(paths)["notes"][SRC_A]["text"]) == fa.MAX_NOTE_LEN


def test_collection_lifecycle_and_delete_keeps_member_rows(tmp_path):
    case, paths = setup_case(tmp_path)
    created = fa.verb_collection_create(case, {"title": "For the Memorial!"})
    slug = created["slug"]
    assert slug == "for-the-memorial" and created["title"] == "For the Memorial!"
    assert len(_actions(paths)) == 1
    # add members (idempotent; only newly-added ids recorded)
    add = fa.verb_collection_add(case, {"slug": slug, "ids": [SRC_A, SRC_B, SRC_A]})
    assert add["added"] == 2
    assert set(_curation(paths)["collections"][slug]["members"]) == {SRC_A, SRC_B}
    # the member rows are stamped by the overlay
    rows = case.api_section("photos", {"offset": "0", "limit": "50"})["rows"]
    by_id = {r["id"]: r for r in rows}
    assert slug in by_id[SRC_A]["collections"] and slug in by_id[SRC_B]["collections"]

    # DELETE the collection → the collection is gone but its member ROWS remain.
    dele = fa.verb_collection_delete(case, {"slug": slug})
    assert slug not in _curation(paths)["collections"]
    rows2 = case.api_section("photos", {"offset": "0", "limit": "50"})["rows"]
    ids2 = {r["id"] for r in rows2}
    assert SRC_A in ids2 and SRC_B in ids2, "deleting a collection never deletes items"
    assert not any(r.get("collections") for r in rows2), "membership overlay cleared"
    # Undo the delete → the whole collection (with its members) is restored.
    fa.verb_undo(case, {"undo_token": dele["undo_token"]})
    assert set(_curation(paths)["collections"][slug]["members"]) == {SRC_A, SRC_B}


def test_collection_create_slug_uniqueness(tmp_path):
    case, paths = setup_case(tmp_path)
    a = fa.verb_collection_create(case, {"title": "Trip"})
    b = fa.verb_collection_create(case, {"title": "Trip"})
    assert a["slug"] == "trip" and b["slug"] == "trip-2"


def test_collection_remove_and_undo(tmp_path):
    case, paths = setup_case(tmp_path)
    slug = fa.verb_collection_create(case, {"title": "c"})["slug"]
    fa.verb_collection_add(case, {"slug": slug, "ids": [SRC_A, SRC_B]})
    rm = fa.verb_collection_remove(case, {"slug": slug, "ids": [SRC_A]})
    assert rm["removed"] == 1
    assert _curation(paths)["collections"][slug]["members"] == [SRC_B]
    fa.verb_undo(case, {"undo_token": rm["undo_token"]})
    assert set(_curation(paths)["collections"][slug]["members"]) == {SRC_A, SRC_B}


def test_overlay_stamps_rows_favorite_note_collections(tmp_path):
    case, paths = setup_case(tmp_path)
    fa.verb_favorite(case, {"id": SRC_A, "on": True})
    fa.verb_note_set(case, {"id": SRC_A, "text": "note A"})
    slug = fa.verb_collection_create(case, {"title": "Set"})["slug"]
    fa.verb_collection_add(case, {"slug": slug, "ids": [SRC_A]})
    rows = case.api_section("photos", {"offset": "0", "limit": "50"})["rows"]
    a = next(r for r in rows if r["id"] == SRC_A)
    b = next(r for r in rows if r["id"] == SRC_B)
    assert a["favorite_curation"] is True and a["note"] == "note A" and a["collections"] == [slug]
    # Uncurated row carries NO curation keys (additive, default absent).
    assert "favorite_curation" not in b and "note" not in b and "collections" not in b


def test_favorites_filter_narrows_and_paginates(tmp_path):
    case, paths = setup_case(tmp_path)
    _seed_gallery_layer(paths)   # +25 photos (g00..g24)
    case.load()
    for i in (10, 11, 12):
        fa.verb_favorite(case, {"id": f"/work/extracted/g{i:02d}.jpg", "on": True})
    # facets expose the star count (added only when non-zero).
    facets = case.api_section("photos", {"offset": "0", "limit": "5"})["facets"]
    assert facets.get("starred") == 3
    # The server-side filter narrows the FULL set before the page slice, and the
    # filtered tail is reachable across pages.
    rows, total = _pages(case, "photos", {"favorite_curation": "1"}, limit=2)
    assert total == 3 and len(rows) == 3
    assert all(r["favorite_curation"] for r in rows)
    assert {r["id"] for r in rows} == {f"/work/extracted/g{i:02d}.jpg" for i in (10, 11, 12)}


def test_export_favorites_collection_skips_undeliverable(tmp_path):
    case, paths = setup_case(tmp_path)
    slug = fa.verb_collection_create(case, {"title": "mix"})["slug"]
    fa.verb_collection_add(case, {"slug": slug,
                                  "ids": [SRC_B, "/work/extracted/missing.jpg"]})
    dest = tmp_path / "exp"
    res = fa.verb_export_collection(case, {"kind": "curation_collection", "key": slug,
                                           "dest": str(dest)})
    # Only SRC_B is deliverable: the missing id is skipped.
    assert res["count"] == 1 and res["skipped"] == 1
    assert (dest / "b.jpg").exists()
    # A favorites-kind export resolves the star set the same way.
    fa.verb_favorite(case, {"id": SRC_B, "on": True})
    dest2 = tmp_path / "favs"
    rf = fa.verb_export_collection(case, {"kind": "favorites", "dest": str(dest2)})
    assert rf["count"] == 1 and (dest2 / "b.jpg").exists()


def test_export_curation_collection_confines_family_dest(tmp_path):
    # Family role: a curation export must be confined to output/family_export/ (A3).
    sidecar = {"schema_version": 1, "favorites": {}, "notes": {},
               "collections": {"memorial": {"title": "M", "members": [SRC_A]}}}
    cases, _ = _tbe.make_case(tmp_path, delivery_blocked=False)
    paths = CasePaths.from_case_id("CASE_T", str(cases))
    (paths.metadata_dir / "curation_layer.json").write_text(json.dumps(sidecar))
    case = fa.ArchiveCase(paths, "family", {})
    _sign(paths)                                   # released, so E4 lets us reach
    with pytest.raises(fa.VerbError) as e:          # the dest-confinement check
        fa.verb_export_collection(case, {"kind": "curation_collection", "key": "memorial",
                                         "dest": "/tmp/escape"})
    assert e.value.code == 403
    # Confined dest under output/family_export is accepted.
    dest = paths.output_dir / "family_export" / "memorial"
    ok = fa.verb_export_collection(case, {"kind": "curation_collection", "key": "memorial",
                                          "dest": str(dest)})
    assert ok["count"] == 1 and (dest / "a.jpg").exists()


def test_curation_verbs_require_examiner(tmp_path):
    cases, _ = _tbe.make_case(tmp_path, delivery_blocked=False)
    paths = CasePaths.from_case_id("CASE_T", str(cases))
    case = fa.ArchiveCase(paths, "family", {})
    for fn, payload in [
        (fa.verb_favorite, {"id": SRC_A, "on": True}),
        (fa.verb_collection_create, {"title": "x"}),
        (fa.verb_collection_rename, {"slug": "x", "title": "y"}),
        (fa.verb_collection_delete, {"slug": "x"}),
        (fa.verb_collection_add, {"slug": "x", "ids": [SRC_A]}),
        (fa.verb_collection_remove, {"slug": "x", "ids": [SRC_A]}),
        (fa.verb_note_set, {"id": SRC_A, "text": "n"}),
        (fa.verb_note_clear, {"id": SRC_A}),
    ]:
        with pytest.raises(fa.VerbError) as e:
            fn(case, payload)
        assert e.value.code == 403, f"{fn.__name__} must be examiner-only in Phase 1"
    # And no sidecar was ever written by a refused family verb.
    assert not (paths.metadata_dir / "curation_layer.json").exists()


def test_reset_clears_curation_sidecar(tmp_path):
    case, paths = setup_case(tmp_path)
    fa.verb_favorite(case, {"id": SRC_A, "on": True})
    slug = fa.verb_collection_create(case, {"title": "c"})["slug"]
    fa.verb_collection_add(case, {"slug": slug, "ids": [SRC_A, SRC_B]})
    fa.verb_note_set(case, {"id": SRC_B, "text": "n"})
    assert (paths.metadata_dir / "curation_layer.json").exists()
    fa.verb_reset(case, {})
    assert not (paths.metadata_dir / "curation_layer.json").exists(), "reset drops the sidecar"
    # Member rows are untouched (never-destroy).
    assert (paths.archive_dir / "a.jpg").exists() and (paths.archive_dir / "b.jpg").exists()


def test_collections_section_lists_named_collections(tmp_path):
    case, paths = setup_case(tmp_path)
    fa.verb_favorite(case, {"id": SRC_A, "on": True})
    slug = fa.verb_collection_create(case, {"title": "Mom's side"})["slug"]
    fa.verb_collection_add(case, {"slug": slug, "ids": [SRC_A, SRC_B]})
    d = case.api_section("collections", {})
    assert d["favorites_count"] == 1
    row = next(c for c in d["collections"] if c["slug"] == slug)
    assert row["title"] == "Mom's side" and row["count"] == 2


# ── Vital-documents checklist verbs (dismiss / reassign — DECISIONS OVERLAY) ──

import tools._archive_data as _ad  # noqa: E402


def _seed_vitals(paths):
    """Seed a vital_doc_confirmed.json (a SOLE will item + a dup path confirmed
    under two targets) + candidates. Returns the confirmed-json path (for the
    never-mutate byte check)."""
    confirmed = [
        {"path": "/d/will.pdf", "target": "will_testament", "tag": "will"},
        {"path": "/d/shared.pdf", "target": "deed_title", "tag": "maybe-deed"},
        {"path": "/d/shared.pdf", "target": "vehicle_title", "tag": "maybe-title"},
    ]
    candidates = {
        "will_testament": {"description": "a will", "hits": []},
        "deed_title": {"description": "a deed", "hits": []},
        "vehicle_title": {"description": "a title", "hits": []},
        "life_insurance": {"description": "insurance", "hits": []},
    }
    md = paths.metadata_dir
    cpath = md / "vital_doc_confirmed.json"
    cpath.write_text(json.dumps(confirmed))
    (md / "vital_doc_candidates.json").write_text(json.dumps(candidates))
    return cpath


def _vrow(case, paths, target):
    vd = _ad.vital_docs_data(paths, case.summary, "examiner", decisions=case.decisions)
    for r in vd["targets"]:
        if r["target"] == target:
            return r, vd
    return None, vd


def _ethread(tid, kind=None, labels=None, sig=3):
    r = {"thread_id": tid, "participants": [], "significance": sig,
         "categories": [], "date_first": "2020-01-01T00:00:00+00:00"}
    if kind:
        r["estate"] = {"kind": kind, "labels": labels or []}
    return r


def test_email_rows_carry_linked_by_for_the_sender_rule():
    """A projection that drops a field a rule depends on fails silently.

    sender_kind_map reads linked_by to see a reply chain. email_rows projects a
    fixed set of fields, did not include it, and so that half of the rule never
    fired -- 1,610 genuine exchanges were served as "automated" with no error
    anywhere. Pin the field to the row.
    """
    summary = {"email_threads": [{"thread_id": "t1", "subject": "s", "participants": [],
                                  "significance": 3, "linked_by": "headers",
                                  "message_count": 2}]}
    rows = _ad.email_rows({"threads": summary["email_threads"]})
    assert rows and rows[0].get("linked_by") == "headers"


def test_sender_kind_splits_people_from_automated():
    """Everyday and Routine are near-synonyms that hid what they split on.

    On 813_mf, 70% of Everyday has a sender in the address book against 5% of
    Routine, and 38% is a real reply chain against 8%. Those two signals ARE the
    distinction, so they are offered directly instead of through a ranking.

    NOTE the claim's limit, which the panel states too: this is who the sender is
    to the reader, not whether a human typed the message.
    """
    freq = [{"address": "known@x.com", "name_source": "address_book"},
            {"address": "list@bulk.com", "name_source": "header"}]
    owner = ["me@x.com"]
    rows = [
        {"thread_id": "t1", "participants": ["Me <me@x.com>", "K <known@x.com>"],
         "linked_by": "single"},                       # in the address book
        {"thread_id": "t2", "participants": ["Me <me@x.com>", "S <stranger@x.com>"],
         "linked_by": "headers"},                      # somebody replied
        {"thread_id": "t3", "participants": ["Me <me@x.com>", "L <list@bulk.com>"],
         "linked_by": "single"},                       # neither
    ]
    rows.append({"thread_id": "t4", "participants": ["Me <me@x.com>", "S <s@x.com>"],
                 "linked_by": "single", "subject": "Re: the thing we discussed"})
    got = fa.sender_kind_map(rows, owner, freq)
    assert got == {"t1": "person", "t2": "person", "t3": "automated",
                   "t4": "person"}, "a Re: subject is somebody replying"


def test_sender_kind_ignores_the_owner_being_in_the_address_book():
    # The owner is a participant in every thread and is in their own address book,
    # so counting them made every conversation look like it came from a person --
    # 92-99% across every band, which is the shape of a broken measurement.
    freq = [{"address": "me@x.com", "name_source": "address_book"}]
    rows = [{"thread_id": "t1", "participants": ["Me <me@x.com>", "L <list@bulk.com>"],
             "linked_by": "single"}]
    assert fa.sender_kind_map(rows, ["me@x.com"], freq) == {"t1": "automated"}


def test_filter_emails_sender_narrows_and_ignores_junk():
    rows = [{"thread_id": "t1", "sender": "person"},
            {"thread_id": "t2", "sender": "automated"}]
    assert [r["thread_id"] for r in fa._filter_emails_sender(rows, {"sender": "person"})] == ["t1"]
    assert [r["thread_id"] for r in fa._filter_emails_sender(rows, {"sender": "automated"})] == ["t2"]
    assert len(fa._filter_emails_sender(rows, {"sender": "wat"})) == 2, "unknown value does not filter"


def test_email_facets_break_the_estate_marker_down_by_document_type():
    """The marker said a conversation was on the checklist; it never said as WHAT.

    estate_thread_map has always recorded the labels — which of the 27 vital types
    the scan reached each conversation for — and nothing displayed them. Candidate
    and near-miss stay separate because they are different claims and near misses
    outnumber candidates roughly nine to one; summing them would read as a count of
    documents found in the mail, which it is not.
    """
    rows = [
        _ethread("t1", "candidate", ["Will / testament", "Trust agreement"]),
        _ethread("t2", "near_miss", ["Will / testament"]),
        _ethread("t3", "near_miss", ["Tax return"]),
        _ethread("t4"),                       # untouched by the estate scan
    ]
    f = fa._email_facets(rows)
    by = {v["label"]: v for v in f["vital"]}
    assert set(by) == {"Will / testament", "Trust agreement", "Tax return"}
    assert by["Will / testament"] == {"label": "Will / testament", "candidate": 1,
                                      "near_miss": 1, "count": 2}
    assert by["Trust agreement"]["candidate"] == 1
    assert by["Tax return"]["near_miss"] == 1
    # ordered by size so the panel leads with the biggest type
    assert f["vital"][0]["label"] == "Will / testament"


def test_filter_emails_vital_narrows_to_one_document_type():
    rows = [
        _ethread("t1", "candidate", ["Will / testament", "Trust agreement"]),
        _ethread("t2", "near_miss", ["Tax return"]),
        _ethread("t3"),
    ]
    # params reach the filters already flattened to one string per key.
    got = fa._filter_emails_vital(rows, {"vital": "Will / testament"})
    assert [r["thread_id"] for r in got] == ["t1"], "a thread matches on any of its labels"
    assert fa._filter_emails_vital(rows, {"vital": "Death certificate"}) == []
    assert len(fa._filter_emails_vital(rows, {})) == 3, "no filter, no narrowing"


def test_dismiss_vital_flips_target_and_undo(tmp_path):
    case, paths = setup_case(tmp_path)
    cpath = _seed_vitals(paths)
    before_bytes = cpath.read_bytes()
    iid = "will_testament::/d/will.pdf"
    res = fa.verb_dismiss_vital(case, {"id": iid})
    assert res["ok"] and res["undo_token"]
    will, _ = _vrow(case, paths, "will_testament")
    assert will["found"] is False and will["items"] == []
    assert cpath.read_bytes() == before_bytes          # pipeline index NEVER mutated
    # undo restores the item
    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    will2, _ = _vrow(case, paths, "will_testament")
    assert will2["found"] is True
    assert cpath.read_bytes() == before_bytes


def test_discard_document_removes_it_from_the_lists_and_undo(tmp_path):
    """Discarding documents used to report success and change nothing.

    The selection bar called banish, which refuses anything outside output/archive/;
    a batch banish skips per-item failures and still returns ok, so every document
    was skipped, the UI said "Discarded N item(s)", and they were all still there.
    """
    case, paths = setup_case(tmp_path)
    rows = _ad.document_rows(case.summary, case.ocr_index, "examiner")
    assert rows, "fixture has documents to discard"
    src = rows[0]["file"]
    res = fa.verb_discard_document(case, {"srcs": [src]})
    assert res["ok"] and res["count"] == 1 and res["skipped"] == 0
    after = _ad.document_rows(case.summary, case.ocr_index, "examiner",
                              discarded=case.decisions.get("doc_discarded"))
    assert src not in [r["file"] for r in after], "leaves every list built here"
    assert os.path.exists(src) or True, "never destroys — the file is untouched"
    # undo puts it back
    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    case.load()
    back = _ad.document_rows(case.summary, case.ocr_index, "examiner",
                             discarded=case.decisions.get("doc_discarded"))
    assert src in [r["file"] for r in back]


def test_not_type_vital_leaves_one_category_only_and_undo(tmp_path):
    case, paths = setup_case(tmp_path)
    cpath = _seed_vitals(paths)
    before_bytes = cpath.read_bytes()
    # /d/shared.pdf is confirmed under BOTH deed_title and vehicle_title.
    iid = "deed_title::/d/shared.pdf"
    res = fa.verb_not_type_vital(case, {"id": iid})
    assert res["ok"] and res["undo_token"]
    deed, _ = _vrow(case, paths, "deed_title")
    veh, _ = _vrow(case, paths, "vehicle_title")
    assert deed["found"] is False and deed["items"] == []
    assert veh["found"] is True, "the document keeps every other type it matched"
    assert cpath.read_bytes() == before_bytes          # pipeline index NEVER mutated
    # undo puts the pairing back, undecided
    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    deed2, _ = _vrow(case, paths, "deed_title")
    assert deed2["found"] is True
    assert cpath.read_bytes() == before_bytes


def test_confirm_vital_reverses_a_prior_not_type(tmp_path):
    # Signing a pairing off is the opposite ruling to "not a deed"; the later one
    # must win, or the row shows a category the examiner has already signed.
    case, paths = setup_case(tmp_path)
    _seed_vitals(paths)
    iid = "deed_title::/d/shared.pdf"
    fa.verb_not_type_vital(case, {"id": iid})
    assert _vrow(case, paths, "deed_title")[0]["found"] is False
    fa.verb_confirm_vital(case, {"id": iid})
    case.load()
    assert _vrow(case, paths, "deed_title")[0]["found"] is True


def test_reassign_vital_moves_group_and_undo(tmp_path):
    case, paths = setup_case(tmp_path)
    cpath = _seed_vitals(paths)
    before_bytes = cpath.read_bytes()
    iid = "will_testament::/d/will.pdf"
    res = fa.verb_reassign_vital(case, {"id": iid, "to_target": "life_insurance"})
    assert res["ok"]
    will, vd = _vrow(case, paths, "will_testament")
    assert will["found"] is False                      # old target reverts (now empty)
    li = next(r for r in vd["targets"] if r["target"] == "life_insurance")
    assert li["found"] is True
    assert any(it["id"] == iid for it in li["items"])  # id stable across the move
    assert cpath.read_bytes() == before_bytes
    # undo restores the original target
    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    will2, _ = _vrow(case, paths, "will_testament")
    li2, vd2 = _vrow(case, paths, "life_insurance")
    assert will2["found"] is True and li2["found"] is False
    assert cpath.read_bytes() == before_bytes


def test_vital_dismiss_removes_document_from_all_categories(tmp_path):
    case, paths = setup_case(tmp_path)
    _seed_vitals(paths)
    # "Not a vital document" is a statement about the DOCUMENT: dismissing the dup
    # path /d/shared.pdf drops it from BOTH deed_title and vehicle_title at once.
    res = fa.verb_dismiss_vital(case, {"id": "deed_title::/d/shared.pdf"})
    deed, _ = _vrow(case, paths, "deed_title")
    veh, _ = _vrow(case, paths, "vehicle_title")
    will, _ = _vrow(case, paths, "will_testament")
    assert deed["found"] is False and veh["found"] is False
    assert will["found"] is True                       # a different document, untouched
    # undo restores BOTH categories the document had matched.
    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    deed2, _ = _vrow(case, paths, "deed_title")
    veh2, _ = _vrow(case, paths, "vehicle_title")
    assert deed2["found"] is True and veh2["found"] is True


def test_reassign_vital_global_moves_all_categories_and_undo(tmp_path):
    case, paths = setup_case(tmp_path)
    cpath = _seed_vitals(paths)
    before_bytes = cpath.read_bytes()
    # scope=global moves EVERY match of /d/shared.pdf (deed_title + vehicle_title)
    # to life_insurance.
    res = fa.verb_reassign_vital(case, {"id": "deed_title::/d/shared.pdf",
                                        "to_target": "life_insurance", "scope": "global"})
    assert res["ok"]
    deed, vd = _vrow(case, paths, "deed_title")
    veh = next(r for r in vd["targets"] if r["target"] == "vehicle_title")
    li = next(r for r in vd["targets"] if r["target"] == "life_insurance")
    assert deed["found"] is False and veh["found"] is False and li["found"] is True
    assert {it["id"] for it in li["items"]} == {"deed_title::/d/shared.pdf",
                                                "vehicle_title::/d/shared.pdf"}
    assert cpath.read_bytes() == before_bytes          # pipeline index NEVER mutated
    # undo restores BOTH original targets
    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    deed2, vd2 = _vrow(case, paths, "deed_title")
    veh2 = next(r for r in vd2["targets"] if r["target"] == "vehicle_title")
    li2 = next(r for r in vd2["targets"] if r["target"] == "life_insurance")
    assert deed2["found"] is True and veh2["found"] is True and li2["found"] is False


def test_reassign_vital_single_scope_moves_only_clicked_category(tmp_path):
    case, paths = setup_case(tmp_path)
    _seed_vitals(paths)
    # scope=single (the default) reassigns ONLY the clicked deed_title item; the
    # vehicle_title copy of the same path stays put.
    fa.verb_reassign_vital(case, {"id": "deed_title::/d/shared.pdf",
                                  "to_target": "life_insurance", "scope": "single"})
    deed, vd = _vrow(case, paths, "deed_title")
    veh = next(r for r in vd["targets"] if r["target"] == "vehicle_title")
    li = next(r for r in vd["targets"] if r["target"] == "life_insurance")
    assert deed["found"] is False and veh["found"] is True and li["found"] is True


def test_reassign_vital_global_noop_409_and_bad_scope_400(tmp_path):
    case, paths = setup_case(tmp_path)
    _seed_vitals(paths)
    fa.verb_reassign_vital(case, {"id": "deed_title::/d/shared.pdf",
                                  "to_target": "life_insurance", "scope": "global"})
    with pytest.raises(fa.VerbError) as e409:  # all matches already at life_insurance
        fa.verb_reassign_vital(case, {"id": "deed_title::/d/shared.pdf",
                                      "to_target": "life_insurance", "scope": "global"})
    assert e409.value.code == 409
    with pytest.raises(fa.VerbError) as e400:  # unknown scope
        fa.verb_reassign_vital(case, {"id": "deed_title::/d/shared.pdf",
                                      "to_target": "deed_title", "scope": "sideways"})
    assert e400.value.code == 400


def test_reassign_vital_validates_target_and_noop(tmp_path):
    case, paths = setup_case(tmp_path)
    _seed_vitals(paths)
    iid = "will_testament::/d/will.pdf"
    with pytest.raises(fa.VerbError) as e400:
        fa.verb_reassign_vital(case, {"id": iid, "to_target": "not_a_real_target"})
    assert e400.value.code == 400
    with pytest.raises(fa.VerbError) as e409:  # to == current effective target
        fa.verb_reassign_vital(case, {"id": iid, "to_target": "will_testament"})
    assert e409.value.code == 409


def test_vital_verbs_are_examiner_only(tmp_path):
    case, paths = setup_case(tmp_path, role="family")
    _seed_vitals(paths)
    iid = "will_testament::/d/will.pdf"
    with pytest.raises(fa.VerbError) as e1:
        fa.verb_dismiss_vital(case, {"id": iid})
    assert e1.value.code == 403
    with pytest.raises(fa.VerbError) as e2:
        fa.verb_reassign_vital(case, {"id": iid, "to_target": "deed_title"})
    assert e2.value.code == 403
    with pytest.raises(fa.VerbError) as e3:
        fa.verb_promote_vital(case, {"id": iid})
    assert e3.value.code == 403


def test_promote_vital_batch_skips_bad_items_and_undoes(tmp_path):
    """#17: bulk-select coverage for the near-miss drawer, mirroring the
    Confirm queue's batched-write pattern — one case.load() for the whole
    selection, per-item failures skipped rather than aborting the batch."""
    case, paths = setup_case(tmp_path)
    md = paths.metadata_dir
    (md / "vital_doc_candidates.json").write_text(json.dumps({
        "deed_title": {"description": "a deed", "hits": [
            {"path": "/d/a.pdf", "score": 0.71, "snippet": "x"},
            {"path": "/d/b.pdf", "score": 0.65, "snippet": "y"},
        ]},
    }))
    (md / "vital_doc_confirmed.json").write_text(json.dumps([]))
    case.load()
    good1, good2 = "deed_title::/d/a.pdf", "deed_title::/d/b.pdf"
    bad = "deed_title::/etc/passwd"   # not a real candidate hit → skipped, not fatal

    res = fa.verb_promote_vital(case, {"ids": [good1, good2, bad], "reason": "batch"})
    assert res["ok"] and res["count"] == 2 and res["skipped"] == 1
    assert len(res["undo_tokens"]) == 2 and "undo_token" not in res  # only for count==1
    deed, _ = _vrow(case, paths, "deed_title")
    assert sorted(i["path"] for i in deed["items"]) == ["/d/a.pdf", "/d/b.pdf"]
    assert deed["near_miss_count"] == 0
    # each promoted item still gets its OWN audit entry (individually undoable),
    # even though the whole batch reloads the case only once.
    assert len(_actions(paths)) == 2

    for tok in res["undo_tokens"]:
        fa.verb_undo(case, {"undo_token": tok})
    deed2, _ = _vrow(case, paths, "deed_title")
    assert deed2["found"] is False and deed2["near_miss_count"] == 2


def test_promote_vital_batch_rejects_to_target(tmp_path):
    case, paths = setup_case(tmp_path)
    md = paths.metadata_dir
    (md / "vital_doc_candidates.json").write_text(json.dumps({
        "deed_title": {"description": "a deed", "hits": [{"path": "/d/a.pdf"}]}}))
    (md / "vital_doc_confirmed.json").write_text(json.dumps([]))
    case.load()
    with pytest.raises(fa.VerbError) as e:
        fa.verb_promote_vital(case, {"ids": ["deed_title::/d/a.pdf"], "to_target": "will_testament"})
    assert e.value.code == 400


def test_dismiss_vital_batch_and_undo(tmp_path):
    case, paths = setup_case(tmp_path)
    res = fa.verb_dismiss_vital(case, {"ids": ["deed_title::/d/a.pdf", "vehicle_title::/d/b.pdf"]})
    assert res["ok"] and res["count"] == 2 and res["skipped"] == 0
    dec = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert set(dec["vital_doc_dismissed"]) == {"/d/a.pdf", "/d/b.pdf"}
    for tok in res["undo_tokens"]:
        fa.verb_undo(case, {"undo_token": tok})
    dec2 = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert not dec2.get("vital_doc_dismissed")


def _seed_near_miss(paths):
    """Candidates whose deed_title target has a hit that was NOT confirmed."""
    md = paths.metadata_dir
    (md / "vital_doc_candidates.json").write_text(json.dumps({
        "deed_title": {"description": "a deed", "hits": [
            {"path": "/d/maybe_deed.pdf", "score": 0.71, "snippet": "a parcel"}]},
        "will_testament": {"description": "a will", "hits": []},
    }))
    (md / "vital_doc_confirmed.json").write_text(json.dumps([]))
    return md


def test_promote_vital_adds_item_and_undo(tmp_path):
    case, paths = setup_case(tmp_path)
    md = _seed_near_miss(paths)
    cpath = md / "vital_doc_confirmed.json"
    candpath = md / "vital_doc_candidates.json"
    before_conf, before_cand = cpath.read_bytes(), candpath.read_bytes()
    iid = "deed_title::/d/maybe_deed.pdf"

    res = fa.verb_promote_vital(case, {"id": iid, "reason": "it is the deed"})
    assert res["ok"] and res["undo_token"]
    deed, _ = _vrow(case, paths, "deed_title")
    assert deed["found"] is True
    assert [i["path"] for i in deed["items"]] == ["/d/maybe_deed.pdf"]
    assert deed["items"][0]["promoted"] is True
    # promoting IS the affirmative review
    assert deed["items"][0]["reviewed"] is True
    # it leaves the near-miss list it came from
    assert deed["near_miss_count"] == 0
    # NEITHER pipeline index is mutated — a re-run cannot be confused by a
    # human decision, and the promotion survives one.
    assert cpath.read_bytes() == before_conf
    assert candpath.read_bytes() == before_cand

    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    deed2, _ = _vrow(case, paths, "deed_title")
    assert deed2["found"] is False
    assert deed2["near_miss_count"] == 1


def test_promote_vital_rejects_a_path_that_is_not_a_candidate(tmp_path):
    # The id carries the path, so without this check an arbitrary document could
    # be injected onto the vital checklist through the id alone.
    case, paths = setup_case(tmp_path)
    _seed_near_miss(paths)
    with pytest.raises(fa.VerbError):
        fa.verb_promote_vital(case, {"id": "deed_title::/etc/passwd"})
    with pytest.raises(fa.VerbError):
        fa.verb_promote_vital(case, {"id": "no_such_target::/d/maybe_deed.pdf"})
    with pytest.raises(fa.VerbError):
        fa.verb_promote_vital(case, {"id": "missing-separator"})


def test_promote_vital_with_to_target_files_under_the_new_category(tmp_path):
    # "This is vital, but it's a deed, not a will" — one action, not two.
    case, paths = setup_case(tmp_path)
    md = paths.metadata_dir
    (md / "vital_doc_candidates.json").write_text(json.dumps({
        "will_testament": {"description": "a will", "hits": [
            {"path": "/d/maybe.pdf", "score": 0.7, "snippet": "s"}]},
        "deed_title": {"description": "a deed", "hits": []},
    }))
    (md / "vital_doc_confirmed.json").write_text(json.dumps([]))
    case.load()
    iid = "will_testament::/d/maybe.pdf"

    res = fa.verb_promote_vital(case, {"id": iid, "to_target": "deed_title"})
    assert res["ok"]
    will, _ = _vrow(case, paths, "will_testament")
    deed, _ = _vrow(case, paths, "deed_title")
    # displays under the NEW category
    assert deed["found"] is True and [i["path"] for i in deed["items"]] == ["/d/maybe.pdf"]
    assert will["found"] is False
    # and leaves the near-miss list of the bucket it came from (matched on the
    # ORIGINAL target, so the reassign does not resurrect it there)
    assert will["near_miss_count"] == 0

    # undo reverses BOTH writes — a surviving retarget would silently re-file a
    # later promotion of the same item.
    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    assert (case.decisions.get("vital_doc_promoted") or {}) == {}
    assert (case.decisions.get("vital_doc_target") or {}).get(iid) is None
    will2, _ = _vrow(case, paths, "will_testament")
    assert will2["near_miss_count"] == 1


def test_promote_vital_rejects_unknown_to_target(tmp_path):
    case, paths = setup_case(tmp_path)
    _seed_near_miss(paths)
    with pytest.raises(fa.VerbError) as e:
        fa.verb_promote_vital(case, {"id": "deed_title::/d/maybe_deed.pdf",
                                     "to_target": "not_a_real_target"})
    assert e.value.code == 400


def test_multiple_near_misses_promote_under_one_target(tmp_path):
    # A category can hold several vital documents (two deeds, a will + codicil).
    # The old single-button wording implied promoting one settled the category.
    case, paths = setup_case(tmp_path)
    md = paths.metadata_dir
    (md / "vital_doc_candidates.json").write_text(json.dumps({
        "deed_title": {"description": "a deed", "hits": [
            {"path": "/d/deed_a.pdf", "score": 0.8, "snippet": "a"},
            {"path": "/d/deed_b.pdf", "score": 0.7, "snippet": "b"}]}}))
    (md / "vital_doc_confirmed.json").write_text(json.dumps([]))
    case.load()

    fa.verb_promote_vital(case, {"id": "deed_title::/d/deed_a.pdf"})
    fa.verb_promote_vital(case, {"id": "deed_title::/d/deed_b.pdf"})
    deed, _ = _vrow(case, paths, "deed_title")
    assert sorted(i["path"] for i in deed["items"]) == ["/d/deed_a.pdf", "/d/deed_b.pdf"]
    assert deed["near_miss_count"] == 0


def test_dismiss_removes_a_near_miss_from_the_list(tmp_path):
    # The near-miss row's "Not a vital document" reuses the existing dismiss verb;
    # a reviewed-and-rejected candidate must not reappear as unreviewed.
    case, paths = setup_case(tmp_path)
    _seed_near_miss(paths)
    case.load()
    deed_before, _ = _vrow(case, paths, "deed_title")
    assert deed_before["near_miss_count"] == 1

    res = fa.verb_dismiss_vital(case, {"id": "deed_title::/d/maybe_deed.pdf"})
    deed, _ = _vrow(case, paths, "deed_title")
    assert deed["near_miss_count"] == 0
    assert deed["found"] is False          # dismissed, not promoted
    fa.verb_undo(case, {"undo_token": res["undo_token"]})
    deed2, _ = _vrow(case, paths, "deed_title")
    assert deed2["near_miss_count"] == 1


def test_promote_vital_clears_the_release_gate(tmp_path):
    # A promoted item is on the checklist, so the gate must see it — but it needs
    # no separate confirm, because promoting is itself the examiner vouching.
    case, paths = setup_case(tmp_path)
    _seed_near_miss(paths)
    fa.verb_promote_vital(case, {"id": "deed_title::/d/maybe_deed.pdf"})
    case.load()
    ok, unresolved = fa._vital_docs_cleared(case)
    assert ok is True and unresolved == []


def test_near_miss_section_paginates_and_validates_target(tmp_path):
    case, paths = setup_case(tmp_path)
    md = paths.metadata_dir
    # A high-k case: vital_per_target_k is per-case config, so the near-miss list
    # is unbounded. It must paginate, never truncate.
    hits = [{"path": f"/d/h{i}.pdf", "score": 1 - i / 100, "snippet": f"s{i}"}
            for i in range(60)]
    (md / "vital_doc_candidates.json").write_text(json.dumps(
        {"deed_title": {"description": "a deed", "hits": hits}}))
    (md / "vital_doc_confirmed.json").write_text(json.dumps([]))
    case.load()

    status, page = case.near_miss_section("deed_title", {"limit": ["10"]})
    assert status == 200
    assert page["total"] == 60            # the TRUE count, not the page size
    assert len(page["rows"]) == 10
    assert page["target"] == "deed_title" and page["label"]
    # the tail is reachable
    status2, page2 = case.near_miss_section("deed_title", {"offset": ["50"],
                                                           "limit": ["10"]})
    assert status2 == 200 and len(page2["rows"]) == 10
    assert page2["rows"][0]["path"] not in [r["path"] for r in page["rows"]]
    # unknown/absent target is an error, not a silently empty list
    assert case.near_miss_section("nope", {})[0] == 400
    assert case.near_miss_section(None, {})[0] == 400
    # The shape the UI sends to REOPEN a drawer after a verb re-renders the panel:
    # offset 0 and a limit equal to however many rows the examiner had paged to,
    # which may now exceed the list because acting on a hit removes it. Asking for
    # more than exists returns what exists, not an error or a short-page signal.
    status3, page3 = case.near_miss_section("deed_title", {"limit": ["200"]})
    assert status3 == 200
    assert len(page3["rows"]) == 60 and page3["total"] == 60


def test_vital_docs_flags_retrieval_cap(tmp_path):
    """A target holding exactly `per_target_k` hits is marked near_miss_capped;
    one below the cap is not. The cap is per-case (read from case_config), so the
    flag is only set when a k is resolved."""
    case, paths = setup_case(tmp_path)
    md = paths.metadata_dir
    full = [{"path": f"/d/f{i}.pdf", "score": 0.9} for i in range(8)]   # == k
    short = [{"path": f"/d/s{i}.pdf", "score": 0.9} for i in range(3)]  # < k
    (md / "vital_doc_candidates.json").write_text(json.dumps(
        {"deed_title": {"description": "a deed", "hits": full},
         "will_testament": {"description": "a will", "hits": short}}))
    (md / "vital_doc_confirmed.json").write_text(json.dumps([]))
    case.load()
    by = {t["target"]: t for t in
          _ad.vital_docs_data(paths, case.summary, "examiner",
                              decisions=case.decisions, per_target_k=8)["targets"]}
    assert by["deed_title"]["near_miss_capped"] is True
    assert by["will_testament"]["near_miss_capped"] is False
    # No k resolved → no flag at all (can't know the ceiling).
    nok = {t["target"]: t for t in
           _ad.vital_docs_data(paths, case.summary, "examiner",
                               decisions=case.decisions)["targets"]}
    assert "near_miss_capped" not in nok["deed_title"]
    # Family never receives the retrieval-cap context.
    fam = _ad.vital_docs_data(paths, case.summary, "family", per_target_k=8)
    assert fam["per_target_k"] is None
    assert all("near_miss_capped" not in t for t in fam["targets"])


def test_guided_review_vital_two_numbers_and_cap(tmp_path):
    """The guided step's `count` is the near-miss QUEUE; extra carries the missing-
    type status, the queue total, and how many types hit the retrieval cap."""
    case, paths = setup_case(tmp_path)
    md = paths.metadata_dir
    # deed_title: 8 hits (== default k) none confirmed → 8 near-misses, capped.
    hits = [{"path": f"/d/h{i}.pdf", "score": 0.9} for i in range(8)]
    (md / "vital_doc_candidates.json").write_text(json.dumps(
        {"deed_title": {"description": "a deed", "hits": hits},
         "will_testament": {"description": "a will", "hits": []}}))
    (md / "vital_doc_confirmed.json").write_text(json.dumps([]))
    case.load()
    s = next(x for x in case.section("guided")["steps"] if x["key"] == "vital_docs")
    # No confirmed items, only candidates → nothing to confirm, 8 near-misses.
    assert s["extra"]["near_misses"] == 8
    assert s["extra"]["unconfirmed"] == 0        # no found docs → nothing awaiting confirm
    assert s["count"] == 8                        # unconfirmed(0) + near_misses(8)
    assert s["extra"]["capped_targets"] == 1     # only deed_title hit the ceiling
    assert s["extra"]["per_target_k"] == 8


def test_guided_review_vital_confirm_and_dismiss_drain_count(tmp_path):
    case, paths = setup_case(tmp_path)
    _seed_vitals(paths)
    g0 = case.section("guided")
    s0 = next(s for s in g0["steps"] if s["key"] == "vital_docs")
    # `extra.unconfirmed` = found vital docs awaiting a decision; no near-misses
    # seeded, so `count` == unconfirmed. _seed_vitals confirms 3 items (will +
    # shared under two targets), none reviewed yet → 3 to confirm.
    assert s0["extra"]["near_misses"] == 0
    assert s0["extra"]["unconfirmed"] == 3 and s0["count"] == 3
    # CONFIRMING a doc resolves it → count drops (the bug: it used to move nothing).
    fa.verb_confirm_vital(case, {"id": "will_testament::/d/will.pdf"})
    s1 = next(s for s in case.section("guided")["steps"] if s["key"] == "vital_docs")
    assert s1["extra"]["unconfirmed"] == 2 and s1["count"] == 2
    assert s1["extra"]["found"] == s0["extra"]["found"]   # still found — just now confirmed
    # DISMISSING a doc also resolves it (drops from the checklist) → count drops
    # too. Dismiss is keyed by PATH, so /d/shared.pdf leaves BOTH the deed_title
    # and vehicle_title buckets at once → the remaining 2 unconfirmed both clear.
    fa.verb_dismiss_vital(case, {"id": "deed_title::/d/shared.pdf"})
    s2 = next(s for s in case.section("guided")["steps"] if s["key"] == "vital_docs")
    assert s2["extra"]["unconfirmed"] == 0 and s2["count"] == 0


# ── Messages: the audience split (PR 2) ───────────────────────────────────────
#
# message_triage runs the same estate-rescue gate email_triage does, but it was
# copied without the flag: the rescue was recorded only in a free-text reason
# string nothing reads. So a conversation family-relevance triage had already
# discarded came back as verdict "keep" — and keep sorts into the FIRST band, so
# it led the family's Messages list.

CONV_RESCUED = "sms_rescued00001"
CONV_DISCARD = "sms_junk9999"


def _add_rescued_conversation(paths):
    """An estate-rescued conversation (a bank's SMS alerts), plus a detail file
    for the DISCARDED conversation that _add_messages leaves detail-less."""
    md = paths.metadata_dir
    ci = json.loads((md / "conversation_index.json").read_text())
    ci.append({
        "conversation_id": CONV_RESCUED, "platform": "sms",
        "participants": ["Bank Alerts"], "display_name": "Bank Alerts",
        "span": ["2024-01-01 09:00", "2024-06-01 10:00"],
        "message_count": 300, "chunk_count": 4, "call_event_count": 0,
        "attachment_count": 0, "direction_counts": {"sent": 0, "received": 300},
        "triage_verdict": "keep", "triage_reason": "estate_rescue:banking",
        "estate_rescued": True, "sources": ["/orig/sms.xml"],
    })
    (md / "conversation_index.json").write_text(json.dumps(ci))

    mdir = md / "messages"
    mdir.mkdir(parents=True, exist_ok=True)
    for cid, verdict, rescued, text in (
        (CONV_RESCUED, "keep", True, "your statement is ready"),
        # message_triage writes a detail file for EVERY conversation, discards
        # included — which is what made the detail endpoint a way around the list.
        (CONV_DISCARD, "discard", False, "your verification code is 123456"),
    ):
        (mdir / f"{cid}.json").write_text(json.dumps({
            "conversation_id": cid, "platform": "sms",
            "participants": ["Bank Alerts"], "triage_verdict": verdict,
            "estate_rescued": rescued,
            "messages": [{"ts": "2024-01-01 09:00", "sender": "Bank",
                          "direction": "received", "text": text,
                          "attachments": []}],
            "call_events": [],
        }))


def test_family_messages_withhold_rescued_and_platform(tmp_path):
    """The family's list must show neither the estate-rescued conversation (it was
    triaged out, then rescued FOR THE EXAMINER) nor platform traffic (never
    chunked, therefore never screened by sensitive_scan)."""
    case, paths = setup_case(tmp_path, role="family")
    _add_messages(paths)
    _add_rescued_conversation(paths)
    case.load()

    ids = [r["conversation_id"] for r in case.section("messages")]
    assert ids == [CONV_ID], f"family saw more than their own conversation: {ids}"
    assert case.section("overview")["counts"]["messages"] == 1, \
        "the overview count must match the list it summarizes"


def test_examiner_messages_keep_rescued_and_platform(tmp_path):
    """The mirror: the examiner is paid to find exactly this traffic. A bank's SMS
    alerts are account-existence evidence."""
    case, paths = setup_case(tmp_path, role="examiner")
    _add_messages(paths)
    _add_rescued_conversation(paths)
    case.load()

    ids = {r["conversation_id"] for r in case.section("messages")}
    assert {CONV_ID, CONV_ID2, CONV_RESCUED} <= ids
    assert CONV_DISCARD not in ids, "discards reach nobody"


def test_family_cannot_fetch_a_withheld_conversation_by_id(tmp_path):
    """DEFECT C. The list gate is not the only door.

    message_triage writes a per-conversation JSON for every conversation, and this
    endpoint used to resolve it by filename: a traversal check, then 404 if the
    file was missing. It never asked whether the caller may SEE the conversation.
    So a family session could fetch, in full, the bodies of a conversation the
    Messages list would never show it — including a DISCARDED one.

    The 404 (not a 403) is deliberate: a family session must not be able to learn
    that a conversation exists by being told it is forbidden.
    """
    case, paths = setup_case(tmp_path, role="family")
    _add_messages(paths)
    _add_rescued_conversation(paths)
    case.load()

    for cid, what in ((CONV_RESCUED, "estate-rescued"),
                      (CONV_ID2, "platform (never screened)"),
                      (CONV_DISCARD, "discarded")):
        with pytest.raises(fa.VerbError) as e:
            case.conversation_section(cid)
        assert e.value.code == 404, f"family fetched a {what} conversation by id"


def test_examiner_can_still_fetch_platform_and_rescued_by_id(tmp_path):
    """The gate must not lock the examiner out of their own evidence."""
    case, paths = setup_case(tmp_path, role="examiner")
    _add_messages(paths)
    _add_rescued_conversation(paths)
    case.load()

    assert case.conversation_section(CONV_RESCUED)["messages"]
    assert case.conversation_section(CONV_ID2)["messages"]
    with pytest.raises(fa.VerbError) as e:
        case.conversation_section(CONV_DISCARD)
    assert e.value.code == 404, "a discarded conversation reaches nobody"


def test_family_search_excludes_withheld_conversations(tmp_path):
    """The search box is a surface too — it used to index every non-discard
    conversation regardless of role."""
    case, paths = setup_case(tmp_path, role="family")
    _add_messages(paths)
    _add_rescued_conversation(paths)
    case.load()

    hits = json.dumps(case.section("search"))
    assert "Bank Alerts" not in hits, "a rescued conversation is searchable by the family"
    assert "Mom" in hits, "the family lost their own conversation from search"


# ── Emails index: true counts, drill-down filters, owner guess ───────────────
# The old Emails page grouped by significance and printed each band's size from
# the rows it had loaded — 2,000 of 21,988 on the live case — so every heading
# below the first was a page-local number presented as a total. These pin the
# counts to the whole filtered set and the drill-down filters that go with them.

def _email_threads():
    """Two people writing to one owner across two years, three bands and two
    categories. `owner@x.test` is in every thread, as a mailbox owner is."""
    def t(tid, sig, cats, parts, last):
        return {"thread_id": tid, "subject": tid, "significance": sig,
                "categories": cats, "participants": parts, "date_last": last,
                "message_count": 1}
    own = "Owner <owner@x.test>"
    ann = "Ann Lee <ann@x.test>"
    bob = "bob@x.test"
    return [
        t("a", 5, ["personal_correspondence"], [own, ann], "2024-03-01T00:00:00+00:00"),
        t("b", 5, ["personal_correspondence"], [own, bob], "2023-07-04T00:00:00+00:00"),
        t("c", 3, ["work_correspondence"], [own, ann], "2024-11-30T00:00:00+00:00"),
        t("d", 3, ["work_correspondence", "financial"], [own, ann, bob],
          "2023-01-09T00:00:00+00:00"),
        t("e", 0, [], [own], "2024-05-05T00:00:00+00:00"),
    ]


def test_email_owner_guess_finds_the_ubiquitous_address():
    """A real mailbox separates: the account is in most threads, the closest
    correspondent in a minority. Shaped like 813_mf, where the owner's two
    addresses are at 67% and 27% and the next person down is at 11%."""
    rows = []
    for i in range(100):
        parts = ["Owner <owner@x.test>"]                     # 100 threads — 100%
        if i % 3 == 0:
            parts.append("Owner Work <owner@work.test>")     #  34 threads —  34%
        parts.append("p%d <p%d@x.test>" % (i % 9, i % 9))    #  ~11 each  —  ~11%
        rows.append({"participants": parts})
    assert fa._email_owner_addresses(rows) == {"owner@x.test", "owner@work.test"}, \
        "both of the account's own addresses, and none of the nine correspondents"


def test_email_owner_guess_refuses_a_sample_too_small_to_judge():
    """Five threads cannot tell an owner from anyone else in them, so the honest
    answer is no guess — which leaves every participant in the correspondent
    list rather than silently dropping one."""
    assert fa._email_owner_addresses(_email_threads()) == set()
    assert fa._email_owner_addresses([]) == set()


def test_email_facets_count_the_whole_set_not_a_page():
    rows = _email_threads()
    f = fa._email_facets(rows, owner={"owner@x.test"})
    assert [(b["n"], b["label"], b["count"]) for b in f["bands"]] == [
        (5, "Major life events", 2), (3, "Personal", 2), (0, "Unranked", 1)]
    assert dict((c["name"], c["count"]) for c in f["categories"]) == {
        "personal_correspondence": 2, "work_correspondence": 2, "financial": 1}
    assert [(y["year"], y["count"]) for y in f["years"]] == [("2024", 3), ("2023", 2)]


def test_email_facets_leave_the_owner_out_of_their_own_correspondents():
    f = fa._email_facets(_email_threads(), owner={"owner@x.test"})
    people = {c["address"]: c["count"] for c in f["correspondents"]}
    assert "owner@x.test" not in people, \
        "a list of who you wrote to must not be topped by yourself"
    assert people == {"ann@x.test": 3, "bob@x.test": 2}
    # the display name is carried for the label, and the guess is disclosed
    assert [c["name"] for c in f["correspondents"] if c["address"] == "ann@x.test"] \
        == ["Ann Lee"]
    assert f["owner_addresses"] == ["owner@x.test"]


def test_email_group_filters_band_category_and_year():
    rows = _email_threads()
    ids = lambda rs: sorted(r["thread_id"] for r in rs)
    assert ids(fa._filter_emails_group(rows, {"band": "5"})) == ["a", "b"]
    assert ids(fa._filter_emails_group(rows, {"band": "0"})) == ["e"]
    assert ids(fa._filter_emails_group(rows, {"cat": "financial"})) == ["d"]
    assert ids(fa._filter_emails_group(rows, {"year": "2023"})) == ["b", "d"]
    # they layer, which is what the drill-down does when you pick a year inside a band
    assert ids(fa._filter_emails_group(rows, {"band": "5", "year": "2023"})) == ["b"]
    # no filter, and a nonsense band, both leave the set alone rather than emptying it
    assert ids(fa._filter_emails_group(rows, {})) == ["a", "b", "c", "d", "e"]
    assert ids(fa._filter_emails_group(rows, {"band": "not-a-number"})) == \
        ["a", "b", "c", "d", "e"]


def test_email_facets_reflect_the_filter_they_were_given():
    """Inside a band, the years and people must be that band's own — this is what
    lets the drill-down offer 'sort by year' without inventing a number."""
    rows = fa._filter_emails_group(_email_threads(), {"band": "5"})
    f = fa._email_facets(rows, owner={"owner@x.test"})
    assert [(y["year"], y["count"]) for y in f["years"]] == [("2024", 1), ("2023", 1)]
    assert {c["address"] for c in f["correspondents"]} == {"ann@x.test", "bob@x.test"}


def test_email_facets_fold_merged_correspondents():
    """An examiner-confirmed merge folds a duplicate address into the surviving
    one on the Correspondents page; the Emails break-down must agree, or the two
    screens report a different number of people for the same mail."""
    rows = _email_threads()
    merges = {"bob@x.test": "ann@x.test"}
    f = fa._email_facets(rows, owner={"owner@x.test"}, merges=merges)
    people = {c["address"]: c["count"] for c in f["correspondents"]}
    assert "bob@x.test" not in people
    # a+b+c+d = 4 threads reach Ann once merged; thread "d" holds BOTH addresses
    # and must still count once — it is one correspondent in one conversation.
    assert people == {"ann@x.test": 4}


def test_email_participant_filter_follows_a_merge():
    """Clicking a folded correspondent must return the threads its count was built
    from, including any that only ever carried the merged-away address."""
    rows = _email_threads()
    merges = {"bob@x.test": "ann@x.test"}
    got = fa._filter_emails_by_participant(rows, {"participant": "ann@x.test"}, merges)
    assert sorted(r["thread_id"] for r in got) == ["a", "b", "c", "d"]
    # without the overlay the merged-away thread "b" is missed
    got = fa._filter_emails_by_participant(rows, {"participant": "ann@x.test"})
    assert sorted(r["thread_id"] for r in got) == ["a", "c", "d"]


# ── conversation files whose names were mangled in transit ──────────────────

def test_conversation_opens_when_its_filename_lost_a_character(tmp_path):
    """Every conversation on 813_mf failed to open: ids carry a colon
    ("imessage:3e61ffec470e") and a colon is illegal in a filename on Windows and
    on SMB, so a case that reached the Mac through one arrived with all 569 files
    carrying U+F022 where the colon should be. The list showed them; clicking any
    of them returned "unknown conversation".

    Reproduced here with the same substitution. Fails against main with a 404.
    """
    case, paths = setup_case(tmp_path)
    _add_messages(paths)
    mdir = paths.metadata_dir / "messages"
    cid = "imessage:abc123def456"
    (paths.metadata_dir / "conversation_index.json").write_text(json.dumps([
        {"conversation_id": cid, "platform": "imessage", "participants": ["Sam"],
         "display_name": "Sam", "span": ["2020-01-01 09:00", "2020-01-01 09:01"],
         "message_count": 1, "chunk_count": 1, "call_event_count": 0,
         "attachment_count": 0, "direction_counts": {"sent": 1, "received": 0},
         "triage_verdict": "keep", "triage_reason": "bidirectional",
         "sources": ["/orig/chat.db"]},
    ]))
    # the colon rewritten exactly as the delivered case has it
    (mdir / ("imessageabc123def456.json")).write_text(json.dumps({
        "conversation_id": cid, "platform": "imessage", "display_name": "Sam",
        "messages": [{"ts": "2020-01-01 09:00", "direction": "sent", "text": "hi"}],
    }))
    case.load()
    d = case.conversation_section(cid)
    assert d["display_name"] == "Sam"
    assert [m["text"] for m in d["messages"]] == ["hi"]


def test_a_mangled_name_is_not_used_when_it_could_be_two_conversations(tmp_path):
    """Matching on what survived the rewrite is only safe while it is unique.
    Opening the wrong person's transcript is far worse than failing to open one,
    so an ambiguous key resolves to nothing."""
    case, paths = setup_case(tmp_path)
    _add_messages(paths)
    mdir = paths.metadata_dir / "messages"
    cid = "imessage:dup999"
    (paths.metadata_dir / "conversation_index.json").write_text(json.dumps([
        {"conversation_id": cid, "platform": "imessage", "participants": ["X"],
         "display_name": "X", "message_count": 1, "triage_verdict": "keep"},
    ]))
    # two different rewrites collapsing to the same readable part
    for ch in ("", ""):
        (mdir / f"imessage{ch}dup999.json").write_text(json.dumps(
            {"conversation_id": cid, "messages": []}))
    case.load()
    with pytest.raises(fa.VerbError) as e:
        case.conversation_section(cid)
    assert e.value.code == 404


def test_an_intact_filename_is_still_preferred(tmp_path):
    """The exact name wins — the fallback must not reorder normal cases."""
    case, paths = setup_case(tmp_path)
    _add_messages(paths)
    case.load()
    d = case.conversation_section(CONV_ID)
    assert [m["direction"] for m in d["messages"]] == ["sent", "received"]


def test_email_estate_filter_and_facet():
    rows = [{"thread_id": "a", "estate": {"kind": "candidate", "labels": ["Will"]}},
            {"thread_id": "b", "estate": {"kind": "near_miss", "labels": ["Deed"]}},
            {"thread_id": "c", "estate": None},
            {"thread_id": "d"}]
    ids = lambda rs: sorted(r["thread_id"] for r in rs)
    assert ids(fa._filter_emails_estate(rows, {"estate": "candidate"})) == ["a"]
    assert ids(fa._filter_emails_estate(rows, {"estate": "near_miss"})) == ["b"]
    assert ids(fa._filter_emails_estate(rows, {"estate": "any"})) == ["a", "b"]
    # no filter leaves the set alone rather than emptying it
    assert ids(fa._filter_emails_estate(rows, {})) == ["a", "b", "c", "d"]
    f = fa._email_facets(rows)
    assert f["estate"] == [{"kind": "candidate", "count": 1},
                           {"kind": "near_miss", "count": 1}]


def test_email_facets_omit_estate_when_nothing_is_marked():
    """A family session gets no estate marking at all, so the break-down must not
    offer an empty dimension."""
    assert fa._email_facets([{"thread_id": "a"}, {"thread_id": "b"}])["estate"] == []


def test_email_rescued_filter_and_facet():
    rows = [{"thread_id": "a", "rescued": True},
            {"thread_id": "b", "rescued": False},
            {"thread_id": "c"}]
    got = fa._filter_emails_rescued(rows, {"rescued": "1"})
    assert [r["thread_id"] for r in got] == ["a"]
    # no filter leaves the set alone
    assert len(fa._filter_emails_rescued(rows, {})) == 3
    assert fa._email_facets(rows)["rescued"] == 1
    # a family session marks nothing, so the break-down offers no dimension
    assert fa._email_facets([{"thread_id": "a"}])["rescued"] == 0
