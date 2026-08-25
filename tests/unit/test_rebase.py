"""Load-time path rebasing (wyeast/core/rebase.py + its hook in the server).

A finished case records ABSOLUTE paths — the ones the pipeline workstation used
("/data/cases/813_mf/..."). Copy the delivered output/ tree to another machine and
every one of those strings is a dead path: the photo universe drops each tile whose
archive canonical fails os.path.exists(), and resolve_media_path refuses every
document with "path outside case". --cases-root does NOT fix this; it only decides
where the case FOLDER is (CasePaths.from_case_id), never the strings inside the
indexes.

These tests build a case, move it, and assert it still serves. The fixture mirrors
the two things a real delivery does:

  1. output/ comes along, the working trees (extracted/, original_files/) do not;
  2. documents and audio are delivered into output/<kind>/<category>/ rather than
     the extracted/ paths the indexes name.

so a plain prefix swap is necessary but NOT sufficient — see the relocation tests.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import tools.family_archive as fa  # noqa: E402
from wyeast.core.paths import CasePaths  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _case_fixture as _tbe  # noqa: E402

make_case = _tbe.make_case

RECORDED = "/data/cases/CASE_T"          # the workstation root baked into the indexes
REC_A = RECORDED + "/extracted/photos/a.jpg"
REC_B = RECORDED + "/extracted/photos/b.jpg"
REC_DOC = RECORDED + "/extracted/documents/invoice.pdf"
REC_AUDIO = RECORDED + "/extracted/other/audio/memo.m4a"


def local(paths, recorded):
    """The id the SERVER uses for a plainly-rebased recorded path once the case has
    moved. A RELOCATED item's id is its delivered path instead — the `delivered`
    map returned by relocated_case."""
    return recorded.replace(RECORDED, str(paths.case_dir), 1)


def relocated_case(tmp_path, role="examiner"):
    """A finished case as it arrives on a SECOND machine: the files sit under
    tmp_path, every recorded path still names RECORDED, and the working trees the
    indexes point at (extracted/) were never delivered.

    Returns (case, paths, delivered) where `delivered` maps the recorded id of each
    relocated item to the real file that carries its bytes here.
    """
    cases, case_dir = make_case(tmp_path)
    paths = CasePaths.from_case_id("CASE_T", str(cases))

    # Documents and audio are delivered into output/<kind>/<category>/, NOT into the
    # extracted/ paths the indexes name. Basename is the only link back.
    doc = paths.output_dir / "documents" / "financial" / "invoice.pdf"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_bytes(b"%PDF-1.4\n%%EOF\n")
    audio = paths.output_dir / "audio" / "voice_memo" / "memo.m4a"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"\x00\x00\x00\x20ftypM4A ")

    # A one-message email thread. The thread index names the message FILE; the
    # bodies live in email_index.json, keyed by that same path. Two indexes, two
    # readers, one join — which is exactly what a half-applied rebase breaks.
    eml = "/work/mail/INBOX.mbox#42"
    (paths.metadata_dir / "email_threads_index_examiner.json").write_text(json.dumps({
        "generated_at": "2026-06-28T00:00:00", "case_id": "CASE_T",
        "audience": "examiner",
        "threads": [{"thread_id": "t1", "subject": "Re: the cabin",
                     "message_count": 1, "participants": ["a@example.com"],
                     "date_first": "2020-05-01", "date_last": "2020-05-01",
                     "significance": 3, "categories": [], "linked_by": [],
                     "files": [eml]}],
    }))
    (paths.metadata_dir / "email_index.json").write_text(json.dumps([
        {"file": eml, "message_id": "<m1@example.com>",
         "email_subject": "Re: the cabin", "email_from": "a@example.com",
         "email_to": ["b@example.com"], "email_date_iso": "2020-05-01",
         "ocr_text": "The keys are under the mat.", "attachments": [],
         "thread_id": "t1", "audience": "family"},
    ]))

    # Add the audio row the base fixture has no reason to carry.
    summary_path = paths.metadata_dir / "case_summary.json"
    summary = json.loads(summary_path.read_text())
    summary["audio_classifications"] = [
        {"file": "/work/audio/memo.m4a", "filename": "memo.m4a",
         "category": "voice_memo", "significance": 3, "summary": "A voice memo."},
    ]
    summary["audio_counts"] = {"voice_memo": 1}
    summary["total_audio"] = 1
    summary_path.write_text(json.dumps(summary))

    # Re-root every index onto the workstation paths the pipeline actually wrote.
    for f in sorted(paths.metadata_dir.glob("*.json")):
        t = f.read_text()
        t = t.replace(str(case_dir), RECORDED)
        t = t.replace("/work/extracted/", RECORDED + "/extracted/photos/")
        t = t.replace("/work/docs/invoice.pdf", REC_DOC)
        t = t.replace("/work/audio/memo.m4a", REC_AUDIO)
        t = t.replace("/work/mail/", RECORDED + "/original_files/")
        f.write_text(t)

    case = fa.ArchiveCase(paths, role, {})
    return case, paths, {REC_DOC: doc, REC_AUDIO: audio}


# ── the bug: a relocated case serves nothing ────────────────────────────────────

def test_relocated_case_keeps_its_photos(tmp_path):
    """build_photo_universe drops any tile whose archive canonical fails an
    os.path.exists() check. Every canonical is a workstation path, so the gallery
    empties silently — the drop is by design (it is how quarantined items stay
    hidden), which is why this reads as "no photos" rather than as an error."""
    case, paths, _delivered = relocated_case(tmp_path)
    assert set(case.universe) == {local(paths, REC_A), local(paths, REC_B)}


def test_relocated_case_serves_photo_bytes(tmp_path):
    """archive_map maps the working-path id to the canonical, which must now
    resolve under the NEW case dir. The id itself stays a path under extracted/,
    which was never delivered — that is fine, and deliberately not "fixed": it is
    only ever a map key, never a file the server opens."""
    case, paths, _delivered = relocated_case(tmp_path)
    real = fa.resolve_media_path(case, local(paths, REC_A))
    assert real == paths.archive_dir / "a.jpg"
    assert real.is_file()


def test_ids_are_local_and_sidecars_are_recorded(tmp_path):
    """The id-space contract, stated once so it is not rediscovered by accident.

    IN MEMORY every path is LOCAL. That is what lets the existence checks, the
    resolvers, the OCR joins and the search index work without each of them having
    to know the case moved. ON DISK the case's own files keep the RECORDED form —
    so the next move rebases them too, instead of orphaning them.
    """
    case, paths, delivered = relocated_case(tmp_path)
    rows = {r["file"] for r in case.section("documents")["rows"]}
    assert str(delivered[REC_DOC]) in rows
    assert REC_DOC not in rows
    # The index on disk is untouched — rebasing is a read-time view, never a
    # rewrite of what the pipeline produced.
    raw = json.loads((paths.metadata_dir / "case_summary.json").read_text())
    assert {d["file"] for d in raw["document_classifications"]} >= {REC_DOC}


def test_relocated_case_serves_document_bytes(tmp_path):
    """The reported symptom. The Documents LIST renders fine (it is pure JSON), so
    the failure only shows on open: resolve_media_path refuses the recorded path
    with "path outside case". A prefix swap alone is not enough here — extracted/
    was never delivered, so the id must land on output/documents/<category>/."""
    case, _paths, delivered = relocated_case(tmp_path)
    row = next(r for r in case.section("documents")["rows"] if r["name"] == "invoice.pdf")
    real = fa.resolve_media_path(case, row["file"])
    assert real == delivered[REC_DOC]
    assert real.is_file()


def test_relocated_case_serves_audio_bytes(tmp_path):
    """Audio relocates exactly like documents: extracted/other/audio/X →
    output/audio/<category>/X."""
    case, _paths, delivered = relocated_case(tmp_path)
    row = next(r for r in case.section("recordings") if r["name"] == "memo.m4a")
    real = fa.resolve_media_path(case, row["file"])
    assert real == delivered[REC_AUDIO]
    assert real.is_file()


def test_relocated_case_keeps_ocr_joined_to_documents(tmp_path):
    """ocr_index is keyed by the SAME recorded path as document_classifications.
    Both must be rewritten identically or the preview text silently disappears —
    a join that breaks without erroring."""
    case, _paths, _delivered = relocated_case(tmp_path)
    row = next(r for r in case.section("documents")["rows"] if r["name"] == "invoice.pdf")
    assert _tbe.OCR_TOKEN in row["preview"]


def test_relocated_case_keeps_email_threads_joined_to_their_messages(tmp_path):
    """A thread lists its message FILES; the bodies live in email_index.json under
    the same paths. Those two indexes are read by DIFFERENT modules
    (wyeast.core.audience and tools._archive_data), so rebasing one and not the
    other leaves every thread reporting zero messages — with no error anywhere.
    This is the shape of failure the shared registry exists to prevent."""
    case, _paths, _delivered = relocated_case(tmp_path)
    detail = case.thread_messages("t1")
    assert detail["subject"] == "Re: the cabin"
    assert len(detail["messages"]) == 1
    assert "keys are under the mat" in detail["messages"][0]["body"]


# ── the guards: what the rebase must NOT change ─────────────────────────────────

def test_in_place_case_is_untouched(tmp_path):
    """A case served from where it was produced must take the identity path — no
    detection, no rewriting, ids byte-identical to what the pipeline wrote."""
    cases, _case_dir = make_case(tmp_path)
    paths = CasePaths.from_case_id("CASE_T", str(cases))
    case = fa.ArchiveCase(paths, "examiner", {})
    assert set(case.universe) == {"/work/extracted/a.jpg", "/work/extracted/b.jpg"}
    rows = {r["file"] for r in case.section("documents")["rows"]}
    assert "/work/docs/invoice.pdf" in rows


def test_decisions_sidecar_is_written_in_recorded_form(tmp_path):
    """Family decisions are keyed by item path. They must persist in RECORDED form,
    or moving the case again orphans every decision the family made — the sidecar
    would be keyed to a location that no longer exists and no longer rebases."""
    case, paths, delivered = relocated_case(tmp_path)
    res = fa.verb_move(case, {"view": "document",
                              "src": str(delivered[REC_DOC]), "to": "legal"})
    assert res["ok"]
    raw = json.loads((paths.metadata_dir / "family_decisions.json").read_text())
    assert raw["doc_placements"] == {REC_DOC: "legal"}
    # …and it still reads back onto the relocated document.
    row = next(r for r in case.section("documents")["rows"] if r["name"] == "invoice.pdf")
    assert row["category"] == "legal"


def test_audit_log_is_written_in_recorded_form_and_reads_back_local(tmp_path):
    """History links on an action's `target`, so the audit log is an id space too.
    Same rule as the decision sidecars: recorded on disk, local in memory. Without
    both halves History becomes the one surface still pointing at dead paths."""
    case, paths, delivered = relocated_case(tmp_path)
    fa.verb_move(case, {"view": "document", "src": str(delivered[REC_DOC]), "to": "legal"})
    line = json.loads(
        (paths.metadata_dir / "family_actions.ndjson").read_text().strip().splitlines()[-1])
    assert line["target"] == REC_DOC
    rows, _by_token, _undone = case.actions_index()   # newest-first
    assert rows[0]["target"] == str(delivered[REC_DOC])


def test_missing_delivered_file_stays_missing(tmp_path):
    """An item with no delivered bytes must 404 on open, not resolve to some other
    file that happens to be nearby. Relocation is basename-exact or nothing."""
    case, _paths, _delivered = relocated_case(tmp_path)
    with pytest.raises(fa.VerbError):
        fa.resolve_media_path(case, RECORDED + "/extracted/documents/never-delivered.pdf")


# ── the rewriting rules themselves ──────────────────────────────────────────────

from wyeast.core import rebase as rb  # noqa: E402


def test_detects_recorded_root_from_archive_map():
    got = rb.detect_recorded_root({"archive_root": "/data/cases/813_mf/output/archive"},
                                  "813_mf")
    assert got == "/data/cases/813_mf"


def test_detects_recorded_root_from_a_sample_path_when_no_archive_map():
    """build_archive is optional, so a case can legitimately arrive with no map."""
    got = rb.detect_recorded_root({}, "813_mf",
                                  samples=["/srv/wyeast/813_mf/extracted/documents/a.pdf"])
    assert got == "/srv/wyeast/813_mf"


def test_case_id_matching_a_non_case_directory_is_not_a_root():
    """The segment after the case id has to be a case subdirectory, or the id just
    collided with some unrelated folder name."""
    assert rb.detect_recorded_root({}, "813_mf",
                                   samples=["/home/pics/813_mf/holiday/a.jpg"]) is None


def test_no_rebaser_when_the_case_has_not_moved(tmp_path):
    assert rb.PathRebaser.for_case(tmp_path, str(tmp_path)) is rb.IDENTITY
    assert rb.PathRebaser.for_case(tmp_path, None) is rb.IDENTITY


def test_rewrites_only_whole_prefixes(tmp_path):
    r = rb.PathRebaser("/data/cases/C", tmp_path)
    assert r.local("/data/cases/C/output/a.jpg") == f"{tmp_path}/output/a.jpg"
    # A path merely QUOTED inside prose is left alone — the rewrite is anchored.
    assert r.local("see /data/cases/C/output/a.jpg") == "see /data/cases/C/output/a.jpg"
    # A sibling case whose name merely starts the same way is not this case.
    assert r.local("/data/cases/C2/output/a.jpg") == "/data/cases/C2/output/a.jpg"


def test_rebase_round_trips(tmp_path):
    r = rb.PathRebaser("/data/cases/C", tmp_path)
    for p in ("/data/cases/C/output/archive/a.jpg", "/elsewhere/x", "not a path"):
        assert r.recorded(r.local(p)) == p


def test_relocation_fails_closed_on_a_colliding_basename(tmp_path):
    """The same filename delivered into two categories cannot be told apart by
    basename, so it relocates to NEITHER — an honest 404 beats the wrong file."""
    for category in ("legal", "medical"):
        d = tmp_path / "output" / "documents" / category
        d.mkdir(parents=True)
        (d / "scan.pdf").write_bytes(b"%PDF")
    (tmp_path / "output" / "documents" / "legal" / "unique.pdf").write_bytes(b"%PDF")
    forward, _reverse, report = rb.build_relocations("/data/cases/C", tmp_path)
    assert "extracted/documents/scan.pdf" not in forward
    assert forward["extracted/documents/unique.pdf"].endswith("legal/unique.pdf")
    assert report[0]["mapped"] == 1 and report[0]["ambiguous"] == 1


def test_no_relocation_when_the_working_tree_came_along(tmp_path):
    """A full case (not a delivery) keeps extracted/ — its paths need the prefix
    swap and nothing else, so relocation must not second-guess them."""
    (tmp_path / "extracted" / "documents").mkdir(parents=True)
    (tmp_path / "extracted" / "documents" / "a.pdf").write_bytes(b"%PDF")
    d = tmp_path / "output" / "documents" / "legal"
    d.mkdir(parents=True)
    (d / "a.pdf").write_bytes(b"%PDF")
    forward, _reverse, report = rb.build_relocations("/data/cases/C", tmp_path)
    assert forward == {} and report == []


def test_ocr_sidecars_are_never_a_relocation_target(tmp_path):
    """output/documents/_ocr_sidecars/ mirrors every document's NAME with .txt/.json
    sidecars; indexing it would add thousands of names that are not documents."""
    side = tmp_path / "output" / "documents" / "_ocr_sidecars"
    side.mkdir(parents=True)
    (side / "a.pdf").write_bytes(b"decoy")
    d = tmp_path / "output" / "documents" / "legal"
    d.mkdir(parents=True)
    (d / "a.pdf").write_bytes(b"%PDF")
    forward, _reverse, _report = rb.build_relocations("/data/cases/C", tmp_path)
    assert forward["extracted/documents/a.pdf"] == str(d / "a.pdf")
