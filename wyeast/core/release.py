"""
wyeast.core.release — the family-release fingerprint, stamp, and verify.

This is the mechanical heart of the examiner release gate
(docs/specs/examiner-release-gate.md). It answers one question two ways:

    "Is the delivered family tree still exactly what a named human released?"

There are two tiers, and they do different jobs (measured on this box:
`goog` 10 GB / 34,639 files — stat walk 0.3 s, content-hash ~31 s; `2ndapple`
222 GB — stat walk 12.3 s, content-hash ~12 min). A 12-minute check on every
family GET is a `cp`-shaped gate, so:

  * THE FINGERPRINT — content-identity, NO mtime — is the sole AUTHORITY. It is
    what "released tree F" is bound to. Recomputed at export, at server startup,
    and whenever the live stamp trips. Because it is content-based, a pipeline
    `--restart` that moves no family-visible content produces an identical
    fingerprint and forces NO wet re-sign (the fatigue path that manufactures
    `--force-unsigned`, killed).

  * THE VISIBILITY STAMP — a cheap `(name, mtime_ns, size)` hash over ~15
    fixed paths — is a live TRIPWIRE, never an authority. On every family GET:
    stamp matches ⇒ serve (sub-ms fast path); stamp differs ⇒ recompute the
    FINGERPRINT and refuse only if THAT also differs. So a benign, content-
    neutral churn (a `--restart`, a release-then-rebanish) trips the stamp but
    still serves, because the fingerprint is unchanged. The escalation verdict
    is cached against the stamp value so the authoritative walk runs once per
    distinct tree state, not once per request.

WHY NO mtime IN THE FINGERPRINT. A `--restart` of scene/face/geo re-points view
symlinks and rewrites `documents/` with identical content; an mtime scheme
forced a fresh wet-ink re-sign of a byte-identical certificate. Content-identity
ignores it. Standard mode's ONE blind spot: a same-size content substitution of
an `archive/` file (an in-place edit, or a swap of two files' bytes) leaves the
membership+size digest unchanged — caught only by `--deep`, and conceded as
operator tampering behind a shell (threat model).

WHY SERVE-GATING METADATA IS SLICED, NOT WHOLE-FILE HASHED. The running server's
`/media` decides whether a withheld byte is servable by consulting five
sensitivity allowlists (`archive_map.json` — the master, `resolve_media_path`
resolves src→canonical here first — plus `video_frame_map.json`,
`perceptual_dup_groups.json`, `dup_member_scan.json`, and the `deliverable_audio`
slice of `case_summary.json`) and two curation sidecars. All seven are in the
fingerprint. But `archive_map`/`perceptual_dup_groups`/`dup_member_scan` carry a
wall-clock timestamp, and `video_frame_map` is written in completion order — so a
content-neutral `--restart` rewrites them byte-differently with no family-visible
change. Whole-file hashing would flip the fingerprint and resurrect the fatigue
path. Hashing the canonical, sorted, timestamp-free SLICE — the set/map that
actually gates `/media` — makes a byte-neutral regeneration inert while still
catching a real change to what the server will serve.

Two things `build_stacks` reads are deliberately NOT fingerprinted: `scene_index`
and the move-ledger gate RELEVANCE/keeper visibility, not sensitivity. The worst
a post-sign edit to either can do is surface a CLEAN duplicate (its nudity
verdict is already fingerprinted via `dup_member_scan`) of an already-fetchable
photo. No withheld-sensitive byte is re-exposable. Stated, not fingerprinted, on
purpose (rev-7 confirmation caveat). Likewise `transcribe.deliver` is a config
toggle recorded in the certificate's `machine_screen`, not here.

Stdlib-pure (no third-party imports) so it imports under every step venv —
enforced by tests/unit/test_invariants.py::test_core_package_is_stdlib_pure.
"""

import contextlib
import hashlib
import json
import os
import stat as _stat
from pathlib import Path
from typing import NamedTuple, Optional

from wyeast.core.moves import LEDGER_NAME

