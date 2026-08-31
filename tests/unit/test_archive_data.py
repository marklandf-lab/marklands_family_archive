"""Direct unit coverage for the shared data layer (tools/_archive_data.py).

The refactor that extracted these builders from build_explorer.py enables testing
them directly (the explorer tests only exercised them through main()). Focus on
the server-only builders and the role-sensitive ones.

Run under venv-phase1.
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools import _archive_data as ad  # noqa: E402
from wyeast.core.paths import CasePaths  # noqa: E402

SECRET = "SECRETVALUE_DO_NOT_LEAK"


def test_accounts_data_never_emits_secret_and_gates_by_role():
    summary = {
        "digital_account_inventory": {
            "linkedin.com": {"count": 12, "sample_subjects": ["You appeared in 3 searches"]},
            "instagram.com": {"count": 4, "sample_subjects": ["New login"]},
        },
        "document_counts": {"financial": 7},
        "credentials_report": {
            "critical_count": 1, "informational_count": 2,
            "items": [{"file": "passwords.txt", "credential_types": ["password"],
                       "severity": "critical", "secret_value": SECRET}],
        },
    }
    fam = ad.accounts_data(summary, "family")
    exm = ad.accounts_data(summary, "examiner")
    assert SECRET not in json.dumps(fam) and SECRET not in json.dumps(exm)
    # domains sorted by count desc
    assert fam["domains"][0]["domain"] == "linkedin.com"
    assert fam["financial_docs_count"] == 7
    # family credential rows are filename-only; examiner gets types/severity
    assert fam["credentials"]["items"][0] == {"file": "passwords.txt"}
    assert exm["credentials"]["items"][0]["severity"] == "critical"
    # B7: family sees the filenames WITH a caution note; examiner gets no note
    # (they have full detail). Guidance never leaks a secret.
    assert fam["credentials"]["guidance"] == ad.CREDENTIAL_FAMILY_GUIDANCE
    assert "guidance" not in exm["credentials"]
    assert SECRET not in fam["credentials"]["guidance"]


def test_credential_guidance_absent_when_no_credential_files():
    summary = {"credentials_report": {"critical_count": 0, "informational_count": 0,
                                       "items": []}, "document_counts": {}}
    fam = ad.accounts_data(summary, "family")
    assert "guidance" not in fam["credentials"]  # nothing to caution about


def test_confirm_queue_surfaces_low_confidence_only():
    summary = {"event_albums": [{"album_id": "0", "title": "Tahoe 2004"}]}
    scene_index = {"clip_results": {
        "/x/low.jpg": {"category": "beach", "confidence": 0.2, "delivered": True},
        "/x/high.jpg": {"category": "wedding", "confidence": 0.95, "delivered": True},
    }}
    face_clustering = {
        "person_clusters": {"Person_01": ["/x/a.jpg"], "Person_02": ["/x/b.jpg"]},
        "cluster_identities": {"Person_02": {"name": "Jane"}},
        "noise_files": ["/x/face1.jpg"],
    }
    geo_index = {"/x/a.jpg": {"face_cluster_merge_candidates": ["Person_01", "Person_03"]}}
    q = ad.confirm_queue_data(summary, scene_index, face_clustering, geo_index)
    kinds = {i["kind"] for i in q}
    assert "scene_guess" in kinds and "unidentified_face" in kinds
    assert "face_merge" in kinds and "event_guess" in kinds
    # only the unnamed person is surfaced for naming, not the named one
    unnamed = [i for i in q if i["kind"] == "unnamed_person"]
    assert [i["id"] for i in unnamed] == ["Person_01"]
    # the high-confidence scene is NOT in the queue
    assert all(i["id"] != "/x/high.jpg" for i in q)


def test_confirm_queue_data_uncapped_by_default():
    """scene_cap/face_cap used to default to 300 each, silently truncating the
    queue and its count regardless of the true backlog (P0: an examiner working
    through a >300-item backlog saw the "to do" count never move). Both must
    default to unbounded now."""
    scene_index = {"clip_results": {
        f"/x/low{i}.jpg": {"category": "beach", "confidence": 0.2, "delivered": True}
        for i in range(305)
    }}
    face_clustering = {"noise_files": [f"/x/face{i}.jpg" for i in range(305)]}
    q = ad.confirm_queue_data({}, scene_index, face_clustering, {})
    scene_n = sum(1 for i in q if i["kind"] == "scene_guess")
    face_n = sum(1 for i in q if i["kind"] == "unidentified_face")
    assert scene_n == 305, "no default cap on scene guesses"
    assert face_n == 305, "no default cap on unidentified faces"


def test_confirm_queue_count_decrements_past_the_old_cap():
    """Confirming items must actually shrink the queue length past what used to
    be a hard 300-item cap, proving progress is now visible (not backfilled from
    beyond a truncation point)."""
    scene_index = {"clip_results": {
        f"/x/low{i}.jpg": {"category": "beach", "confidence": 0.2, "delivered": True}
        for i in range(350)
    }}
    q0 = ad.confirm_queue_data({}, scene_index, {}, {})
    assert len(q0) == 350
    decisions = {"scene": {f"/x/low{i}.jpg": {"decision": "confirm"} for i in range(60)}}
    q1 = ad.confirm_queue_data({}, scene_index, {}, {}, decisions=decisions)
    assert len(q1) == 290, "count must drop by exactly the number resolved, well past the old 300 cap"


def test_people_rows_named_flag_and_dict_identities():
    face_clustering = {
        "person_clusters": {"Person_01": ["/x/a.jpg", "/x/b.jpg"]},
        "cluster_identities": {"Person_01": {"name": "Jane Harding"}},
    }
    rows = ad.people_rows(face_clustering, {}, {"/x/a.jpg": {}, "/x/b.jpg": {}}, {}, "examiner")
    assert rows[0]["name"] == "Jane Harding"
    assert rows[0]["named"] is True
    assert rows[0]["sample_ids"][:2] == ["/x/a.jpg", "/x/b.jpg"]
    # full member list for the person drill-through view (#1)
    assert rows[0]["member_ids"] == ["/x/a.jpg", "/x/b.jpg"]


def test_people_rows_rename_replaces_internal_id_in_summary():
    # #21: the narrative LLM only ever saw the internal cluster id, so the
    # summary sentence itself still said "Photos capture Person_01..." after a
    # rename even though the card's own title correctly showed the new name.
    face_clustering = {
        "person_clusters": {"Person_01": ["/x/a.jpg"]},
        "cluster_identities": {"Person_01": {"name": "Dawn Merrick"}},
    }
    summary_doc = {"photo_clusters": [
        {"person_id": "Person_01",
         "summary": "Photos capture Person_01 in various everyday settings."}]}
    rows = ad.people_rows(face_clustering, summary_doc, {"/x/a.jpg": {}}, {}, "examiner")
    assert rows[0]["summary"] == "Photos capture Dawn Merrick in various everyday settings."

    # An UNNAMED person (still "Person_01" as their own display name) must not
    # have their own summary mangled — the substitution only fires on a rename.
    unnamed_clustering = {"person_clusters": {"Person_01": ["/x/a.jpg"]}, "cluster_identities": {}}
    rows2 = ad.people_rows(unnamed_clustering, summary_doc, {"/x/a.jpg": {}}, {}, "examiner")
    assert rows2[0]["summary"] == "Photos capture Person_01 in various everyday settings."


def test_people_rows_excludes_video_frames():
    # A person's faces in video keyframes are "video appearances" (person detail),
    # never photos in the People list — keyframes must not leak into the samples,
    # the photo count excludes them, and a distinct video_count is reported.
    face_clustering = {
        "person_clusters": {"Person_01": ["/x/a.jpg", "/x/clip_f000001.jpg", "/x/b.jpg"]},
        "cluster_identities": {},
    }
    universe = {"/x/a.jpg": {}, "/x/b.jpg": {}, "/x/clip_f000001.jpg": {}}
    frame_map = {"/x/clip_f000001.jpg": {"source_video": "/v/clip.mov"}}
    rows = ad.people_rows(face_clustering, {}, universe, {}, "examiner", frame_map=frame_map)
    assert rows[0]["sample_ids"] == ["/x/a.jpg", "/x/b.jpg"]
    assert "/x/clip_f000001.jpg" not in rows[0]["member_ids"]
    # photo count is photos-only (excludes the keyframe); the person still appears
    assert rows[0]["photo_count"] == 2
    # one distinct source video the person appears in
    assert rows[0]["video_count"] == 1


def test_people_rows_video_only_person_gets_frame_thumbs_and_honest_summary():
    # A person recorded only on video (0 delivered photos) has nothing in `sample`
    # for the list card to draw a thumbnail strip from — it must fall back to their
    # own video-frame members instead of leaving sample_ids empty (#12), and the
    # summary must not repeat the LLM's photo-oriented narrative (which had nothing
    # real to describe and hallucinated "Photos show..." text for zero photos).
    face_clustering = {
        "person_clusters": {"Person_02": ["/x/clip_f000001.jpg", "/x/clip_f000002.jpg"]},
        "cluster_identities": {},
    }
    universe = {"/x/clip_f000001.jpg": {}, "/x/clip_f000002.jpg": {}}
    frame_map = {"/x/clip_f000001.jpg": {"source_video": "/v/clip.mov"},
                 "/x/clip_f000002.jpg": {"source_video": "/v/clip.mov"}}
    summary = {"photo_clusters": [{"person_id": "Person_02",
                                   "summary": "Photos show Person_02 in various settings."}]}
    rows = ad.people_rows(face_clustering, summary, universe, {}, "examiner", frame_map=frame_map)
    assert rows[0]["photo_count"] == 0
    assert rows[0]["video_count"] == 1
    assert rows[0]["sample_ids"] == ["/x/clip_f000001.jpg", "/x/clip_f000002.jpg"]
    assert rows[0]["member_ids"] == []   # member_ids stays photo-only
    assert rows[0]["summary"] == "No photographs of this person — appears in 1 video."


def test_people_rows_drops_archive_missing_member(tmp_path):
    # A discarded/moved-out face member (archive copy gone) must not appear in the
    # examiner People samples — its thumbnail would 404. Present-but-not-in-universe
    # members (e.g. junk/doc-misclassified faces) are KEPT (archive still on disk).
    here = tmp_path / "a.jpg"; here.write_bytes(b"\xff\xd8\xff\xd9")
    face_clustering = {"person_clusters": {"Person_01": ["/src/a.jpg", "/src/gone.jpg"]},
                       "cluster_identities": {}}
    archive_map = {"entries": {"/src/a.jpg": str(here),
                               "/src/gone.jpg": str(tmp_path / "gone.jpg")}}  # gone: no file
    rows = ad.people_rows(face_clustering, {}, {}, {}, "examiner", archive_map=archive_map)
    assert rows[0]["sample_ids"] == ["/src/a.jpg"]
    assert "/src/gone.jpg" not in rows[0]["member_ids"]
    assert rows[0]["photo_count"] == 1


def _video_only_person(tmp_path, video_on_disk):
    """A person seen only in one video, whose extracted keyframes are still on
    disk (they always are — frames live in extracted/, nothing moves them)."""
    fc = {"person_clusters": {"Person_02": ["/x/clip_f000001.jpg", "/x/clip_f000002.jpg"]},
          "cluster_identities": {}}
    frame_map = {"/x/clip_f000001.jpg": {"source_video": "/src/clip.mov"},
                 "/x/clip_f000002.jpg": {"source_video": "/src/clip.mov"}}
    canonical = tmp_path / "clip.mov"
    if video_on_disk:
        canonical.write_bytes(b"\x00")
    return fc, frame_map, {"entries": {"/src/clip.mov": str(canonical)}}


def test_people_rows_hides_person_whose_only_video_was_quarantined(tmp_path):
    # The reported leak (case usbd, Person_08/10/14): a video quarantined as
    # explicit content leaves its extracted keyframes behind in extracted/photos,
    # and the video-only card fallback drew its thumbnail strip straight from
    # them — publishing stills of exactly the footage quarantine pulled. present()
    # cannot catch it: a frame is never an archive_map key, so it always reads as
    # present. The source video's archive copy is the thing to check.
    fc, frame_map, archive_map = _video_only_person(tmp_path, video_on_disk=False)
    for role in ("examiner", "family"):
        rows = ad.people_rows(fc, {}, {"/x/clip_f000001.jpg": {}, "/x/clip_f000002.jpg": {}},
                              {}, role, frame_map=frame_map, archive_map=archive_map)
        # No photos, no viewable video: the person leaves the People list entirely
        # rather than showing an empty card under a photo-oriented LLM summary.
        assert rows == [], role


def test_people_rows_keeps_video_only_person_whose_video_is_delivered(tmp_path):
    # The other side of the guard: a delivered video's frames are still the right
    # thumbnails for a video-only person (#12) — the fix must not empty them out.
    fc, frame_map, archive_map = _video_only_person(tmp_path, video_on_disk=True)
    rows = ad.people_rows(fc, {}, {"/x/clip_f000001.jpg": {}, "/x/clip_f000002.jpg": {}},
                          {}, "examiner", frame_map=frame_map, archive_map=archive_map)
    assert rows[0]["video_count"] == 1
    assert rows[0]["sample_ids"] == ["/x/clip_f000001.jpg", "/x/clip_f000002.jpg"]


def test_people_rows_family_hides_frames_of_undelivered_video(tmp_path):
    # Family role: a video with no archive entry at all was never delivered, so
    # its frames must not become that role's person thumbnails either — the same
    # rule person_detail already applies to video appearances.
    fc = {"person_clusters": {"Person_02": ["/x/clip_f000001.jpg"]}, "cluster_identities": {}}
    frame_map = {"/x/clip_f000001.jpg": {"source_video": "/src/clip.mov"}}
    rows = ad.people_rows(fc, {}, {"/x/clip_f000001.jpg": {}}, {}, "family",
                          frame_map=frame_map, archive_map={"entries": {}})
    assert rows == []
    # …while the examiner, who sees undelivered material, still gets the person.
    rows = ad.people_rows(fc, {}, {"/x/clip_f000001.jpg": {}}, {}, "examiner",
                          frame_map=frame_map, archive_map={"entries": {}})
    assert rows[0]["video_count"] == 1


def test_scanned_image_rows_excludes_video_frames():
    # A video keyframe CLIP-mislabeled as a scanned document must not surface in
    # Correspondence (the reported bug).
    label = list(ad.SCENE_LABELS)[0]
    scene_index = {"clip_results": {
        "/x/doc.jpg": {"category": label, "delivered": True},
        "/x/clip_f000002.jpg": {"category": label, "delivered": True},
    }}
    rows = ad.scanned_image_rows(scene_index, {"entries": {}}, {}, "examiner")
    ids = [r["id"] for r in rows]
    assert "/x/doc.jpg" in ids and "/x/clip_f000002.jpg" not in ids


def test_review_data_remaps_video_frame_to_source_video(tmp_path):
    # A flagged keyframe on the examiner review lists is shown as its SOURCE VIDEO
    # (a viewport into the movie), never a bare still.
    md = tmp_path / "md"; md.mkdir()
    (md / "sensitive_scan_index.json").write_text(json.dumps({
        "/x/clip_f000001.jpg": {"sensitivity_filters": {"nudity": {"triggered": True}}},
        "/x/photo.jpg": {"sensitivity_filters": {"nudity": {"triggered": True}}},
    }))
    (md / "video_frame_map.json").write_text(json.dumps(
        {"/x/clip_f000001.jpg": {"source_video": "/v/movie.mov"}}))
    (md / "human_review_required.json").write_text(json.dumps({"paths": ["/x/clip_f000001.jpg"]}))

    class _P:
        metadata_dir = md
    data = ad.review_data(_P(), {})
    sby = {r["name"]: r for r in data["sensitive"]}
    assert sby["movie.mov"]["src"] == "/v/movie.mov", "frame shown as its source video"
    assert "clip_f000001.jpg" not in sby, "bare keyframe still must not appear"
    assert sby["photo.jpg"]["src"] == "/x/photo.jpg", "real photos unaffected"
    # same remap on the human-review list
    assert data["human_review"][0]["src"] == "/v/movie.mov"


def test_overview_data_attaches_clickthrough_targets():
    summary = {
        "ranked_items": [
            {"type": "photo_cluster", "label": "Person_01", "person_id": "Person_01"},
            {"type": "scene", "label": "holiday celebration"},
            {"type": "document", "label": "will.pdf"},
            {"type": "document", "label": "msg.eml"},
        ],
        "document_classifications": [
            {"file": "/d/will.pdf", "filename": "will.pdf", "source": "document"},
            {"file": "/m/msg.eml", "filename": "msg.eml", "source": "email"},
        ],
    }
    ov = ad.overview_data(summary, "examiner", {})
    tg = {r["label"]: r["target"] for r in ov["ranked_top"]}
    assert tg["Person_01"] == {"page": "people", "person_id": "Person_01"}
    assert tg["holiday celebration"] == {"page": "photos", "scene": "holiday celebration"}
    assert tg["will.pdf"] == {"page": "documents", "open": True, "file": "/d/will.pdf"}
    assert tg["msg.eml"] == {"page": "emails"}  # emails route to the section, not a file


def test_document_rows_excludes_email_and_carries_text_kind():
    summary = {"document_classifications": [
        {"file": "/d/letter.pdf", "filename": "letter.pdf", "category": "personal_correspondence",
         "source": "document", "significance": 4, "summary": "A typed letter."},
        {"file": "/d/scan.png", "filename": "scan.png", "category": "personal_correspondence",
         "source": "document", "significance": 3, "summary": "A handwritten note."},
        {"file": "/m/msg.eml", "filename": "msg.eml", "category": "personal_correspondence",
         "source": "email", "significance": 5, "summary": "An email."},
        {"file": "/d/tax.pdf", "filename": "tax.pdf", "category": "financial",
         "subcategory": "banking", "source": "document", "significance": 2, "summary": "Statement."},
    ]}
    ocr = [{"file": "/d/letter.pdf", "ocr_text": "Dear", "text_kind": "printed"},
           {"file": "/d/scan.png", "ocr_text": "hi", "text_kind": "handwritten"}]
    rows = ad.document_rows(summary, ocr, "examiner")
    files = [r["file"] for r in rows]
    assert "/m/msg.eml" not in files, "emails must be routed out of document_rows"
    assert len(rows) == 3
    by_file = {r["file"]: r for r in rows}
    assert by_file["/d/letter.pdf"]["text_kind"] == "printed"
    assert by_file["/d/scan.png"]["text_kind"] == "handwritten"
    assert by_file["/d/tax.pdf"]["subcategory"] == "banking"
    # a doc with no OCR entry still has the field, as None
    assert by_file["/d/tax.pdf"]["text_kind"] is None


def test_doc_placements_overrides_document_rows_category():
    """§13.2: a doc_placements entry re-buckets a row's effective category, and the
    derived documents_index counts move with it (browse counts are derived from the
    overlaid rows)."""
    summary = {"document_classifications": [
        {"file": "/d/a.pdf", "filename": "a.pdf", "category": "miscellaneous",
         "source": "document", "significance": 3, "summary": "A note."},
        {"file": "/d/b.pdf", "filename": "b.pdf", "category": "miscellaneous",
         "source": "document", "significance": 2, "summary": "Another note."},
    ]}
    # No overlay: both are miscellaneous.
    base = ad.document_rows(summary, [], "examiner")
    assert {r["file"]: r["category"] for r in base} == {
        "/d/a.pdf": "miscellaneous", "/d/b.pdf": "miscellaneous"}
    assert {c["category"]: c["count"] for c in ad.documents_index(base)} == {"miscellaneous": 2}
    # Overlay a.pdf → legal: the row's category flips and the browse counts follow.
    rows = ad.document_rows(summary, [], "examiner", doc_placements={"/d/a.pdf": "legal"})
    by = {r["file"]: r for r in rows}
    assert by["/d/a.pdf"]["category"] == "legal"
    assert by["/d/b.pdf"]["category"] == "miscellaneous"
    idx = {c["category"]: c["count"] for c in ad.documents_index(rows)}
    assert idx == {"legal": 1, "miscellaneous": 1}, "documents_index counts are DERIVED"


def test_doc_move_into_letter_category_appears_on_correspondence_and_leaves_documents():
    """§13.2: the Correspondence page filters document_rows by LETTER_CATEGORIES —
    the SAME (overlaid) builder — so a doc moved INTO personal_correspondence shows
    on Correspondence and leaves its old Documents bucket."""
    LETTER_CATEGORIES = {"personal_correspondence", "work_correspondence"}
    summary = {"document_classifications": [
        {"file": "/d/a.pdf", "filename": "a.pdf", "category": "miscellaneous",
         "source": "document", "significance": 3, "summary": "A letter, mis-filed."},
    ]}
    pl = {"/d/a.pdf": "personal_correspondence"}
    rows = ad.document_rows(summary, [], "examiner", doc_placements=pl)
    letters = [r for r in rows if r.get("category") in LETTER_CATEGORIES]
    others = [r for r in rows if r.get("category") not in LETTER_CATEGORIES]
    assert [r["file"] for r in letters] == ["/d/a.pdf"], "now on Correspondence"
    assert others == [], "left its old miscellaneous Documents bucket"


def test_render_seal_credential_doc_dropped_for_family_even_with_stale_placement():
    """§13.3 render-time seal (the stale-placement leak test): a doc whose PIPELINE
    category is account_credentials is dropped for FAMILY even when a doc_placements
    entry tries to move it to a browsable category — the drop keys on the DERIVED
    category, not the overlay. Covers BOTH document_rows AND the build_search/FTS-fed
    family search rows."""
    summary = {"document_classifications": [
        {"file": "/d/creds.txt", "filename": "creds.txt", "category": "account_credentials",
         "source": "document", "significance": 5, "summary": "Login is hunter2"},
        {"file": "/d/ok.pdf", "filename": "ok.pdf", "category": "legal",
         "source": "document", "significance": 4, "summary": "A will."},
    ]}
    # A stale placement trying to re-bucket the credential doc into "legal".
    pl = {"/d/creds.txt": "legal"}
    fam = ad.document_rows(summary, [], "family", doc_placements=pl)
    fam_files = [r["file"] for r in fam]
    assert "/d/creds.txt" not in fam_files, "render seal: credential doc stays dropped"
    assert "/d/ok.pdf" in fam_files
    # The family search index is fed by these SAME (family) rows → also leak-free.
    search = ad.build_search([], [], fam, [])
    doc_recs = [r for r in search["records"] if r.get("k") == "document"]
    assert all("hunter2" not in (r.get("s") or "") for r in doc_recs)
    assert all(r.get("h") != "/d/creds.txt" for r in doc_recs)
    # Examiner still sees the credential doc, and the overlay re-buckets it for that role.
    ex = ad.document_rows(summary, [], "examiner", doc_placements=pl)
    by = {r["file"]: r for r in ex}
    assert by["/d/creds.txt"]["category"] == "legal", "examiner: overlay applies"


def test_neutralize_summary_removes_identity_language_idempotently():
    assert ad.neutralize_summary("A final message left by the deceased.") == \
        "A final message left by the owner."
    assert ad.neutralize_summary("The deceased's bank statement.") == "The owner's bank statement."
    assert ad.neutralize_summary("Decedent's will.") == "The owner's will."
    # adjectival use is left untouched (not the "the <noun>" form)
    assert ad.neutralize_summary("a deceased person's note") == "a deceased person's note"
    # idempotent
    once = ad.neutralize_summary("The departed wrote this to the deceased.")
    assert ad.neutralize_summary(once) == once
    assert "deceased" not in once.lower() and "departed" not in once.lower()
    # None/empty are pass-through
    assert ad.neutralize_summary(None) is None
    # it is applied by the row builders
    summary = {"document_classifications": [
        {"file": "/d/a.pdf", "category": "legal", "source": "document",
         "summary": "Written by the deceased."}]}
    assert ad.document_rows(summary, [], "examiner")[0]["summary"] == "Written by the owner."


def test_confirm_queue_skips_archive_missing_media(tmp_path):
    # A discarded/moved-out item (archive copy gone) must not stay in the confirm
    # queue — its thumbnail would 404 (the reported broken-link bug).
    here = tmp_path / "here.jpg"; here.write_bytes(b"\xff\xd8\xff\xd9")
    scene_index = {"clip_results": {
        "/src/here.jpg": {"category": "beach", "confidence": 0.2, "delivered": True},
        "/src/gone.jpg": {"category": "beach", "confidence": 0.2, "delivered": True},
    }}
    fc = {"person_clusters": {}, "cluster_identities": {}, "noise_files": []}
    am = {"entries": {"/src/here.jpg": str(here), "/src/gone.jpg": str(tmp_path / "gone.jpg")}}
    q = ad.confirm_queue_data({}, scene_index, fc, {}, archive_map=am)
    ids = [i["id"] for i in q]
    assert "/src/here.jpg" in ids, "present item kept"
    assert "/src/gone.jpg" not in ids, "moved-out item dropped (no broken thumb)"


def test_review_data_skips_archive_missing(tmp_path):
    md = tmp_path / "md"; md.mkdir()
    here = tmp_path / "here.jpg"; here.write_bytes(b"\xff\xd8\xff\xd9")
    (md / "sensitive_scan_index.json").write_text(json.dumps({
        "/src/here.jpg": {"sensitivity_filters": {"nudity": {"triggered": True}}},
        "/src/gone.jpg": {"sensitivity_filters": {"nudity": {"triggered": True}}},
    }))
    (md / "human_review_required.json").write_text(json.dumps(
        {"paths": ["/src/here.jpg", "/src/gone.jpg"]}))
    (md / "archive_map.json").write_text(json.dumps({"entries": {
        "/src/here.jpg": str(here), "/src/gone.jpg": str(tmp_path / "gone.jpg")}}))

    class _P:
        metadata_dir = md
    data = ad.review_data(_P(), {})
    sens_names = {r["name"] for r in data["sensitive"]}
    assert sens_names == {"here.jpg"}, "moved-out sensitive flag dropped"
    assert data["sensitive_total"] == 1                 # count matches visible list
    assert {r["name"] for r in data["human_review"]} == {"here.jpg"}
    assert data["human_review_count"] == 1


def test_places_data_carries_ids_and_member_ids():
    rows = [
        {"id": "/p/a.jpg", "name": "a.jpg", "gps": {"lat": 1.0, "lon": 2.0}, "place": "Tahoe",
         "trip": "Tahoe 2004"},
        {"id": "/p/b.jpg", "name": "b.jpg", "gps": {"lat": 1.1, "lon": 2.1}, "place": "Tahoe",
         "trip": "Tahoe 2004"},
        {"id": "/p/c.jpg", "name": "c.jpg", "gps": None},  # no GPS → skipped
    ]
    d = ad.places_data(rows)
    assert d["points"][0]["id"] == "/p/a.jpg"
    assert len(d["points"]) == 2
    trip = d["trips"][0]
    assert trip["count"] == 2
    assert sorted(trip["member_ids"]) == ["/p/a.jpg", "/p/b.jpg"]


def test_timeline_data_bands_events_ranges_and_undated():
    # G-5: photos band by temporal_chapter → group by temporal_event_id → capped
    # strips, with correct min/max date ranges and an undated shelf count.
    rows = [
        {"id": "/p/a.jpg", "name": "a.jpg", "ts": "2012-06-01T09:00:00", "place": "Portland_Oregon"},
        {"id": "/p/b.jpg", "name": "b.jpg", "ts": "2012-06-15T09:00:00", "place": "Portland_Oregon"},
        {"id": "/p/c.jpg", "name": "c.jpg", "ts": "2011-06-20T09:00:00", "place": None},
        {"id": "/p/d.jpg", "name": "d.jpg", "ts": None, "place": None},           # undated (no ts)
    ]
    geo = {
        "/p/a.jpg": {"temporal_chapter": "2012-06", "temporal_event_id": 1,
                     "compound_label": "Portland_Oregon | 2012-06 | event 1"},
        "/p/b.jpg": {"temporal_chapter": "2012-06", "temporal_event_id": 2,
                     "compound_label": "Portland_Oregon | 2012-06 | event 2"},
        "/p/c.jpg": {"temporal_chapter": "2011-06", "temporal_event_id": 3,
                     "compound_label": "Unknown_Location | 2011-06 | event 3"},
        "__clusters__": {"undated_file_count": 1},
    }
    d = ad.timeline_data(rows, geo, {})
    assert d["chapter_count"] == 2 and d["event_count"] == 3
    # newest chapter first
    assert [c["chapter"] for c in d["chapters"]] == ["2012-06", "2011-06"]
    ch = d["chapters"][0]
    assert ch["label"] == "Portland_Oregon"           # dominant real place
    assert ch["date_from"] == "2012-06-01" and ch["date_to"] == "2012-06-15"
    assert ch["count"] == 2 and ch["event_count"] == 2
    ev = ch["events"][0]
    assert ev["date_from"] == "2012-06-01" and ev["date_to"] == "2012-06-01"
    assert [p["id"] for p in ev["photos"]] == ["/p/a.jpg"]
    # Unknown_Location chapter has no real place label
    assert d["chapters"][1]["label"] is None
    # undated shelf surfaced from __clusters__.undated_file_count
    assert d["undated"]["count"] == 1


def test_timeline_data_strip_cap_keeps_count_true():
    rows = [{"id": f"/p/{i}.jpg", "name": f"{i}.jpg", "ts": "2015-01-01T00:00:00"} for i in range(20)]
    geo = {f"/p/{i}.jpg": {"temporal_chapter": "2015-01", "temporal_event_id": 1} for i in range(20)}
    d = ad.timeline_data(rows, geo, {}, strip_cap=5)
    ev = d["chapters"][0]["events"][0]
    assert ev["count"] == 20 and len(ev["photos"]) == 5   # true count, capped strip


def test_on_this_day_filters_month_day_injected_today():
    # G-8: inject a fixed "today" so the test isn't clock-dependent.
    rows = [
        {"id": "/p/a.jpg", "name": "a.jpg", "ts": "2015-06-26T09:00:00"},
        {"id": "/p/b.jpg", "name": "b.jpg", "ts": "2010-06-26T12:00:00"},
        {"id": "/p/c.jpg", "name": "c.jpg", "ts": "2015-06-27T09:00:00"},   # wrong day
        {"id": "/p/d.jpg", "name": "d.jpg", "ts": None},                    # undated
    ]
    d = ad.on_this_day_data(rows, "2024-06-26")
    assert d["mmdd"] == "06-26" and d["total_count"] == 2
    assert [y["year"] for y in d["years"]] == ["2015", "2010"]   # newest year first
    assert [p["id"] for p in d["years"][0]["photos"]] == ["/p/a.jpg"]
    # nothing on a day with no photos
    assert ad.on_this_day_data(rows, "2024-01-01")["total_count"] == 0


def test_venues_data_groups_by_venue_with_dominant_label():
    # G-10: group by gps_venue_cluster_id, label by dominant place, drop singletons.
    rows = [
        {"id": "/p/a.jpg", "name": "a.jpg", "gps": {"lat": 45.5, "lon": -122.6}, "place": "Portland_Oregon"},
        {"id": "/p/b.jpg", "name": "b.jpg", "gps": {"lat": 45.6, "lon": -122.7}, "place": "Portland_Oregon"},
        {"id": "/p/c.jpg", "name": "c.jpg", "gps": {"lat": 40.0, "lon": -70.0}, "place": "Beverly_Massachusetts"},
    ]
    geo = {
        "/p/a.jpg": {"gps_venue_cluster_id": "0-0"},
        "/p/b.jpg": {"gps_venue_cluster_id": "0-0"},
        "/p/c.jpg": {"gps_venue_cluster_id": "1-9"},   # singleton → dropped at min_members=2
    }
    d = ad.venues_data(rows, geo)
    assert len(d["venues"]) == 1                        # singleton excluded
    v = d["venues"][0]
    assert v["venue_id"] == "0-0" and v["count"] == 2
    assert v["name"] == "Portland_Oregon"              # dominant place label
    assert sorted(v["member_ids"]) == ["/p/a.jpg", "/p/b.jpg"]
    assert abs(v["lat"] - 45.55) < 1e-6 and abs(v["lon"] - (-122.65)) < 1e-6   # centroid
    # min_members=1 keeps the singleton
    assert len(ad.venues_data(rows, geo, min_members=1)["venues"]) == 2


def test_venues_data_disambiguates_same_named_venues_by_year_span():
    # #23: two genuinely distinct GPS/DBSCAN venue clusters (different physical
    # places) sharing the same dominant city label must not read as duplicates —
    # disambiguated by each one's own capture-year span, not a fabricated index.
    rows = [
        {"id": "/p/a.jpg", "name": "a.jpg", "ts": "2012-01-01T00:00:00",
         "gps": {"lat": 45.5, "lon": -122.6}, "place": "Portland_Oregon"},
        {"id": "/p/b.jpg", "name": "b.jpg", "ts": "2013-06-01T00:00:00",
         "gps": {"lat": 45.5, "lon": -122.6}, "place": "Portland_Oregon"},
        {"id": "/p/c.jpg", "name": "c.jpg", "ts": "2019-01-01T00:00:00",
         "gps": {"lat": 45.7, "lon": -122.8}, "place": "Portland_Oregon"},
        {"id": "/p/d.jpg", "name": "d.jpg", "ts": "2019-01-01T00:00:00",
         "gps": {"lat": 45.7, "lon": -122.8}, "place": "Portland_Oregon"},
        # A lone Beverly venue has no name collision → left unchanged.
        {"id": "/p/e.jpg", "name": "e.jpg", "ts": "2015-01-01T00:00:00",
         "gps": {"lat": 40.0, "lon": -70.0}, "place": "Beverly_Massachusetts"},
        {"id": "/p/f.jpg", "name": "f.jpg", "ts": "2015-06-01T00:00:00",
         "gps": {"lat": 40.0, "lon": -70.0}, "place": "Beverly_Massachusetts"},
    ]
    geo = {
        "/p/a.jpg": {"gps_venue_cluster_id": "0-0"}, "/p/b.jpg": {"gps_venue_cluster_id": "0-0"},
        "/p/c.jpg": {"gps_venue_cluster_id": "0-1"}, "/p/d.jpg": {"gps_venue_cluster_id": "0-1"},
        "/p/e.jpg": {"gps_venue_cluster_id": "1-0"}, "/p/f.jpg": {"gps_venue_cluster_id": "1-0"},
    }
    d = ad.venues_data(rows, geo)
    names = sorted(v["name"] for v in d["venues"])
    assert names == ["Beverly_Massachusetts", "Portland_Oregon (2012–2013)", "Portland_Oregon (2019)"]


def test_photo_rows_tags_event_album_from_trip_cluster():
    # event_album_titles maps album_id->title; photo_rows joins it to each photo's
    # gps_trip_cluster_id so Photographs can filter by Event (named trip cluster).
    summary = {"event_albums": [
        {"album_id": "0", "title": "Portland Life 2009-2026"},
        {"album_id": 1, "title": "London Holiday"},  # non-string id must still join
    ]}
    titles = ad.event_album_titles(summary)
    assert titles == {"0": "Portland Life 2009-2026", "1": "London Holiday"}
    universe = {
        "/p/a.jpg": {"category": "beach", "delivered": True},
        "/p/b.jpg": {"category": "city", "delivered": True},
        "/p/c.jpg": {"category": "home", "delivered": True},
    }
    geo = {
        "/p/a.jpg": {"gps_trip_cluster_id": 0, "gps_trip_name": "Portland_Oregon"},
        "/p/b.jpg": {"gps_trip_cluster_id": 1},
        "/p/c.jpg": {"gps_trip_cluster_id": -3},  # singleton/noise → no album
    }
    rows = ad.photo_rows(universe, {}, geo, {}, titles)
    by = {r["id"]: r for r in rows}
    assert by["/p/a.jpg"]["event"] == "Portland Life 2009-2026"
    assert by["/p/b.jpg"]["event"] == "London Holiday"
    assert by["/p/c.jpg"]["event"] is None
    # back-compat: omitting event_titles leaves event None, never raises
    assert all(r["event"] is None for r in ad.photo_rows(universe, {}, geo, {}))


def test_photo_rows_carries_event_id_and_placement_override():
    # Move Phase 2: each row carries event_id (album_id string) ALONGSIDE the title,
    # and event_placements overrides the derived album (title follows the target).
    summary = {"event_albums": [
        {"album_id": "0", "title": "Tahoe 2004"},
        {"album_id": "1", "title": "Paris Trip"},
    ]}
    titles = ad.event_album_titles(summary)
    universe = {
        "/p/a.jpg": {"category": "beach", "delivered": True},
        "/p/b.jpg": {"category": "city", "delivered": True},
        "/p/c.jpg": {"category": "home", "delivered": True},
    }
    geo = {
        "/p/a.jpg": {"gps_trip_cluster_id": 0},
        "/p/b.jpg": {"gps_trip_cluster_id": 1},
        "/p/c.jpg": {"gps_trip_cluster_id": -9},   # noise → no album
    }
    rows = ad.photo_rows(universe, {}, geo, {}, titles)
    by = {r["id"]: r for r in rows}
    assert by["/p/a.jpg"]["event_id"] == "0" and by["/p/a.jpg"]["event"] == "Tahoe 2004"
    assert by["/p/b.jpg"]["event_id"] == "1" and by["/p/b.jpg"]["event"] == "Paris Trip"
    assert by["/p/c.jpg"]["event_id"] is None and by["/p/c.jpg"]["event"] is None
    # Placement override: /p/a.jpg re-filed into album 1 → id AND title follow.
    rows2 = ad.photo_rows(universe, {}, geo, {}, titles,
                          event_placements={"/p/a.jpg": "1"})
    by2 = {r["id"]: r for r in rows2}
    assert by2["/p/a.jpg"]["event_id"] == "1" and by2["/p/a.jpg"]["event"] == "Paris Trip"
    # Empty/None placements → fast path, unchanged tagging.
    assert {r["id"]: r["event_id"] for r in ad.photo_rows(universe, {}, geo, {}, titles,
                                                          event_placements={})} \
        == {"/p/a.jpg": "0", "/p/b.jpg": "1", "/p/c.jpg": None}


def test_event_albums_data_derives_live_count_not_static_photo_count():
    # The card count is DERIVED from the effective photo rows — a move changes it,
    # unlike the static case_summary event_albums[].photo_count.
    summary = {"event_albums": [
        {"album_id": "0", "title": "Tahoe 2004", "place": "Lake Tahoe",
         "date_range": "2004", "scenes": ["beach"], "photo_count": 999},
        {"album_id": "1", "title": "Paris Trip", "place": "Paris",
         "date_range": "2011", "scenes": ["city"], "photo_count": 999},
    ]}
    titles = ad.event_album_titles(summary)
    universe = {f"/p/{n}.jpg": {"category": "x", "delivered": True} for n in "abc"}
    geo = {"/p/a.jpg": {"gps_trip_cluster_id": 0},
           "/p/b.jpg": {"gps_trip_cluster_id": 0},
           "/p/c.jpg": {"gps_trip_cluster_id": 1}}
    rows = ad.photo_rows(universe, {}, geo, {}, titles)
    albums = ad.event_albums_data(summary, rows)
    by = {a["album_id"]: a for a in albums}
    assert by["0"]["count"] == 2 and by["1"]["count"] == 1     # DERIVED, not 999
    assert by["0"]["place"] == "Lake Tahoe" and by["0"]["date_range"] == "2004"
    assert set(by["0"]["sample_ids"]) <= {"/p/a.jpg", "/p/b.jpg"}
    # Cards sorted by live count desc.
    assert [a["album_id"] for a in albums] == ["0", "1"]
    # A move (event_placements) shifts the counts — the whole point of the view.
    rows_moved = ad.photo_rows(universe, {}, geo, {}, titles,
                               event_placements={"/p/c.jpg": "0"})
    moved = {a["album_id"]: a["count"] for a in ad.event_albums_data(summary, rows_moved)}
    assert moved == {"0": 3, "1": 0}
    # All configured albums are kept even at 0 count.
    assert set(moved) == {"0", "1"}


def test_confirm_queue_excludes_already_decided():
    summary = {"event_albums": [{"album_id": "0", "title": "Tahoe"}]}
    scene_index = {"clip_results": {"/x/low.jpg": {"category": "beach", "confidence": 0.2,
                                                   "delivered": True}}}
    face_clustering = {"person_clusters": {"Person_01": ["/x/a.jpg"]},
                       "cluster_identities": {}, "noise_files": ["/x/face1.jpg"]}
    geo_index = {}
    decisions = {"scene": {"/x/low.jpg": {"decision": "reject"}},
                 "name_person": {"Person_01": {"decision": "reject"}}}
    q = ad.confirm_queue_data(summary, scene_index, face_clustering, geo_index, decisions=decisions)
    ids = {(i["queue"], i["id"]) for i in q}
    assert ("scene", "/x/low.jpg") not in ids, "decided scene guess must not reappear"
    assert ("name_person", "Person_01") not in ids, "decided person must not reappear"
    # an undecided item (the unidentified face) still shows
    assert ("face", "/x/face1.jpg") in ids
    # with no decisions, the scene + person return
    q2 = ad.confirm_queue_data(summary, scene_index, face_clustering, geo_index)
    assert ("scene", "/x/low.jpg") in {(i["queue"], i["id"]) for i in q2}


def test_documents_index_groups_and_subcategorizes():
    rows = [
        {"category": "financial", "subcategory": "banking"},
        {"category": "financial", "subcategory": "banking"},
        {"category": "financial", "subcategory": "paystubs"},
        {"category": "financial", "subcategory": None},
        {"category": "financial"},
        {"category": "legal", "subcategory": None},
        {"category": "legal"},
        {"category": "miscellaneous"},
    ]
    idx = ad.documents_index(rows)
    by = {c["category"]: c for c in idx}
    assert by["financial"]["count"] == 5
    subs = {s["name"]: s["count"] for s in by["financial"]["subcategories"]}
    # financial rows with a null/absent subcategory fall into "uncategorized"
    assert subs == {"banking": 2, "uncategorized": 2, "paystubs": 1}
    # non-financial categories don't get a sub-taxonomy
    assert by["legal"]["count"] == 2 and by["legal"]["subcategories"] == []
    # sorted by count desc → financial leads
    assert idx[0]["category"] == "financial"


def test_email_rows_thread_grain_no_file_leak_and_capped():
    ti = {"threads": [
        {"thread_id": "thread_0001", "subject": "Re: hi\n there", "participants": ["a@x", "b@y"],
         "date_first": "2020-01-01T00:00:00", "date_last": "2020-01-03T00:00:00",
         "significance": 4, "categories": ["personal_correspondence"], "message_count": 3,
         "files": ["/orig/m1.eml", "/orig/m2.eml"]},
        {"thread_id": "thread_0002", "subject": "newer", "date_last": "2021-01-01T00:00:00",
         "files": ["/orig/m3.eml"]},
    ]}
    rows = ad.email_rows(ti)
    # ranked by significance (thread_0001 sig=4) over the newer-but-insignificant thread_0002
    assert rows[0]["thread_id"] == "thread_0001", "most significant thread first"
    assert "files" not in rows[0], "raw .eml paths must not leak into the list"
    r1 = next(r for r in rows if r["thread_id"] == "thread_0001")
    assert r1["subject"] == "Re: hi  there" and r1["message_count"] == 3
    big = {"threads": [{"thread_id": f"t{i}", "date_last": f"2020-01-{i % 28 + 1:02d}"}
                       for i in range(10)]}
    assert len(ad.email_rows(big, cap=3)) == 3


def test_email_rows_demote_sinks_thread_to_bottom():
    # An examiner-demoted thread is forced to significance 0 (drops to the bottom
    # band) and flagged demoted — still listed, just no longer at the top.
    ti = {"threads": [{"thread_id": "t1", "subject": "A", "significance": 5},
                      {"thread_id": "t2", "subject": "B", "significance": 4}]}
    rows = ad.email_rows(ti, decisions={"email_demoted": {"t1": {}}})
    by = {r["thread_id"]: r for r in rows}
    assert by["t1"]["significance"] == 0 and by["t1"]["demoted"] is True
    assert by["t2"]["demoted"] is False
    assert [r["thread_id"] for r in rows] == ["t2", "t1"]  # demoted thread sinks last


def test_email_thread_messages_resolves_and_truncates():
    ebf = {
        "/orig/m1.eml": {"email_from": "a@x", "email_to": "b@y", "email_subject": "Hi",
                         "email_date_iso": "2020-01-02", "ocr_text": "hello world"},
        "/orig/m2.eml": {"email_from": "b@y", "email_subject": "Re",
                         "email_date_iso": "2020-01-01", "ocr_text": "x" * 9000},
    }
    msgs = ad.email_thread_messages(ebf, ["/orig/m1.eml", "/orig/m2.eml"], body_cap=100)
    # oldest-first: m2 (2020-01-01, the long body) leads, then m1
    assert msgs[0]["date"] == "2020-01-01", "messages sorted oldest-first"
    assert len(msgs[0]["body"]) == 100, "long body truncated to cap"
    assert msgs[0]["from"] == "b@y"
    assert msgs[1]["from"] == "a@x" and msgs[1]["body"] == "hello world"
    # a file with no index record yields an empty-ish record, never an error
    assert ad.email_thread_messages(ebf, ["/nope.eml"])[0]["from"] is None


def test_build_photo_universe_excludes_video_frames():
    scene = {"clip_results": {
        "/x/real.jpg": {"category": "beach", "delivered": True},
        "/x/clip_f000001.jpg": {"category": "beach", "delivered": True},   # frame by pattern
        "/x/mapped.jpg": {"category": "beach", "delivered": True},          # frame by map
    }, "junk_results": {}}
    # /x/real.jpg has NO archive entry → examiner keeps it (resolves via extracted/);
    # frame entries are irrelevant (excluded before the archive check).
    amap = {"entries": {"/x/clip_f000001.jpg": "/a/v.jpg", "/x/mapped.jpg": "/a/m.jpg"}}
    frame_map = {"/x/mapped.jpg": {"source_video": "v.mp4"}}
    u = ad.build_photo_universe(scene, amap, "examiner", frame_map)
    assert set(u) == {"/x/real.jpg"}
    # family too
    uf = ad.build_photo_universe(scene, amap, "family", frame_map)
    assert "/x/clip_f000001.jpg" not in uf and "/x/mapped.jpg" not in uf


def test_overview_data_thumbs_preview_and_demote():
    summary = {
        "ranked_items": [
            {"type": "photo_cluster", "label": "Person_01", "person_id": "Person_01"},
            {"type": "scene", "label": "beach"},
            {"type": "document", "label": "will.pdf"},
        ],
        "document_classifications": [{"file": "/d/will.pdf", "filename": "will.pdf", "source": "document"}],
    }
    fc = {"person_clusters": {"Person_01": ["/x/a.jpg", "/x/b.jpg"]}}
    universe = {"/x/a.jpg": {"category": "beach"}, "/x/b.jpg": {"category": "party"}}
    ov = ad.overview_data(summary, "examiner", {}, face_clustering=fc, universe=universe)
    by = {r["label"]: r for r in ov["ranked_top"]}
    assert by["Person_01"]["thumb"] == "/x/a.jpg"   # back-compat: thumb = thumbs[0]
    assert by["Person_01"]["thumbs"] == ["/x/a.jpg", "/x/b.jpg"]  # #5 up to 5 previews
    assert by["beach"]["thumb"] == "/x/a.jpg"        # representative scene photo
    assert by["will.pdf"]["thumb"] is None and by["will.pdf"]["thumbs"] == []  # document → icon
    assert by["beach"]["key"] == "scene:beach"
    assert "photo_preview" not in ov                 # #4 banner removed
    # a demoted item is filtered out of the list (#12)
    dec = {"ranked_demoted": {"scene:beach": {}}}
    ov2 = ad.overview_data(summary, "examiner", {}, face_clustering=fc, universe=universe, decisions=dec)
    assert all(r["label"] != "beach" for r in ov2["ranked_top"])


def test_correspondents_data_ranking_shape_and_role():
    freq = [
        {"address": "b@x", "display_name": "Bob", "sent_count": 5, "received_count": 3,
         "total": 8, "bidirectional": True, "first_seen": "2010-02-01T00:00:00",
         "last_seen": "2015-06-01T00:00:00", "subject_diversity": 0.4},
        {"address": "a@x", "display_name": "Alice", "sent_count": 50, "received_count": 40,
         "total": 90, "bidirectional": True, "first_seen": "2005-01-01", "last_seen": "2020-01-01"},
        {"address": "", "display_name": "blank"},  # no address → dropped
    ]
    fam = ad.correspondents_data(freq, role="family")
    exam = ad.correspondents_data(freq, role="examiner")
    assert fam == exam, "relationship metadata is non-sensitive → identical for both roles"
    assert [r["address"] for r in fam] == ["a@x", "b@x"], "sorted by total desc; blank dropped"
    top = fam[0]
    assert set(top) == {"address", "name", "sent", "received", "total", "bidirectional",
                        "first_seen", "last_seen", "years_span", "subject_diversity",
                        "merged_addresses"}
    assert top["name"] == "Alice" and top["total"] == 90 and top["years_span"] == 15
    assert top["merged_addresses"] == []
    # total falls back to sent+received when absent; name falls back to address
    fallback = ad.correspondents_data([{"address": "c@x", "sent_count": 2, "received_count": 1}])
    assert fallback[0]["total"] == 3 and fallback[0]["name"] == "c@x"
    assert fallback[0]["years_span"] is None  # no dates → None, never a crash


def test_correspondent_duplicate_candidates_surfaces_real_person_not_bulk_sender():
    # "Dawn Merrick" across 2 personal domains, bidirectional → a real candidate.
    # "Amazon.com" across many subaddresses of ONE shared domain, one-directional
    # → must NOT surface (same pattern the usability review found in production
    # data: exact-name matching alone clusters brand subaddresses just as
    # readily as real people).
    freq = [
        {"address": "dawn@example.net", "display_name": "Dawn Merrick", "total": 100,
         "bidirectional": True},
        {"address": "dawn.merrick@example.com", "display_name": "Dawn Merrick", "total": 50,
         "bidirectional": True},
        {"address": "auto-confirm@amazon.com", "display_name": "Amazon.com", "total": 900,
         "bidirectional": False},
        {"address": "ship-confirm@amazon.com", "display_name": "Amazon.com", "total": 500,
         "bidirectional": False},
        {"address": "solo@x.com", "display_name": "Solo Person", "total": 10,
         "bidirectional": True},  # only one address → never a candidate
    ]
    cands = ad.correspondent_duplicate_candidates(freq)
    names = [c["name"] for c in cands]
    assert names == ["Dawn Merrick"], "bulk single-domain sender excluded; lone address excluded"
    c = cands[0]
    assert c["total_combined"] == 150
    assert [a["address"] for a in c["addresses"]] == ["dawn@example.net", "dawn.merrick@example.com"]
    assert c["cluster_id"] and isinstance(c["cluster_id"], str)


def test_correspondent_duplicate_candidates_requires_two_word_name_and_bidirectional():
    # 3+ word "name" (reads as a brand/program, not a person) is excluded even
    # spanning 2 domains; so is a cluster with NO bidirectional address at all.
    freq = [
        {"address": "a@hilton.com", "display_name": "Hilton HHonors Rewards", "total": 10,
         "bidirectional": True},
        {"address": "b@hiltonhhonors.net", "display_name": "Hilton HHonors Rewards", "total": 5,
         "bidirectional": True},
        {"address": "c@one.com", "display_name": "Jane Doe", "total": 10, "bidirectional": False},
        {"address": "d@two.com", "display_name": "Jane Doe", "total": 5, "bidirectional": False},
    ]
    assert ad.correspondent_duplicate_candidates(freq) == []


def test_correspondent_duplicate_candidates_excludes_merged_and_rejected():
    freq = [
        {"address": "a@one.com", "display_name": "Jane Doe", "total": 10, "bidirectional": True},
        {"address": "b@two.com", "display_name": "Jane Doe", "total": 5, "bidirectional": True},
    ]
    base = ad.correspondent_duplicate_candidates(freq)
    assert len(base) == 1
    cid = base[0]["cluster_id"]
    # Already confirmed (an overlay merge already unifies these two addresses).
    merged = ad.correspondent_duplicate_candidates(
        freq, {"correspondent_merges": {"b@two.com": "a@one.com"}})
    assert merged == []
    # Explicitly rejected by cluster_id.
    rejected = ad.correspondent_duplicate_candidates(
        freq, {"correspondent_merge_rejected": [cid]})
    assert rejected == []


def test_correspondents_data_applies_merge_overlay():
    freq = [
        {"address": "dawn@example.net", "display_name": "Dawn Merrick", "sent_count": 60,
         "received_count": 40, "total": 100, "bidirectional": True,
         "first_seen": "2010-01-01", "last_seen": "2015-01-01"},
        {"address": "dawn.merrick@example.com", "display_name": "Dawn Merrick", "sent_count": 30,
         "received_count": 20, "total": 50, "bidirectional": True,
         "first_seen": "2016-01-01", "last_seen": "2020-01-01"},
    ]
    decisions = {"correspondent_merges": {"dawn.merrick@example.com": "dawn@example.net"}}
    rows = ad.correspondents_data(freq, decisions=decisions)
    assert len(rows) == 1, "loser row folds into winner and disappears"
    row = rows[0]
    assert row["address"] == "dawn@example.net"
    assert row["total"] == 150 and row["sent"] == 90 and row["received"] == 60
    assert row["first_seen"] == "2010-01-01" and row["last_seen"] == "2020-01-01"
    assert row["merged_addresses"] == ["dawn.merrick@example.com"]


def test_delivered_basename_index_unique_collision_and_role():
    summary = {"document_classifications": [
        {"file": "/d/report.pdf", "filename": "report.pdf", "category": "legal", "source": "document"},
        {"file": "/m/msg.eml", "filename": "report.pdf", "category": "legal", "source": "email"},
        {"file": "/d/passwords.txt", "filename": "passwords.txt", "category": "account_credentials",
         "source": "document"},
    ]}
    archive_map = {"entries": {"/work/photo.jpg": "/arc/photo.jpg",
                               "/work/dup/photo.jpg": "/arc/dup/photo.jpg"}}
    idx = ad.delivered_basename_index(summary, archive_map, role="family")
    # email-sourced doc is skipped, so report.pdf resolves uniquely to the real doc
    assert idx["report.pdf"] == "/d/report.pdf"
    # two distinct archive items share basename photo.jpg → ambiguous → None
    assert idx.get("photo.jpg") is None
    # account_credentials skipped for family (not browsable) → absent
    assert "passwords.txt" not in idx
    # examiner DOES get the credentials doc
    idx_ex = ad.delivered_basename_index(summary, archive_map, role="examiner")
    assert idx_ex["passwords.txt"] == "/d/passwords.txt"


def test_delivered_basename_index_strips_mail_extraction_prefix():
    # An attachment extracted from mail is deconflicted with a msgNNNNN_ prefix
    # (expandfiles._extract_attachments), but the email's own attachments[]
    # metadata still names it by the ORIGINAL filename — a bare basename match
    # never fires (#7). De-prefixed indexing must resolve it, and a genuine
    # collision between two different messages' stripped names must still fail
    # closed (never guess).
    archive_map = {"entries": {
        "/data/cases/x/extracted/photos/msg00101_DSC00399.JPG":
            "/data/cases/x/output/archive/_no_album/2013/msg00101_DSC00399.JPG",
        "/data/cases/x/extracted/photos/msg00042_collide.jpg": "/arc/1/collide.jpg",
        "/data/cases/x/extracted/photos/msg00099_collide.jpg": "/arc/2/collide.jpg",
    }}
    idx = ad.delivered_basename_index({}, archive_map, role="family")
    assert idx["DSC00399.JPG"] == "/data/cases/x/extracted/photos/msg00101_DSC00399.JPG"
    # the prefixed form is still indexed too (unaffected/no regression)
    assert idx["msg00101_DSC00399.JPG"] == idx["DSC00399.JPG"]
    # two different messages' attachments stripped to the same name → ambiguous
    assert idx.get("collide.jpg") is None


def test_email_thread_detail_attachments_resolve_conservatively():
    ti = {"threads": [{"thread_id": "t1", "subject": "Scans",
                       "files": ["/m/1.eml"]}]}
    ebf = {"/m/1.eml": {"file": "/m/1.eml", "message_id": "<1>", "email_from": "a",
                        "email_date_iso": "2020-01-01", "ocr_text": "see attached",
                        "attachments": [
                            {"filename": "scan.pdf", "content_type": "application/pdf",
                             "size_bytes": 1234, "is_inline": False, "content_id": None},
                            {"filename": "collide.jpg", "content_type": "image/jpeg",
                             "size_bytes": 22, "is_inline": False},
                            {"filename": "logo.png", "content_type": "image/png",
                             "size_bytes": 9, "is_inline": True, "content_id": "<ii_1>"},
                            {"filename": "missing.doc", "content_type": "application/msword",
                             "size_bytes": 7, "is_inline": False},
                        ]}}
    # scan.pdf → unique delivered doc; collide.jpg → two archive items (ambiguous);
    # missing.doc → not delivered.
    attach_index = ad.delivered_basename_index(
        {"document_classifications": [
            {"file": "/d/scan.pdf", "filename": "scan.pdf", "source": "document"}]},
        {"entries": {"/w/collide.jpg": "/a/1/collide.jpg", "/w/x/collide.jpg": "/a/2/collide.jpg"}},
        role="family")
    d = ad.email_thread_detail(ti, ebf, {}, "t1", attachment_index=attach_index)
    atts = {a["filename"]: a for a in d["messages"][0]["attachments"]}
    assert atts["scan.pdf"]["file_id"] == "/d/scan.pdf"          # unique basename → linked
    assert atts["scan.pdf"]["size_bytes"] == 1234
    assert atts["collide.jpg"]["file_id"] is None               # collision → name-only
    assert atts["missing.doc"]["file_id"] is None               # no match → name-only
    assert atts["logo.png"]["is_inline"] is True                # inline flagged for UI suppression
    # Without an attachment_index every attachment is name-only (never a broken link).
    d0 = ad.email_thread_detail(ti, ebf, {}, "t1")
    assert all(a["file_id"] is None for a in d0["messages"][0]["attachments"])
    # A message with no attachments carries an empty list, not a missing key.
    ebf["/m/1.eml"].pop("attachments")
    d1 = ad.email_thread_detail(ti, ebf, {}, "t1", attachment_index=attach_index)
    assert d1["messages"][0]["attachments"] == []


def test_email_thread_detail_threads_nests_and_scores():
    ti = {"threads": [{"thread_id": "thread_0001", "subject": "Trip",
                       "files": ["/m/1.eml", "/m/2.eml", "/m/3.eml"]}]}
    ebf = {
        "/m/1.eml": {"file": "/m/1.eml", "message_id": "<1>", "email_from": "a",
                     "email_subject": "Trip", "email_date_iso": "2020-01-01", "ocr_text": "hi\nthere"},
        "/m/2.eml": {"file": "/m/2.eml", "message_id": "<2>", "in_reply_to": "<1>",
                     "references": ["<1>"], "email_from": "b", "email_date_iso": "2020-01-02",
                     "ocr_text": "reply"},
        "/m/3.eml": {"file": "/m/3.eml", "message_id": "<3>", "in_reply_to": "<2>",
                     "references": ["<1>", "<2>"], "email_from": "a", "email_date_iso": "2020-01-03",
                     "ocr_text": "re reply"},
    }
    d = ad.email_thread_detail(ti, ebf, {"/m/1.eml": 5}, "thread_0001")
    assert d["subject"] == "Trip"
    ms = {m["file"]: m for m in d["messages"]}
    assert ms["/m/1.eml"]["depth"] == 0
    assert ms["/m/2.eml"]["depth"] == 1
    assert ms["/m/3.eml"]["depth"] == 2          # reply-of-reply nests two levels
    assert ms["/m/1.eml"]["significance"] == 5
    assert ms["/m/1.eml"]["body"] == "hi\nthere"  # newline preserved (UI renders pre-wrap)
    assert ad.email_thread_detail(ti, ebf, {}, "nope") is None


def test_person_detail_photo_and_video_members(tmp_path):
    canon = tmp_path / "A.mov"; canon.write_bytes(b"\x00")   # canonical on disk
    photo_canon = tmp_path / "photo.jpg"; photo_canon.write_bytes(b"\x00")   # canonical on disk
    fc = {"person_clusters": {"P1": ["/x/photo.jpg", "/x/vidA_f000001.jpg"]},
          "cluster_identities": {"P1": {"name": "Jane"}}}
    universe = {"/x/photo.jpg": {"category": "beach"}}
    archive_map = {"entries": {"/x/photo.jpg": str(photo_canon), "/v/A.mov": str(canon)}}
    metadata_index = {"/x/photo.jpg": {"place": "Tahoe"}}
    scene_index = {"clip_results": {"/x/photo.jpg": {"category": "beach"}}}
    vfm = {"/x/vidA_f000001.jpg": {"source_video": "/v/A.mov", "frame_offset_seconds": 3}}
    d = ad.person_detail(fc, universe, archive_map, metadata_index, scene_index, vfm, "P1", "examiner")
    # #20: photo_count now matches photo_n (photos only, video frames excluded) —
    # it used to be len(person_clusters[pid]) (2: the photo AND the video frame),
    # which could never equal the list card's photo_count from people_rows.
    assert d["name"] == "Jane" and d["named"] and d["photo_count"] == 1
    assert d["photo_n"] == 1 and d["video_n"] == 1
    assert d["members"][0]["kind"] == "photo"          # photographs first
    vid = next(m for m in d["members"] if m["kind"] == "video")
    assert vid["video_src"] == "/v/A.mov" and vid["video_name"] == "A.mov"
    pho = next(m for m in d["members"] if m["kind"] == "photo")
    assert pho["scene"] == "beach" and pho["place"] == "Tahoe"
    assert ad.person_detail(fc, universe, archive_map, {}, {}, vfm, "ZZ", "examiner") is None


def test_person_detail_video_only_person(tmp_path):  # the Person_05 bug case
    canon = tmp_path / "A.mov"; canon.write_bytes(b"\x00")
    fc = {"person_clusters": {"P5": ["/x/vidA_f000001.jpg", "/x/vidA_f000002.jpg"]},
          "cluster_identities": {}}
    vfm = {"/x/vidA_f000001.jpg": {"source_video": "/v/A.mov"},
           "/x/vidA_f000002.jpg": {"source_video": "/v/A.mov"}}
    d = ad.person_detail(fc, {}, {"entries": {"/v/A.mov": str(canon)}}, {}, {}, vfm, "P5", "examiner")
    assert d["photo_n"] == 0 and d["video_n"] == 2     # now shows video appearances, not empty


def test_person_detail_video_with_missing_canonical_hidden_both_roles(tmp_path):
    # A video whose archive copy was moved out (quarantined / perceptual-dupe
    # loser) must not render a dead "play": /media on it 404s. Mirror of
    # video_rows' canonical-exists guard — applies to BOTH roles.
    fc = {"person_clusters": {"P1": ["/x/vidA_f000001.jpg"]}, "cluster_identities": {}}
    vfm = {"/x/vidA_f000001.jpg": {"source_video": "/v/A.mov"}}
    amap = {"entries": {"/v/A.mov": str(tmp_path / "gone.mov")}}   # mapped, not on disk
    for role in ("examiner", "family"):
        d = ad.person_detail(fc, {}, amap, {}, {}, vfm, "P1", role)
        assert d["video_n"] == 0, role


def test_person_detail_photo_missing_canonical_hidden_examiner(tmp_path):
    # #20: an examiner sees undelivered members generally, but a photo whose
    # archive copy was moved out (quarantined/discarded) must not appear here
    # either — its thumbnail would 404, and people_rows' present() check
    # already excludes it from that person's list-card photo_count. Without
    # this, the detail page's photo_n/photo_count ran higher than the same
    # person's count on their own list card.
    fc = {"person_clusters": {"P1": ["/x/gone.jpg", "/x/kept.jpg"]}, "cluster_identities": {}}
    kept = tmp_path / "kept.jpg"; kept.write_bytes(b"\x00")
    amap = {"entries": {"/x/gone.jpg": str(tmp_path / "gone.jpg"),   # mapped, not on disk
                        "/x/kept.jpg": str(kept)}}
    d = ad.person_detail(fc, {}, amap, {}, {}, {}, "P1", "examiner")
    assert d["photo_n"] == 1 and d["photo_count"] == 1
    assert [m["id"] for m in d["members"]] == ["/x/kept.jpg"]


def test_person_detail_family_gating():
    fc = {"person_clusters": {"P1": ["/x/photo.jpg", "/x/vidA_f000001.jpg"]}, "cluster_identities": {}}
    vfm = {"/x/vidA_f000001.jpg": {"source_video": "/v/undelivered.mov"}}
    # family: photo not in universe → dropped; video source not in archive_map → dropped
    fam = ad.person_detail(fc, {}, {"entries": {}}, {}, {}, vfm, "P1", "family")
    assert fam["photo_n"] == 0 and fam["video_n"] == 0
    # examiner sees both regardless of delivery
    exm = ad.person_detail(fc, {}, {"entries": {}}, {}, {}, vfm, "P1", "examiner")
    assert exm["photo_n"] == 1 and exm["video_n"] == 1


def test_scanned_images_excluded_from_universe_but_surfaced():
    scene = {"clip_results": {
        "/x/doc1.jpg": {"category": "scanned document or handwritten letter", "delivered": True},
        "/x/real.jpg": {"category": "beach", "delivered": True},
    }, "junk_results": {}}
    amap = {"entries": {"/x/doc1.jpg": "/a/doc1.jpg"}}  # real.jpg has no archive entry → kept (examiner)
    u = ad.build_photo_universe(scene, amap, "examiner")
    assert "/x/doc1.jpg" not in u and "/x/real.jpg" in u, "scanned doc excluded, photo kept"
    sr = ad.scanned_image_rows(scene, amap, {}, "examiner")
    assert [r["id"] for r in sr] == ["/x/doc1.jpg"]
    assert sr[0]["scene"] == "Documents & letters"  # SCENE_LABELS display value


def test_scanned_released_overlay_rejoins_universe_leaves_scanned_rows(tmp_path):  # BACKLOG #19
    archived = tmp_path / "doc1.jpg"; archived.write_bytes(b"\xff\xd8\xff\xd9")
    scene = {"clip_results": {
        "/x/doc1.jpg": {"category": "scanned document or handwritten letter", "delivered": True},
        "/x/real.jpg": {"category": "beach", "delivered": True},
    }, "junk_results": {}}
    amap = {"entries": {"/x/doc1.jpg": str(archived)}}
    released = {"/x/doc1.jpg": True}
    u = ad.build_photo_universe(scene, amap, "examiner", scanned_released=released)
    assert "/x/doc1.jpg" in u, "released item rejoins the gallery"
    assert u["/x/doc1.jpg"]["category"] == "uncategorized", "no longer tagged as scanned"
    # An unrelated id in the overlay (never a scanned item to begin with) is a no-op.
    u2 = ad.build_photo_universe(scene, amap, "examiner", scanned_released={"/x/nope.jpg": True})
    assert "/x/doc1.jpg" not in u2, "overlay only rescues items it actually names"
    sr = ad.scanned_image_rows(scene, amap, {}, "examiner", released=released)
    assert sr == [], "released item leaves the Correspondence scanned list"


def test_video_rows_poster_and_gating(tmp_path):  # Item 3
    vid = tmp_path / "clip.mp4"; vid.write_bytes(b"\x00")
    archive_map = {"entries": {
        "/src/clip.mp4": str(vid),
        "/src/gone.mov": str(tmp_path / "missing.mov"),   # canonical missing → skipped
        "/src/photo.jpg": str(tmp_path / "p.jpg"),        # not a video → skipped
    }}
    vfm = {"/x/clip_f000001.jpg": {"source_video": "/src/clip.mp4"}}
    rows = ad.video_rows(archive_map, {"/src/clip.mp4": {"timestamp": "2020"}}, vfm, "examiner")
    assert [r["id"] for r in rows] == ["/src/clip.mp4"]
    r = rows[0]
    assert r["kind"] == "video" and r["name"] == "clip.mp4" and r["poster"] == "/x/clip_f000001.jpg"
    # No video_index passed → the G-11 facet keys default to empty (pre-video_index case).
    assert r["persons"] == [] and r["scenes"] == []


def test_video_rows_facets_persons_scenes(tmp_path):  # G-11
    vid = tmp_path / "clip.mp4"; vid.write_bytes(b"\x00")
    archive_map = {"entries": {"/src/clip.mp4": str(vid)}}
    vfm = {"/x/clip_f000001.jpg": {"source_video": "/src/clip.mp4"}}
    video_index = {"videos": [
        {"source_video": "/src/clip.mp4",
         # sentinels (no_faces/unidentified) dropped; duplicate Person_01 deduped
         "assigned_persons": ["no_faces", "Person_01", "unidentified", "Person_01"],
         # duplicate + blank scene coalesced
         "assigned_scenes": ["birthday party", "birthday party", ""]},
    ]}
    identities = {"Person_01": {"name": "Dawn"}}
    rows = ad.video_rows(archive_map, {}, vfm, "examiner",
                         video_index=video_index, cluster_identities=identities)
    r = rows[0]
    assert r["persons"] == [{"person_id": "Person_01", "name": "Dawn"}]  # name resolved
    assert r["scenes"] == ["birthday party"]


def test_video_rows_facets_bare_list_and_unnamed_person(tmp_path):  # G-11
    vid = tmp_path / "c.mp4"; vid.write_bytes(b"\x00")
    # video_index as a BARE LIST (not {"videos": [...]}) + an unnamed Person cluster
    # (absent from cluster_identities → display name is the prettified id).
    vi = [{"source_video": "/s/c.mp4", "assigned_persons": ["Person_02"],
           "assigned_scenes": ["concert"]}]
    rows = ad.video_rows({"entries": {"/s/c.mp4": str(vid)}}, {}, {}, "examiner",
                         video_index=vi, cluster_identities={})
    assert rows[0]["persons"] == [{"person_id": "Person_02", "name": "Person 02"}]
    assert rows[0]["scenes"] == ["concert"]


def test_build_photo_universe_drops_missing_archive_both_roles(tmp_path):  # Item 2
    present = tmp_path / "p.jpg"; present.write_bytes(b"\xff\xd8\xff\xd9")
    scene = {"clip_results": {
        "/x/present.jpg": {"category": "beach", "delivered": True},
        "/x/gone.jpg": {"category": "beach", "delivered": True},      # quarantined → archive moved out
        "/x/noarch.jpg": {"category": "beach", "delivered": True},    # no archive entry (examiner-only)
    }, "junk_results": {}}
    amap = {"entries": {"/x/present.jpg": str(present), "/x/gone.jpg": str(tmp_path / "missing.jpg")}}
    ex = ad.build_photo_universe(scene, amap, "examiner")
    assert "/x/present.jpg" in ex and "/x/noarch.jpg" in ex
    assert "/x/gone.jpg" not in ex, "archive-missing (quarantined) image must not show → no broken tile"
    fam = ad.build_photo_universe(scene, amap, "family")
    assert "/x/present.jpg" in fam
    assert "/x/gone.jpg" not in fam and "/x/noarch.jpg" not in fam  # family also needs a present archive


def test_people_rows_excludes_removed():  # Item 6
    fc = {"person_clusters": {"P1": ["/a.jpg"], "P2": ["/b.jpg"]}, "cluster_identities": {}}
    rows = ad.people_rows(fc, {}, {"/a.jpg": {}, "/b.jpg": {}}, {}, "examiner", removed=["P2"])
    assert [r["person_id"] for r in rows] == ["P1"]


def test_review_data_triggered_only(tmp_path):  # Item 14
    paths = CasePaths.from_case_id("C", str(tmp_path))
    md = paths.metadata_dir; md.mkdir(parents=True)
    (md / "sensitive_scan_index.json").write_text(json.dumps({
        "/x/clean.jpg": {"sensitivity_filters": {"weapons": {"triggered": False}}},   # no trigger → excluded
        "/x/hit.jpg": {"sensitivity_filters": {"weapons": {"triggered": True}, "drugs": {"triggered": False}}},
    }))
    (md / "human_review_required.json").write_text(json.dumps({"paths": ["/x/hr.jpg"]}))
    (md / "quarantine_manifest.json").write_text(json.dumps({"entries": []}))
    d = ad.review_data(paths, {})
    sens = {s["name"]: s for s in d["sensitive"]}
    assert "clean.jpg" not in sens, "non-triggered entry excluded"
    assert sens["hit.jpg"]["filters"] == ["weapons"]      # only triggered names
    assert sens["hit.jpg"]["src"] == "/x/hit.jpg"
    hr = {h["name"]: h for h in d["human_review"]}
    assert hr["hr.jpg"]["src"] == "/x/hr.jpg"


def test_message_rows_shape_order_and_discard_excluded():
    ci = [
        {"conversation_id": "sms_aaa", "platform": "sms", "participants": ["+1555", "Mom"],
         "display_name": "Mom", "span": ["2020-01-01 10:00", "2021-06-01 09:00"],
         "message_count": 400, "chunk_count": 12, "call_event_count": 3,
         "attachment_count": 2, "direction_counts": {"sent": 200, "received": 200},
         "triage_verdict": "keep", "triage_reason": "personal", "sources": ["/o/sms.xml"]},
        {"conversation_id": "wa_bbb", "platform": "whatsapp", "participants": ["Dad"],
         "display_name": "", "span": ["2019-01-01 10:00", "2022-01-01 09:00"],
         "message_count": 10, "call_event_count": 0, "triage_verdict": "platform"},
        {"conversation_id": "sms_ccc", "platform": "sms", "participants": ["SpamCo"],
         "display_name": "SpamCo", "span": ["2023-01-01 00:00", "2023-01-02 00:00"],
         "message_count": 900, "triage_verdict": "discard"},
        {"conversation_id": "sms_ddd", "platform": "sms", "participants": ["Sis"],
         "display_name": "Sis", "span": ["2018-01-01 10:00", "2019-01-01 09:00"],
         "message_count": 50, "call_event_count": 1, "triage_verdict": "keep"},
    ]
    # The EXAMINER sees keep + platform (discards reach nobody). Ordering: the
    # keep band first (recency within it), then the platform band — wa_bbb is more
    # recent than sms_ddd but sits behind both keeps.
    rows = ad.message_rows(ci, "examiner")
    ids = [r["conversation_id"] for r in rows]
    assert "sms_ccc" not in ids, "discard verdicts reach nobody"
    assert ids == ["sms_aaa", "sms_ddd", "wa_bbb"]

    # The FAMILY does not see platform traffic. This is not a taste call: only
    # keep-verdict conversations are CHUNKED by message_triage, and chunks are the
    # only thing sensitive_scan ever reads — so a platform conversation shown to
    # the family is a conversation shown to the family that nobody screened.
    fam_ids = [r["conversation_id"] for r in ad.message_rows(ci, "family")]
    assert fam_ids == ["sms_aaa", "sms_ddd"]
    assert "wa_bbb" not in fam_ids, "unscreened platform traffic reached the family"
    # exact row shape (conversation grain; no raw source paths leak)
    assert set(rows[0]) == {"conversation_id", "platform", "display_name", "participants",
                            "span", "message_count", "call_event_count", "verdict"}
    assert rows[0]["message_count"] == 400 and rows[0]["call_event_count"] == 3
    assert rows[0]["verdict"] == "keep"
    # empty display_name falls back to the participant list
    wb = next(r for r in rows if r["conversation_id"] == "wa_bbb")
    assert wb["display_name"] == "Dad"
    # absent index (message_triage not run) degrades to empty
    assert ad.message_rows(None) == []
    assert ad.message_rows([]) == []
    # capped like email_rows
    big = [{"conversation_id": f"c{i}", "triage_verdict": "keep",
            "span": [None, f"2020-01-{i % 28 + 1:02d} 00:00"]} for i in range(10)]
    assert len(ad.message_rows(big, cap=3)) == 3


def test_conversation_detail_transcript_and_attachments():
    conv = {
        "conversation_id": "sms_aaa", "platform": "sms", "participants": ["Mom"],
        "triage_verdict": "keep",
        "messages": [
            {"ts": "2020-01-02 10:00", "sender": "Mom", "direction": "received",
             "text": "photo attached",
             "attachments": ["/orig/mms/IMG_001.jpg", "/orig/mms/gone.jpg"]},
            {"ts": "2020-01-01 09:00", "sender": "owner", "direction": "sent",
             "text": "hi", "attachments": []},
        ],
        "call_events": [{"ts": "2020-01-03 08:00", "call_type": "missed", "duration_s": 0}],
    }
    resolved = {"/orig/mms/IMG_001.jpg": "/orig/mms/IMG_001.jpg"}
    d = ad.conversation_detail(conv, attachment_resolver=resolved.get)
    assert d["conversation_id"] == "sms_aaa" and d["platform"] == "sms"
    # chronological transcript, direction available for bubble styling
    assert [m["ts"] for m in d["messages"]] == ["2020-01-01 09:00", "2020-01-02 10:00"]
    assert [m["direction"] for m in d["messages"]] == ["sent", "received"]
    atts = {a["name"]: a["src"] for a in d["messages"][1]["attachments"]}
    assert atts["IMG_001.jpg"] == "/orig/mms/IMG_001.jpg", "resolvable → servable src"
    assert atts["gone.jpg"] is None, "unresolvable → name only, never a broken link"
    assert d["call_events"][0]["call_type"] == "missed"
    # long bodies truncated; unknown conversation (stage not run) → None
    long_conv = {"conversation_id": "x", "messages": [{"ts": "2020", "text": "y" * 9000}]}
    assert len(ad.conversation_detail(long_conv, body_cap=100)["messages"][0]["text"]) == 100
    assert ad.conversation_detail(None) is None


def test_review_data_chunk_flag_gets_text_not_broken_preview(tmp_path):
    # A flagged message chunk ("<path>#chunk=<12hex>") must strip the suffix for
    # the source-file name, carry NO preview src (the raw export file is not a
    # useful preview), and attach the flagged chunk's TEXT + conversation_id from
    # message_index.json — mirrors the video-frame remap pin above.
    md = tmp_path / "md"; md.mkdir()
    flagged = "/orig/messages/chat.txt#chunk=a3f9c02b1d2e"
    (md / "sensitive_scan_index.json").write_text(json.dumps({
        flagged: {"sensitivity_filters": {"suicidal_ideation": {"triggered": True}}},
        "/x/photo.jpg": {"sensitivity_filters": {"nudity": {"triggered": True}}},
    }))
    (md / "human_review_required.json").write_text(json.dumps({"paths": [flagged]}))
    (md / "message_index.json").write_text(json.dumps([
        {"file": flagged, "chunk_sha256": "a3f9c02b1d2e" + "0" * 52,
         "ocr_text": "the flagged transcript text " * 200, "conversation_id": "sms_aaa"},
        {"file": "/orig/messages/chat.txt#chunk=ffffffffffff",
         "chunk_sha256": "f" * 64, "ocr_text": "other chunk"},
    ]))

    class _P:
        metadata_dir = md
    data = ad.review_data(_P(), {})
    sby = {r["name"]: r for r in data["sensitive"]}
    s = sby["chat.txt"]
    assert s["src"] is None, "chunk ref must not be offered as a file preview"
    assert s["chunk_text"].startswith("the flagged transcript text")
    assert len(s["chunk_text"]) <= 2000, "chunk text truncated"
    assert s["conversation_id"] == "sms_aaa"
    assert sby["photo.jpg"]["src"] == "/x/photo.jpg", "plain paths unaffected"
    assert "chunk_text" not in sby["photo.jpg"]
    # same fix on the human-review list
    h = data["human_review"][0]
    assert h["name"] == "chat.txt" and h["src"] is None
    assert h["chunk_text"].startswith("the flagged transcript text")
    assert h["conversation_id"] == "sms_aaa"


def test_review_data_chunk_flag_degrades_without_message_index(tmp_path):
    # Older cases: a chunk-suffixed flag with NO message_index.json still renders
    # name-only (src None), no chunk_text, no crash.
    md = tmp_path / "md"; md.mkdir()
    flagged = "/orig/messages/chat.txt#chunk=a3f9c02b1d2e"
    (md / "sensitive_scan_index.json").write_text(json.dumps({
        flagged: {"sensitivity_filters": {"suicidal_ideation": {"triggered": True}}},
    }))

    class _P:
        metadata_dir = md
    data = ad.review_data(_P(), {})
    s = data["sensitive"][0]
    assert s["name"] == "chat.txt" and s["src"] is None
    assert "chunk_text" not in s and "conversation_id" not in s


# ── vital-documents checklist (G-2) ──────────────────────────────────────────

def _vital_case(tmp_path):
    """A case with a confirmed will (browsable document), a confirmed deed found
    only in an email (not browsable), a confirmed credentials doc, plus a
    searched-for-but-not-found passport."""
    paths = CasePaths.from_case_id("V", str(tmp_path))
    md = paths.metadata_dir; md.mkdir(parents=True)
    (md / "vital_doc_confirmed.json").write_text(json.dumps([
        {"path": "/d/will.pdf", "target": "will_testament", "tag": "vital_doc:will_testament"},
        {"path": "/m/deed.eml", "target": "deed_title", "tag": "vital_doc:deed_title"},
        {"path": "/d/creds.pdf", "target": "credentials", "tag": "vital_doc:credentials"},
    ]))
    (md / "vital_doc_candidates.json").write_text(json.dumps({
        "will_testament": {"description": "last will", "hits": [{"path": "/x", "score": 0.5}]},
        "deed_title": {"description": "property deed", "hits": []},
        "passport_id": {"description": "passport", "hits": [{"a": 1}, {"b": 2}]},
        "credentials": {"description": "credentials", "hits": []},
    }))
    summary = {"document_classifications": [
        {"file": "/d/will.pdf", "filename": "will.pdf", "category": "legal", "source": "document"},
        {"file": "/m/deed.eml", "filename": "deed.eml", "category": "legal", "source": "email"},
        {"file": "/d/creds.pdf", "filename": "creds.pdf", "category": "account_credentials",
         "source": "document"},
    ]}
    return paths, summary


def test_vital_docs_found_not_found_and_ordering(tmp_path):
    paths, summary = _vital_case(tmp_path)
    vd = ad.vital_docs_data(paths, summary, "family")
    assert vd["available"] is True
    by = {t["target"]: t for t in vd["targets"]}
    # candidate keys are the canonical searched-for list (found + not-found rows)
    assert vd["targets"][0]["target"] == "will_testament"        # candidate order preserved
    assert [t["target"] for t in vd["targets"]] == \
        ["will_testament", "deed_title", "passport_id", "credentials"]
    assert by["will_testament"]["found"] is True
    assert by["passport_id"]["found"] is False and by["passport_id"]["items"] == []
    assert by["passport_id"]["label"] == "Passport / ID"
    assert vd["found_count"] == 3 and vd["total_count"] == 4


def test_vital_docs_file_id_maps_only_browsable(tmp_path):
    paths, summary = _vital_case(tmp_path)
    fam = {t["target"]: t for t in ad.vital_docs_data(paths, summary, "family")["targets"]}
    # browsable document → deep-linkable via its source path
    assert fam["will_testament"]["items"][0]["file_id"] == "/d/will.pdf"
    assert fam["will_testament"]["items"][0]["name"] == "will.pdf"
    # email-sourced find → shown but not linkable (lives in the Emails section)
    assert fam["deed_title"]["found"] is True
    assert fam["deed_title"]["items"][0]["file_id"] is None
    # family must never deep-link a raw account_credentials doc
    assert fam["credentials"]["items"][0]["file_id"] is None
    # examiner MAY open the credentials doc
    exam = {t["target"]: t for t in ad.vital_docs_data(paths, summary, "examiner")["targets"]}
    assert exam["credentials"]["items"][0]["file_id"] == "/d/creds.pdf"


def test_vital_docs_near_miss_count_examiner_only(tmp_path):
    paths, summary = _vital_case(tmp_path)
    fam = ad.vital_docs_data(paths, summary, "family")["targets"]
    assert all("near_miss_count" not in t for t in fam), "family never sees near-miss counts"
    assert all("candidate_count" not in t for t in fam)
    exam = {t["target"]: t for t in ad.vital_docs_data(paths, summary, "examiner")["targets"]}
    assert exam["passport_id"]["near_miss_count"] == 2   # near-miss hits surfaced
    assert exam["will_testament"]["near_miss_count"] == 1
    assert exam["deed_title"]["near_miss_count"] == 0


def _near_miss_case(tmp_path, *, k=4, rejections=True):
    """A case whose will_testament target has `k` candidate hits, ONE of which was
    confirmed — so there are k-1 near-misses. Mixes a browsable document, an
    email-sourced hit, and (when `rejections`) a not-evaluated one."""
    paths = CasePaths.from_case_id("N", str(tmp_path))
    md = paths.metadata_dir
    md.mkdir(parents=True, exist_ok=True)
    hits = [{"path": "/d/will.pdf", "score": 0.90, "snippet": "the confirmed one"},
            {"path": "/m/msg1.eml", "score": 0.80, "snippet": "mail about a will"},
            {"path": "/d/blank.pdf", "score": 0.20, "snippet": ""}]
    # Pad out to k with plain document hits so a high-k case can be exercised.
    hits += [{"path": f"/d/pad{i}.pdf", "score": 0.5, "snippet": f"pad {i}"}
             for i in range(max(0, k - len(hits)))]
    (md / "vital_doc_candidates.json").write_text(json.dumps(
        {"will_testament": {"description": "last will", "hits": hits}}))
    (md / "vital_doc_confirmed.json").write_text(json.dumps(
        [{"path": "/d/will.pdf", "target": "will_testament",
          "tag": "vital_doc:will_testament"}]))
    if rejections:
        (md / "vital_doc_rejections.json").write_text(json.dumps({"will_testament": [
            {"path": "/m/msg1.eml", "score": 0.80, "disposition": "rejected",
             "reason_code": "type_mismatch",
             "reason": "type mismatch: identified as 'correspondence', not a 'last will'"},
            {"path": "/d/blank.pdf", "score": 0.20, "disposition": "not_evaluated",
             "reason_code": "no_text",
             "reason": "no extractable text — the confirm step never read this file"},
        ] + [{"path": f"/d/pad{i}.pdf", "score": 0.5, "disposition": "rejected",
              "reason_code": "llm_no", "reason": None}
             for i in range(max(0, k - 3))]}))
    summary = {"document_classifications": [
        {"file": "/d/will.pdf", "filename": "will.pdf", "source": "document"},
        {"file": "/d/blank.pdf", "filename": "blank.pdf", "source": "document"},
        {"file": "/m/msg1.eml", "filename": "msg1.eml", "source": "email"},
    ] + [{"file": f"/d/pad{i}.pdf", "filename": f"pad{i}.pdf", "source": "document"}
         for i in range(max(0, k - 3))]}
    return paths, summary


def test_near_miss_rows_excludes_confirmed_and_carries_reasons(tmp_path):
    paths, summary = _near_miss_case(tmp_path)
    rows = ad.near_miss_rows(paths, summary, "examiner", "will_testament")
    paths_out = [r["path"] for r in rows]
    # the confirmed hit is NOT a near-miss; everything else is
    assert "/d/will.pdf" not in paths_out
    assert len(rows) == 3
    by = {r["path"]: r for r in rows}
    assert by["/m/msg1.eml"]["reason_code"] == "type_mismatch"
    assert "correspondence" in by["/m/msg1.eml"]["reason"]
    assert by["/d/blank.pdf"]["disposition"] == "not_evaluated"


def test_near_miss_rows_not_evaluated_sorts_first(tmp_path):
    # The lowest-scoring row (0.20) must still come first: "we never read this
    # file" outranks "we read it and said no", and with pagination that ordering
    # is what keeps it on page 1.
    paths, summary = _near_miss_case(tmp_path)
    rows = ad.near_miss_rows(paths, summary, "examiner", "will_testament")
    assert rows[0]["path"] == "/d/blank.pdf"
    assert rows[0]["disposition"] == "not_evaluated"
    # the rest are score-descending
    rest = [r["score"] for r in rows[1:]]
    assert rest == sorted(rest, reverse=True)


def test_near_miss_rows_deep_links_email_via_thread(tmp_path):
    paths, summary = _near_miss_case(tmp_path)
    # An email-sourced near-miss is NOT browsable in the documents view; it must
    # resolve to its conversation instead (a majority of real hits are email).
    idx = {"threads": [{"thread_id": "T7", "subject": "Re: the will",
                        "files": ["/m/msg1.eml"]}]}
    rows = ad.near_miss_rows(paths, summary, "examiner", "will_testament",
                             threads_index=idx)
    by = {r["path"]: r for r in rows}
    assert by["/m/msg1.eml"]["file_id"] is None
    assert by["/m/msg1.eml"]["thread_id"] == "T7"
    assert by["/m/msg1.eml"]["thread_subject"] == "the will"   # reply prefix stripped
    # a browsable document near-miss links the other way
    assert by["/d/blank.pdf"]["file_id"] == "/d/blank.pdf"
    assert by["/d/blank.pdf"]["thread_id"] is None


def test_near_miss_rows_deep_links_message_via_conversation(tmp_path):
    # Backlog #26: a vital-doc hit sourced from a chat/SMS database chunk
    # ('<source>#chunk=<12hex>') is neither a browsable document nor an email
    # thread — it must resolve through message_index.json/conversation_index.json
    # to its conversation instead of staying an unopenable stub.
    # Deliberately NOT added to document_classifications — message chunks never
    # go through llm_synthesis's document classifier (a parallel, doc-only lane),
    # so a real message-sourced hit is never "browsable" the way a document is.
    paths, summary = _near_miss_case(tmp_path)
    chunk_path = "/orig/messages/chat.txt#chunk=a3f9c02b1d2e"
    (paths.metadata_dir / "message_index.json").write_text(json.dumps([
        {"file": chunk_path, "chunk_sha256": "a3f9c02b1d2e" + "0" * 52,
         "conversation_id": "sms_1", "participants": ["Alice", "Bob"]},
    ]))
    (paths.metadata_dir / "conversation_index.json").write_text(json.dumps([
        {"conversation_id": "sms_1", "platform": "sms", "participants": ["Alice", "Bob"],
         "display_name": "", "triage_verdict": "keep"},
    ]))
    candidates = json.loads((paths.metadata_dir / "vital_doc_candidates.json").read_text())
    candidates["will_testament"]["hits"].append(
        {"path": chunk_path, "score": 0.75, "snippet": "a text about the will"})
    (paths.metadata_dir / "vital_doc_candidates.json").write_text(json.dumps(candidates))

    rows = ad.near_miss_rows(paths, summary, "examiner", "will_testament")
    by = {r["path"]: r for r in rows}
    assert by[chunk_path]["file_id"] is None
    assert by[chunk_path]["thread_id"] is None
    assert by[chunk_path]["conversation_id"] == "sms_1"
    # no display_name set → falls back to participants
    assert by[chunk_path]["conversation_subject"] == "Alice, Bob"


def test_near_miss_rows_message_link_scoped_to_audience(tmp_path):
    # Mirrors _vital_thread_links' own-audience rule: a message chunk whose
    # conversation the family may not see (estate-rescued here) resolves to
    # nothing for family and stays a stub, while the examiner still gets it.
    paths, summary = _near_miss_case(tmp_path)
    chunk_path = "/orig/messages/chat.txt#chunk=a3f9c02b1d2e"
    (paths.metadata_dir / "message_index.json").write_text(json.dumps([
        {"file": chunk_path, "chunk_sha256": "a3f9c02b1d2e" + "0" * 52,
         "conversation_id": "sms_1", "participants": ["Alice"], "estate_rescued": True},
    ]))
    (paths.metadata_dir / "conversation_index.json").write_text(json.dumps([
        {"conversation_id": "sms_1", "platform": "sms", "participants": ["Alice"],
         "triage_verdict": "keep"},
    ]))
    candidates = json.loads((paths.metadata_dir / "vital_doc_candidates.json").read_text())
    candidates["will_testament"]["hits"].append(
        {"path": chunk_path, "score": 0.75, "snippet": "a text about the will"})
    (paths.metadata_dir / "vital_doc_candidates.json").write_text(json.dumps(candidates))

    # near_miss_rows itself is examiner-only (returns [] for family), so the
    # audience scoping is exercised directly on the lower-level resolver.
    fam_links = ad._vital_message_links(paths.metadata_dir, "family", {chunk_path})
    exam_links = ad._vital_message_links(paths.metadata_dir, "examiner", {chunk_path})
    assert fam_links == {}
    assert exam_links[chunk_path]["conversation_id"] == "sms_1"


def test_vital_docs_message_chunk_deep_links_via_conversation(tmp_path):
    paths, summary = _vital_case(tmp_path)
    chunk_path = "/orig/messages/chat.txt#chunk=a3f9c02b1d2e"
    confirmed = json.loads((paths.metadata_dir / "vital_doc_confirmed.json").read_text())
    confirmed.append({"path": chunk_path, "target": "power_of_attorney",
                      "tag": "vital_doc:power_of_attorney"})
    (paths.metadata_dir / "vital_doc_confirmed.json").write_text(json.dumps(confirmed))
    (paths.metadata_dir / "message_index.json").write_text(json.dumps([
        {"file": chunk_path, "chunk_sha256": "a3f9c02b1d2e" + "0" * 52,
         "conversation_id": "sms_1", "participants": ["Alice", "Bob"]},
    ]))
    (paths.metadata_dir / "conversation_index.json").write_text(json.dumps([
        {"conversation_id": "sms_1", "platform": "sms", "participants": ["Alice", "Bob"],
         "display_name": "Alice & Bob", "triage_verdict": "keep"},
    ]))

    vd = ad.vital_docs_data(paths, summary, "examiner")
    by = {t["target"]: t for t in vd["targets"]}
    item = by["power_of_attorney"]["items"][0]
    assert item["file_id"] is None
    assert item["thread_id"] is None
    assert item["conversation_id"] == "sms_1"
    assert item["conversation_subject"] == "Alice & Bob"


def test_vital_pager_items_carries_conversation_fields():
    unconfirmed = [{"id": "x::/c#chunk=abc", "target": "t", "name": "chat.txt",
                    "file_id": None, "thread_id": None, "thread_subject": None,
                    "conversation_id": "sms_1", "conversation_subject": "Alice, Bob"}]
    items = ad.vital_pager_items(unconfirmed, [])
    assert items[0]["conversation_id"] == "sms_1"
    assert items[0]["conversation_subject"] == "Alice, Bob"


def test_near_miss_rows_examiner_only(tmp_path):
    paths, summary = _near_miss_case(tmp_path)
    assert ad.near_miss_rows(paths, summary, "family", "will_testament") == []
    assert ad.near_miss_rows(paths, summary, "examiner", "will_testament") != []


def test_near_miss_rows_without_rejections_file_still_links(tmp_path):
    # Every case processed before this feature has no vital_doc_rejections.json.
    # The rows must still appear (path/score/snippet/links), just without a reason.
    paths, summary = _near_miss_case(tmp_path, rejections=False)
    rows = ad.near_miss_rows(paths, summary, "examiner", "will_testament")
    assert len(rows) == 3
    assert all(r["disposition"] == "unknown" for r in rows)
    assert all(r["reason"] is None for r in rows)
    by = {r["path"]: r for r in rows}
    assert by["/d/blank.pdf"]["file_id"] == "/d/blank.pdf"   # links still work


def test_near_miss_count_matches_rows_length(tmp_path):
    # The count on the checklist row and the list it opens share one definition;
    # them disagreeing is the defect this feature exists to fix.
    paths, summary = _near_miss_case(tmp_path, k=9)
    exam = {t["target"]: t for t in
            ad.vital_docs_data(paths, summary, "examiner")["targets"]}
    rows = ad.near_miss_rows(paths, summary, "examiner", "will_testament")
    assert exam["will_testament"]["near_miss_count"] == len(rows) == 8


def test_near_miss_count_zero_when_all_confirmed(tmp_path):
    # The old candidate_count reported the whole retrieval pool, so a
    # fully-confirmed target claimed near-misses that did not exist.
    paths = CasePaths.from_case_id("Z", str(tmp_path))
    md = paths.metadata_dir
    md.mkdir(parents=True)
    (md / "vital_doc_candidates.json").write_text(json.dumps(
        {"deed_title": {"description": "deed", "hits": [
            {"path": "/d/a.pdf", "score": 0.9, "snippet": "a"},
            {"path": "/d/b.pdf", "score": 0.8, "snippet": "b"}]}}))
    (md / "vital_doc_confirmed.json").write_text(json.dumps([
        {"path": "/d/a.pdf", "target": "deed_title", "tag": "vital_doc:deed_title"},
        {"path": "/d/b.pdf", "target": "deed_title", "tag": "vital_doc:deed_title"}]))
    exam = {t["target"]: t for t in
            ad.vital_docs_data(paths, {}, "examiner")["targets"]}
    assert exam["deed_title"]["near_miss_count"] == 0
    assert ad.near_miss_rows(paths, {}, "examiner", "deed_title") == []


def test_near_miss_count_unaffected_by_reassign(tmp_path):
    # A reassigned item keeps its ORIGINAL candidate bucket. Counting against the
    # DISPLAY bucket would over-count the bucket it left and under-count the one
    # it joined.
    paths, summary = _near_miss_case(tmp_path)
    iid = ad.vital_doc_item_id("will_testament", "/d/will.pdf")
    decisions = {"vital_doc_target": {iid: "deed_title"}}
    exam = {t["target"]: t for t in ad.vital_docs_data(
        paths, summary, "examiner", decisions=decisions)["targets"]}
    # the will moved to the deed bucket for DISPLAY, but it is still not a
    # near-miss of will_testament — it was confirmed under it.
    assert exam["will_testament"]["near_miss_count"] == 3
    assert exam["deed_title"]["near_miss_count"] == 0


def test_promoted_near_miss_becomes_a_found_item(tmp_path):
    paths, summary = _near_miss_case(tmp_path)
    iid = ad.vital_doc_item_id("will_testament", "/m/msg1.eml")
    decisions = {"vital_doc_promoted": {
        iid: {"target": "will_testament", "path": "/m/msg1.eml"}}}
    vd = ad.vital_docs_data(paths, summary, "examiner", decisions=decisions)
    row = {t["target"]: t for t in vd["targets"]}["will_testament"]
    promoted = [i for i in row["items"] if i["promoted"]]
    assert len(promoted) == 1 and promoted[0]["path"] == "/m/msg1.eml"
    # promotion is an assertion by the examiner → reviewed by construction
    assert promoted[0]["reviewed"] is True
    # and it leaves the near-miss list
    assert row["near_miss_count"] == 2
    assert "/m/msg1.eml" not in [
        r["path"] for r in ad.near_miss_rows(paths, summary, "examiner",
                                             "will_testament", decisions=decisions)]


def test_dismissed_near_miss_leaves_the_list(tmp_path):
    # Dismissal is a ruling ("not a vital document"); it must not come back as an
    # unreviewed near-miss.
    paths, summary = _near_miss_case(tmp_path)
    decisions = {"vital_doc_dismissed": {"/m/msg1.eml": {"reason": "not a will"}}}
    rows = ad.near_miss_rows(paths, summary, "examiner", "will_testament",
                             decisions=decisions)
    assert "/m/msg1.eml" not in [r["path"] for r in rows]
    assert len(rows) == 2


def test_vital_docs_death_certificate_label():
    # User-approved exception to the no-death-wording policy: the legal document
    # type is surfaced by its factual name (see VITAL_DOC_LABELS comment).
    assert ad.VITAL_DOC_LABELS["death_certificate"] == "Death certificate"
    assert ad.vital_doc_label("death_certificate") == "Death certificate"
    assert ad.vital_doc_label("unknown_new_target") == "Unknown new target"  # readable fallback


def test_vital_docs_absent_files_graceful(tmp_path):
    # Older cases predate vital_doc_confirm — neither file exists → available False,
    # no crash.
    paths = CasePaths.from_case_id("Old", str(tmp_path))
    paths.metadata_dir.mkdir(parents=True)
    vd = ad.vital_docs_data(paths, {}, "family")
    assert vd == {"available": False, "targets": [], "found_count": 0, "total_count": 0}
    # overview card degrades to an unavailable stub (no crash, no key explosion)
    assert ad._vital_docs_overview(vd) == {"available": False}


def test_overview_data_carries_vital_docs_card(tmp_path):
    paths, summary = _vital_case(tmp_path)
    vd = ad.vital_docs_data(paths, summary, "family")
    ov = ad.overview_data(summary, "family", {}, vital_docs=vd)
    card = ov["vital_docs"]
    assert card["available"] is True and card["found_count"] == 3 and card["total_count"] == 4
    assert {"label": "Passport / ID", "found": False, "near_misses": None} \
        in card["types"], \
        "near_misses stays absent for family — the card must not be able to say " \
        "'0 unreviewed' to somebody who is not doing the reviewing"
    # omitting vital_docs (e.g. static explorer) must not crash — stub card
    assert ad.overview_data(summary, "family", {})["vital_docs"] == {"available": False}


def test_build_search_includes_conversations():
    convs = [{"conversation_id": "sms_aaa", "display_name": "Mom",
              "participants": ["Mom", "+15551234567"], "platform": "sms"}]
    s = ad.build_search([], [], [], [], conversations=convs)
    assert len(s["records"]) == 1
    rec = s["records"][0]
    assert rec["p"] == "messages" and rec["k"] == "conversation"
    assert rec["t"] == "Mom" and rec["h"] == "sms_aaa"
    assert "sms" in rec["s"]
    assert 0 in s["index"].get("mom", []), "participants searchable"
    # no conversations (stage absent) → unchanged empty index
    assert ad.build_search([], [], [], [])["records"] == []


def test_audio_rows_carries_transcript_affordance_fields():
    # G-3: audio_rows additively carries segment_count + has_transcript so the list
    # can show a "has transcript" affordance, and back-fills duration/language from
    # the transcription record when the classification lacks them.
    summary = {"audio_classifications": [
        {"file": "/a/vm.caf", "filename": "vm.caf", "category": "voicemail",
         "significance": 4, "summary": "A voicemail."},
        {"file": "/a/silent.caf", "filename": "silent.caf", "category": "other"},
    ]}
    tindex = [
        {"file": "/a/vm.caf", "transcript_text": "Hi there", "duration": 20.6,
         "language": "en", "segment_count": 3},
        {"file": "/a/silent.caf", "transcript_text": "", "segment_count": 0},
    ]
    rows = {r["file"]: r for r in ad.audio_rows(summary, tindex, "family", {})}
    assert rows["/a/vm.caf"]["has_transcript"] is True
    assert rows["/a/vm.caf"]["segment_count"] == 3
    assert rows["/a/vm.caf"]["duration"] == 20.6 and rows["/a/vm.caf"]["language"] == "en"
    assert rows["/a/silent.caf"]["has_transcript"] is False
    # transcribe.deliver=false still withholds ALL audio from a family build
    assert ad.audio_rows(summary, tindex, "family", {"transcribe": {"deliver": False}}) == []


def test_parse_vtt_stdlib_parser():
    vtt = ("WEBVTT\n\n"
           "1\n"
           "00:00:00.000 --> 00:00:02.000\n"
           "Can you turn that down?\n\n"
           "00:00:02.000 --> 00:01:07.580\n"
           "Use the remote.\nPlease.\n")
    segs = ad.parse_vtt(vtt)
    assert segs[0] == {"start": 0.0, "end": 2.0, "text": "Can you turn that down?"}
    # HH:MM:SS carrying → 1:07.58 = 67.58s; multi-line cue text joined
    assert segs[1]["start"] == 2.0 and segs[1]["end"] == 67.58
    assert segs[1]["text"] == "Use the remote. Please."
    # header-only / empty → no cues, never raises
    assert ad.parse_vtt("WEBVTT\n") == [] and ad.parse_vtt("") == []


def test_transcript_detail_from_json_sidecar():
    # .json sidecar is preferred (per-segment start/end/confidence); leading
    # whitespace on segment text is stripped.
    idx = [{"file": "/c/audio/a.caf", "json_sidecar": "/c/audio/a.json",
            "vtt_sidecar": "/c/audio/a.vtt", "transcript_text": "Hi there",
            "duration": 20.6, "language": "en", "segment_count": 2}]
    sidecars = {"/c/audio/a.json": json.dumps({"segments": [
        {"start": 0.85, "end": 2.05, "text": " Hi", "confidence": 0.9},
        {"start": 2.05, "end": 4.0, "text": " there"}]})}
    d = ad.transcript_detail(idx, "/c/audio/a.caf", sidecars.get, has_audio=True)
    assert [s["start"] for s in d["segments"]] == [0.85, 2.05]
    assert d["segments"][0] == {"start": 0.85, "end": 2.05, "text": "Hi"}
    assert d["has_audio"] is True and d["language"] == "en" and d["duration"] == 20.6


def test_transcript_detail_falls_back_to_vtt_when_json_unreadable():
    # .json sidecar missing/reaped → parse the .vtt (parser path).
    idx = [{"file": "/c/audio/b.caf", "json_sidecar": "/c/audio/b.json",
            "vtt_sidecar": "/c/audio/b.vtt", "transcript_text": "x", "segment_count": 2}]
    vtt = "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nfirst\n\n00:00:02.000 --> 00:00:07.580\nsecond\n"
    d = ad.transcript_detail(idx, "/c/audio/b.caf",
                             {"/c/audio/b.vtt": vtt}.get, has_audio=True)
    assert [s["text"] for s in d["segments"]] == ["first", "second"]
    assert d["segments"][1]["end"] == 7.58


def test_transcript_detail_absent_sidecar_degrades_to_text():
    # Reaped/refused sidecars (read_sidecar → None for everything) → empty segments
    # but transcript_text is preserved so the UI still shows the text (goog case).
    idx = [{"file": "/c/audio/c.caf", "json_sidecar": "/c/audio/c.json",
            "vtt_sidecar": "/c/audio/c.vtt", "transcript_text": "reaped words",
            "segment_count": 0}]
    d = ad.transcript_detail(idx, "/c/audio/c.caf", lambda _p: None, has_audio=False)
    assert d["segments"] == [] and d["transcript_text"] == "reaped words"
    assert d["has_audio"] is False
    # unknown recording → None (→ 404 at the endpoint)
    assert ad.transcript_detail(idx, "/c/audio/nope.caf", lambda _p: None) is None


def test_split_chunk_ref():
    assert ad.split_chunk_ref("/a/chat.txt#chunk=0123456789ab") == ("/a/chat.txt", "0123456789ab")
    assert ad.split_chunk_ref("/a/chat.txt") == ("/a/chat.txt", None)
    # wrong length / non-hex suffixes are not chunk refs
    assert ad.split_chunk_ref("/a/x#chunk=123") == ("/a/x#chunk=123", None)
    assert ad.split_chunk_ref("/a/x#chunk=ZZZZZZZZZZZZ") == ("/a/x#chunk=ZZZZZZZZZZZZ", None)


# ── pagination: builders return the FULL set uncapped by default (F-3) ──

def test_builders_uncapped_by_default():
    # The silent hard caps are now opt-in (cap=None default) — the full sorted
    # list is returned so the section/API layer can paginate it honestly. A caller
    # that passes an explicit cap still gets it (the explorer may, for its bundle).
    docs = {"document_classifications": [
        {"file": f"/d/{i}.pdf", "filename": f"{i}.pdf", "category": "legal",
         "source": "document", "significance": i % 5} for i in range(9000)]}
    assert len(ad.document_rows(docs, [], "examiner")) == 9000
    assert len(ad.document_rows(docs, [], "examiner", cap=100)) == 100  # opt-in still works

    ti = {"threads": [{"thread_id": f"t{i}", "subject": f"s{i}", "significance": 0,
                       "date_last": "2020-01-01"} for i in range(6000)]}
    assert len(ad.email_rows(ti)) == 6000                                # was capped at 5000
    assert len(ad.email_rows(ti, cap=10)) == 10

    ci = [{"conversation_id": f"c{i}", "triage_verdict": "keep",
           "span": [None, "2020-01-01"]} for i in range(6000)]
    assert len(ad.message_rows(ci)) == 6000                              # was capped at 5000


def test_scanned_and_video_rows_uncapped_by_default(tmp_path):
    label = list(ad.SCENE_LABELS)[0]
    scene = {"clip_results": {f"/s/{i}.jpg": {"category": label, "delivered": True}
                              for i in range(4500)}, "junk_results": {}}
    assert len(ad.scanned_image_rows(scene, {"entries": {}}, {}, "examiner")) == 4500  # was 4000

    vids = tmp_path / "v"; vids.mkdir()
    entries = {}
    for i in range(4200):
        p = vids / f"{i}.mp4"; p.write_bytes(b"\x00")
        entries[f"/src/{i}.mp4"] = str(p)
    am = {"entries": entries}
    assert len(ad.video_rows(am, {}, {}, "examiner")) == 4200                          # was 4000


def test_build_search_indexes_all_documents_uncapped():
    # The search-index caveat (family-archive-pagination.md): build_search must be
    # fed the UNCAPPED builder output — a document past the old 8000 cap is
    # otherwise unfindable (the index itself is truncated, not just the UI).
    docs = [{"file": f"/d/{i}.pdf", "name": f"doc{i}", "category": "misc",
             "summary": "", "preview": f"token{i}"} for i in range(8500)]
    s = ad.build_search([], [], docs, [])
    assert len(s["records"]) == 8500
    # a record past the old 8000 cap is present AND findable via its token
    assert any(r["t"] == "doc8400" for r in s["records"])
    assert "token8400" in s["index"]


def test_photo_rows_carries_owner_gallery_layer_and_llava_caption():
    # G-1/G-7: photo_rows must surface the owner's favorites/hidden/albums/source
    # and a caption (owner's gallery_caption if set, else the LLaVA description),
    # all SOURCE-CONDITIONAL and defaulting to falsy/empty when absent.
    universe = {
        "/p/fav.jpg": {"category": "beach", "delivered": True},
        "/p/hid.jpg": {"category": "home", "delivered": True},
        "/p/plain.jpg": {"category": "city", "delivered": True},
    }
    metadata_index = {
        "/p/fav.jpg": {"photo_library_favorite": True, "gallery_source": "iphoto",
                       "album_membership": ["Summer 2019", ""], "gallery_caption": "Owner's own words"},
        "/p/hid.jpg": {"photo_library_hidden": True, "gallery_source": "google_takeout",
                       "album_membership": "Trips"},   # scalar → coerced to a 1-list
    }
    llava = {"/p/plain.jpg": "A dog on a beach at sunset.",
             "/p/fav.jpg": "IGNORED because gallery_caption wins."}
    rows = {r["id"]: r for r in ad.photo_rows(universe, metadata_index, {}, {}, llava_map=llava)}

    fav = rows["/p/fav.jpg"]
    assert fav["favorite"] is True and fav["hidden"] is False
    assert fav["albums"] == ["Summer 2019"]          # blank dropped
    assert fav["source"] == "iphoto"
    assert fav["caption"] == "Owner's own words"      # gallery_caption beats LLaVA

    hid = rows["/p/hid.jpg"]
    assert hid["hidden"] is True and hid["favorite"] is False
    assert hid["albums"] == ["Trips"]                 # scalar coerced
    assert hid["source"] == "google_takeout"
    assert hid["caption"] is None                     # no caption for this one

    plain = rows["/p/plain.jpg"]
    assert plain["caption"] == "A dog on a beach at sunset."   # LLaVA fallback
    assert plain["favorite"] is False and plain["hidden"] is False
    assert plain["albums"] == [] and plain["source"] is None
    assert plain["people"] == []                      # plumbed defensively, empty


def test_photo_rows_carries_exif_metadata_panel_fields():
    # F-10: the lightbox metadata panel is fed from photo_rows. The EXIF facts that
    # exist in metadata_index (camera make/model, pixel dimensions, structured place,
    # GPS altitude) must be surfaced as additive keys, defaulting to None when absent
    # (a case that never carried them is indistinguishable from before).
    universe = {
        "/p/shot.jpg": {"category": "beach", "delivered": True},
        "/p/bare.jpg": {"category": "home", "delivered": True},
    }
    metadata_index = {
        "/p/shot.jpg": {
            "camera_make": "Apple", "camera_model": "iPad",
            "width_px": 2048, "height_px": 1530,
            "place": "Portland_Oregon",
            "place_detail": {"name": "Portland", "admin1": "Oregon", "cc": "US"},
            "gps": {"lat": 45.507, "lon": -122.634},
            "gps_altitude_m": 67.0,
        },
    }
    rows = {r["id"]: r for r in ad.photo_rows(universe, metadata_index, {}, {})}

    shot = rows["/p/shot.jpg"]
    assert shot["camera_make"] == "Apple" and shot["camera_model"] == "iPad"
    assert shot["width_px"] == 2048 and shot["height_px"] == 1530
    assert shot["place_detail"] == {"name": "Portland", "admin1": "Oregon", "cc": "US"}
    assert shot["gps_altitude_m"] == 67.0

    # Absent → present-but-None keys (additive, non-breaking); envelope unchanged.
    bare = rows["/p/bare.jpg"]
    for k in ("camera_make", "camera_model", "width_px", "height_px",
              "place_detail", "gps_altitude_m"):
        assert k in bare and bare[k] is None, k


def test_photo_rows_caption_neutralized_and_absent_by_default():
    # The caption is OUR generated text → neutralize_summary is applied (identity
    # scrub). With no metadata + no llava map, all gallery-layer fields degrade.
    universe = {"/p/a.jpg": {"category": "x", "delivered": True}}
    llava = {"/p/a.jpg": "The deceased's dog in the yard."}
    r = ad.photo_rows(universe, {}, {}, {}, llava_map=llava)[0]
    assert r["caption"] == "The owner's dog in the yard."   # identity language scrubbed
    # no metadata, no llava → clean defaults, never raises
    r2 = ad.photo_rows(universe, {}, {}, {})[0]
    assert r2["caption"] is None and r2["albums"] == [] and r2["favorite"] is False


def test_build_search_photo_carries_caption_and_href():
    # G-7: the caption is folded into the photo's searchable text; G-9: photo /
    # document / audio records carry an href (their id) so a hit opens the item.
    photos = [{"id": "/p/a.jpg", "name": "a.jpg", "scene": "beach", "place": "Tahoe",
               "trip": None, "caption": "A red kite over the water.", "albums": ["Vacation"]}]
    docs = [{"file": "/d/will.pdf", "name": "will.pdf", "category": "legal",
             "summary": "", "preview": ""}]
    audio = [{"file": "/a/vm.mp3", "name": "vm.mp3", "category": "voicemail",
              "summary": "", "preview": ""}]
    s = ad.build_search(photos, [], docs, audio)
    by = {r["k"]: r for r in s["records"]}
    assert by["photo"]["h"] == "/p/a.jpg"
    assert by["document"]["h"] == "/d/will.pdf"
    assert by["audio"]["h"] == "/a/vm.mp3"
    # caption tokens reach the index → the photo is findable by its description
    assert "kite" in s["index"], "caption fed the search index"
    assert "vacation" in s["index"], "album name fed the search index"
    assert by["photo"]["p"] == "photos"


def test_actions_history_newest_first(tmp_path):
    paths = CasePaths.from_case_id("C", str(tmp_path))
    md = paths.metadata_dir
    md.mkdir(parents=True)
    (md / "family_actions.ndjson").write_text(
        '{"ts":"2026-01-01","action":"banish"}\n{"ts":"2026-01-02","action":"rename_person"}\n',
        encoding="utf-8")
    hist = ad.actions_history(paths)
    assert [h["action"] for h in hist] == ["rename_person", "banish"]
    # missing file → empty list, no error
    assert ad.actions_history(CasePaths.from_case_id("Z", str(tmp_path))) == []


# ── G-13 junk_rows / G-14 transparency_data / G-12 guided_review_data ──

def test_junk_rows_shape_reason_fallback_sort_and_cap():
    # One row per junk_results key; reason = metadata junk_reason (route_junk rule)
    # if present, else the CLIP junk_label. Rows sort by basename; cap truncates.
    scene_index = {"junk_results": {
        "/w/photos/zebra.png": {"junk_label": "a logo", "confidence": 0.41, "source": "clip"},
        "/w/photos/apple.jpg": {"junk_label": "a banner", "confidence": 0.8, "source": "clip"},
        "/w/photos/mango.gif": {"junk_label": "an icon", "confidence": 0.2, "source": "clip"},
    }}
    metadata_index = {"/w/photos/apple.jpg": {"junk_reason": "tiny", "junk_routed": True}}
    rows = ad.junk_rows(scene_index, metadata_index)
    assert [r["name"] for r in rows] == ["apple.jpg", "mango.gif", "zebra.png"]
    apple = next(r for r in rows if r["name"] == "apple.jpg")
    assert apple["id"] == "/w/photos/apple.jpg"
    assert apple["reason"] == "tiny"          # metadata junk_reason wins
    assert apple["confidence"] == 0.8 and apple["source"] == "clip"
    zebra = next(r for r in rows if r["name"] == "zebra.png")
    assert zebra["reason"] == "a logo"        # falls back to CLIP junk_label
    # cap keeps the whole set honest via the caller's envelope; the builder truncates
    assert len(ad.junk_rows(scene_index, {}, cap=2)) == 2
    # absent junk_results → empty, no error
    assert ad.junk_rows({}, {}) == []


def test_transparency_data_family_vs_examiner_gating():
    summary = {"deduplicated_removed": 8278}
    dedup = {"exact_dupes_moved": 999}          # overridden by summary field above
    perceptual = {"groups": [{"keeper": "a"}, {"keeper": "b"}, {"keeper": "c"}]}
    suspense = [{"file": "x"}, {"file": "y"}]
    noise = [
        {"email_from": "a@b.com", "email_subject": "Receipt", "email_date_iso": "2020-01-01",
         "triage_reason": "esp_header", "has_significant_attachment": True},
        {"email_from": "c@d.com", "email_subject": "Spam", "has_significant_attachment": False},
    ]
    # family: numbers only, NO suspense / noise detail
    fam = ad.transparency_data(summary, dedup, perceptual, suspense, noise, "family")
    assert fam["exact_duplicates_removed"] == 8278
    assert fam["near_duplicate_groups"] == 3 and fam["nothing_deleted"] is True
    assert "suspense_count" not in fam and "significant_attachment_noise" not in fam
    # examiner: adds suspense count + only the significant-attachment noise rows
    ex = ad.transparency_data(summary, dedup, perceptual, suspense, noise, "examiner")
    assert ex["suspense_count"] == 2
    assert ex["significant_attachment_total"] == 1
    assert len(ex["significant_attachment_noise"]) == 1
    row = ex["significant_attachment_noise"][0]
    assert row["from"] == "a@b.com" and row["subject"] == "Receipt"
    # no message body / file path leaks into the examiner detail
    assert "file" not in row and "body" not in row
    # exact_duplicates_removed falls back to dedup_summary when summary lacks it
    ex2 = ad.transparency_data({}, dedup, perceptual, [], [], "examiner")
    assert ex2["exact_duplicates_removed"] == 999


def _guided_case(tmp_path, *, delivery_blocked=False, ack=None):
    """Minimal finished case for guided_review_data: quarantine (1 pending),
    human-review (1), one unnamed person cluster, reconciliation attention (1)."""
    paths = CasePaths.from_case_id("G", str(tmp_path))
    md = paths.metadata_dir
    md.mkdir(parents=True)
    (md / "quarantine_manifest.json").write_text(json.dumps({
        "entries": [{"file": "/w/q/bad.jpg", "filter": "explicit_sexual_imagery",
                     "canonical_path": "/c/q/bad.jpg", "quarantine_path": "/c/q/bad.jpg",
                     "timestamp": "t"}],
        "released": []}))
    (md / "sensitive_scan_index.json").write_text(json.dumps({
        "/w/s/x.jpg": {"human_review_required": True,
                       "sensitivity_filters": {"explicit_sexual_imagery": {"triggered": True}}}}))
    (md / "human_review_required.json").write_text(json.dumps({"paths": ["/w/s/x.jpg"]}))
    (md / "reconciliation_manifest.json").write_text(json.dumps({
        "needs_examiner_review": True, "attention_counts": {"unknown_type": 1},
        "review_items": ["Check unknown types"]}))
    (md / "ocr_summary.json").write_text(json.dumps({"manual_review_count": 4}))
    decisions = {"guided_progress": {k: {"decision": "accept"} for k in (ack or [])}}
    summary = {"case_id": "G", "export_gate": {"delivery_blocked": delivery_blocked,
               "reasons": ["blocked"] if delivery_blocked else []}}
    scene_index = {"clip_results": {}, "junk_results": {}}
    face = {"person_clusters": {"Person_01": ["/w/p/a.jpg"]}, "cluster_identities": {},
            "noise_files": []}
    return paths, summary, scene_index, face, decisions


def test_guided_review_assembles_steps_counts_and_done_state(tmp_path):
    paths, summary, scene_index, face, decisions = _guided_case(tmp_path)
    d = ad.guided_review_data(paths, summary, scene_index, face, {}, decisions=decisions)
    steps = {s["key"]: s for s in d["steps"]}
    assert set(steps) == {"quarantine", "human_review", "confirm", "name_persons",
                          "vital_docs", "reconciliation"}
    assert steps["quarantine"]["count"] == 1 and steps["quarantine"]["done"] is False
    assert steps["human_review"]["count"] == 1
    assert steps["human_review"]["extra"]["ocr_manual_review"] == 4
    # one unnamed person cluster → confirm queue name_person item + name_persons step
    assert steps["confirm"]["count"] >= 1
    assert steps["name_persons"]["count"] == 1
    # no vital_doc files → not available → 0 missing → step is done
    assert steps["vital_docs"]["count"] == 0 and steps["vital_docs"]["done"] is True
    assert steps["reconciliation"]["count"] == 1
    assert steps["reconciliation"]["extra"]["needs_examiner_review"] is True
    # each step deep-links to where the action already happens
    assert steps["confirm"]["link"] == "/review?group=confirm"
    assert steps["name_persons"]["link"] == "/people"
    # handoff: not all done, gate clear → not ready
    assert d["handoff"]["ready"] is False and d["handoff"]["delivery_blocked"] is False


def test_guided_review_acknowledged_step_is_done_and_gate_blocks_handoff(tmp_path):
    # Acknowledging a step (persisted under guided_progress via the confirm verb)
    # marks it done even though its count is non-zero. A blocked gate never reads ready.
    paths, summary, scene_index, face, decisions = _guided_case(
        tmp_path, delivery_blocked=True, ack=["quarantine", "human_review", "confirm",
                                              "name_persons", "reconciliation"])
    d = ad.guided_review_data(paths, summary, scene_index, face, {}, decisions=decisions)
    steps = {s["key"]: s for s in d["steps"]}
    assert steps["quarantine"]["acknowledged"] is True and steps["quarantine"]["done"] is True
    assert d["handoff"]["all_steps_done"] is True     # all acknowledged + vital auto-done
    assert d["handoff"]["delivery_blocked"] is True
    assert d["handoff"]["ready"] is False, "a blocked gate can never be handoff-ready"


def test_guided_review_confirm_step_uncapped_and_decrements(tmp_path):
    """The 'Work the confirm queue' step count used to be silently capped at
    ~300/category (620 across scene+face), so it never reflected the true
    backlog and did not decrement as the examiner worked past that many items —
    the queue that gates the release signature looked stuck. This locks in the
    fix: the step count is the TRUE remaining total, and it drops by exactly
    the number resolved, well past the old cap."""
    paths, summary, _scene_index, face, decisions = _guided_case(tmp_path)
    big_scene_index = {"clip_results": {
        f"/w/low{i}.jpg": {"category": "beach", "confidence": 0.2, "delivered": True}
        for i in range(350)
    }, "junk_results": {}}
    d0 = ad.guided_review_data(paths, summary, big_scene_index, face, {}, decisions=decisions)
    confirm0 = {s["key"]: s for s in d0["steps"]}["confirm"]
    # 350 scene guesses + the fixture's 1 unnamed-person item, no cap applied
    assert confirm0["count"] == 351, "must reflect the true backlog, not a ~300 cap"

    decisions["scene"] = {f"/w/low{i}.jpg": {"decision": "confirm"} for i in range(60)}
    d1 = ad.guided_review_data(paths, summary, big_scene_index, face, {}, decisions=decisions)
    confirm1 = {s["key"]: s for s in d1["steps"]}["confirm"]
    assert confirm1["count"] == 291, "count must drop by exactly the 60 resolved"


# ── Vital-documents checklist overlay (dismiss / reassign decisions) ──────────

def _vital_paths(tmp_path, confirmed, candidates):
    import types
    md = tmp_path / "output" / "metadata"
    md.mkdir(parents=True, exist_ok=True)
    (md / "vital_doc_confirmed.json").write_text(json.dumps(confirmed))
    (md / "vital_doc_candidates.json").write_text(json.dumps(candidates))
    return types.SimpleNamespace(metadata_dir=md)


def _vital_fixture(tmp_path):
    """A confirmed list with a SOLE will item and a dup path (same /d/shared.pdf)
    confirmed under TWO targets (deed_title + vehicle_title) — so the two items are
    keyed by (target, path), not path."""
    confirmed = [
        {"path": "/d/will.pdf", "target": "will_testament", "tag": "will"},
        {"path": "/d/shared.pdf", "target": "deed_title", "tag": "maybe-deed"},
        {"path": "/d/shared.pdf", "target": "vehicle_title", "tag": "maybe-title"},
    ]
    candidates = {
        "will_testament": {"description": "a will", "hits": []},
        "deed_title": {"description": "a deed", "hits": []},
        "vehicle_title": {"description": "a title", "hits": []},
        "life_insurance": {"description": "insurance", "hits": []},  # a not-found target
    }
    return _vital_paths(tmp_path, confirmed, candidates), {}


def _row(vd, target):
    for r in vd["targets"]:
        if r["target"] == target:
            return r
    return None


def test_vital_docs_baseline_ids_and_all_targets(tmp_path):
    paths, summary = _vital_fixture(tmp_path)
    vd = ad.vital_docs_data(paths, summary, "examiner")
    # all_targets is the canonical target set (+ labels) for the reassign picker.
    # 27 = 13 base + 13 Approach-A estate + power_of_attorney.
    assert "all_targets" in vd
    assert len(vd["all_targets"]) == 27
    assert {"target": "power_of_attorney", "label": "Power of attorney"} \
        in vd["all_targets"], \
        "POA must be reassignable in the examiner picker, not just retrievable"
    assert all("target" in a and "label" in a for a in vd["all_targets"])
    # Baseline: three found targets (will / deed / vehicle), life_insurance not found.
    assert vd["found_count"] == 3
    assert _row(vd, "life_insurance")["found"] is False
    # Each item carries the stable composite id (original target + path).
    will = _row(vd, "will_testament")
    assert will["items"][0]["id"] == "will_testament::/d/will.pdf"
    # The dup path is TWO distinct items under two targets.
    assert _row(vd, "deed_title")["items"][0]["id"] == "deed_title::/d/shared.pdf"
    assert _row(vd, "vehicle_title")["items"][0]["id"] == "vehicle_title::/d/shared.pdf"


def test_vital_dismiss_overlay_flips_target_to_not_found(tmp_path):
    paths, summary = _vital_fixture(tmp_path)
    iid = "will_testament::/d/will.pdf"
    vd = ad.vital_docs_data(paths, summary, "examiner",
                            decisions={"vital_doc_dismissed": {iid: {"actor": "examiner"}}})
    will = _row(vd, "will_testament")
    assert will["found"] is False and will["items"] == []
    assert vd["found_count"] == 2          # deed + vehicle still found
    # total unchanged (the target row still appears, just not-found).
    assert vd["total_count"] == 4


def test_vital_dismiss_by_path_removes_document_from_all_categories(tmp_path):
    paths, summary = _vital_fixture(tmp_path)
    # "Not a vital document" is keyed by PATH — dismissing /d/shared.pdf drops it
    # from EVERY category it matched (both deed_title and vehicle_title).
    vd = ad.vital_docs_data(paths, summary, "examiner",
                            decisions={"vital_doc_dismissed": {"/d/shared.pdf": {"actor": "examiner"}}})
    assert _row(vd, "deed_title")["found"] is False
    assert _row(vd, "vehicle_title")["found"] is False
    assert _row(vd, "will_testament")["found"] is True   # a different document, untouched
    assert vd["found_count"] == 1


def test_vital_dismiss_legacy_composite_key_still_drops_single_item(tmp_path):
    paths, summary = _vital_fixture(tmp_path)
    # Backward compat: a legacy per-item composite key (target::path) still drops
    # ONLY that one item — the vehicle_title copy of the same path survives.
    vd = ad.vital_docs_data(paths, summary, "examiner",
                            decisions={"vital_doc_dismissed": {"deed_title::/d/shared.pdf": {}}})
    assert _row(vd, "deed_title")["found"] is False
    assert _row(vd, "vehicle_title")["found"] is True


def test_vital_reassign_overlay_moves_group_and_keeps_id(tmp_path):
    paths, summary = _vital_fixture(tmp_path)
    iid = "will_testament::/d/will.pdf"
    vd = ad.vital_docs_data(paths, summary, "examiner",
                            decisions={"vital_doc_target": {iid: "life_insurance"}})
    # Old target reverts to not-found (its only item moved away).
    assert _row(vd, "will_testament")["found"] is False
    li = _row(vd, "life_insurance")
    assert li["found"] is True
    # The DISPLAY target changed but the composite id is stable.
    assert li["items"][0]["id"] == iid


def test_vital_no_overlay_keys_unchanged(tmp_path):
    paths, summary = _vital_fixture(tmp_path)
    base = ad.vital_docs_data(paths, summary, "examiner")
    with_empty = ad.vital_docs_data(paths, summary, "examiner", decisions={})
    assert base == with_empty


# ── vital-doc conversation deep links (backlog P2 #12) ───────────────────────
#
# A vital document is often an EMAIL, and emails are deliberately absent from the
# documents view — so those rows used to render as a dead, unclickable .eml
# basename. They now deep-link into the Emails section instead. The audience gate
# is the load-bearing part: the family's conversation index excludes estate-rescued
# bulk mail, so a vital item living only in rescued mail must resolve to NOTHING
# for the family (stub row) while still resolving for the examiner.

def _vital_email_case(tmp_path):
    """A case whose vital documents are emails: one in a conversation BOTH
    audiences may see, one only in the examiner's (estate-rescued) index."""
    paths = CasePaths.from_case_id("VE", str(tmp_path))
    md = paths.metadata_dir; md.mkdir(parents=True)
    (md / "vital_doc_confirmed.json").write_text(json.dumps([
        {"path": "/m/shared.eml", "target": "will_testament"},
        {"path": "/m/rescued.eml", "target": "deed_title"},
        {"path": "/d/tax.pdf", "target": "tax_return"},
    ]))
    (md / "vital_doc_candidates.json").write_text(json.dumps({
        "will_testament": {"description": "will", "hits": []},
        "deed_title": {"description": "deed", "hits": []},
        "tax_return": {"description": "tax", "hits": []},
    }))
    shared = {"thread_id": "T-shared", "subject": "Re: Dad's will — final",
              "message_count": 2, "files": ["/m/shared.eml"]}
    rescued = {"thread_id": "T-rescued", "subject": "Deed of trust",
               "message_count": 1, "files": ["/m/rescued.eml"]}
    (md / "email_threads_index_family.json").write_text(
        json.dumps({"case_id": "VE", "threads": [shared]}))
    (md / "email_threads_index_examiner.json").write_text(
        json.dumps({"case_id": "VE", "threads": [shared, rescued]}))
    summary = {"document_classifications": [
        {"file": "/m/shared.eml", "filename": "shared.eml", "source": "email"},
        {"file": "/m/rescued.eml", "filename": "rescued.eml", "source": "email"},
        {"file": "/d/tax.pdf", "filename": "tax.pdf", "source": "document"},
    ]}
    return paths, summary


