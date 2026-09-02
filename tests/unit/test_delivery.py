"""Does the archive still behave once the working trees are not there?

THE GAP THIS SUITE CLOSES. A delivery carries `output/` and leaves the rest of the
case tree on the workstation — no `extracted/`, no `quarantine/`, no `duplicates/`,
no `original_files/`. Every copy this app serves is a delivery. But the tests build
whatever tree they need, so they all run against a workstation, and an entire class
of defect — works in pytest, refuses on the served copy — was invisible.

Five shipped that way in a single session before this suite existed:

  * Document Photos listed 27 tiles whose bytes had moved out; each rendered as a
    broken image and each was counted in the page's own total.
  * Discard on a document selection called `banish`, which refuses anything outside
    `output/archive/`. It skipped every item, reported success, and the documents
    stayed — with nothing written, so even the audit log was silent.
  * Un-junk refused 394 of the first 400 rows ("junk file not present") because it
    tested for the original working file. 388 of those were perfectly rescuable.
  * The quarantine tab offered a preview and two verbs for 149 items whose bytes are
    not in the copy. All three could only 404.
  * The sensitivity tab did the same for 27 more.

Each test here sets its case up however it likes, then calls `deliver()` and asks
the question that matters. Add to this suite whenever a verb or a preview learns to
touch the filesystem.

Run under venv-phase1.
"""
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tools._archive_data as ad  # noqa: E402
import tools.family_archive as fa  # noqa: E402
import _case_fixture as _cf  # noqa: E402
import test_family_archive as T  # noqa: E402  (reuses its case/junk/vital helpers)

SCANNED = "scanned document or handwritten letter"


def _delivered(tmp_path, **kw):
    """A case set up, then stripped to what a served copy actually has."""
    case, paths = T.setup_case(tmp_path, **kw)
    _cf.deliver(paths.case_dir)
    case.load()
    return case, paths


# ── the helper itself, so this suite cannot quietly stop testing a delivery ──

def test_deliver_leaves_only_the_delivered_trees(tmp_path):
    case, paths = T.setup_case(tmp_path)
    (paths.extracted_dir / "photos").mkdir(parents=True, exist_ok=True)
    (paths.case_dir / "quarantine").mkdir(exist_ok=True)
    (paths.case_dir / "duplicates").mkdir(exist_ok=True)
    _cf.deliver(paths.case_dir)
    left = sorted(p.name for p in paths.case_dir.iterdir())
    assert left == ["output"] or left == ["case_config.json", "output"], left
    for gone in ("extracted", "quarantine", "duplicates", "original_files"):
        assert not (paths.case_dir / gone).exists(), gone + " survived delivery"


# ── 1. never list a tile whose bytes are gone (the Document Photos defect) ──

def test_scanned_rows_drop_items_whose_canonical_is_gone_on_a_delivery(tmp_path):
    case, paths = _delivered(tmp_path)
    here = paths.archive_dir / "doc_here.jpg"
    here.write_bytes(b"\xff\xd8\xff\xd9")
    gone = paths.archive_dir / "doc_gone.jpg"          # never created
    scene = {"clip_results": {
        "/work/extracted/here.jpg": {"category": SCANNED, "delivered": True},
        "/work/extracted/gone.jpg": {"category": SCANNED, "delivered": True},
    }, "junk_results": {}}
    amap = {"entries": {"/work/extracted/here.jpg": str(here),
                        "/work/extracted/gone.jpg": str(gone)}}
    for role in ("examiner", "family"):
        ids = [r["id"] for r in ad.scanned_image_rows(scene, amap, {}, role)]
        assert ids == ["/work/extracted/here.jpg"], role


def test_photo_universe_drops_items_whose_canonical_is_gone_on_a_delivery(tmp_path):
    # The rule the scanned list mirrors: a tile whose delivered copy is missing is
    # not a tile, for EITHER role.
    case, paths = _delivered(tmp_path)
    gone = paths.archive_dir / "vanished.jpg"          # never created
    scene = {"clip_results": {"/work/extracted/v.jpg": {"category": "beach",
                                                        "delivered": True}},
             "junk_results": {}}
    amap = {"entries": {"/work/extracted/v.jpg": str(gone)}}
    for role in ("examiner", "family"):
        assert ad.build_photo_universe(scene, amap, role) == {}, role


# ── 2. a document can be discarded with no working tree ──