# The delivered tree's top-level allowlist. PINNED to
# tools/export_delivery.INCLUDE_TOP by a test — the fingerprint must cover
# exactly what ships, no more, no less. Kept as a local literal because
# export_delivery imports wyeast.core (importing it here would be circular) and
# is a venv-phase1 tool, not stdlib-pure core.
DELIVERED_TOP = (
    "archive",
    "all_photos_by_scene",
    "by_event",
    "by_person",
    "audio",
    "documents",
    "email_threads",
    "explorer",
    "case_report.html",
)

# The symlink view trees, grouped by cluster/scene/event. PINNED to
# family_archive.VIEW_DIRS by a test. These are a subset of DELIVERED_TOP; they
# are fingerprinted by label-independent GROUPING content (below), not by their
# labeled relpaths, so a Person_NN renumber is inert.
VIEW_DIRS = ("by_person", "all_photos_by_scene", "by_event")

# Serve-gating metadata filenames, all under output/metadata/. DELIVERED_TOP and
# VIEW_DIRS are pinned to their source of truth by tests; these filenames are the
# literals the pipeline stages write, kept here so the fingerprint/stamp read the
# same files /media consults. If a stage renames one, a slice silently reads an
# absent file (a false-negative), so keep them in lockstep with the writers.
RELEASE_FILE = "family_release.json"
DECISIONS_FILE = "family_decisions.json"       # verb-written curation overlay
CURATION_FILE = "curation_layer.json"          # verb-written curation overlay
QUARANTINE_MANIFEST = "quarantine_manifest.json"
FACE_CLUSTERING_FILE = "face_clustering.json"
ARCHIVE_MAP_FILE = "archive_map.json"
VIDEO_FRAME_MAP_FILE = "video_frame_map.json"
PERCEPTUAL_DUP_GROUPS_FILE = "perceptual_dup_groups.json"
DUP_MEMBER_SCAN_FILE = "dup_member_scan.json"
CASE_SUMMARY_FILE = "case_summary.json"

MODE_STANDARD = "standard"
MODE_DEEP = "deep"

# Bump whenever the fingerprint's LINE FORMAT changes (not its inputs' content),
# so a stale signature is attributable to a format change — re-sign required —
# rather than to tree tampering. v2: CSAM matching was removed from the pipeline,
# so the dup-gating slice no longer carries a per-member csam bit; every case
# signed under v1 must be re-signed.
#
# That attribution only works because the version is BOTH mixed into the digest
# (see fingerprint()) AND persisted in the release record at sign time, then
# checked in verify()'s common gates. Versioning the digest alone is worse than
# not versioning it: every pre-bump case would fail with the tamper reason, and
# a genuine tamper would be indistinguishable from routine upgrade noise — which
# trains the examiner to sign straight past a real alert. Records written before
# the field existed are treated as v1 (RECORD_VERSION_DEFAULT).
FINGERPRINT_VERSION = 2
RECORD_VERSION_DEFAULT = 1

# The stamp's absent-file sentinel (red-team MUST). A fresh case has no ledger,
# no manifest, no sidecars. Mapping "absent" to a value distinct from anything an
# empty file could produce ((name, mtime, 0)) is what makes the FIRST post-sign
# verb that CREATES one of these files register as a change.
_ABSENT = "absent"


class ReleaseError(RuntimeError):
    """A release record is present but unreadable/corrupt — fail closed."""


# ── hashing helpers ──────────────────────────────────────────────────────────

def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_lines(lines) -> str:
    """Digest a set/list of str lines, order-independent (sorted first)."""
    return _sha256_bytes("\n".join(sorted(lines)).encode("utf-8"))


