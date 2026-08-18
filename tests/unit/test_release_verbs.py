"""Tests for the examiner release-gate verbs (T3).

verb_signoff / verb_signoff_revoke / verb_review_keep / verb_waive_review, and
the §4 disposition gate: clearance (state-changing, logged dispositions), never
acknowledgement. Reuses the setup_case / _add_quarantine fixtures from
test_family_archive.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import tools.family_archive as fa  # noqa: E402
from wyeast.core import release  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "tfa", str(REPO / "tests" / "unit" / "test_family_archive.py"))
_tfa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tfa)
setup_case = _tfa.setup_case
_add_quarantine = _tfa._add_quarantine

# The human-review item make_case seeds into human_review_required.json.
HR_PATH = "/work/sensitive/x.jpg"

SIGN = {"name": "Jane Fiduciary", "capacity": "Estate attorney",
        "judgment": "The family-visible set is sentimental; I release it."}


def _clear_human_review(case):
    fa.verb_review_keep(case, {"id": HR_PATH, "reason": "reviewed, retained"})


def _clear_quarantine(paths):
    """make_case seeds one quarantine entry; empty the manifest to simulate every
    item having been released/discarded (quarantine_total -> 0)."""
    mpath = paths.metadata_dir / "quarantine_manifest.json"
    m = json.loads(mpath.read_text()) if mpath.exists() else {}
    m["released"] = (m.get("released") or []) + (m.get("entries") or [])
    m["entries"] = []
    mpath.write_text(json.dumps(m))


def _clear_gate(case, paths):
    _clear_quarantine(paths)
    _clear_human_review(case)


# ── the disposition gate refuses until everything is dispositioned ────────────

def test_signoff_refused_while_human_review_open(tmp_path):
    case, paths = setup_case(tmp_path)
    _clear_quarantine(paths)
    with pytest.raises(fa.VerbError) as exc:
        fa.verb_signoff(case, dict(SIGN))
    assert exc.value.code == 409
    assert "human-review" in str(exc.value)


def test_signoff_refused_while_quarantine_pending(tmp_path):
    case, paths = setup_case(tmp_path)
    _clear_human_review(case)
    _add_quarantine(paths, "flagged.jpg")
    case.load()                                   # pick up the new manifest
    with pytest.raises(fa.VerbError) as exc:
        fa.verb_signoff(case, dict(SIGN))
    assert exc.value.code == 409
    assert "quarantine" in str(exc.value)


def test_signoff_refused_when_delivery_blocked(tmp_path):
    case, _ = setup_case(tmp_path, delivery_blocked=True)
    _clear_human_review(case)
    with pytest.raises(fa.VerbError) as exc:
        fa.verb_signoff(case, dict(SIGN))
    assert exc.value.code == 409
    assert "export gate" in str(exc.value)


def test_signoff_requires_judgment(tmp_path):
    case, _ = setup_case(tmp_path)
    _clear_human_review(case)
    with pytest.raises(fa.VerbError):
        fa.verb_signoff(case, {"name": "J", "capacity": "attorney", "judgment": ""})


def test_signoff_is_examiner_only(tmp_path):
    case, _ = setup_case(tmp_path, role="family")
    with pytest.raises(fa.VerbError) as exc:
        fa.verb_signoff(case, dict(SIGN))
    assert exc.value.code == 403


# ── a clean sign-off writes evidence-first and verifies ───────────────────────

def test_signoff_writes_record_and_custody(tmp_path):
    case, paths = setup_case(tmp_path)
    _clear_gate(case, paths)
    res = fa.verb_signoff(case, dict(SIGN))
    assert res["ok"]

    rec = release.load_release(paths)
    assert rec is not None and rec["revoked"] is False
    assert rec["case_id"] == "CASE_T"
    assert rec["actor"]["name"] == "Jane Fiduciary"
    assert rec["delivery_fingerprint"] and rec["visibility_stamp"]
    assert "scan_filters_enabled" in rec["machine_screen"]
    assert rec["dispositions"]["review_kept"] == 1

    # custody event landed, and it precedes the record (evidence-first).
    log = paths.custody_log.read_text()
    assert "EVENT  release  " in log and rec["delivery_fingerprint"] in log

    # the authoritative verify passes over the freshly-signed tree.
    assert release.verify(paths, rec, live=False).ok
    # and the live tripwire serves (does not stale itself).
    assert release.verify(paths, rec, live=True).ok


def test_signoff_then_revoke_stops_verification(tmp_path):
    case, paths = setup_case(tmp_path)
    _clear_gate(case, paths)
    fa.verb_signoff(case, dict(SIGN))
    fa.verb_signoff_revoke(case, {})

    rec = release.load_release(paths)
    assert rec["revoked"] is True
    assert not release.verify(paths, rec, live=True).ok
    assert not release.verify(paths, rec, live=False).ok
    assert "EVENT  revoke  " in paths.custody_log.read_text()


# ── review keep / waive semantics ─────────────────────────────────────────────

def test_review_keep_optional_reason(tmp_path):
    case, paths = setup_case(tmp_path)
    fa.verb_review_keep(case, {"id": HR_PATH})           # no reason: allowed
    d = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert HR_PATH in d["human_review_reviewed"]


def test_waive_requires_reason(tmp_path):
    case, _ = setup_case(tmp_path)
    with pytest.raises(fa.VerbError):
        fa.verb_waive_review(case, {"id": HR_PATH})


def test_keep_and_waive_are_mutually_exclusive(tmp_path):
    case, paths = setup_case(tmp_path)
    fa.verb_review_keep(case, {"id": HR_PATH, "reason": "keep"})
    fa.verb_waive_review(case, {"id": HR_PATH, "reason": "on reflection, waive"})
    d = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert HR_PATH in d["human_review_waived"]
    assert HR_PATH not in (d.get("human_review_reviewed") or {})


def test_signoff_clears_via_waive_too(tmp_path):
    case, paths = setup_case(tmp_path)
    _clear_quarantine(paths)
    fa.verb_waive_review(case, {"id": HR_PATH, "reason": "no reviewer for this class"})
    res = fa.verb_signoff(case, dict(SIGN))
    assert res["ok"]
    assert release.load_release(paths)["dispositions"]["review_waived"] == 1


# ── vital-docs review is a HARD gate (forced) ─────────────────────────────────

VITAL_TARGET = "will_testament"
VITAL_PATH = "/work/docs/will.pdf"
VITAL_IID = f"{VITAL_TARGET}::{VITAL_PATH}"


def _add_vital(paths):
    """Write a confirmed vital-doc item so the vital gate has something to clear."""
    (paths.metadata_dir / "vital_doc_confirmed.json").write_text(json.dumps(
        [{"path": VITAL_PATH, "target": VITAL_TARGET, "tag": "vital_doc:will"}]))


def _ready_but_vital(case, paths):
    """Clear quarantine + human review, leaving only vital open."""
    _clear_quarantine(paths)
    _clear_human_review(case)
    _add_vital(paths)


def test_signoff_refused_while_vital_open(tmp_path):
    case, paths = setup_case(tmp_path)
    _ready_but_vital(case, paths)
    with pytest.raises(fa.VerbError) as exc:
        fa.verb_signoff(case, dict(SIGN))
    assert exc.value.code == 409
    assert "vital" in str(exc.value)


def test_signoff_clears_via_vital_confirm(tmp_path):
    case, paths = setup_case(tmp_path)
    _ready_but_vital(case, paths)
    fa.verb_confirm_vital(case, {"id": VITAL_IID, "reason": "this is the will"})
    res = fa.verb_signoff(case, dict(SIGN))
    assert res["ok"]
    assert release.load_release(paths)["dispositions"]["vital_confirmed"] == 1


def test_signoff_clears_via_vital_dismiss(tmp_path):
    case, paths = setup_case(tmp_path)
    _ready_but_vital(case, paths)
    fa.verb_dismiss_vital(case, {"id": VITAL_IID})
    assert fa.verb_signoff(case, dict(SIGN))["ok"]


def test_signoff_clears_via_vital_reassign(tmp_path):
    case, paths = setup_case(tmp_path)
    _ready_but_vital(case, paths)
    fa.verb_reassign_vital(case, {"id": VITAL_IID, "to_target": "deed_title"})
    assert fa.verb_signoff(case, dict(SIGN))["ok"]


def test_confirm_vital_undismisses(tmp_path):
    case, paths = setup_case(tmp_path)
    _add_vital(paths)
    fa.verb_dismiss_vital(case, {"id": VITAL_IID})
    fa.verb_confirm_vital(case, {"id": VITAL_IID})
    d = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert VITAL_IID in d["vital_doc_reviewed"]
    assert VITAL_PATH not in (d.get("vital_doc_dismissed") or {})


def test_signoff_unaffected_when_no_vital_stage(tmp_path):
    """A case where vital_doc_confirm never ran (no vital_doc_confirmed.json) has
    nothing to review — the vital gate is a no-op (OQ2: absent ⇒ cleared)."""
    case, paths = setup_case(tmp_path)
    _clear_quarantine(paths)
    _clear_human_review(case)
    assert fa.verb_signoff(case, dict(SIGN))["ok"]


# ── verb edge cases + input validation ────────────────────────────────────────

def test_signoff_requires_name_and_capacity(tmp_path):
    case, paths = setup_case(tmp_path)
    _clear_gate(case, paths)
    with pytest.raises(fa.VerbError):
        fa.verb_signoff(case, {"name": "", "capacity": "atty", "judgment": "go"})
    with pytest.raises(fa.VerbError):
        fa.verb_signoff(case, {"name": "J", "capacity": "", "judgment": "go"})


def test_signoff_rejects_unknown_mode(tmp_path):
    case, paths = setup_case(tmp_path)
    _clear_gate(case, paths)
    with pytest.raises(fa.VerbError):
        fa.verb_signoff(case, dict(SIGN, mode="turbo"))


def test_review_verbs_require_id(tmp_path):
    case, _ = setup_case(tmp_path)
    for verb in (fa.verb_review_keep, fa.verb_waive_review, fa.verb_confirm_vital):
        with pytest.raises(fa.VerbError):
            verb(case, {"reason": "x"})   # no id


def test_revoke_with_no_record_is_404(tmp_path):
    case, _ = setup_case(tmp_path)
    with pytest.raises(fa.VerbError) as exc:
        fa.verb_signoff_revoke(case, {})
    assert exc.value.code == 404


def test_revoke_is_idempotent(tmp_path):
    case, paths = setup_case(tmp_path)
    _clear_gate(case, paths)
    fa.verb_signoff(case, dict(SIGN))
    fa.verb_signoff_revoke(case, {})
    again = fa.verb_signoff_revoke(case, {})
    assert again.get("already_revoked") is True


def test_signoff_dispositions_count_from_action_log(tmp_path):
    """The certificate's release/discard/banish counts are parsed from
    family_actions.ndjson using the ACTUAL logged action strings — the discard
    verb logs "discard_quarantine" (underscore), not the "discard/quarantine"
    route key. A regression guard for the dead-count bug."""
    case, paths = setup_case(tmp_path)
    log = paths.metadata_dir / "family_actions.ndjson"
    log.write_text("".join(json.dumps({"action": a}) + "\n" for a in [
        "release", "release", "discard_quarantine", "banish", "banish", "banish",
        "confirm"]))
    disp = fa._signoff_dispositions(case)
    assert disp["quarantine_released"] == 2
    assert disp["quarantine_discarded"] == 1
    assert disp["banished"] == 3


def test_signoff_dispositions_excludes_undone_actions(tmp_path):
    """An undone banish is a net no-op and must not inflate the certificate."""
    case, paths = setup_case(tmp_path)
    log = paths.metadata_dir / "family_actions.ndjson"
    # a banish (token b1) later undone by an entry whose `undoes` == b1
    log.write_text(
        json.dumps({"action": "banish", "undo_token": "b1"}) + "\n"
        + json.dumps({"action": "banish", "undo_token": "b2"}) + "\n"
        + json.dumps({"action": "unbanish", "undo_token": "u1", "undoes": "b1"}) + "\n")
    disp = fa._signoff_dispositions(case)
    assert disp["banished"] == 1        # b2 stands; b1 was undone