def test_vital_email_item_deep_links_into_its_conversation(tmp_path):
    paths, summary = _vital_email_case(tmp_path)
    fam = {t["target"]: t for t in ad.vital_docs_data(paths, summary, "family")["targets"]}
    it = fam["will_testament"]["items"][0]
    assert it["file_id"] is None, "an email is still not a browsable document"
    assert it["thread_id"] == "T-shared", "…but it is reachable via its conversation"
    # the reply prefix is stripped so the row reads as the conversation, not as
    # `message_43267.eml` (2 of goog's 26 real thread links have no usable subject)
    assert it["thread_subject"] == "Dad's will — final"


def test_vital_email_link_is_scoped_to_the_callers_audience(tmp_path):
    """The whole point: rescued mail resolves for the examiner and NOT the family.

    A family link here would either 404 or hand over a message the audience split
    exists to withhold, so the row must stay a stub.
    """
    paths, summary = _vital_email_case(tmp_path)
    fam = {t["target"]: t for t in ad.vital_docs_data(paths, summary, "family")["targets"]}
    exam = {t["target"]: t for t in ad.vital_docs_data(paths, summary, "examiner")["targets"]}
    assert fam["deed_title"]["items"][0]["thread_id"] is None
    assert fam["deed_title"]["items"][0]["thread_subject"] is None
    assert fam["deed_title"]["found"] is True, "still listed — only the link is withheld"
    assert exam["deed_title"]["items"][0]["thread_id"] == "T-rescued"
    # the shared conversation resolves for both
    assert fam["will_testament"]["items"][0]["thread_id"] == "T-shared"
    assert exam["will_testament"]["items"][0]["thread_id"] == "T-shared"


