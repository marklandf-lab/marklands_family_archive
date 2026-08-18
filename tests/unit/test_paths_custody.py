import re

from wyeast.core.custody import ChainOfCustody, sha256_of
from wyeast.core.paths import (
    CasePaths,
    display_person_folder,
    sanitize_person_name,
)


def test_sanitize_person_name():
    assert sanitize_person_name("Jane Harding") == "Jane_Harding"
    assert sanitize_person_name("  O'Brien-Smith, Jr.  ") == "O_Brien-Smith_Jr."
    assert sanitize_person_name("///") == ""
    assert sanitize_person_name("") == ""


def test_display_person_folder():
    # no identities, or un-named cluster -> bare structural id
    assert display_person_folder("Person_03", None) == "Person_03"
    assert display_person_folder("Person_03", {}) == "Person_03"
    assert display_person_folder("Person_03", {"Person_01": {"name": "X"}}) == "Person_03"
    # enrolled name -> additive, sanitized
    ids = {"Person_03": {"name": "Jane Harding"}}
    assert display_person_folder("Person_03", ids) == "Person_03_Jane_Harding"
    # synthetic / passthrough ids are unaffected
    assert display_person_folder("unidentified", ids) == "unidentified"


def test_case_paths_follow_repo_conventions():
    p = CasePaths.from_case_id("CASE_001")
    assert str(p.case_dir) == "/cases/CASE_001"
    assert str(p.logs_dir).endswith("logs_CASE_001")
    assert str(p.metadata_dir).endswith("output/metadata")
    assert str(p.stage_log("06_scene_classify")).endswith(
        "logs_CASE_001/06_scene_classify_CASE_001.log")
    assert str(p.custody_log).endswith("logs_CASE_001/chain_of_custody.log")
    assert p.index("metadata_index.json").name == "metadata_index.json"


def test_case_paths_from_case_dir_and_cases_root(tmp_path):
    p = CasePaths.from_case_dir(tmp_path / "CASE_X")
    assert p.case_id == "CASE_X"
    q = CasePaths.from_case_id("CASE_Y", cases_root=tmp_path)
    assert q.case_dir == tmp_path / "CASE_Y"


# The exact production line format: "<sha256>  <path>  [<iso ts>]"
CUSTODY_LINE = re.compile(
    r"^[0-9a-f]{64}  \S.*  \[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\]$")


def test_custody_line_format_is_stable(tmp_path):
    target = tmp_path / "sensitive_scan_index.json"
    target.write_text("{}")
    custody = ChainOfCustody(tmp_path / "logs" / "chain_of_custody.log")
    digest = custody.record_file(target)

    lines = custody.log_path.read_text().splitlines()
    assert len(lines) == 1
    assert CUSTODY_LINE.match(lines[0]), lines[0]
    assert lines[0].startswith(digest)
    assert digest == sha256_of(target)


def test_custody_is_append_only(tmp_path):
    f = tmp_path / "a.json"
    f.write_text("[]")
    custody = ChainOfCustody(tmp_path / "chain_of_custody.log")
    custody.record_file(f)
    custody.record_file(f)
    assert len(custody.log_path.read_text().splitlines()) == 2


# ── record_event: a human ACTION, not a file move ────────────────────────────
# "EVENT  <event>  <detail>  [<iso ts>]"
EVENT_LINE = re.compile(
    r"^EVENT  \S+  .*  \[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\]$")


def test_record_event_writes_an_event_line(tmp_path):
    """record_event must write a parseable EVENT line.

    What breaks if this fails: a release — the moment an examiner puts material
    the machine flagged back into the family's delivery, on their own authority —
    leaves no trace in the custody log. A fiduciary can be personally surcharged
    for that decision, and "I reviewed it on this date and released these items"
    is the only defence; it has to actually be written down.
    """
    custody = ChainOfCustody(tmp_path / "logs" / "chain_of_custody.log")
    custody.record_event("quarantine_release",
                         "actor=examiner items=2 flagged_by=romantic_intimate")

    lines = custody.log_path.read_text().splitlines()
    assert len(lines) == 1
    assert EVENT_LINE.match(lines[0]), lines[0]
    assert "quarantine_release" in lines[0]
    assert "actor=examiner" in lines[0]


def test_record_event_and_file_lines_stay_distinguishable(tmp_path):
    """The existing file-move line format is untouched by the new EVENT line.

    What breaks if this fails: every existing reader of a production custody log
    (and every audit of one) starts mis-parsing it. The two kinds must stay
    trivially separable — a SHA-256 digest is never the literal string "EVENT".
    """
    target = tmp_path / "photo.jpg"
    target.write_bytes(b"img")
    custody = ChainOfCustody(tmp_path / "chain_of_custody.log")
    custody.record_event("quarantine_release", "actor=examiner items=1")
    digest = custody.record_file(target)

    event_line, file_line = custody.log_path.read_text().splitlines()
    assert EVENT_LINE.match(event_line)
    assert CUSTODY_LINE.match(file_line), file_line     # unchanged format
    assert file_line.startswith(digest)
    assert not CUSTODY_LINE.match(event_line)           # never mistaken for a move
    assert not EVENT_LINE.match(file_line)


def test_record_event_is_append_only(tmp_path):
    """A second event never overwrites the first.

    What breaks if this fails: the custody log keeps only the most recent
    release and the earlier ones vanish — an audit trail that erases itself is
    worse than none, because it looks complete.
    """
    custody = ChainOfCustody(tmp_path / "chain_of_custody.log")
    custody.record_event("quarantine_release", "actor=examiner_a items=1")
    custody.record_event("quarantine_release", "actor=examiner_b items=3")

    lines = custody.log_path.read_text().splitlines()
    assert len(lines) == 2
    assert "actor=examiner_a" in lines[0]     # the first line survives verbatim
    assert "actor=examiner_b" in lines[1]


def test_record_event_empty_detail_is_still_well_formed(tmp_path):
    """An event with no detail still produces a parseable line."""
    custody = ChainOfCustody(tmp_path / "chain_of_custody.log")
    custody.record_event("quarantine_release")
    line = custody.log_path.read_text().splitlines()[0]
    assert line.startswith("EVENT  quarantine_release  ")
    assert line.endswith("]")
