"""tools/build_fts — the family's full-text search index. Previously untested.

build_fts writes output/metadata/_archive_fts_<role>.sqlite: a plaintext
searchable copy of every document, transcript and email body a role is allowed to
see. Its own header comment has warned since it was written that reading email
bodies from the raw index rather than from the gated thread set is "a
confidentiality LEAK". These tests finally hold it to that.

The gate is the CONVERSATION INDEX, and it is now per-role. So the property under
test is simple and blunt: a body the family may not see must not be findable in
the family's database — even though the raw email_index the builder joins against
still contains it, sitting right there.
"""

import json

import pytest

from tools import build_fts
from wyeast.core.paths import CasePaths

ORGANIC_TOKEN = "picnicphrasexyz"     # body of an organic, family-visible email
RESCUED_TOKEN = "statementphrasexyz"  # body of an estate-rescued platform email
ORPHAN_TOKEN = "orphanphrasexyz"      # in email_index, referenced by NO thread

EML_ORGANIC = "/mail/organic.eml"
EML_RESCUED = "/mail/rescued.eml"
EML_ORPHAN = "/mail/orphan.eml"


@pytest.fixture
def paths(tmp_path):
    p = CasePaths.from_case_id("CASE_F", str(tmp_path))
    p.metadata_dir.mkdir(parents=True, exist_ok=True)

    p.index("email_index.json").write_text(json.dumps([
        {"file": EML_ORGANIC, "email_subject": "picnic",
         "ocr_text": f"we should go {ORGANIC_TOKEN} on sunday"},
        {"file": EML_RESCUED, "email_subject": "your statement is ready",
         "estate_rescued": True,
         "ocr_text": f"your {RESCUED_TOKEN} is now available to view"},
        {"file": EML_ORPHAN, "email_subject": "noise",
         "ocr_text": f"{ORPHAN_TOKEN} unsubscribe"},
    ]))

    def thread(tid, subject, files):
        return {"thread_id": tid, "subject": subject, "files": files,
                "participants": ["a@example.com"], "date_first": "2024-01-01",
                "date_last": "2024-01-02", "significance": 3, "categories": [],
                "message_count": len(files)}

    # The family's thread set excludes the rescued mail; the examiner's has both.
    # NEITHER contains the orphan — it is in the raw index only.
    p.index("email_threads_index_family.json").write_text(json.dumps(
        {"threads": [thread("t_org", "picnic", [EML_ORGANIC])]}))
    p.index("email_threads_index_examiner.json").write_text(json.dumps(
        {"threads": [thread("t_org", "picnic", [EML_ORGANIC]),
                     thread("t_res", "your statement is ready", [EML_RESCUED])]}))
    return p


def _db(paths, role):
    return build_fts.build_fts_db(paths, role, {})


def _hits(db, token):
    return build_fts.search(db, token)["total"]


# ── the leak ─────────────────────────────────────────────────────────────────

def test_the_family_index_holds_no_rescued_body(paths):
    """The family could search their archive and get a hit on a marketing email's
    body. The bodies are all still in email_index.json, which this builder opens —
    only the thread set keeps them out, and until now that set was shared with the
    examiner."""
    db = _db(paths, "family")

    assert _hits(db, ORGANIC_TOKEN) >= 1, "the family's own mail must be findable"
    assert _hits(db, RESCUED_TOKEN) == 0, \
        "an estate-rescued body is searchable in the FAMILY's index"


def test_the_examiner_index_holds_the_rescued_body(paths):
    """The mirror image, and just as important: the examiner is paid to find this
    mail. A split that hid it from them would defeat the gate that rescued it."""
    db = _db(paths, "examiner")

    assert _hits(db, ORGANIC_TOKEN) >= 1
    assert _hits(db, RESCUED_TOKEN) >= 1, \
        "the examiner lost the estate-rescued mail from search"


def test_neither_index_holds_a_body_no_thread_references(paths):
    """The invariant the module's :16 comment states: iterate the thread set, never
    the raw index. Noise mail lives in email_index but appears in no thread, and
    must never be indexed for anyone."""
    for role in ("family", "examiner"):
        assert _hits(_db(paths, role), ORPHAN_TOKEN) == 0, \
            f"{role}: a body referenced by no thread reached the index"


def test_the_roles_get_separate_databases(paths):
    fam = build_fts.db_path_for(paths.metadata_dir, "family")
    exam = build_fts.db_path_for(paths.metadata_dir, "examiner")
    assert fam != exam
    _db(paths, "family")
    _db(paths, "examiner")
    assert fam.exists() and exam.exists()


# ── freshness (T7) ───────────────────────────────────────────────────────────

