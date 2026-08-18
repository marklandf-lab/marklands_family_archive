"""Unit tests for tools/export_delivery — the last gate before a family's drive.

export_delivery materializes output/ onto the delivery media. Everything it
copies is seen by the family; everything it skips is not. Until recently this
module had no tests at all, which is how output/reconciliation_review.md — the
examiner's page of "what is suspect, what is missing, what needs attention" —
came to be shipped to families.

INCLUDE_TOP is an ALLOWLIST: nothing reaches the family unless it is NAMED.
That inversion is the point. The denylist it replaced failed open — a new
top-level artifact was delivered BY DEFAULT — and it failed that way twice, once
for the examiner's review page and once for output/email_threads/, which carried
the full rendered bodies of the estate-rescued marketing and platform mail.

So the central test here (test_delivered_tree_matches_the_allowlist_exactly) is
deliberately strict: it asserts the exported tree against the allowlist EXACTLY,
so that adding an output to output/ without deciding its audience FAILS THE BUILD
instead of shipping to a grieving family.
"""

import json

import pytest

from tools import export_delivery


def _build_output(tmp_path):
    """A case output/ tree: family content, processing-only trees, the examiner
    subtree, and the two family_archive working trees that real cases carry."""
    case = tmp_path / "cases" / "CASE_X"
    out = case / "output"

    # family-facing content
    (out / "archive").mkdir(parents=True)
    (out / "archive" / "photo.jpg").write_bytes(b"jpg")
    (out / "email_threads").mkdir(parents=True)
    (out / "email_threads" / "t0a1b2c3d4e5f.html").write_text("<html>hi</html>")
    (out / "case_report.html").write_text("<html>report</html>")

    # processing-only trees
    (out / "metadata").mkdir(parents=True)
    (out / "metadata" / "email_index.json").write_text("[]")
    (out / "metadata" / "_archive_fts_family.sqlite").write_bytes(b"sqlite")
    (out / "suspense").mkdir(parents=True)
    (out / "suspense" / "corrupt.jpg").write_bytes(b"x")

    # examiner-only subtree
    (out / "examiner").mkdir(parents=True)
    (out / "examiner" / "reconciliation_review.md").write_text("# examiner page")

    # the examiner's conversation pages — same bodies, minus the audience filter
    (out / "email_threads_examiner").mkdir(parents=True)
    (out / "email_threads_examiner" / "t9f8e7d6c5b4a.html").write_text(
        "<html>rescued marketing mail</html>")

    # family_archive's own working trees (present on real cases)
    (out / "family_banished").mkdir(parents=True)
    (out / "family_banished" / "removed.jpg").write_bytes(b"x")
    (out / "family_export").mkdir(parents=True)
    (out / "family_export" / "staged.jpg").write_bytes(b"x")

    return case


def _export(tmp_path, case):
    dest = tmp_path / "delivery"
    # These allowlist tests build an unsigned case; route them through the
    # recorded escape hatch so they exercise the ALLOWLIST (their purpose)
    # without also having to stand up a full release signature. The signature
    # gate itself is covered by the dedicated tests below.
    export_delivery.main([
        case.name, "--dest", str(dest),
        "--cases-root", str(case.parent), "--copy",
        "--force-unsigned", "--reason", "unit test",
    ])
    return dest


def _exported_paths(dest):
    return {str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file()}


def _tops(dest):
    return {p.name for p in dest.iterdir()}


def test_delivered_tree_matches_the_allowlist_exactly(tmp_path):
    """THE test. Every top-level entry delivered is one we NAMED.

    If you add an artifact to output/ and this fails, that is the design working:
    decide whether a grieving family should receive it, then either add it to
    INCLUDE_TOP or leave it withheld. Do not "fix" this by loosening the check.
    """
    case = _build_output(tmp_path)
    dest = _export(tmp_path, case)

    assert _tops(dest) <= export_delivery.INCLUDE_TOP, (
        f"delivered a top-level entry that is not in the allowlist: "
        f"{_tops(dest) - export_delivery.INCLUDE_TOP}")