def test_vital_browsable_document_prefers_its_document_link(tmp_path):
    paths, summary = _vital_email_case(tmp_path)
    fam = {t["target"]: t for t in ad.vital_docs_data(paths, summary, "family")["targets"]}
    it = fam["tax_return"]["items"][0]
    assert it["file_id"] == "/d/tax.pdf" and it["thread_id"] is None


def test_vital_thread_index_may_be_passed_in(tmp_path):
    """The server hands over the index it already loaded (an examiner one carries
    ~64k file entries); the result must match the lazy-loading path."""
    paths, summary = _vital_email_case(tmp_path)
    lazy = ad.vital_docs_data(paths, summary, "family")
    idx = json.loads((paths.metadata_dir / "email_threads_index_family.json").read_text())
    assert ad.vital_docs_data(paths, summary, "family", threads_index=idx) == lazy


def test_vital_docs_without_any_thread_index_is_unchanged(tmp_path):
    """Cases predating email_threads (or with no mail) keep their stub rows."""
    paths, summary = _vital_case(tmp_path)     # writes no conversation index
    fam = {t["target"]: t for t in ad.vital_docs_data(paths, summary, "family")["targets"]}
    assert fam["deed_title"]["items"][0]["thread_id"] is None
    assert fam["deed_title"]["found"] is True


