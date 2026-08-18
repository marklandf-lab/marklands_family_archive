"""wyeast.core.audience — who is allowed to see an item.

This module decides, in one place, what the family sees and what stays with the
examiner. Every test here is really testing the same property from a different
side: FORGETTING MUST FAIL TOWARD THE FAMILY BEING SHOWN LESS, NEVER MORE.

Concretely, what these guard against:
  * a caller that forgets to pass an audience shipping the examiner's mail;
  * a legacy record (written before the rescue gate existed) vanishing from the
    family archive because a missing key was read as "rescued";
  * the family's Emails page silently falling back to the union index — which is
    the exact leak the split exists to close.
"""

import json

import pytest

from wyeast.core import audience as A
from wyeast.core.paths import CasePaths


@pytest.fixture
def paths(tmp_path):
    p = CasePaths.from_case_id("CASE_A", str(tmp_path))
    p.metadata_dir.mkdir(parents=True, exist_ok=True)
    return p


ORGANIC = {"file": "/m/1.eml", "email_subject": "dinner sunday"}
RESCUED = {"file": "/m/2.eml", "email_subject": "your statement is ready",
           "estate_rescued": True}
LEGACY = {"file": "/m/3.eml", "email_subject": "old case, no such key"}


# ── the predicate ────────────────────────────────────────────────────────────

def test_estate_rescued_mail_is_examiner_only():
    """Rescued mail was rescued BECAUSE family-relevance triage discarded it.
    If this flips, marketing and platform mail lands back in the family archive."""
    assert A.is_family_visible(ORGANIC) is True
    assert A.is_family_visible(RESCUED) is False


def test_a_record_with_no_flag_is_family_visible():
    """Legacy indexes (pre-rescue-gate) carry no `estate_rescued` key at all.

    If a missing key were read as "rescued", every email in every case processed
    before the gate existed would silently disappear from the family's archive —
    a data-loss bug that would look exactly like a working filter.
    """
    assert A.is_family_visible(LEGACY) is True
    assert A.is_family_visible({}) is True
    assert A.is_family_visible(None) is True


def test_filter_scopes_to_the_audience():
    entries = [ORGANIC, RESCUED, LEGACY]
    assert A.filter_email_entries(entries, A.FAMILY) == [ORGANIC, LEGACY]
    assert A.filter_email_entries(entries, A.EXAMINER) == entries


def test_an_unknown_audience_is_an_error():
    """Typos must not silently resolve to "show everything"."""
    with pytest.raises(ValueError):
        A.filter_email_entries([], "familly")
    with pytest.raises(ValueError):
        A.thread_index_path("/tmp", "everyone")


# ── the default ──────────────────────────────────────────────────────────────

def test_the_default_audience_is_the_restrictive_one(paths):
    """The load-bearing default. A caller that forgets to pass an audience must
    get the family subset — i.e. forgetting withholds, it does not leak."""
    paths.index("email_index.json").write_text(json.dumps([ORGANIC, RESCUED]))

    assert A.load_email_index(paths) == [ORGANIC]                  # no audience
    assert A.filter_email_entries([ORGANIC, RESCUED]) == [ORGANIC]  # no audience
    assert A.thread_index_path(paths).name == "email_threads_index_family.json"
    assert A.correspondent_path(paths).name == "correspondent_frequency_family.json"


def test_load_email_index_scopes_and_survives_a_missing_file(paths):
    assert A.load_email_index(paths, A.FAMILY) == []               # no index yet

    paths.index("email_index.json").write_text(json.dumps([ORGANIC, RESCUED]))
    assert A.load_email_index(paths, A.FAMILY) == [ORGANIC]
    assert A.load_email_index(paths, A.EXAMINER) == [ORGANIC, RESCUED]


def test_a_corrupt_index_yields_nothing_rather_than_raising(paths):
    """A truncated index must not take down the family archive server — and must
    not be read as "no filtering needed" either."""
    paths.index("email_index.json").write_text("{ this is not json")
    assert A.load_email_index(paths, A.FAMILY) == []


# ── the paths ────────────────────────────────────────────────────────────────

def test_the_roles_get_different_files(paths):
    """They used to share one filename, so whichever role built last decided what
    BOTH roles saw next: an examiner explorer build left the union sitting in the
    file the family's archive server read."""
    assert A.thread_index_path(paths, A.FAMILY) != A.thread_index_path(paths, A.EXAMINER)
    assert A.thread_pages_dirname(A.FAMILY) == "email_threads"
    assert A.thread_pages_dirname(A.EXAMINER) == "email_threads_examiner"


def test_the_examiner_keeps_the_legacy_correspondent_filename(paths):
    """correspondent_frequency.json has ALWAYS held the union, so it keeps meaning
    that. The family's is the new file.

    Inverting this would serve a stale union — every marketing sender the case
    ever saw — to precisely the audience that must never see it, on any case not
    yet re-triaged.
    """
    assert A.correspondent_path(paths, A.EXAMINER).name == "correspondent_frequency.json"
    assert A.correspondent_path(paths, A.FAMILY).name == "correspondent_frequency_family.json"


# ── the asymmetry: how each audience degrades when its index is missing ──────

