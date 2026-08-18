#!/usr/bin/env python3
"""
export_delivery.py — materialize the client delivery tree for handoff.
Digital Estate Recovery Service | Zone B | venv-phase1

The working delivery tree (output/) keeps one physical copy per file in
archive/ and relative symlinks everywhere else, which is compact on /cases but
breaks on the media families actually receive (exFAT/NTFS USB drives, cloud
uploads, zips — all symlink-hostile). This tool writes a fresh, self-contained
export where every symlink has been materialized into a real file.

What ships is an ALLOWLIST (INCLUDE_TOP below), not a denylist: a top-level entry
in output/ is delivered only if it is NAMED there. Anything else — the metadata
indexes, the examiner's artifacts, the archive server's working trees, a stray
backup directory someone left behind — is withheld by default. Items still in
quarantine/ are outside output/ and so are naturally excluded.

This is the boundary between the fiduciary's workbench and a grieving family's
USB stick. It has failed open twice. Read the comment on INCLUDE_TOP before
adding a name to it.

Usage:
  export_delivery.py CASE_001 --dest /mnt/usb/CASE_001_delivery
  export_delivery.py CASE_001 --dest /export/CASE_001 --copy   # force copy (no hardlink)
"""

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from wyeast.core.config import cases_root, load_pipeline_config
from wyeast.core.custody import ChainOfCustody
from wyeast.core.paths import CasePaths
from wyeast.core import release

# Where the "released without signature" marker lands. It goes into the EXAMINER
# subtree, never the family tree — the accountability lives on the workstation,
# and INCLUDE_TOP does not deliver examiner/ (see the allowlist above).
FORCED_MARKER = "release_forced_unsigned.txt"


def _delivery_blocked_reasons(paths: Path) -> list:
    """Export-gate reasons, read straight from case_summary.json (mirrors
    _archive_data.family_block_reasons — replicated here to keep this tool off
    the tools/ import graph). [] when delivery is allowed."""
    summary_path = paths.metadata_dir / "case_summary.json"
    try:
        summary = json.loads(summary_path.read_text())
    except (OSError, ValueError):
        return []
    gate = (summary or {}).get("export_gate", {}) or {}
    if gate.get("delivery_blocked"):
        return gate.get("reasons") or [gate.get("waiver_reason", "delivery blocked")]
    return []

# WHAT THE FAMILY RECEIVES. Nothing else. This is an ALLOWLIST, and that is the
# whole point: an artifact written to output/ by some future stage is WITHHELD by
# default, and stays withheld until a human decides it is for the family and adds
# it here. The test asserts the exported tree against this set exactly, so adding
# an output without deciding its audience fails the build instead of shipping.
#
# It replaced a denylist of three ({metadata, suspense, examiner}), which failed
# open toward the family — a new top-level artifact was delivered BY DEFAULT. It
# failed that way twice: the examiner's reconciliation_review.md shipped to the
# family, and so did output/email_threads/thread_*.html, containing the full
# rendered bodies of ~29k estate-rescued marketing and platform emails. Real
# cases also carry entries the denylist never contemplated — family_banished/
# (items the family REMOVED), family_export/ (the archive server's own export
# staging), and hand-made backup directories — all of which it shipped.
#
# Every name here is a deliberate decision about a grieving family's USB stick.
#
#   NOT listed, and why:
#     metadata/    the indexes — including _archive_fts_*.sqlite, a plaintext
#                  full-text database of every OCR'd document and email body.
#                  Never onto an unencrypted stick (family-archive-full-text-search.md).
#     examiner/    the fiduciary's artifacts (reconciliation_review.md, …).
#     suspense/    corrupt/unreadable source files held for the examiner.
#     family_banished/, family_export/   family_archive's working trees.
#     email_threads_examiner/   the examiner's conversation pages: the same
#                  bodies, minus the audience filter.
INCLUDE_TOP = {
    "archive",              # the delivered photo/video originals
    "all_photos_by_scene",  # scene_classify's symlink views
    "by_event",             # geo_cluster's event albums
    "by_person",            # face_cluster's people views
    "audio",                # delivered audio + transcripts
    "documents",            # llm_synthesis's categorized documents
    "email_threads",        # the FAMILY's conversation pages (audience-filtered)
    "explorer",             # the static family explorer bundle
    "case_report.html",     # the family-facing report
}