def test_thread_label_strips_reply_noise_else_none():
    assert ad._thread_label("Re: Fwd:  Estate  ") == "Estate"
    assert ad._thread_label("Re:") is None
    assert ad._thread_label("") is None and ad._thread_label(None) is None
    assert ad._thread_label("Regards from Ann") == "Regards from Ann"  # not a prefix


def test_vital_email_link_never_falls_back_to_the_union_index_for_family(tmp_path):
    """A case built before the audience split has only the legacy UNION index,
    which contains the estate-rescued mail. The examiner may fall back to it; the
    family may not, so its vital rows stay stubs rather than linking to withheld
    mail. (This is the leak path — `load_thread_index` fails closed for family.)"""
    paths, summary = _vital_email_case(tmp_path)
    md = paths.metadata_dir
    (md / "email_threads_index_family.json").unlink()
    (md / "email_threads_index_examiner.json").rename(md / "email_threads_index.json")
    fam = {t["target"]: t for t in ad.vital_docs_data(paths, summary, "family")["targets"]}
    exam = {t["target"]: t for t in ad.vital_docs_data(paths, summary, "examiner")["targets"]}
    assert [i["thread_id"] for t in fam.values() for i in t["items"]] == [None, None, None]
    assert exam["deed_title"]["items"][0]["thread_id"] == "T-rescued"