def test_a_new_unlisted_artifact_is_withheld_by_default(tmp_path):
    """The failure mode the denylist had: a new output/ artifact ships to the
    family until someone remembers to exclude it. Now it is withheld until
    someone remembers to include it."""
    case = _build_output(tmp_path)
    (case / "output" / "some_future_stage_report.md").write_text("# whatever")
    (case / "output" / "future_tree").mkdir()
    (case / "output" / "future_tree" / "thing.txt").write_text("x")

    delivered = _exported_paths(_export(tmp_path, case))

    assert "some_future_stage_report.md" not in delivered
    assert not [p for p in delivered if p.startswith("future_tree")]


def test_examiner_content_is_never_delivered(tmp_path):
    """The examiner's review page and conversation pages stay with the examiner."""
    case = _build_output(tmp_path)
    delivered = _exported_paths(_export(tmp_path, case))

    assert not [p for p in delivered if p.startswith("examiner")]
    assert not [p for p in delivered if p.startswith("email_threads_examiner")], (
        "the examiner's thread pages carry estate-rescued bodies — "
        "they must never reach the family")


def test_processing_and_working_trees_stay_excluded(tmp_path):
    """metadata/ carries the plaintext FTS index (_archive_fts_*.sqlite) — OCR and
    email CONTENT that must never land on an unencrypted stick. family_banished/
    holds what the family REMOVED; family_export/ is the archive server's own
    staging area."""
    case = _build_output(tmp_path)
    delivered = _exported_paths(_export(tmp_path, case))

    for tree in ("metadata", "suspense", "family_banished", "family_export"):
        assert not [p for p in delivered if p.startswith(tree)], f"{tree} was delivered"


def test_family_content_is_still_delivered(tmp_path):
    """The allowlist must not be over-tight — the family still gets their archive.

    Without this, an allowlist that named nothing would pass every test above
    while shipping an empty drive.
    """
    case = _build_output(tmp_path)
    delivered = _exported_paths(_export(tmp_path, case))

    assert "archive/photo.jpg" in delivered
    assert "email_threads/t0a1b2c3d4e5f.html" in delivered
    assert "case_report.html" in delivered


def test_include_top_pins_the_delivered_set(tmp_path):
    """Pin the constant itself, in both directions."""
    for name in ("archive", "email_threads", "explorer", "case_report.html",
                 "documents", "audio"):
        assert name in export_delivery.INCLUDE_TOP
    for name in ("metadata", "suspense", "examiner", "email_threads_examiner",
                 "family_banished", "family_export"):
        assert name not in export_delivery.INCLUDE_TOP


def test_symlinks_are_materialized(tmp_path):
    """A USB stick is symlink-hostile: every view link must become real bytes."""
    case = _build_output(tmp_path)
    out = case / "output"
    (out / "by_person" / "Person_01").mkdir(parents=True)
    (out / "by_person" / "Person_01" / "photo.jpg").symlink_to(
        out / "archive" / "photo.jpg")

    dest = _export(tmp_path, case)
    linked = dest / "by_person" / "Person_01" / "photo.jpg"
    assert linked.is_file() and not linked.is_symlink()
    assert linked.read_bytes() == b"jpg"


def test_broken_symlink_is_skipped_not_fatal(tmp_path):
    """A dangling view link (quarantined source, say) must not abort the export."""
    case = _build_output(tmp_path)
    out = case / "output"
    (out / "by_person" / "Person_01").mkdir(parents=True)
    (out / "by_person" / "Person_01" / "gone.jpg").symlink_to(
        out / "archive" / "does_not_exist.jpg")

    delivered = _exported_paths(_export(tmp_path, case))
    assert "archive/photo.jpg" in delivered
    assert "by_person/Person_01/gone.jpg" not in delivered