# The explorer bundle's directory name does not encode its role — `build_explorer
# --role examiner` writes an examiner bundle to whatever --out it is given. If
# that landed at output/explorer, the allowlist would happily ship the examiner's
# view of the case. The bundle records its own role, so we check it.
EXPLORER_MANIFEST = "build_manifest.json"


def check_explorer_role(output_dir: Path) -> None:
    """Refuse to export an EXAMINER explorer bundle to the family.

    output/explorer is in the allowlist because it is normally the family's
    bundle. Nothing in the path says so, though — only the manifest inside it
    does. A bundle built with --role examiner sitting at that path would ship the
    examiner's view (including estate-rescued mail) to the family, and the
    allowlist alone cannot see that. So we ask the bundle who it is for.
    """
    manifest = output_dir / "explorer" / EXPLORER_MANIFEST
    if not manifest.exists():
        return
    try:
        role = json.loads(manifest.read_text()).get("role")
    except (OSError, ValueError) as exc:
        print(f"ERROR: cannot read {manifest} ({exc}) — refusing to export an "
              f"explorer bundle whose audience cannot be confirmed.",
              file=sys.stderr)
        sys.exit(1)
    if role and role != "family":
        print(f"ERROR: output/explorer was built with --role {role}, not "
              f"'family' — it shows the examiner's view of the case and must "
              f"not be delivered.\n"
              f"       Rebuild it first:\n"
              f"         python3 -m tools.build_explorer CASE_ID --role family "
              f"--out <case>/output/explorer",
              file=sys.stderr)
        sys.exit(1)