# ── inline office-document views ─────────────────────────────────────────────

def test_build_doctext_views_matches_a_hardlinked_delivered_copy(tmp_path):
    """build_archive LINKS delivered documents rather than copying them, so the
    working path in ocr_index and the delivered path a client asks for are two
    names for one inode. A string compare (or realpath, which resolves symlinks
    and not hardlinks) would report "no inline view" for every delivered doc."""
    work = tmp_path / "will.docx"
    work.write_bytes(b"docx bytes")
    view = tmp_path / "will.view.json"
    view.write_text(json.dumps({"method": "docx", "blocks": [{"t": "p", "text": "hi"}]}))
    delivered = tmp_path / "archive" / "Legal" / "will.docx"
    delivered.parent.mkdir(parents=True)
    delivered.hardlink_to(work)

    views = ad.build_doctext_views([{"file": str(work), "sidecar_view": str(view)}])
    assert views[ad.file_identity(delivered)] == str(view)
    assert views[ad.file_identity(work)] == str(view)


def test_build_doctext_views_skips_records_with_no_sidecar_on_disk(tmp_path):
    """A sidecar can be deleted out from under the index; existence is checked
    once per load rather than per request, so a stale record must not register."""
    doc = tmp_path / "a.docx"
    doc.write_bytes(b"x")
    views = ad.build_doctext_views([
        {"file": str(doc), "sidecar_view": str(tmp_path / "gone.view.json")},
        {"file": str(doc)},                      # pre-feature record: no view
        {"sidecar_view": str(tmp_path / "x.json")},   # no file
    ])
    assert views == {}