def test_documents_can_be_discarded_on_a_delivery(tmp_path):
    case, paths = _delivered(tmp_path)
    rows = ad.document_rows(case.summary, case.ocr_index, "examiner")
    assert rows, "fixture delivers documents"
    src = rows[0]["file"]
    assert not os.path.exists(src), "a delivery has no working copy of it"
    res = fa.verb_discard_document(case, {"srcs": [src]})
    assert res["count"] == 1 and res["skipped"] == 0, "must not be skipped"
    after = ad.document_rows(case.summary, case.ocr_index, "examiner",
                             discarded=case.decisions.get("doc_discarded"))
    assert src not in [r["file"] for r in after]


# ── 3. un-junk needs the delivered copy, not the working one ──

def test_unjunk_works_on_a_delivery(tmp_path):
    case, paths = T.setup_case(tmp_path)
    key, work_file = T._add_label_junk(case, paths, "junky.png", with_archive=True)
    _cf.deliver(paths.case_dir)          # takes the working file with it
    case.load()
    assert not Path(key).exists()
    res = fa.verb_unjunk(case, {"id": key})
    assert res["ok"]
    universe = ad.build_photo_universe(case.scene_index, case.archive_map, "examiner",
                                       rescued=case.decisions.get("junk_rescued"))
    assert key in universe, "the rescued item rejoins the gallery"


# ── 4 + 5. what CANNOT work must say so rather than offer itself ──

def _seed_quarantine(paths, qpath):
    _cf._write(paths.metadata_dir / "quarantine_manifest.json", {"entries": [
        {"file": "flagged.jpg", "filter": "explicit_sexual_content",
         "timestamp": "2026-01-01T00:00:00",
         "canonical_path": str(paths.archive_dir / "flagged.jpg"),
         "quarantine_path": str(qpath)},
    ]})


def test_quarantine_is_marked_unactionable_on_a_delivery(tmp_path):
    case, paths = T.setup_case(tmp_path)
    qpath = paths.case_dir / "quarantine" / "explicit" / "flagged.jpg"
    qpath.parent.mkdir(parents=True, exist_ok=True)
    qpath.write_bytes(b"\xff\xd8\xff\xd9")
    _seed_quarantine(paths, qpath)
    case.load()
    assert case.quarantine_section()["entries"][0]["present"] is True

    _cf.deliver(paths.case_dir)          # the quarantine tree does not ship
    case.load()
    row = case.quarantine_section()["entries"][0]
    assert row["present"] is False, "the list must say the bytes are not here"
    items = ad.quarantine_pager_items(case.quarantine_entries())
    assert items[0]["present"] is False
    assert items[0]["actions"] == [], "release/discard both move the file: offer neither"
    assert items[0]["src"] is None, "no link that can only 404"
    # and the verbs refuse honestly rather than reporting a silent success
    with pytest.raises(fa.VerbError):
        fa.verb_release(case, {"canonical_path": row["canonical_path"]})
    with pytest.raises(fa.VerbError):
        fa.verb_discard_quarantine(case, {"canonical_path": row["canonical_path"]})


def test_sensitivity_rows_marked_when_their_bytes_are_not_delivered(tmp_path):
    case, paths = T.setup_case(tmp_path)
    flagged = paths.case_dir / "duplicates" / "documents" / "dupe.jpg"
    flagged.parent.mkdir(parents=True, exist_ok=True)
    flagged.write_bytes(b"\xff\xd8\xff\xd9")
    _cf._write(paths.metadata_dir / "sensitive_scan_index.json", {
        str(flagged): {"sensitivity_filters": {"weapons": {"triggered": True}}},
    })
    _cf._write(paths.metadata_dir / "quarantine_manifest.json", {"entries": []})
    before = {s["name"]: s for s in ad.review_data(paths, {})["sensitive"]}
    assert before["dupe.jpg"]["present"] is True

    _cf.deliver(paths.case_dir)          # duplicates/ does not ship
    after = {s["name"]: s for s in ad.review_data(paths, {})["sensitive"]}
    assert after["dupe.jpg"]["present"] is False, "cannot be previewed on this copy"
    # The flag is still part of the record, and `src` still says what the file IS.
    assert after["dupe.jpg"]["filters"] == ["weapons"]
    assert after["dupe.jpg"]["src"] == str(flagged)


# ── 6. the decision overlays must not care about the working tree at all ──

def test_vital_decisions_survive_a_delivery(tmp_path):
    """Confirm / not-this-type / dismiss are pure overlays and must never need a
    file. They are the model the other verbs should follow where they can."""
    case, paths = T.setup_case(tmp_path)
    T._seed_vitals(paths)
    _cf.deliver(paths.case_dir)
    case.load()
    iid = "deed_title::/d/shared.pdf"
    assert fa.verb_not_type_vital(case, {"id": iid})["ok"]
    assert fa.verb_confirm_vital(case, {"id": iid})["ok"]
    assert fa.verb_dismiss_vital(case, {"id": iid})["ok"]
