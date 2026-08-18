"""
Unit tests for wyeast.core.safe_names — the single name sanitizer (review M9).
Covers slashes/control chars/very-long/Unicode, the relative-path helper, the
collision strategy, and equivalence with the two former duplicate call sites
(delivery.safe_album_dirname, s13.safe_album_dirname).
"""

from pathlib import PurePosixPath

import pytest

from wyeast.core import safe_names


# ── safe_component ───────────────────────────────────────────────────────────

def test_basic_collapse_and_underscore():
    assert safe_names.safe_component("Cancun 2004") == "Cancun_2004"
    assert safe_names.safe_component("Caribbean  Wedding") == "Caribbean_Wedding"


def test_strips_path_separators():
    # A slash must never survive into a single component.
    assert "/" not in safe_names.safe_component("a/b/c")
    assert "\\" not in safe_names.safe_component("a\\b")
    assert safe_names.safe_component("../etc/passwd") == "etc_passwd"


def test_strips_control_chars():
    out = safe_names.safe_component("hel\x00lo\tworld\n!")
    assert out == "hel_lo_world"
    assert all(ord(c) >= 32 for c in out)


def test_unicode_folds_to_ascii():
    # NFKD + drop-combining: accented letters keep their base form rather than
    # being stripped to a stub (strictly better than the old regex).
    assert safe_names.safe_component("Café Münchën") == "Cafe_Munchen"
    # CJK has no ASCII fold -> stripped, falls back.
    assert safe_names.safe_component("日本語", "fallback") == "fallback"


def test_empty_and_unsafe_only_use_fallback():
    assert safe_names.safe_component("", "album") == "album"
    assert safe_names.safe_component("***", "album") == "album"
    assert safe_names.safe_component(None, "album") == "album"


def test_length_cap():
    long = "A" * 500
    out = safe_names.safe_component(long)
    assert len(out) == safe_names.MAX_COMPONENT_LEN
    out2 = safe_names.safe_component(long, max_len=10)
    assert len(out2) == 10


def test_strips_leading_trailing_separators():
    # Strictly better than the old behaviour: no folder named "-Trip-" or "_x".
    assert safe_names.safe_component("-Trip-") == "Trip"
    assert safe_names.safe_component("__weird__") == "weird"


# ── safe_relpath ─────────────────────────────────────────────────────────────

def test_relpath_sanitizes_each_segment():
    p = safe_names.safe_relpath("Vacation 2004/Beach Day")
    assert p == PurePosixPath("Vacation_2004") / "Beach_Day"


def test_relpath_rejects_absolute_and_traversal():
    assert safe_names.safe_relpath("/etc/passwd") == PurePosixPath("etc") / "passwd"
    assert safe_names.safe_relpath("../../secret") == PurePosixPath("secret")
    # Windows separators are split too.
    assert safe_names.safe_relpath("a\\b\\c") == \
        PurePosixPath("a") / "b" / "c"


def test_relpath_empty_uses_fallback():
    assert safe_names.safe_relpath("", "fb") == PurePosixPath("fb")
    assert safe_names.safe_relpath("///", "fb") == PurePosixPath("fb")
    assert safe_names.safe_relpath(None, "fb") == PurePosixPath("fb")


# ── dedupe_name ──────────────────────────────────────────────────────────────

def test_dedupe_name_collision_strategy():
    used: set = set()
    assert safe_names.dedupe_name("Trip", used) == "Trip"
    assert safe_names.dedupe_name("Trip", used) == "Trip_2"
    assert safe_names.dedupe_name("Trip", used) == "Trip_3"
    assert safe_names.dedupe_name("Other", used) == "Other"
    assert used == {"Trip", "Trip_2", "Trip_3", "Other"}


# ── equivalence with the former duplicate call sites ─────────────────────────

def test_delivery_safe_album_dirname_delegates():
    from wyeast.core import delivery
    # Same outputs as the historical implementation for the ASCII titles the
    # pipeline produced.
    assert delivery.safe_album_dirname("Cancun 2004") == "Cancun_2004"
    assert delivery.safe_album_dirname("My/Bad:Title") == "My_Bad_Title"
    assert delivery.safe_album_dirname("", ) == "album"  # default fallback


# NOTE: upstream also has test_safe_album_dirname_delegates, which asserts the
# same delegation from wyeast.stages.llm_synthesis. That stage is outside the
# Family Archive import closure and is not carried here, so the test is dropped.