def test_build_doctext_views_tolerates_an_empty_or_missing_index():
    assert ad.build_doctext_views([]) == {}
    assert ad.build_doctext_views(None) == {}


def test_file_identity_falls_back_to_a_string_for_an_unstattable_path(tmp_path):
    missing = tmp_path / "nope" / "gone.docx"
    assert ad.file_identity(missing) == str(missing)


# ── typed attachment placeholders ────────────────────────────────────────────
#
# "Not recovered" is the wrong words for every unresolved attachment. An
# iMessage app payload (link preview, sticker, Apple Pay) never had a media
# file, so reporting it as missing invents a loss; a photo the case does not
# contain IS a real gap and should say so. `kind` lets the client tell them
# apart. Measured on one case: of 297 unresolved, only 35 were app payloads and
# 261 were real media — so conflating them mislabels the large majority.

def test_attachment_kind_classifies_from_config_driven_sets():
    assert ad.attachment_kind("IMG_6827.heic") == "image"
    assert ad.attachment_kind("1fbee8db.png") == "image"
    assert ad.attachment_kind("IMG_4966.tiff") == "image"
    assert ad.attachment_kind("IMG_7798.mov") == "video"
    assert ad.attachment_kind("Deal memo.pdf") == "document"
    assert ad.attachment_kind("notes.docx") == "document"


