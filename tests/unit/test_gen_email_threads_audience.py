"""gen_email_threads — stable conversation ids, and the audience filter.

Two properties, and they are load-bearing for different reasons.

STABLE IDS (T2). Ids used to be assigned by RANK — `thread_0001`, `thread_0002`,
… — so a conversation's id depended on what else was in the case. The audience
split changes what else is in the case. family_decisions.json's `email_demoted`
map is keyed by thread_id and holds an examiner's real curation decisions, so a
renumber would not dangle those keys — it would silently re-point them at
DIFFERENT CONVERSATIONS, with nothing reported. Ids are now derived from the
identity of a conversation's root message.

THE AUDIENCE FILTER (T9/T13). Filtering is RECORD-level: a rescued message is
dropped from the family's view of a thread even when its neighbours are organic.
The alternative rule ("a thread is family-visible unless EVERY message in it is
rescued") is the one a reader tends to reach for, and it is wrong here — it would
show the family a message body that sensitive_scan, which now screens only the
family-visible set, never looked at. SEEN must stay a subset of SCREENED.
"""

import json

import pytest

from tools import gen_email_threads as G
from wyeast.core.audience import EXAMINER, FAMILY
from wyeast.core.paths import CasePaths


def _email(n, *, rescued=False, subject=None, mid=None, refs=None):
    e = {
        "file": f"/mail/{n}.eml",
        "message_id": mid or f"<{n}@example.com>",
        "email_subject": subject or f"subject {n}",
        "email_from": f"person{n} <person{n}@example.com>",
        "email_to": "owner@example.com",
        "email_date_iso": f"2024-01-{n:02d}T10:00:00+00:00",
        "ocr_text": f"body of message {n}",
    }
    if rescued:
        e["estate_rescued"] = True
    if refs:
        e["references"] = refs
    return e


@pytest.fixture
def case(tmp_path):
    paths = CasePaths.from_case_id("CASE_T", str(tmp_path))
    paths.metadata_dir.mkdir(parents=True, exist_ok=True)
    return paths


def _write_index(paths, entries):
    paths.index("email_index.json").write_text(json.dumps(entries))


def _generate(paths, audience):
    return G.generate("CASE_T", output_dir=paths.output_dir, quiet=True,
                      case_dir=paths.case_dir, audience=audience)


# ── T2: stable, content-derived ids ──────────────────────────────────────────

def test_thread_ids_survive_filtering_the_entry_set(case):
    """THE property that makes it safe to run any of this on an existing case.

    Build the threads twice — once over everything, once with a message removed —
    and every conversation that survives must keep the id it had. Under the old
    positional scheme this was false, and the examiner's demotions would land on
    innocent conversations.
    """
    entries = [_email(1), _email(2, rescued=True), _email(3)]

    _write_index(case, entries)
    full = _generate(case, EXAMINER)
    ids_full = {t["subject"]: t["thread_id"] for t in full["threads"]}

    _write_index(case, [e for e in entries if not e.get("estate_rescued")])
    fewer = _generate(case, EXAMINER)
    ids_fewer = {t["subject"]: t["thread_id"] for t in fewer["threads"]}

    for subject, tid in ids_fewer.items():
        assert ids_full[subject] == tid, (
            f"conversation {subject!r} was renumbered when the entry set shrank — "
            f"family_decisions.json's demotions would now point at the wrong thread")


def test_a_conversation_has_the_same_id_for_both_audiences(case):
    """Ids are assigned over the WHOLE index, before the audience filter runs.

    They have to be: the examiner is the one who records demotions, and the family
    is who they are applied for. If the two audiences numbered conversations
    differently, an examiner's demotion would silently miss in the family's view.
    """
    _write_index(case, [_email(1), _email(2, rescued=True), _email(3)])

    fam = {t["subject"]: t["thread_id"] for t in _generate(case, FAMILY)["threads"]}
    exam = {t["subject"]: t["thread_id"] for t in _generate(case, EXAMINER)["threads"]}

    shared = set(fam) & set(exam)
    assert shared, "the two audiences should share the organic conversations"
    for subject in shared:
        assert fam[subject] == exam[subject]


def test_ids_are_not_positional(case):
    _write_index(case, [_email(1), _email(2)])
    summary = _generate(case, FAMILY)
    for t in summary["threads"]:
        assert not t["thread_id"].startswith("thread_"), \
            "ids must not be rank-assigned any more"
        assert t["thread_id"].startswith("t")


# ── T9/T13: the audience filter ──────────────────────────────────────────────

def test_the_family_never_sees_a_rescued_message(case):
    """The live leak: estate-rescued marketing and platform mail was browsable in
    the family archive, and its full body shipped on their USB stick as rendered
    HTML."""
    _write_index(case, [_email(1), _email(2, rescued=True)])

    summary = _generate(case, FAMILY)

    files = {f for t in summary["threads"] for f in t["files"]}
    assert files == {"/mail/1.eml"}
    assert summary["withheld_estate_rescued"] == 1

    pages = list((case.output_dir / "email_threads").glob("*.html"))
    bodies = "\n".join(p.read_text() for p in pages)
    assert "body of message 2" not in bodies, \
        "a rescued message body was rendered into the family's delivery tree"