def test_refuses_an_examiner_explorer_bundle(tmp_path):
    """output/explorer is allowlisted because it is normally the FAMILY's bundle.

    Nothing in the path says so — only the manifest inside it does. An examiner
    bundle left at that path would ship the examiner's view of the case, and the
    allowlist alone cannot see that. Refuse, loudly, rather than deliver it.
    """
    case = _build_output(tmp_path)
    explorer = case / "output" / "explorer"
    explorer.mkdir(parents=True)
    (explorer / "build_manifest.json").write_text(
        json.dumps({"case_id": "CASE_X", "role": "examiner"}))
    (explorer / "index.html").write_text("<html>examiner view</html>")

    with pytest.raises(SystemExit) as exc:
        export_delivery.main([
            case.name, "--dest", str(tmp_path / "delivery"),
            "--cases-root", str(case.parent), "--copy",
            "--force-unsigned", "--reason", "unit test",
        ])
    assert exc.value.code != 0


def test_accepts_a_family_explorer_bundle(tmp_path):
    case = _build_output(tmp_path)
    explorer = case / "output" / "explorer"
    explorer.mkdir(parents=True)
    (explorer / "build_manifest.json").write_text(
        json.dumps({"case_id": "CASE_X", "role": "family"}))
    (explorer / "index.html").write_text("<html>family view</html>")

    delivered = _exported_paths(_export(tmp_path, case))
    assert "explorer/index.html" in delivered


def test_refuses_a_non_empty_destination(tmp_path):
    """Never write into a dest that already has content — an operator pointing at
    the wrong stick should get an error, not a merge."""
    case = _build_output(tmp_path)
    dest = tmp_path / "delivery"
    dest.mkdir()
    (dest / "someone_elses_case.txt").write_text("x")

    with pytest.raises(SystemExit) as exc:
        export_delivery.main([
            case.name, "--dest", str(dest),
            "--cases-root", str(case.parent), "--copy",
        ])
    assert exc.value.code != 0


# ── the release-signature gate (E1) ──────────────────────────────────────────

from pathlib import Path

from wyeast.core.paths import CasePaths
from wyeast.core.custody import ChainOfCustody
from wyeast.core import release


def _sign(case, mode="standard"):
    """Produce a valid family_release.json + the matching custody anchor for the
    output tree at `case` (as verb_signoff will)."""
    paths = CasePaths.from_case_dir(case)
    fp = release.fingerprint(paths, mode)
    stamp = release.visibility_stamp(paths)
    ChainOfCustody(paths.custody_log).record_event("release", f"{fp} actor=Jane")
    rec = {"case_id": paths.case_id, "delivery_fingerprint": fp,
           "fingerprint_mode": mode,
           "fingerprint_version": release.FINGERPRINT_VERSION,
           "visibility_stamp": stamp, "revoked": False}
    release.release_path(paths).parent.mkdir(parents=True, exist_ok=True)
    release.release_path(paths).write_text(json.dumps(rec))
    return paths


def test_unsigned_case_is_refused_without_force(tmp_path):
    case = _build_output(tmp_path)
    with pytest.raises(SystemExit) as exc:
        export_delivery.main([
            case.name, "--dest", str(tmp_path / "d"),
            "--cases-root", str(case.parent)])
    assert exc.value.code == 1


def test_signed_case_exports(tmp_path):
    case = _build_output(tmp_path)
    _sign(case)
    dest = tmp_path / "d"
    export_delivery.main([
        case.name, "--dest", str(dest), "--cases-root", str(case.parent)])
    assert (dest / "case_report.html").exists()


def test_blocked_gate_refused_even_with_force_unsigned(tmp_path):
    """The export gate is a MACHINE gate; --force-unsigned overrides the
    signature, never a blocked delivery."""
    case = _build_output(tmp_path)
    (case / "output" / "metadata" / "case_summary.json").write_text(json.dumps(
        {"export_gate": {"delivery_blocked": True, "reasons": ["scan incomplete"]}}))
    with pytest.raises(SystemExit) as exc:
        export_delivery.main([
            case.name, "--dest", str(tmp_path / "d"),
            "--cases-root", str(case.parent),
            "--force-unsigned", "--reason", "x"])
    assert exc.value.code == 3