def test_attachment_kind_recognises_app_payload():
    # Opaque UUID stem, real marker is the extension. Case-insensitive.
    assert ad.attachment_kind(
        "75D2842F-1126-4F45-9961-CF264F866D3E.pluginPayloadAttachment") == "app_payload"
    assert ad.attachment_kind("x.PLUGINPAYLOADATTACHMENT") == "app_payload"


def test_attachment_kind_unknown_is_neutral_not_a_guess():
    # A group chat avatar carries no extension; guessing "image" would let the
    # client claim a photo is missing when we do not know that.
    assert ad.attachment_kind("GroupPhotoImage") == "unknown"
    assert ad.attachment_kind("") == "unknown"
    assert ad.attachment_kind(None) == "unknown"


def test_conversation_detail_carries_kind_for_resolved_and_unresolved():
    conv = {
        "conversation_id": "imessage_x", "platform": "imessage",
        "participants": ["Mom"], "triage_verdict": "keep",
        "messages": [{
            "ts": "2020-01-02 10:00", "sender": "Mom", "direction": "received",
            "text": "", "attachments": [
                "/case/extracted/photos/IMG_001.jpg",   # resolvable
                "gone.jpg",                              # real media, absent
                "75D2842F.pluginPayloadAttachment",      # never had a file
            ]}],
        "call_events": [],
    }
    resolved = {"/case/extracted/photos/IMG_001.jpg":
                "/case/extracted/photos/IMG_001.jpg"}
    atts = ad.conversation_detail(
        conv, attachment_resolver=resolved.get)["messages"][0]["attachments"]
    by_name = {a["name"]: a for a in atts}
    assert by_name["IMG_001.jpg"]["kind"] == "image"
    assert by_name["IMG_001.jpg"]["src"]                    # available
    assert by_name["gone.jpg"]["kind"] == "image"
    assert by_name["gone.jpg"]["src"] is None               # a genuine gap
    payload = by_name["75D2842F.pluginPayloadAttachment"]
    assert payload["kind"] == "app_payload" and payload["src"] is None


def test_email_payloads_carry_the_resolved_sender_name():
    # email_triage resolves the sender against the case's address books and
    # stamps from_display; the raw header rides along so the examiner keeps
    # the address.
    ebf = {
        "/a.eml": {"email_from": "Jennifer <jen@family.com>",
                   "from_display": "Jennifer Williams",
                   "email_to": "owner@me.com", "email_subject": "hi",
                   "email_date_iso": "2024-01-01T00:00:00+00:00",
                   "ocr_text": "body"},
        "/b.eml": {"email_from": "bare@family.com",
                   "email_to": "owner@me.com", "email_subject": "yo",
                   "email_date_iso": "2024-01-02T00:00:00+00:00",
                   "ocr_text": "body"},
    }
    msgs = {m["file"]: m for m in ad.email_thread_messages(ebf, ["/a.eml", "/b.eml"])}
    assert msgs["/a.eml"]["from_display"] == "Jennifer Williams"
    assert msgs["/a.eml"]["from"] == "Jennifer <jen@family.com>"
    # No resolution (or an older case): falls back to the raw header.
    assert msgs["/b.eml"]["from_display"] == "bare@family.com"


def test_conversation_detail_carries_the_list_title_and_resolved_senders():
    # The detail view built its own heading by joining participants, so one
    # thread read as "A, B, C + 1" in the list and "A (+1...), B (+1...)" one
    # click later. And the stored `sender` is deliberately the RAW handle
    # (chunk keys hash the rendered text), so the name is resolved at render.
    conv = {
        "conversation_id": "imessage:abc",
        "platform": "imessage",
        "display_name": "Ada Lovelace, Bo Kim + 1",
        "display_name_source": "participants",
        "participants": ["Ada Lovelace (+15035550142)", "Bo Kim (+15035550143)"],
        "participant_contacts": [
            {"handle": "+15035550142", "display_name": "Ada Lovelace",
             "contact_tier": "A", "contact_sources": ["abcddb:a"]},
            {"handle": "+15035550143", "display_name": "Bo Kim",
             "contact_tier": "B", "contact_sources": ["abcddb:a", "vcf"]},
            {"handle": "+15039990000", "display_name": "+15039990000",
             "contact_tier": None, "contact_sources": []},
        ],
        "messages": [
            {"ts": "2024-01-01 00:00", "sender": "+15035550142", "text": "hi"},
            {"ts": "2024-01-01 00:01", "sender": "+15039990000", "text": "yo"},
            {"ts": "2024-01-01 00:02", "sender": "owner", "text": "hello"},
        ],
    }
    out = ad.conversation_detail(conv)
    assert out["display_name"] == "Ada Lovelace, Bo Kim + 1"
    assert out["display_name_source"] == "participants"
    senders = [(m["sender"], m["sender_display"]) for m in out["messages"]]
    assert senders == [("+15035550142", "Ada Lovelace"),
                       ("+15039990000", "+15039990000"),   # unresolved: unchanged
                       ("owner", "owner")]


def test_conversation_detail_takes_its_title_from_the_index_record():
    # The transcript file (messages/<id>.json) has no title and no resolved
    # participant names — those live on the conversation_index record, which
    # the server already holds. Without the merge the detail heading silently
    # fell back to joining raw participants.
    per_file = {"conversation_id": "imessage:abc", "platform": "imessage",
                "participants": ["+15035550142"],
                "messages": [{"ts": "t", "sender": "+15035550142", "text": "hi"}]}
    index_record = {"conversation_id": "imessage:abc",
                    "display_name": "Ada Lovelace, Bo Kim + 1",
                    "display_name_source": "participants",
                    "participant_contacts": [
                        {"handle": "+15035550142", "display_name": "Ada Lovelace",
                         "contact_tier": "A", "contact_sources": ["abcddb:a"]}]}
    out = ad.conversation_detail(per_file, index_record=index_record)
    assert out["display_name"] == "Ada Lovelace, Bo Kim + 1"
    assert out["messages"][0]["sender_display"] == "Ada Lovelace"


# ── vital-doc rows carry the summary the decision needs ──────────────────────
# The examiner confirms "yes, this is the deed" from a row that showed only a
# filename and where the pipeline filed it. On 813_mf that produced ten sign-offs
# under "Property deed / title" whose own summaries call them a draft will, a
# durable power of attorney and four emails about deed research. The sentence
# that answers the question is already in document_classifications; it just was
# never carried onto the row. These tests pin it there, and pin the audience gate
# that decides who may read it.

def _vital_case_with_summaries(tmp_path):
    """_vital_case, plus the plain-language summary each classification carries
    in a real case, and an email-sourced deed that resolves to a thread."""
    paths, summary = _vital_case(tmp_path)
    texts = {
        "/d/will.pdf": "A draft of a will outlining the distribution of the deceased's assets.",
        "/m/deed.eml": "A title company emails a seller about the sale of their property.",
        "/d/creds.pdf": "A list of online account logins and passwords.",
    }
    for d in summary["document_classifications"]:
        d["summary"] = texts[d["file"]]
    return paths, summary


def test_vital_item_carries_the_document_summary(tmp_path):
    paths, summary = _vital_case_with_summaries(tmp_path)
    exam = {t["target"]: t for t in
            ad.vital_docs_data(paths, summary, "examiner")["targets"]}
    it = exam["will_testament"]["items"][0]
    assert it["summary"] == \
        "A draft of a will outlining the distribution of the owner's assets.", \
        "the row that asks 'is this the will?' must carry the summary that answers it"


def test_vital_item_summary_is_neutralized(tmp_path):
    """Same identity-neutralisation document_rows applies — 'the deceased' never
    reaches a family screen unrewritten."""
    paths, summary = _vital_case_with_summaries(tmp_path)
    fam = {t["target"]: t for t in
           ad.vital_docs_data(paths, summary, "family")["targets"]}
    assert "deceased" not in fam["will_testament"]["items"][0]["summary"]
    assert "the owner's assets" in fam["will_testament"]["items"][0]["summary"]


def test_vital_item_summary_respects_the_credentials_gate(tmp_path):
    """A family session may not open the raw credentials document, so it may not
    read its summary either — audience.py's asymmetry: a family-side leak fails
    open, so gate rather than reword."""
    paths, summary = _vital_case_with_summaries(tmp_path)
    fam = {t["target"]: t for t in
           ad.vital_docs_data(paths, summary, "family")["targets"]}
    exam = {t["target"]: t for t in
            ad.vital_docs_data(paths, summary, "examiner")["targets"]}
    assert fam["credentials"]["items"][0]["summary"] is None
    assert exam["credentials"]["items"][0]["summary"] == \
        "A list of online account logins and passwords."