def materialize(src: Path, dest: Path, allow_hardlink: bool) -> str:
    """Place src's real content at dest. Hardlink when permitted and same FS,
    else copy. Returns "link" or "copy"."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    real = Path(os.path.realpath(src))
    if allow_hardlink:
        try:
            os.link(str(real), str(dest))
            return "link"
        except OSError:
            pass
    shutil.copy2(str(real), str(dest))
    return "copy"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Materialize the client delivery tree")
    ap.add_argument("case_id", help="Case ID, e.g. CASE_001")
    ap.add_argument("--dest", required=True, help="Destination directory for the export")
    ap.add_argument("--cases-root", default=None,
                    help="Override the cases root (default: pipeline_config paths.cases)")
    ap.add_argument("--copy", action="store_true",
                    help="(default now) copy every file; kept for back-compat")
    ap.add_argument("--allow-hardlink", action="store_true",
                    help="Opt back into hardlinking on a same-filesystem dest. "
                         "NOT a frozen snapshot — the export then shares inodes "
                         "with the live source, so the TOCTOU re-verify cannot "
                         "protect it. Use only for a throwaway same-box copy.")
    ap.add_argument("--force-unsigned", action="store_true",
                    help="Deliver WITHOUT a current release signature. Records a "
                         "durable chain-of-custody event and an examiner-tree "
                         "marker; requires --reason.")
    ap.add_argument("--reason", default=None,
                    help="Required with --force-unsigned: why this is being "
                         "delivered without a signature.")
    args = ap.parse_args(argv)

    root = args.cases_root or cases_root(load_pipeline_config(None))
    paths = CasePaths.from_case_id(args.case_id, root)
    output_dir = paths.output_dir
    dest_root = Path(args.dest)

    if not output_dir.exists():
        print(f"ERROR: {output_dir} not found — has the pipeline run?", file=sys.stderr)
        sys.exit(1)
    if dest_root.exists() and any(dest_root.iterdir()):
        print(f"ERROR: destination {dest_root} exists and is not empty", file=sys.stderr)
        sys.exit(1)

    # ── The machine export gate (unchanged in meaning; now actually enforced
    #    here, closing the README:670 doc-not-code gap). --force-unsigned is a
    #    SIGNATURE override; it never bypasses a blocked delivery gate.
    blocked = _delivery_blocked_reasons(paths)
    if blocked:
        print("ERROR: delivery is blocked by export_gate.delivery_blocked:",
              file=sys.stderr)
        for r in blocked:
            print(f"  • {r}", file=sys.stderr)
        sys.exit(3)

    # ── The release-signature gate (E1). The fingerprint is verified over the
    #    SOURCE (live=False, authoritative). Absent record ⇒ legacy_unsigned.
    if args.force_unsigned and not args.reason:
        print("ERROR: --force-unsigned requires --reason \"<text>\"", file=sys.stderr)
        sys.exit(1)

    try:
        record = release.load_release(paths)
    except release.ReleaseError as exc:
        print(f"ERROR: {exc} — refusing to export a case whose release record "
              f"cannot be read.", file=sys.stderr)
        sys.exit(1)

    mode = (record or {}).get("fingerprint_mode", release.MODE_STANDARD)
    if record is None:
        if not args.force_unsigned:
            print("ERROR: this case has no release signature "
                  "(output/metadata/family_release.json is absent).\n"
                  "       A named human must sign the family bundle before it "
                  "ships:  python3 -m tools.sign_release " + args.case_id + "\n"
                  "       Or deliver unsigned on the record with "
                  "--force-unsigned --reason \"<text>\".", file=sys.stderr)
            sys.exit(1)
    else:
        result = release.verify(paths, record, live=False)
        if not result.ok and not args.force_unsigned:
            print(f"ERROR: the release signature is not current — {result.reason}.\n"
                  f"       The delivered tree no longer matches what was signed. "
                  f"Re-sign the case, or override with --force-unsigned --reason.",
                  file=sys.stderr)
            sys.exit(1)

    # F0: the authoritative fingerprint of the SOURCE, captured before the copy
    # so we can prove the source did not change under us (TOCTOU).
    f0 = release.fingerprint(paths, mode)

    allow_hardlink = args.allow_hardlink   # default False → an independent copy
    files = links = broken = skipped = 0

    check_explorer_role(output_dir)

    for src in sorted(output_dir.rglob("*")):
        rel = src.relative_to(output_dir)
        if rel.parts and rel.parts[0] not in INCLUDE_TOP:
            skipped += 1
            continue
        dest = dest_root / rel
        if src.is_dir() and not src.is_symlink():
            dest.mkdir(parents=True, exist_ok=True)
            continue
        # Symlink or regular file → materialize the underlying content.
        if src.is_symlink() and not Path(os.path.realpath(src)).exists():
            print(f"  WARNING: broken link skipped: {rel}")
            broken += 1
            continue
        kind = materialize(src, dest, allow_hardlink)
        files += 1
        if kind == "link":
            links += 1

    # F1: re-verify over the SOURCE (not the dest — it has no metadata/). If the
    # source changed under us during the minutes-long copy, the delivered tree is
    # not the signed tree; discard it rather than ship a torn snapshot.
    f1 = release.fingerprint(paths, mode)
    if f1 != f0:
        print("ERROR: the source tree changed during export (F0 != F1) — a verb "
              "or --restart ran mid-copy. Discarding the partial export.",
              file=sys.stderr)
        shutil.rmtree(dest_root, ignore_errors=True)
        sys.exit(1)

    if args.force_unsigned:
        _record_forced_unsigned(paths, args.reason, f0)

    print(f"Exported {files} file(s) to {dest_root} "
          f"({links} hardlinked, {files - links} copied; "
          f"{broken} broken link(s) skipped, {skipped} processing file(s) excluded).")
    print("Delivery tree is symlink-free and self-contained.")
    if args.force_unsigned:
        print("*** RELEASED WITHOUT SIGNATURE — recorded to chain_of_custody.log "
              "and output/examiner/. ***")


def _record_forced_unsigned(paths, reason: str, fingerprint: str) -> None:
    """Durable custody event + examiner-tree marker for an unsigned delivery.

    A hard block would only move unsigned delivery to `cp -r`, which leaves no
    record. So the sanctioned unsigned path is made WORSE (it brands itself),
    not impossible. The marker lands in output/examiner/ — never in the family
    tree (INCLUDE_TOP does not ship it), consistent with the design's OQ1.
    """
    ChainOfCustody(paths.custody_log).record_event(
        "release_forced_unsigned",
        f"{fingerprint} reason={reason!r} os_user={_os_user()}")
    examiner_dir = paths.output_dir / "examiner"
    examiner_dir.mkdir(parents=True, exist_ok=True)
    (examiner_dir / FORCED_MARKER).write_text(
        "RELEASED WITHOUT SIGNATURE\n"
        f"case: {paths.case_id}\n"
        f"reason: {reason}\n"
        f"delivery_fingerprint: {fingerprint}\n"
        f"os_user: {_os_user()}\n"
        "This delivery was made without a named human's release signature. "
        "The accountability record is chain_of_custody.log on the workstation.\n")


def _os_user() -> str:
    try:
        import getpass
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER", "unknown")


if __name__ == "__main__":
    main()