def test_the_examiner_sees_everything(case):
    """The examiner is paid to find exactly this mail — the estate-rescue gate
    exists to surface it. Withholding it from them would defeat the gate."""
    _write_index(case, [_email(1), _email(2, rescued=True)])

    summary = _generate(case, EXAMINER)

    files = {f for t in summary["threads"] for f in t["files"]}
    assert files == {"/mail/1.eml", "/mail/2.eml"}
    assert summary["withheld_estate_rescued"] == 0


def test_a_rescued_message_is_dropped_from_a_MIXED_thread(case):
    """T13, decided by the SEEN-is-a-subset-of-SCREENED invariant.

    A thread with four organic messages and one rescued one stays visible to the
    family — but WITHOUT the rescued message. The tempting alternative ("keep the
    whole thread, it is mostly organic") would show a body that sensitive_scan
    never screened, because the scan corpus is now scoped by the same predicate.
    """
    root = _email(1)
    reply_organic = _email(3, subject="Re: subject 1", refs=["<1@example.com>"])
    reply_rescued = _email(2, rescued=True, subject="Re: subject 1",
                           refs=["<1@example.com>"])
    _write_index(case, [root, reply_rescued, reply_organic])

    fam = _generate(case, FAMILY)
    exam = _generate(case, EXAMINER)

    fam_files = {f for t in fam["threads"] for f in t["files"]}
    exam_files = {f for t in exam["threads"] for f in t["files"]}

    assert "/mail/2.eml" in exam_files, "the examiner keeps the rescued reply"
    assert "/mail/2.eml" not in fam_files, "the family must not see the rescued reply"
    assert {"/mail/1.eml", "/mail/3.eml"} <= fam_files, \
        "the organic conversation itself must survive"


def test_a_thread_of_only_rescued_mail_disappears_for_the_family(case):
    _write_index(case, [_email(1), _email(2, rescued=True)])
    fam = _generate(case, FAMILY)
    assert all("/mail/2.eml" not in t["files"] for t in fam["threads"])
    assert len(fam["threads"]) == 1


# ── role separation of the derived artifacts ─────────────────────────────────

def test_the_two_audiences_write_separate_indexes_and_page_dirs(case):
    """They used to write the same two paths, so whichever ran last won — an
    examiner explorer build would leave the union in the file the family's archive
    server reads, and overwrite the family's thread pages with the examiner's."""
    _write_index(case, [_email(1), _email(2, rescued=True)])

    _generate(case, FAMILY)
    _generate(case, EXAMINER)

    fam_index = json.loads(
        case.index("email_threads_index_family.json").read_text())
    exam_index = json.loads(
        case.index("email_threads_index_examiner.json").read_text())

    assert fam_index["audience"] == "family"
    assert exam_index["audience"] == "examiner"
    assert not case.index("email_threads_index.json").exists(), \
        "the unsuffixed (union) index must no longer be written"

    fam_files = {f for t in fam_index["threads"] for f in t["files"]}
    exam_files = {f for t in exam_index["threads"] for f in t["files"]}
    assert "/mail/2.eml" not in fam_files
    assert "/mail/2.eml" in exam_files

    assert (case.output_dir / "email_threads" / "index.html").exists()
    assert (case.output_dir / "email_threads_examiner" / "index.html").exists()


def test_building_the_examiner_pages_does_not_alter_the_family_bundle(case):
    """T17, in miniature: build family, build examiner, RE-READ the family. No
    existing test built both roles and re-checked the first — which is exactly why
    the shared-thread-index overwrite went unnoticed."""
    _write_index(case, [_email(1), _email(2, rescued=True)])

    _generate(case, FAMILY)
    before = sorted(p.name for p in (case.output_dir / "email_threads").glob("*.html"))
    fam_before = case.index("email_threads_index_family.json").read_text()

    _generate(case, EXAMINER)

    after = sorted(p.name for p in (case.output_dir / "email_threads").glob("*.html"))
    fam_after = case.index("email_threads_index_family.json").read_text()

    assert before == after
    assert json.loads(fam_before)["threads"] == json.loads(fam_after)["threads"]

    bodies = "\n".join(p.read_text()
                       for p in (case.output_dir / "email_threads").glob("*.html"))
    assert "body of message 2" not in bodies


def test_stale_pages_from_the_positional_scheme_are_swept(case):
    """Old thread_NNNN.html pages are stale the moment ids become content-derived,
    and in the family's tree that is a DELIVERED file nothing else would remove:
    it would ship with the rescued bodies still in it."""
    threads_dir = case.output_dir / "email_threads"
    threads_dir.mkdir(parents=True, exist_ok=True)
    (threads_dir / "thread_0001.html").write_text("<html>stale rescued body</html>")

    _write_index(case, [_email(1)])
    _generate(case, FAMILY)

    assert not (threads_dir / "thread_0001.html").exists()