def test_vital_item_summary_withheld_when_the_email_is_unreachable(tmp_path):
    """An email-sourced item is summarised only when this role's own conversation
    index actually resolved the thread. No thread for this role means the mail was
    never theirs to read, and its summary is a paraphrase of the same content."""
    paths, summary = _vital_case_with_summaries(tmp_path)
    fam = {t["target"]: t for t in
           ad.vital_docs_data(paths, summary, "family")["targets"]}
    item = fam["deed_title"]["items"][0]
    assert item["thread_id"] is None          # nothing resolved it
    assert item["summary"] is None


# ── audio kinds: the Recordings page's six groups ────────────────────────────

def test_audio_kind_maps_every_classifier_category():
    """Every category the classifier emits must land somewhere deliberate."""
    assert ad.audio_kind("voicemail") == "voicemail"
    assert ad.audio_kind("voice_memo") == "voice_note"
    assert ad.audio_kind("personal_recording") == "conversation"
    assert ad.audio_kind("interview_or_meeting") == "conversation"
    assert ad.audio_kind("music_or_performance") == "music"
    assert ad.audio_kind("non_speech") == "untranscribed"
    assert ad.audio_kind("miscellaneous") == "other"


def test_audio_kind_keeps_voicemail_apart_from_voice_notes():
    """Both are one person talking, so merging them is tempting. A voicemail is
    somebody ELSE's voice — often the voice of the person who died — and there are
    a couple of dozen against several hundred notes. Merged, they disappear."""
    assert ad.audio_kind("voicemail") != ad.audio_kind("voice_memo")


def test_audio_kind_does_not_file_untranscribed_audio_as_other():
    """`non_speech` is a processing outcome, not a kind: on 813_mf it is exactly
    the set with no transcript, and the files are numbered album tracks. Filing it
    under "other" would bury a few hundred songs."""
    assert ad.audio_kind("non_speech") == "untranscribed"
    assert ad.audio_kind_label("untranscribed") == "Nothing was transcribed"


def test_audio_kind_never_drops_an_unknown_category():
    """A classifier label we have never seen must still reach the page."""
    for unknown in ("a_brand_new_label", "", None, "   ", "VOICEMAIL_TYPO"):
        assert ad.audio_kind(unknown) == "other"
    # and the known ones are case/whitespace tolerant
    assert ad.audio_kind("  Voicemail  ") == "voicemail"


def test_audio_rows_carry_kind_and_label(tmp_path):
    summary = {"audio_classifications": [
        {"file": "/a/vm.m4a", "filename": "vm.m4a", "category": "voicemail"},
        {"file": "/a/song.aif", "filename": "song.aif", "category": "non_speech"},
        {"file": "/a/new.wav", "filename": "new.wav", "category": "some_new_label"},
    ]}
    rows = ad.audio_rows(summary, [], "family", {})
    by = {r["file"]: r for r in rows}
    assert (by["/a/vm.m4a"]["kind"], by["/a/vm.m4a"]["kind_label"]) \
        == ("voicemail", "Voicemail")
    assert by["/a/song.aif"]["kind_label"] == "Nothing was transcribed"
    assert by["/a/new.wav"]["kind"] == "other"
    # the raw classifier answer is kept beside it — the classifier is not reliable
    # enough to throw away what it actually said
    assert by["/a/song.aif"]["category"] == "non_speech"


# ── the estate report: "not there" vs "not finished looking" ─────────────────

def _vd(targets, per_target_k=25):
    return {"available": True, "per_target_k": per_target_k, "targets": targets}


def _t(label, items=(), near=0, capped=False):
    return {"target": label.lower().replace(" ", "_"), "label": label,
            "items": [{"reviewed": r} for r in items], "near_miss_count": near,
            "near_miss_capped": capped}


def test_estate_report_three_states():
    rep = ad.estate_report_data(_vd([
        _t("Will", items=[True, False]),        # signed off → present
        _t("Deed", items=[False, False]),       # candidates, none ruled on
        _t("Trust", near=4),                    # only weak matches
        _t("Passport"),                         # nothing at all
    ]))
    by = {r["label"]: r for g in rep["groups"] for r in g["types"]}
    assert by["Will"]["state"] == "present"
    assert by["Deed"]["state"] == "unconfirmed"
    assert by["Trust"]["state"] == "unconfirmed", \
        "weak matches nobody reviewed cannot be reported as absent"
    assert by["Passport"]["state"] == "absent"


def test_estate_report_present_still_reports_what_is_outstanding():
    """A type with the document AND unreviewed candidates is present — the estate
    has it — but the reader is still owed the outstanding count."""
    rep = ad.estate_report_data(_vd([_t("Will", items=[True, False, False], near=7)]))
    row = rep["groups"][0]["types"][0]
    assert (row["state"], row["signed_off"], row["undecided"], row["near_misses"]) \
        == ("present", 1, 2, 7)


def test_estate_report_totals():
    rep = ad.estate_report_data(_vd([
        _t("Will", items=[True, False], near=3),
        _t("Deed", items=[False], near=2),
        _t("Passport"),
    ]))
    assert rep["totals"] == {
        "types": 3, "present": 1, "unconfirmed": 1, "absent": 1,
        "candidates": 3, "signed_off": 1, "undecided": 2, "near_misses": 5}


def test_estate_report_limitations_are_built_from_the_data():
    """A caveat that does not move when the numbers move is worse than none."""
    rep = ad.estate_report_data(_vd([_t("Will", items=[False], near=9, capped=True)]))
    text = " ".join(rep["limitations"])
    assert "25 candidates per document type" in text and "1 of the 1 types" in text
    assert "1 candidate documents have been found but not yet reviewed" in text
    assert "9 weaker matches" in text
    # nothing qualified as absent here, and the report must say so out loud
    assert "No document type can currently be reported as absent" in text
    # and the standing caveat is always present
    assert "not a search of public records" in text


def test_estate_report_drops_the_absent_caveat_when_something_is_absent():
    rep = ad.estate_report_data(_vd([_t("Passport"), _t("Will", items=[True])]))
    text = " ".join(rep["limitations"])
    assert "No document type can currently be reported as absent" not in text
    assert rep["totals"]["absent"] == 1


def test_estate_report_unavailable_when_the_scan_never_ran():
    assert ad.estate_report_data({"available": False})["available"] is False
    assert ad.estate_report_data(None)["available"] is False


# ── the family report: an orientation document ──────────────────────────────

def _family_kwargs(**over):
    base = dict(
        counts={"photos": 100, "videos": 5, "audio": 10, "documents": 40,
                "emails": 200, "messages": 3, "places": 7},
        scene_counts={"beach": 30, "wedding": 4},
        audio_rows_=[{"kind": "voicemail"}, {"kind": "voicemail"}, {"kind": "music"}],
        document_index=[{"category": "financial", "count": 25},
                        {"category": "work_correspondence", "count": 15}],
        email_categories=[{"name": "personal_correspondence", "count": 120}],
        timeline={"chapters": [{"date_from": "2011-04-02", "date_to": "2012-01-09"},
                               {"date_from": "2009-06-01", "date_to": "2010-02-02"}],
                  "undated": {"count": 25}},
        people=[{"name": "A", "named": True, "photo_count": 9},
                {"name": "Person_02", "named": False, "photo_count": 3}],
    )
    base.update(over)
    return base


def test_family_report_span_is_the_outer_edge_of_every_chapter():
    rep = ad.family_report_data(**_family_kwargs())
    assert (rep["span"]["from"], rep["span"]["to"]) == ("2009-06-01", "2012-01-09")


def test_family_report_span_survives_a_chapter_with_no_dates():
    """An empty date must not win the comparison and drag the range to ''."""
    rep = ad.family_report_data(**_family_kwargs(
        timeline={"chapters": [{"date_from": "", "date_to": None},
                               {"date_from": "2015-01-01", "date_to": "2015-12-31"}],
                  "undated": {"count": 0}}))
    assert (rep["span"]["from"], rep["span"]["to"]) == ("2015-01-01", "2015-12-31")
    # nothing dated at all → no span rather than a fabricated one
    empty = ad.family_report_data(**_family_kwargs(timeline={"chapters": []}))
    assert (empty["span"]["from"], empty["span"]["to"]) == (None, None)


def test_family_report_counts_people_named_and_not():
    rep = ad.family_report_data(**_family_kwargs())
    assert (rep["people"]["total"], rep["people"]["named"], rep["people"]["unnamed"]) \
        == (2, 1, 1)
    # only named people are offered as chips — "Person_02" is not a name
    assert [p["name"] for p in rep["people"]["top"]] == ["A"]


def test_family_report_recording_kinds_come_from_the_rows():
    rep = ad.family_report_data(**_family_kwargs())
    recs = [s for s in rep["sections"] if s["key"] == "recordings"][0]
    assert [(i["label"], i["count"]) for i in recs["items"]] == \
        [("Voicemail", 2), ("Music & performance", 1)]


def test_family_report_documents_come_from_the_index_not_classifications():
    """The document classifications count every email as a document too — the
    difference between about 4,600 and about 59,000 on a real case, and the larger
    number has reached a screen before. The index is the only honest source."""
    rep = ad.family_report_data(**_family_kwargs())
    docs = [s for s in rep["sections"] if s["key"] == "documents"][0]
    assert sum(i["count"] for i in docs["items"]) == 40
    assert [i["label"] for i in docs["items"]] == ["Financial", "Work correspondence"]


def test_family_report_says_how_much_is_undated_and_unnamed():
    rep = ad.family_report_data(**_family_kwargs())
    text = " ".join(rep["limitations"])
    assert "25 items carry no date — about 25%" in text
    assert "1 of the 2 people recognised have not been given a name" in text
    assert "only the material that was supplied" in text


def test_family_report_drops_the_undated_caveat_when_everything_is_dated():
    rep = ad.family_report_data(**_family_kwargs(
        timeline={"chapters": [{"date_from": "2015-01-01", "date_to": "2015-12-31"}],
                  "undated": {"count": 0}}))
    assert not any("carry no date" in x for x in rep["limitations"])


def test_overview_card_counts_near_misses_under_the_unfound_types(tmp_path):
    """The Overview used to headline these types as "Still missing". They are the
    types with no CONFIRMED document — and while weaker matches under them are
    unread, "missing" states an absence the archive cannot support, and
    contradicts the estate report, which files the same types under "not yet
    established". The card needs this number to say so."""
    paths, summary = _vital_case(tmp_path)
    vd = ad.vital_docs_data(paths, summary, "examiner", per_target_k=25)
    card = ad._vital_docs_overview(vd)
    unfound = [t for t in card["types"] if not t["found"]]
    assert unfound, "the fixture must have at least one unfound type"
    assert card["unfound_near_misses"] == sum(t["near_misses"] or 0 for t in unfound)
    # a FOUND type's near-misses are not counted — the claim is only about the
    # types the card is about to list as not yet found
    assert card["unfound_near_misses"] != sum(
        (t["near_misses"] or 0) for t in card["types"]) or \
        all(t["found"] is False for t in card["types"])


def test_overview_card_withholds_the_near_miss_total_shape_from_family(tmp_path):
    paths, summary = _vital_case(tmp_path)
    fam = ad._vital_docs_overview(ad.vital_docs_data(paths, summary, "family"))
    assert all(t["near_misses"] is None for t in fam["types"])
    # nothing to total, so the card has no number to print and stays quiet
    assert fam["unfound_near_misses"] == 0


# ── online accounts: services found in the mail ─────────────────────────────

def _thr(subject, *addrs):
    return {"subject": subject, "participants": list(addrs)}


def test_account_root_folds_a_service_split_across_subdomains():
    """One account arriving as several rows was the visible half of the problem:
    linkedin.com / e.linkedin.com / em.linkedin.com are one service."""
    assert ad.account_root("e.linkedin.com") == "linkedin.com"
    assert ad.account_root("rs.email.nextdoor.com") == "nextdoor.com"
    assert ad.account_root("schwab.com") == "schwab.com"
    assert ad.account_root("") == "" and ad.account_root(None) == ""


def test_account_services_finds_a_bank_the_inventory_missed():
    threads = [_thr("Your sign-in from a new device", "alerts@bank.test", "me@own.test"),
               _thr("Reset your password", "alerts@bank.test", "me@own.test"),
               _thr("Account statement ready", "alerts@bank.test", "me@own.test")]
    out = ad.account_services(threads, inventory={}, owner_addresses=["me@own.test"])
    assert [x["service"] for x in out] == ["bank.test"]
    assert out[0]["threads"] == 3 and out[0]["signals"] == 3
    assert out[0]["from_pipeline"] is False


def test_account_services_ignores_a_correspondent_who_once_said_password():
    """The deceased's own firm topped this list on 813_mf: thousands of ordinary
    threads containing, somewhere, a handful that said "password". A person
    emailing you for years eventually uses every word a service uses; a service's
    mail is transactional nearly all the way through."""
    threads = [_thr("Reset your password", "jim@firm.test", "me@own.test")] + \
              [_thr("lunch on tuesday?", "jim@firm.test", "me@own.test")
               for _ in range(300)]
    out = ad.account_services(threads, inventory={}, owner_addresses=["me@own.test"])
    assert [x["service"] for x in out] == []


def test_account_services_excludes_the_owner_and_consumer_mail():
    threads = [_thr("Verify your email", "me@own.test", "friend@gmail.com")
               for _ in range(5)]
    out = ad.account_services(threads, inventory={},
                              owner_addresses=["me@own.test"])
    assert [x["service"] for x in out] == [], \
        "the owner's own domain and free mail providers are people, not services"


def test_account_services_counts_what_the_link_will_deliver():
    """The pipeline's inventory counts RAW notification mail; the Emails page
    holds post-triage threads. A row that promises 700 and opens onto 3 is a
    broken link, so the count is the reachable one and the gap is explained."""
    threads = [_thr("hello", "no-reply@social.test", "me@own.test")]
    out = ad.account_services(threads, inventory={"social.test": {"count": 700}},
                              owner_addresses=["me@own.test"])
    row = [x for x in out if x["service"] == "social.test"][0]
    assert row["threads"] == 1 and row["filtered_out"] == 699
    assert row["from_pipeline"] is True


def test_account_services_keeps_a_pipeline_service_with_nothing_readable():
    """Its notifications were all triaged away. It still belongs on an estate's
    account list — it just cannot be opened, and the page says so."""
    out = ad.account_services([], inventory={"gone.test": {"count": 160}},
                              owner_addresses=[])
    assert out == [{"service": "gone.test", "threads": 0, "signals": 0,
                    "from_pipeline": True, "filtered_out": 160}]


def test_account_services_ranks_a_bank_above_a_bulk_newsletter():
    threads = [_thr("Security alert", "a@bank.test", "me@own.test") for _ in range(4)] + \
              [_thr("This week's picks", "n@news.test", "me@own.test") for _ in range(400)]
    out = ad.account_services(threads, inventory={"news.test": {"count": 400}},
                              owner_addresses=["me@own.test"])
    assert [x["service"] for x in out] == ["bank.test", "news.test"]


# ── pipeline report: what went in against what came out ─────────────────────

def _pipe_summaries():
    return {
        "collect_dedup_summary.json": {
            "timestamp": "2026-01-01T10:00:00",
            "type_counts": {"image": 100, "video": 10, "document": 50, "audio": 8},
            "exact_dupes_moved": 20, "perceptual_dupes_moved": 30,
            "docs_exact_dupes_moved": 5, "video_exact_dupes_moved": 1},
        "email_triage_summary.json": {
            "timestamp": "2026-01-03T15:30:00", "email_count": 1000,
            "kept_count": 600,
            "triage": {"discarded_bulk": 380, "discarded_platform": 20,
                       "rescued_by_estate_keywords": 40}},
        "message_triage_summary.json": {
            "timestamp": "2026-01-02T09:00:00", "conversation_files_written": 30},
        "transcription_summary.json": {
            "timestamp": "2026-01-01T12:00:00", "total_audio": 8, "failed": 1,
            "total_duration_seconds": 7200},
        "ocr_summary.json": {"timestamp": "2026-01-01T11:00:00",
                             "total_documents": 55, "ocr_results_count": 40},
        "expandfiles_summary.json": {"timestamp": "2026-01-01T09:00:00",
                                     "archives_found": 12, "files_added": 300,
                                     "email_attachments": 44},
    }


_PIPE_COUNTS = {"photos": 40, "videos": 9, "documents": 25, "audio": 7,
                "emails": 250, "messages": 28}


def test_pipeline_report_pairs_examined_with_surfaced():
    rep = ad.pipeline_report_data(summaries=_pipe_summaries(), counts=_PIPE_COUNTS)
    by = {r["kind"]: r for r in rep["rows"]}
    assert (by["Photographs"]["examined"], by["Photographs"]["surfaced"]) == (100, 40)
    assert by["Photographs"]["share"] == 40.0
    assert by["Videos"]["share"] == 90.0
    assert by["Recordings"]["examined"] == 8 and by["Recordings"]["surfaced"] == 7


def test_pipeline_report_refuses_a_share_when_the_units_change():
    """1,000 messages become 250 CONVERSATIONS. A percentage there is arithmetic
    on two different units, so the row carries the counts and says why."""
    rep = ad.pipeline_report_data(summaries=_pipe_summaries(), counts=_PIPE_COUNTS)
    mail = [r for r in rep["rows"] if r["kind"] == "Emails"][0]
    assert mail["share"] is None
    assert "conversations surfaced" in mail["unit_change"]
    assert "600" in mail["note"] and "400" in mail["note"]  # kept, and bulk+platform


def test_pipeline_report_elapsed_is_wall_clock_across_the_stages():
    rep = ad.pipeline_report_data(summaries=_pipe_summaries(), counts=_PIPE_COUNTS)
    run = rep["run"]
    assert run["first"] == "2026-01-01T09:00:00"
    assert run["last"] == "2026-01-03T15:30:00"
    assert run["elapsed"] == "2 days 6 hours"
    # ordered by when they actually finished, not by pipeline order
    assert [s["at"] for s in run["stages"]] == sorted(s["at"] for s in run["stages"])


def test_pipeline_report_survives_a_case_that_skipped_stages():
    """A missing summary must not become a row of zeroes — that reads as
    "nothing was found" rather than "this did not run"."""
    rep = ad.pipeline_report_data(summaries={}, counts={})
    assert rep["run"]["elapsed"] is None and rep["run"]["stages"] == []
    assert rep["totals"]["examined"] == 0
    assert rep["size"]["total_human"] == "0 B"


def test_pipeline_report_formats_sizes_and_shares():
    rep = ad.pipeline_report_data(
        summaries=_pipe_summaries(), counts=_PIPE_COUNTS,
        sizes={"total": 3 * 1024 ** 3, "files": 12,
               "parts": {"audio": 2 * 1024 ** 3, "photos": 1024 ** 3, "empty": 0}})
    assert rep["size"]["total_human"] == "3.0 GB"
    # biggest first, and a part with nothing in it is not a row
    assert [p["name"] for p in rep["size"]["parts"]] == ["audio", "photos"]
    assert rep["size"]["parts"][0]["human"] == "2.0 GB"
    assert rep["size"]["parts"][0]["share"] == 66.7


def test_pipeline_report_reading_figures():
    rep = ad.pipeline_report_data(summaries=_pipe_summaries(), counts=_PIPE_COUNTS)
    assert rep["reading"] == {"documents_read": 55, "text_recovered": 40,
                              "audio_hours": 2.0}
    assert rep["expansion"]["files_added"] == 300


def test_pipeline_report_estate_reach_uses_messages_not_conversations():
    """These counts are individual emails; a conversation is a group of them, so
    the conversation total is the wrong denominator by a unit."""
    rel = {"available": True, "per_target_k": 25,
           "candidates": {"decisions": 10, "documents": 8,
                          "from_mail_decisions": 4, "from_mail_documents": 3},
           "near_misses": {"decisions": 100, "documents": 60,
                           "from_mail_decisions": 40, "from_mail_documents": 30}}
    rep = ad.pipeline_report_data(summaries=_pipe_summaries(), counts=_PIPE_COUNTS,
                                  relevance=rel)
    est = rep["estate"]
    assert est["mail_denominator"] == 600, "kept messages, not the 250 conversations"
    assert est["mail_denominator_label"] == "messages kept as worth reading"
    assert est["candidate_mail_share"] == 0.5   # 3 of 600
    assert est["near_mail_share"] == 5.0        # 30 of 600


def test_pipeline_report_estate_reach_absent_when_the_scan_never_ran():
    rep = ad.pipeline_report_data(summaries=_pipe_summaries(), counts=_PIPE_COUNTS,
                                  relevance={"available": False})
    assert rep["estate"] is None
    assert ad.pipeline_report_data(summaries={}, counts={})["estate"] is None


def test_estate_relevance_counts_decisions_and_documents_apart(tmp_path):
    """One document can be a candidate for several types. Reporting only the
    pairings overstates the corpus; only the documents understates the work."""
    paths, summary = _vital_case(tmp_path)
    vd = {"available": True, "per_target_k": 25, "targets": [
        {"target": "will_testament", "items": [{"path": "/d/will.pdf"},
                                               {"path": "/m/deed.eml"}]},
        {"target": "deed_title", "items": [{"path": "/d/will.pdf"}]},
    ]}
    rel = ad.estate_relevance_data(vd, paths, summary, {})
    c = rel["candidates"]
    assert (c["decisions"], c["documents"]) == (3, 2)
    # the .eml is not a browsable document, so it counts as mail
    assert (c["from_mail_decisions"], c["from_mail_documents"]) == (1, 1)