def test_freshness_tracks_the_ROLE_S_conversation_index(paths):
    """SOURCE_INDEXES named the unsuffixed thread index while db_path_for was
    already role-aware. So the examiner's "is my search index stale?" check was
    watching the FAMILY's conversation index: rebuild the family's threads and the
    examiner's database would silently keep serving stale results."""
    fam = build_fts.source_mtimes(paths.metadata_dir, "family")
    exam = build_fts.source_mtimes(paths.metadata_dir, "examiner")

    assert "email_threads_index_family.json" in fam
    assert "email_threads_index_examiner.json" not in fam
    assert "email_threads_index_examiner.json" in exam
    assert "email_threads_index_family.json" not in exam
    assert "email_threads_index.json" not in fam, \
        "the unsuffixed index must not decide any role's freshness"


def test_rebuilding_one_role_s_threads_does_not_stale_the_other(paths):
    fam_db = _db(paths, "family")
    exam_db = _db(paths, "examiner")
    assert build_fts.is_fresh(fam_db, paths)
    assert build_fts.is_fresh(exam_db, paths)

    # Touch ONLY the examiner's conversation index.
    idx = paths.index("email_threads_index_examiner.json")
    data = json.loads(idx.read_text())
    data["threads"] = data["threads"][:1]
    idx.write_text(json.dumps(data))

    assert not build_fts.is_fresh(exam_db, paths), "the examiner's db is now stale"
    assert build_fts.is_fresh(fam_db, paths), \
        "the family's db was invalidated by a change that cannot affect it"


def test_is_fresh_reads_the_role_from_the_database(paths):
    """Callers hold a path, not a role (family_archive does exactly this). The db
    records its own role, so it can still be compared against the right index."""
    fam_db = _db(paths, "family")
    assert build_fts.is_fresh(fam_db, paths, "family")
    assert build_fts.is_fresh(fam_db, paths)  # role inferred from meta


# ── messages: the same gate, the same reason ─────────────────────────────────

MSG_ORGANIC_TOKEN = "kitchentablexyz"
MSG_RESCUED_TOKEN = "brokeragealertxyz"
MSG_PLATFORM_TOKEN = "parceldeliveredxyz"


@pytest.fixture
def msg_paths(paths):
    """Three conversations: organic, estate-rescued, and platform. Chunks exist
    only for the two keep-verdict ones — message_triage does not chunk platform
    traffic, which is exactly why the family must not be shown it."""
    paths.index("conversation_index.json").write_text(json.dumps([
        {"conversation_id": "c_org", "display_name": "Mum", "participants": ["Mum"],
         "message_count": 20, "span": ["2024-01-01", "2024-02-01"],
         "triage_verdict": "keep"},
        {"conversation_id": "c_res", "display_name": "Alerts", "participants": ["Alerts"],
         "message_count": 300, "span": ["2024-01-01", "2024-06-01"],
         "triage_verdict": "keep", "estate_rescued": True},
        {"conversation_id": "c_plat", "display_name": "Courier", "participants": ["Courier"],
         "message_count": 5, "span": ["2024-01-01", "2024-01-05"],
         "triage_verdict": "platform"},
    ]))
    paths.index("message_index.json").write_text(json.dumps([
        {"file": "/m/a#chunk=aaa", "conversation_id": "c_org",
         "ocr_text": f"see you at the {MSG_ORGANIC_TOKEN} tonight"},
        {"file": "/m/b#chunk=bbb", "conversation_id": "c_res", "estate_rescued": True,
         "ocr_text": f"your {MSG_RESCUED_TOKEN} statement is ready"},
        # No chunk for c_plat — message_triage never chunked it.
    ]))
    return paths


def test_the_family_search_index_holds_no_rescued_conversation_body(msg_paths):
    db = _db(msg_paths, "family")
    assert _hits(db, MSG_ORGANIC_TOKEN) >= 1, "the family lost their own messages"
    assert _hits(db, MSG_RESCUED_TOKEN) == 0, \
        "an estate-rescued conversation's body is searchable in the FAMILY's index"


def test_the_examiner_search_index_holds_the_rescued_conversation(msg_paths):
    db = _db(msg_paths, "examiner")
    assert _hits(db, MSG_RESCUED_TOKEN) >= 1, \
        "the examiner lost the rescued conversation — that is their evidence"


def test_a_platform_conversation_reaches_neither_index_as_a_body(msg_paths):
    """It has no chunks to index — which is the same fact that makes it unscreened,
    and therefore the reason the family must not be shown it at all."""
    for role in ("family", "examiner"):
        assert _hits(_db(msg_paths, role), MSG_PLATFORM_TOKEN) == 0