def test_the_family_never_falls_back_to_the_union_index(paths):
    """THE fail-open this module exists to prevent.

    A case processed before the split has only the legacy (union) conversation
    index. If the family's reader fell back to it, every estate-rescued marketing
    email would be browsable in the family archive again — silently, and looking
    entirely normal.

    Fail CLOSED instead: an empty Emails section, and a loud warning telling the
    operator to rebuild.
    """
    paths.index("email_index.json").write_text(json.dumps([ORGANIC, RESCUED]))
    legacy = {"threads": [{"thread_id": "t1", "files": ["/m/2.eml"]}]}
    A.legacy_thread_index_path(paths).write_text(json.dumps(legacy))

    assert A.load_thread_index(paths, A.FAMILY) == {}, \
        "the family must not read the legacy union thread index"


def test_the_examiner_does_fall_back_to_the_union_index(paths):
    """The opposite direction, on purpose: the examiner is entitled to everything,
    so a missing role-scoped index degrades to the union rather than to nothing.
    A fiduciary who cannot see the evidence is a bug; a family who can see the
    marketing mail is the bug we are fixing."""
    paths.index("email_index.json").write_text(json.dumps([ORGANIC, RESCUED]))
    legacy = {"threads": [{"thread_id": "t1", "files": ["/m/2.eml"]}]}
    A.legacy_thread_index_path(paths).write_text(json.dumps(legacy))

    assert A.load_thread_index(paths, A.EXAMINER) == legacy


def test_the_role_scoped_index_wins_over_the_legacy_one(paths):
    fam = {"threads": [{"thread_id": "tf", "files": ["/m/1.eml"]}]}
    A.legacy_thread_index_path(paths).write_text(
        json.dumps({"threads": [{"thread_id": "tlegacy"}]}))
    A.thread_index_path(paths, A.FAMILY).write_text(json.dumps(fam))

    assert A.load_thread_index(paths, A.FAMILY) == fam


def test_a_case_with_no_mail_at_all_is_quiet(paths, caplog):
    """A photos-only case must not log warnings about missing email indexes —
    warning noise on healthy cases is how real warnings get ignored."""
    with caplog.at_level("WARNING"):
        assert A.load_thread_index(paths, A.FAMILY) == {}
    assert not caplog.records


def test_a_case_with_mail_but_no_family_index_warns_loudly(paths, caplog):
    """The operator has to know the Emails section is empty because it needs a
    rebuild — not because the person had no email."""
    paths.index("email_index.json").write_text(json.dumps([ORGANIC]))
    with caplog.at_level("WARNING"):
        assert A.load_thread_index(paths, A.FAMILY) == {}
    assert any("EMPTY" in r.message or "empty" in r.message for r in caplog.records)


# ── Contact resolution tiers (macos-contact-data-sources spec §5) ────────────

TIER_A_CONV = {
    "conversation_id": "sms:1",
    "participant_contacts": [
        {"handle": "5035550142", "display_name": "Ada Lovelace",
         "contact_tier": "A", "contact_sources": ["vcf"]},
    ],
}
TIER_B_CONV = {
    "conversation_id": "sms:2",
    "participant_contacts": [
        {"handle": "5035550143", "display_name": "A. Lovelace (work)",
         "contact_tier": "B", "contact_sources": ["abcddb", "vcf"],
         "contact_candidates": ["A. Lovelace (work)", "Ada Lovelace"]},
    ],
}


def test_tier_a_contact_is_family_visible():
    out = A.participant_contacts(TIER_A_CONV, A.FAMILY)
    assert out == TIER_A_CONV["participant_contacts"]


def test_family_sees_the_picked_name_but_not_the_losing_candidates():
    """Until 2026-08-15 a Tier B resolution was stripped back to the bare
    handle for the family (spec §4/§5). The operator reversed that
    (contact-name-surfaces §2): the family sees the name the pick chose — but
    never the disagreement itself, which is an examiner surface."""
    out = A.participant_contacts(TIER_B_CONV, A.FAMILY)
    assert out == [{"handle": "5035550143",
                    "display_name": "A. Lovelace (work)",
                    "contact_tier": "B",
                    "contact_sources": ["abcddb", "vcf"]}]
    assert "contact_candidates" not in out[0]


def test_examiner_sees_tier_b_candidate_unchanged():
    out = A.participant_contacts(TIER_B_CONV, A.EXAMINER)
    assert out == TIER_B_CONV["participant_contacts"]


def test_participant_contacts_missing_key_is_empty():
    assert A.participant_contacts({}, A.FAMILY) == []
    assert A.participant_contacts(None, A.EXAMINER) == []


def test_participant_contacts_default_audience_is_family():
    # Same fail-closed-by-default contract as load_email_index/load_thread_index.
    assert A.participant_contacts(TIER_B_CONV) == \
        A.participant_contacts(TIER_B_CONV, A.FAMILY)


# ── stdlib purity (wyeast.core must import under every venv) ─────────────────

def test_audience_is_stdlib_pure():
    """wyeast.core is imported by stages running under six different venvs. A
    third-party import here would break the ones that lack it — at case time."""
    import ast
    import pathlib

    src = pathlib.Path(A.__file__).read_text()
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(n.name.split(".")[0] for n in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])

    assert imported <= {"json", "logging", "pathlib"}, \
        f"non-stdlib import in wyeast.core.audience: {imported}"