def _sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _load_json(path: Path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _person_name_suffix(folder: str) -> str:
    """Label-independent name for a person-cluster folder.

    'Person_03' -> ''  (no assigned name)
    'Person_03_Jane_Harding' -> 'Jane_Harding'
    Anything not matching the Person_NN structural prefix is returned verbatim.

    Keyed on the on-disk folder-name suffix (NOT face_clustering.json's
    cluster_identities) precisely so a same-slug rename — which rewrites
    face_clustering.json but does no os.rename — does not change the key, and a
    membership-preserving renumber (Person_03 -> Person_05) yields an identical
    fingerprint. Assigning a name, or re-clustering (different members), changes
    it → re-sign, correctly.
    """
    parts = folder.split("_", 2)
    if len(parts) >= 2 and parts[0] == "Person" and parts[1].isdigit():
        return parts[2] if len(parts) == 3 else ""
    return folder


# ── the serve-gating canonical slices ────────────────────────────────────────
#
# Each returns a single digest of the sorted, timestamp-free set/map that
# actually gates /media — never the raw churned file.

def _slice_archive_map(md: Path) -> str:
    d = _load_json(md / ARCHIVE_MAP_FILE) or {}
    entries = (d.get("entries") or {}) if isinstance(d, dict) else {}
    return _sha256_lines(f"{k}\0{v}" for k, v in entries.items())


def _slice_video_frame_map(md: Path) -> str:
    d = _load_json(md / VIDEO_FRAME_MAP_FILE) or {}
    keys = d.keys() if isinstance(d, dict) else []
    return _sha256_lines(str(k) for k in keys)


def _slice_dup_gating(md: Path) -> str:
    """member→keeper relations (perceptual_dup_groups) + per-member clean/flagged
    verdicts (dup_member_scan) — the closed allowlist resolve_dup_member_path
    serves duplicates/ through. nudity_flag is the only bit that gates; scan
    timestamps and scores are excluded."""
    lines = []
    groups_doc = _load_json(md / PERCEPTUAL_DUP_GROUPS_FILE) or {}
    groups = groups_doc.get("groups") if isinstance(groups_doc, dict) else groups_doc
    for g in (groups or []):
        if not isinstance(g, dict):
            continue
        keeper = g.get("keeper", "")
        members = sorted(
            str(m.get("file", "")) for m in (g.get("members") or [])
            if isinstance(m, dict))
        lines.append("group\0" + str(keeper) + "\0" + "\0".join(members))
    scan_doc = _load_json(md / DUP_MEMBER_SCAN_FILE) or {}
    members = scan_doc.get("members") if isinstance(scan_doc, dict) else {}
    for path, rec in (members or {}).items():
        if not isinstance(rec, dict):
            continue
        lines.append("member\0%s\0%d" % (
            path, int(bool(rec.get("nudity_flag")))))
    return _sha256_lines(lines)


def _slice_deliverable_audio(md: Path) -> str:
    """The exact set the Recordings page serves — normalized audio file paths
    from case_summary.audio_classifications."""
    d = _load_json(md / CASE_SUMMARY_FILE) or {}
    classifications = d.get("audio_classifications") if isinstance(d, dict) else []
    files = {
        os.path.normpath(a["file"])
        for a in (classifications or [])
        if isinstance(a, dict) and a.get("file")
    }
    return _sha256_lines(files)


def _content_or_absent(path: Path) -> str:
    return _sha256_file(path) if path.exists() and path.is_file() else _ABSENT


# ── the fingerprint ──────────────────────────────────────────────────────────

def fingerprint(paths, mode: str = MODE_STANDARD) -> str:
    """Content-identity digest of the delivered family tree + its serve-gating
    metadata. No mtime, no timestamps, no raw bytes for a churned index.

    `mode` = "standard" (archive/ by membership+size, the ~cheap authoritative
    check) or "deep" (archive/ by content sha256). A --deep signature is
    verified --deep; the mode is recorded in the release record.
    """
    if mode not in (MODE_STANDARD, MODE_DEEP):
        raise ValueError(f"unknown fingerprint mode {mode!r}")
    out = paths.output_dir
    md = paths.metadata_dir
    case_real = os.path.realpath(paths.case_dir)
    lines = ["fpver\0" + str(FINGERPRINT_VERSION), "mode\0" + mode]

    # 1. archive/ — membership + size (standard) or content (deep). One lstat per
    #    entry (not is_symlink()+is_file()+stat(), which was three) — the standard
    #    membership+size digest runs on every authoritative check.
    archive = paths.archive_dir
    if archive.exists():
        for f in archive.rglob("*"):
            try:
                st = f.lstat()
            except OSError:
                continue
            if _stat.S_ISLNK(st.st_mode) or not _stat.S_ISREG(st.st_mode):
                continue
            rel = f.relative_to(archive).as_posix()
            if mode == MODE_DEEP:
                lines.append(f"archive\0{rel}\0{_sha256_file(f)}")
            else:
                lines.append(f"archive\0{rel}\0{st.st_size}")

    # 2. View trees — label-independent grouping content. For each group folder:
    #    (assigned-name suffix, hash of the sorted member canonical targets),
    #    taken as a set across groups so a Person_NN renumber is inert.
    for view in VIEW_DIRS:
        vroot = out / view
        if not vroot.exists():
            continue
        for group in sorted(p for p in vroot.iterdir() if p.is_dir()):
            label = (_person_name_suffix(group.name)
                     if view == "by_person" else group.name)
            targets = set()
            for member in group.rglob("*"):
                if member.is_dir() and not member.is_symlink():
                    continue
                target = os.path.realpath(member)
                try:
                    targets.add(os.path.relpath(target, case_real))
                except ValueError:
                    targets.add(target)
            lines.append(
                f"view\0{view}\0{label}\0{_sha256_lines(targets)}")

    # 3. Non-archive delivered real files — content sha256 (cheap).
    for top in DELIVERED_TOP:
        if top == "archive" or top in VIEW_DIRS:
            continue
        p = out / top
        if p.is_file():
            lines.append(f"file\0{top}\0{_sha256_file(p)}")
        elif p.is_dir():
            for f in p.rglob("*"):
                if f.is_symlink() or not f.is_file():
                    continue
                rel = f.relative_to(out).as_posix()
                lines.append(f"file\0{rel}\0{_sha256_file(f)}")

    # 4. Serve-gating metadata — curation overlays (content) + the five
    #    sensitivity allowlists (canonical slices).
    for name in (DECISIONS_FILE, CURATION_FILE):
        lines.append(f"meta\0{name}\0{_content_or_absent(md / name)}")
    lines.append("serve\0archive_map\0" + _slice_archive_map(md))
    lines.append("serve\0video_frame_map\0" + _slice_video_frame_map(md))
    lines.append("serve\0dup_gating\0" + _slice_dup_gating(md))
    lines.append("serve\0deliverable_audio\0" + _slice_deliverable_audio(md))

    return _sha256_lines(lines)


# ── the visibility stamp ─────────────────────────────────────────────────────

def _stamp_line(case_real: str, path: Path) -> str:
    key = os.path.relpath(str(path), case_real)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return f"{key}\0{_ABSENT}\0{_ABSENT}"
    return f"{key}\0{st.st_mtime_ns}\0{st.st_size}"


def visibility_stamp(paths) -> str:
    """A cheap `(name, mtime_ns, size)` hash over ~15 fixed paths — the live
    tripwire. Trips on every verb the running server applies live: byte-movers
    (release/banish/unjunk/unbanish, discard) via the append-only move-ledger and
    quarantine-manifest, and overlay verbs via the two sidecars. face_clustering
    is folded so a same-slug caption relabel trips it too (a label, not a byte
    egress — escalation serves it anyway, but the tripwire stays honest).

    The action log (family_actions.ndjson) is deliberately EXCLUDED: the signoff's
    own append writes it and would self-stale. The signoff moves no bytes, so it
    writes neither ledger nor manifest — it cannot trip its own gate.

    Every input the FINGERPRINT reads is also stamped — the five serve-gating
    metadata files included — so the tripwire invariant holds exactly: any change
    to what `/media` may serve trips the stamp, escalation recomputes the
    (timestamp-free) fingerprint, and it decides. A byte-neutral `--restart` that
    rewrites those files with new mtimes trips the stamp but the fingerprint slice
    is unchanged, so escalation serves (and caches). Without this, an out-of-band
    edit to a serve-gating map flipped the fingerprint but not the stamp, so the
    live gate kept serving on the stamp fast-path until the next restart.
    """
    out = paths.output_dir
    md = paths.metadata_dir
    case_real = os.path.realpath(paths.case_dir)
    targets = [out / t for t in DELIVERED_TOP]           # top-level + view dirs
    targets += [md / DECISIONS_FILE, md / CURATION_FILE,
                md / FACE_CLUSTERING_FILE]
    targets += [md / LEDGER_NAME, md / QUARANTINE_MANIFEST]
    # the five serve-gating metadata files the fingerprint slices (§2)
    targets += [md / ARCHIVE_MAP_FILE, md / VIDEO_FRAME_MAP_FILE,
                md / PERCEPTUAL_DUP_GROUPS_FILE, md / DUP_MEMBER_SCAN_FILE,
                md / CASE_SUMMARY_FILE]
    return _sha256_lines(_stamp_line(case_real, p) for p in targets)


# ── the record ───────────────────────────────────────────────────────────────

def release_path(paths) -> Path:
    return paths.index(RELEASE_FILE)


def load_release(paths) -> Optional[dict]:
    """Read family_release.json. Absent → None (the caller decides legacy vs
    refuse). Present-but-corrupt → ReleaseError (fail closed — never treated as
    absent, which would silently downgrade a signed case to legacy_unsigned)."""
    p = release_path(paths)
    if not p.exists():
        return None
    try:
        rec = json.loads(p.read_text())
    except (OSError, ValueError) as exc:
        raise ReleaseError(f"{p} present but unreadable: {exc}") from exc
    if not isinstance(rec, dict):
        raise ReleaseError(f"{p} is not a JSON object")
    return rec


def last_release_digest(paths) -> Optional[str]:
    """The delivery_fingerprint recorded in the last `EVENT release` custody line.

    The custody log is append-only and hardened (T0), so this is the tamper-check
    anchor: a record whose delivery_fingerprint disagrees with the last release
    event has been edited after signing. `release_forced_unsigned` and `revoke`
    events do not match (the event token differs).
    """
    log = paths.custody_log
    if not log.exists():
        return None
    digest = None
    prefix = "EVENT  release  "
    try:
        for line in log.read_text().splitlines():
            if line.startswith(prefix):
                rest = line[len(prefix):].split()
                digest = rest[0] if rest else None
    except OSError:
        return None
    return digest


class VerifyResult(NamedTuple):
    ok: bool
    reason: str
    fingerprint: Optional[str]
    escalated: bool


def _fail(reason: str) -> VerifyResult:
    return VerifyResult(False, reason, None, False)


def verify(paths, record, *, live: bool, mode: Optional[str] = None,
           escalation_cache: Optional[dict] = None,
           escalation_lock=None) -> VerifyResult:
    """Is `record` a valid, current release of the tree at `paths`?

    Common gates (both tiers): the record must be for this case, not revoked.

    live=False (AUTHORITATIVE — export E1, server startup E3, verb export E4):
      cross-check the record's fingerprint against the last release custody
      event (record-tamper check), then recompute the content fingerprint and
      require it equal record['delivery_fingerprint'].

    live=True (the TRIPWIRE — family GET surface E5): compute the cheap stamp.
      stamp matches ⇒ serve (fast path). stamp differs ⇒ escalate to the full
      fingerprint and refuse ONLY if that also differs. The escalation verdict is
      memoized against the stamp value in `escalation_cache` (a caller-owned
      single-slot dict): the ~15 s walk runs once per distinct tree state, never
      once per request, and the cache self-invalidates the instant the stamp next
      changes. Pass `escalation_lock` (a caller-owned `threading.Lock`) to make the
      escalation single-flight: the family server is threaded, so without it a
      gallery page firing dozens of concurrent `/media` requests on a stamp-tripped
      tree would launch dozens of concurrent full-tree walks before any populated
      the cache. The fast path (stamp match) stays lock-free. The stamp never
      decides a refusal on its own.
    """
    mode = mode or record.get("fingerprint_mode", MODE_STANDARD)

    if record.get("case_id") != paths.case_id:
        return _fail("record case_id mismatch")
    if record.get("revoked"):
        return _fail("release revoked")

    # Format-version gate — a COMMON gate on purpose, so the live tripwire and the
    # authoritative tier refuse identically. (The stamp carries no version, so
    # without this a long-lived server would keep serving a v1-signed tree while
    # export refused it, and whether the family could still see the archive would
    # depend on whether anyone happened to restart the server.) This must precede
    # the fingerprint comparison so a format change never reports as tampering.
    rec_ver = record.get("fingerprint_version", RECORD_VERSION_DEFAULT)
    if rec_ver != FINGERPRINT_VERSION:
        return _fail(
            f"signed under fingerprint format v{rec_ver}, this build writes "
            f"v{FINGERPRINT_VERSION} — re-sign required (this is a format "
            f"change, NOT evidence the tree was altered)")

    signed_fp = record.get("delivery_fingerprint")

    if not live:
        if last_release_digest(paths) != signed_fp:
            return _fail("record fingerprint disagrees with custody log")
        fp = fingerprint(paths, mode)
        if fp != signed_fp:
            return VerifyResult(False, "tree changed since signing", fp, False)
        return VerifyResult(True, "fingerprint current", fp, False)

    # live tripwire
    stamp = visibility_stamp(paths)
    if stamp == record.get("visibility_stamp"):
        return VerifyResult(True, "stamp match", None, False)

    # stamp tripped — escalate (cached against the stamp value, single-flight)
    def _cached():
        if escalation_cache is not None and escalation_cache.get("stamp") == stamp:
            return VerifyResult(escalation_cache["ok"], "cached escalation",
                                escalation_cache["fingerprint"], True)
        return None

    hit = _cached()
    if hit is not None:
        return hit
    lock = escalation_lock if escalation_lock is not None else contextlib.nullcontext()
    with lock:
        # Re-check under the lock: another thread may have computed it while we
        # waited, so exactly one thread walks the tree per distinct stamp value.
        hit = _cached()
        if hit is not None:
            return hit
        fp = fingerprint(paths, mode)
        ok = fp == signed_fp
        if escalation_cache is not None:
            escalation_cache.clear()
            escalation_cache.update(stamp=stamp, ok=ok, fingerprint=fp)
    reason = "escalated: fingerprint current" if ok else \
        "escalated: tree changed since signing"
    return VerifyResult(ok, reason, fp, True)


# ── the certificate (wet-ink primary; the JSON is the machine's record) ───────

CERTIFICATE_FILE = "release_certificate.md"

_COUNSEL_FOOTER = (
    "This is a discoverable legal instrument recording your attestation. Have "
    "counsel review your release process before you rely on it.")


def _screen_line(machine_screen: dict, record_version: int = None) -> str:
    """The honest, plain statement of what the machine screened for. Stating the
    enabled filters plainly is the honesty that makes it defensible.

    `record_version` is the release record's `fingerprint_version`. A certificate
    can be REPRINTED from an existing record (make_certificate.py) long after it
    was signed, and the point of a reprint is to reproduce what was wet-signed.
    A record written under an earlier screening regime described a screen this
    build no longer performs, so re-rendering it with today's sentence would
    re-issue a signed legal instrument with materially different attested text.
    Say so instead of silently rewording it."""
    ms = machine_screen or {}
    enabled = ms.get("scan_filters_enabled") or []
    if enabled:
        line = ("The sensitivity filters enabled for this case: "
                + ", ".join(enabled))
    else:
        line = ("No sensitivity filters were enabled for this case — the "
                "sensitive-content scan performed no content screening.")
    if record_version is not None and record_version < FINGERPRINT_VERSION:
        line += (f"\n\n> **Reprint notice:** this release was signed under an "
                 f"earlier screening regime (record format v{record_version}); "
                 f"the pipeline additionally ran a perceptual-hash image match "
                 f"that has since been removed. The statement above describes "
                 f"the filters recorded at signing and may not match the "
                 f"originally printed page — the wet-signed printout remains "
                 f"the authoritative artifact.")
    return line


def render_certificate(record: dict, decisions: dict = None) -> str:
    """Render the wet-ink certificate markdown from a release record.

    This is the AUTHORITATIVE artifact once printed and hand-signed; the JSON is
    subordinate. It attests to the ACT and the SPECIFIC dispositions made — never
    "I reviewed everything." "Keep" is told in three honest categories (K13): a
    machine-flagged item kept without a reason gets its OWN line, never absorbed
    into the bulk attestation the machine never applied to it.
    """
    r = record or {}
    actor = r.get("actor") or {}
    disp = r.get("dispositions") or {}
    withheld = r.get("withheld") or {}
    revoked = bool(r.get("revoked"))

    reviewed = (decisions or {}).get("human_review_reviewed") or {}
    waived = (decisions or {}).get("human_review_waived") or {}
    kept_with_note = sorted(k for k, v in reviewed.items()
                            if isinstance(v, dict) and v.get("reason"))
    kept_no_note = sorted(k for k, v in reviewed.items()
                          if not (isinstance(v, dict) and v.get("reason")))

    L = []
    a = L.append
    a("# Family Release Certificate")
    a("")
    if revoked:
        a("> **THIS RELEASE HAS BEEN REVOKED.** It is recorded here for the "
          "custody trail; the family bundle is closed.")
        a("")
    a(f"**Case:** {r.get('case_id', '')}  ")
    a(f"**Released by:** {actor.get('name', '')} — {actor.get('capacity', '')}  ")
    a(f"**Workstation user:** {actor.get('os_user', '')}  ")
    a(f"**Signed at:** {r.get('signed_at', '')}")
    a("")
    a("## What I am attesting to")
    a("")
    a(r.get("attestation", ""))
    a("")
    a("## In my own words")
    a("")
    a("> " + (r.get("judgment", "") or "").replace("\n", "\n> "))
    a("")
    a("## What I personally dispositioned")
    a("")
    a(f"- Quarantined items **released** to the family: "
      f"{disp.get('quarantine_released', 0)}")
    a(f"- Quarantined items **discarded**: {disp.get('quarantine_discarded', 0)}")
    a(f"- Vital documents **confirmed**: {disp.get('vital_confirmed', 0)}"
      + (f" (dismissed {disp['vital_dismissed']}, reassigned "
         f"{disp['vital_reassigned']})"
         if disp.get('vital_dismissed') or disp.get('vital_reassigned') else ""))
    a(f"- Items **banished** (removed from the family view): "
      f"{disp.get('banished', 0)}")
    a("")
    a("### Human-review items — told honestly")
    a("")
    a("These items the machine escalated OUT of the bulk set for individual "
      "attention. They are recorded in three categories, not folded into the bulk "
      "attestation, which never covered them:")
    a("")
    a(f"1. **Retained with a recorded note** ({len(kept_with_note)}): I considered "
      f"these and recorded a reason for keeping each.")
    a(f"2. **Flagged for individual review; retained without individual review or a "
      f"recorded reason** ({len(kept_no_note)}): kept, but I make no representation "
      f"that I read each one.")
    a(f"3. **Waived, with a reason** ({len(waived)}): I made a signed, reasoned "
      f"choice not to review these individually.")
    a("")
    a("_The un-flagged photo, video, and email sets are released under the bulk "
      "attestation above — the machine never escalated them for individual review, "
      "and I do not represent that I read each item._")
    a("")
    a("## What the machine screened for")
    a("")
    a(_screen_line(r.get("machine_screen"),
                   r.get("fingerprint_version", RECORD_VERSION_DEFAULT)))
    a("")
    a("## What was withheld from the family, and why")
    a("")
    rescued = withheld.get("estate_rescued_emails", 0)
    a(f"- **Estate-rescued email withheld by category:** {rescued}. This is mail "
      f"the family-relevance triage had already discarded and the pipeline rescued "
      f"for the estate. It is examiner-only and remains open to me in the examiner "
      f"explorer and full-text search (`tools/search.py`) — I confirmed a category "
      f"rule over a set I can inspect and sample, not a set I could not see.")
    a("")
    a("## The binding")
    a("")
    a(f"- **Delivery fingerprint:** `{r.get('delivery_fingerprint', '')}`  ")
    a(f"- **Fingerprint mode:** {r.get('fingerprint_mode', '')}  ")
    a("- The signature below binds this release to that exact fingerprint. If the "
      "delivered tree changes, the signature is no longer current and the family "
      "surface closes automatically.")
    a("")
    a("## Signature")
    a("")
    a("Signed: ______________________________________    Date: ________________")
    a("")
    a(f"Print name: {actor.get('name', '')}")
    a("")
    a("---")
    a("")
    a(f"_{_COUNSEL_FOOTER}_")
    a("")
    return "\n".join(L)