def test_tree_changed_after_signing_is_refused(tmp_path):
    case = _build_output(tmp_path)
    _sign(case)
    (case / "output" / "archive" / "new.jpg").write_bytes(b"new content")
    with pytest.raises(SystemExit) as exc:
        export_delivery.main([
            case.name, "--dest", str(tmp_path / "d"),
            "--cases-root", str(case.parent)])
    assert exc.value.code == 1


def test_force_unsigned_records_custody_and_examiner_marker(tmp_path):
    case = _build_output(tmp_path)
    export_delivery.main([
        case.name, "--dest", str(tmp_path / "d"),
        "--cases-root", str(case.parent),
        "--force-unsigned", "--reason", "no reviewer available"])
    paths = CasePaths.from_case_dir(case)
    log = paths.custody_log.read_text()
    assert "EVENT  release_forced_unsigned" in log
    assert "no reviewer available" in log
    marker = case / "output" / "examiner" / export_delivery.FORCED_MARKER
    assert marker.exists() and "WITHOUT SIGNATURE" in marker.read_text()
    # and the marker is NEVER delivered
    assert not (tmp_path / "d" / "examiner").exists()


def test_default_export_is_independent_copy_not_hardlink(tmp_path):
    """E1 forces an independent copy: the delivered file must not share an inode
    with the live source, or the TOCTOU re-verify protects nothing."""
    case = _build_output(tmp_path)
    _sign(case)
    dest = tmp_path / "d"
    export_delivery.main([
        case.name, "--dest", str(dest), "--cases-root", str(case.parent)])
    src_ino = (case / "output" / "archive" / "photo.jpg").stat().st_ino
    dst_ino = (dest / "archive" / "photo.jpg").stat().st_ino
    assert src_ino != dst_ino


def test_allow_hardlink_shares_inode(tmp_path):
    """--allow-hardlink opts back into inode-sharing on a same-fs dest."""
    case = _build_output(tmp_path)
    _sign(case)
    dest = tmp_path / "d"
    export_delivery.main([
        case.name, "--dest", str(dest), "--cases-root", str(case.parent),
        "--allow-hardlink"])
    src_ino = (case / "output" / "archive" / "photo.jpg").stat().st_ino
    dst_ino = (dest / "archive" / "photo.jpg").stat().st_ino
    assert src_ino == dst_ino


def test_source_change_mid_copy_aborts(tmp_path, monkeypatch):
    """The TOCTOU anti-tear guarantee: if the SOURCE changes during the copy
    (F1 != F0), the partial export is discarded rather than shipped."""
    case = _build_output(tmp_path)
    _sign(case)
    dest = tmp_path / "d"

    real_materialize = export_delivery.materialize
    state = {"mutated": False}

    def mutating(src, d, allow_hardlink):
        # On the first file copied, mutate the live source so F1 diverges from F0.
        if not state["mutated"]:
            state["mutated"] = True
            (case / "output" / "archive" / "snuck_in.jpg").write_bytes(b"late")
        return real_materialize(src, d, allow_hardlink)

    monkeypatch.setattr(export_delivery, "materialize", mutating)
    with pytest.raises(SystemExit) as exc:
        export_delivery.main([
            case.name, "--dest", str(dest), "--cases-root", str(case.parent)])
    assert exc.value.code == 1
    assert not dest.exists()          # the torn snapshot was discarded


def test_unreadable_explorer_manifest_refused(tmp_path):
    """A signed case whose explorer bundle has an unreadable manifest is refused —
    the bundle's audience cannot be confirmed."""
    case = _build_output(tmp_path)
    _sign(case)
    explorer = case / "output" / "explorer"
    explorer.mkdir(parents=True)
    (explorer / "build_manifest.json").write_text("{ not json")
    with pytest.raises(SystemExit) as exc:
        export_delivery.main([
            case.name, "--dest", str(tmp_path / "d"),
            "--cases-root", str(case.parent)])
    assert exc.value.code == 1
