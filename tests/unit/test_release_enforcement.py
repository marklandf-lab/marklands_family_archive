"""E3/E4/E5 enforcement tests (T4).

E5 is the family GET surface, default-closed: a body is served only when the
shell allowlist matches OR the release is valid-and-current. That reduces to
`_e5_shell_allowed(path)` + `ArchiveCase.release_status().valid`, both testable
without a live socket. E3 is the startup gate; E4 the export verbs.
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
from wyeast.core.moves import LEDGER_NAME  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "tfa", str(REPO / "tests" / "unit" / "test_family_archive.py"))
_tfa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tfa)
setup_case = _tfa.setup_case
_sign = _tfa._sign


def _revoke_on_disk(paths):
    """Revoke without the examiner-only verb (these cases are family-role)."""
    rec = release.load_release(paths)
    rec["revoked"] = True
    release.release_path(paths).write_text(json.dumps(rec))


# ── E5 shell allowlist ────────────────────────────────────────────────────────

def test_e5_shell_allowlist_lets_shell_through():
    assert fa._e5_shell_allowed("/")
    assert fa._e5_shell_allowed("/assets/family/family.js")
    assert fa._e5_shell_allowed("/api/release-status")
    assert fa._e5_shell_allowed("/photos")            # a page shell


def test_e5_shell_allowlist_gates_every_body():
    for body in ("/media", "/thumb", "/api/photos", "/api/search",
                 "/api/transcript", "/api/email/thread"):
        assert not fa._e5_shell_allowed(body), body


# ── E5 release_status transitions ─────────────────────────────────────────────

def test_release_status_legacy_then_released_then_stale_then_revoked(tmp_path):
    case, paths = setup_case(tmp_path, role="family")

    # absent record → legacy_unsigned, closed
    st = case.release_status()
    assert st["state"] == "legacy_unsigned" and not st["valid"]

    # a valid signature → open
    _sign(paths)
    assert case.release_status()["valid"]

    # a post-sign content change (a verb_release surfacing a new delivered file)
    # → the stamp trips, the fingerprint differs → closed (the rev-3 hole)
    (paths.archive_dir / "surfaced.jpg").write_bytes(b"surfaced bytes")
    (paths.metadata_dir / LEDGER_NAME).write_text('{"status":"done"}\n')
    st = case.release_status()
    assert not st["valid"] and st["stale"]

    # re-sign to clear, then revoke → closed, revoked
    _sign(paths)
    assert case.release_status()["valid"]
    _revoke_on_disk(paths)
    st = case.release_status()
    assert not st["valid"] and st["revoked"]


def test_release_status_serves_after_benign_ledger_append(tmp_path):
    """A reverted/neutral byte-move grows the append-only ledger (stamp trips) but
    changes no family-visible content → the escalation serves, and the verdict is
    cached so the next status check does not re-walk."""
    case, paths = setup_case(tmp_path, role="family")
    _sign(paths)
    (paths.metadata_dir / LEDGER_NAME).write_text(
        '{"status":"done","reason":"release"}\n'
        '{"status":"done","reason":"rebanish"}\n')

    calls = {"n": 0}
    real_fp = release.fingerprint

    def counting(p, mode=release.MODE_STANDARD):
        calls["n"] += 1
        return real_fp(p, mode)

    release.fingerprint = counting
    try:
        assert case.release_status()["valid"]       # stamp tripped, content same → serve
        assert case.release_status()["valid"]       # served again
    finally:
        release.fingerprint = real_fp
    assert calls["n"] == 1                           # walked once; cache hit thereafter


def test_release_status_reads_fresh_from_disk(tmp_path):
    """The long-lived-server trap: a revoke written to disk is honored on the next
    status check without a case.load()."""
    case, paths = setup_case(tmp_path, role="family")
    _sign(paths)
    assert case.release_status()["valid"]
    # revoke by rewriting the record on disk (os.replace bumps mtime_ns)
    rec = release.load_release(paths)
    rec["revoked"] = True
    release.release_path(paths).write_text(json.dumps(rec))
    assert not case.release_status()["valid"]


# ── E3 startup gate ───────────────────────────────────────────────────────────

def test_e3_absent_record_starts_legacy(tmp_path):
    case, paths = setup_case(tmp_path, role="family")
    # absent → no exit (starts; E5 gates bodies)
    fa._assert_family_release_startup(paths)


def test_e3_present_but_invalid_refuses(tmp_path):
    case, paths = setup_case(tmp_path, role="family")
    _sign(paths)
    (paths.archive_dir / "changed.jpg").write_bytes(b"after signing")
    with pytest.raises(SystemExit):
        fa._assert_family_release_startup(paths)


def test_e3_valid_record_starts(tmp_path):
    case, paths = setup_case(tmp_path, role="family")
    _sign(paths)
    fa._assert_family_release_startup(paths)         # no exit


# ── E4 export verbs ───────────────────────────────────────────────────────────

def test_e4_unsigned_family_export_refused(tmp_path):
    case, paths = setup_case(tmp_path, role="family")
    with pytest.raises(fa.VerbError) as exc:
        fa._assert_family_export_allowed(case)
    assert exc.value.code == 403
    assert "signature" in str(exc.value)


def test_e4_signed_family_export_allowed(tmp_path):
    case, paths = setup_case(tmp_path, role="family")
    _sign(paths)
    fa._assert_family_export_allowed(case)           # no raise


def test_e4_revoked_family_export_refused(tmp_path):
    case, paths = setup_case(tmp_path, role="family")
    _sign(paths)
    _revoke_on_disk(paths)
    with pytest.raises(fa.VerbError) as exc:
        fa._assert_family_export_allowed(case)
    assert exc.value.code == 403


# ── a corrupt (present-but-unreadable) record fails CLOSED at every gate ───────
# It must never be silently downgraded to legacy_unsigned.

def _corrupt_record(paths):
    release.release_path(paths).write_text("{ this is not json")


def test_corrupt_record_release_status_invalid(tmp_path):
    case, paths = setup_case(tmp_path, role="family")
    _corrupt_record(paths)
    st = case.release_status()
    assert st["state"] == "invalid" and not st["valid"]


def test_corrupt_record_e3_refuses_start(tmp_path):
    case, paths = setup_case(tmp_path, role="family")
    _corrupt_record(paths)
    with pytest.raises(SystemExit):
        fa._assert_family_release_startup(paths)


def test_corrupt_record_e4_refuses_export(tmp_path):
    case, paths = setup_case(tmp_path, role="family")
    _corrupt_record(paths)
    with pytest.raises(fa.VerbError) as exc:
        fa._assert_family_export_allowed(case)
    assert exc.value.code == 403
