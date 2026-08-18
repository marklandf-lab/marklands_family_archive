"""
wyeast.core.moves — custody-backed, crash-safe file move ledger.

Stages relocate source material (dedup → duplicates/, junk → photos_junk/,
corrupt → output/suspense/, sensitive → quarantine/). A bare ``shutil.move``
across filesystems silently degrades to copy+unlink, so a crash between "file
left source" and "file arrived at dest" can leave the file half-transferred,
in both places, or in neither — with no record of intent and no way to tell
"already moved" from "lost".

This module supplies that record. ``move_tracked`` performs a cross-device-safe
move (hash → copy to temp → fsync → re-hash → atomic ``os.replace`` → unlink
source) bracketed by an append-only ``intent``/``done`` ledger and a
chain-of-custody line. ``move_tracked_result`` is the same move but returns a
``MoveResult`` carrying the verified sha256 alongside the final path, so a
caller that needs the destination's content hash (e.g. collect_dedup's gallery
path_map) can reuse it instead of re-reading every moved byte. ``reconcile``
replays the ledger at stage start and classifies every unfinished entry per
the spec's state table, surfacing an unresolved loss loudly rather than
silently skipping it.

Design notes
------------
* **Stdlib-pure** (no third-party imports) so it imports under every step venv
  — enforced by ``tests/unit/test_invariants.py::test_core_package_is_stdlib_pure``.
* **The ledger is the authoritative move audit trail**, not the custody log.
  A custody line is only ``<sha>  <path>  [<ts>]`` and cannot express src→dst;
  the ledger stores ``{src, dst, sha256, reason, status, ts}``.
* **Why the ledger is NDJSON, not ``atomic_write_json``.** Stages 01/05/11 move
  from worker pools. A whole-file read-modify-write JSON document loses entries
  under concurrency (last writer wins, clobbering interleaved appends). An
  append-only NDJSON file with an ``fcntl.flock``-guarded ``O_APPEND`` write is
  the concurrency-safe substrate: each ``move_tracked`` appends one line, no
  reader-modify-writer race is possible, and a crash mid-append truncates at
  most the final line (skipped on load) rather than corrupting prior records.
  The non-NDJSON outputs this module writes (the unresolved-loss report) DO go
  through ``atomic_write_json``, per the index-write invariant.
* **No source deletion outside the move itself.** The only ``unlink`` is the
  final step of a verified move; ``reconcile`` never deletes source material.
"""

import json
import os
import stat
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from wyeast.core.custody import sha256_of
from wyeast.core.io import atomic_write_json

# fcntl is POSIX-only (the production target is Linux). Import defensively so
# that the module still imports on a non-POSIX dev box; appends fall back to a
# plain O_APPEND write (still atomic for small lines on local filesystems) when
# flock is unavailable.
try:
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None


LEDGER_NAME = "_move_ledger.ndjson"
UNRESOLVED_NAME = "_move_ledger_unresolved.json"

# Ledger entry statuses.
STATUS_INTENT = "intent"
STATUS_DONE = "done"

# reconcile() resolution codes (returned per entry).
R_REDO = "redo"                  # src exists, dst absent — stage will move again
R_PROMOTED = "promoted"          # src absent, dst exists & matches — now done
R_DUPLICATE = "duplicate"        # src & dst exist & match — recovery; do not move
R_PARTIAL = "partial"            # dst exists but hash mismatch — not done
R_UNRESOLVED = "unresolved"      # src absent, dst absent — LOSS
R_DONE = "done"                  # entry already marked done — nothing to do


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def resolve_collision_free(dst) -> Path:
    """
    Return a destination path that does not yet exist, disambiguating like
    s03b/s03c: ``name.ext`` → ``name_1.ext`` → ``name_2.ext`` → ...

    The collision-free path is resolved BEFORE intent is recorded so the ledger
    names the exact final landing spot (amendment 1) — never record ``dst`` and
    silently land at ``dst_2``.
    """
    dst = Path(dst)
    if not dst.exists():
        return dst
    counter = 1
    while True:
        candidate = dst.with_name(f"{dst.stem}_{counter}{dst.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def _same_device(a: Path, b: Path) -> bool:
    """True when paths a and b are proven to live on the same device.

    Compares st_dev of a (must exist) against the device of b's nearest
    existing ancestor (b itself usually does not exist yet).
    """
    try:
        a_dev = os.stat(a).st_dev
    except OSError:
        return False
    probe = b
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            return False
        probe = parent
    try:
        return os.stat(probe).st_dev == a_dev
    except OSError:
        return False


class MoveLedger:
    """
    Append-only NDJSON move ledger at ``output/metadata/_move_ledger.ndjson``.

    Each line is one JSON object: ``{src, dst, sha256, reason, status, ts}``.
    Appends are concurrency-safe via ``fcntl.flock`` over an ``O_APPEND`` write,
    so a worker pool may append without losing entries. The file is never
    rewritten or compacted by this class — it is a pure audit trail.
    """

    def __init__(self, path):
        self.path = Path(path)
        # Lazily-built {src: dst} index of completed moves, for O(1) resume
        # lookups (see done_dst_index). None = not built yet.
        self._done_index: dict | None = None
        self._indexed_size: int = -1

    @classmethod
    def for_metadata_dir(cls, metadata_dir) -> "MoveLedger":
        return cls(Path(metadata_dir) / LEDGER_NAME)

    # ── append ────────────────────────────────────────────────────────────
    def record(self, src, dst, sha256: str, reason: str, status: str) -> dict:
        """Append one ledger entry and return it.

        The write is a single ``O_APPEND`` ``write()`` of one newline-terminated
        JSON line, guarded by an exclusive ``flock`` so concurrent appenders
        serialise rather than interleave or clobber.
        """
        entry = {
            "src": str(src),
            "dst": str(dst),
            "sha256": sha256,
            "reason": reason,
            "status": status,
            "ts": _now_iso(),
        }
        line = json.dumps(entry, sort_keys=True) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # O_APPEND makes each write() land atomically at EOF on local POSIX
        # filesystems; the flock additionally serialises whole writes so we
        # never split a record across an interleaving writer.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                os.write(fd, line.encode("utf-8"))
                os.fsync(fd)
            finally:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        # Keep the resume index current instead of forcing a full re-read on the
        # next lookup. Only `done` entries are indexed (see done_dst_index).
        if self._done_index is not None:
            if status == STATUS_DONE:
                self._done_index[entry["src"]] = entry["dst"]
            try:
                self._indexed_size = self.path.stat().st_size
            except OSError:          # stat failed — force a rebuild next lookup
                self._done_index = None
        return entry

    def record_intent(self, src, dst, sha256, reason) -> dict:
        return self.record(src, dst, sha256, reason, STATUS_INTENT)

    def record_done(self, src, dst, sha256, reason) -> dict:
        return self.record(src, dst, sha256, reason, STATUS_DONE)

    # ── read ──────────────────────────────────────────────────────────────
    def __iter__(self):
        """Yield each parsed ledger entry in append order.

        A trailing partial line (crash mid-append) is silently skipped — only
        complete JSON objects are yielded.
        """
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError:
                    # Truncated final line from a crash mid-append — skip it.
                    continue

    def load(self) -> list:
        """Return all entries as a list (append order)."""
        return list(self)

    def latest_status(self) -> dict:
        """
        Collapse the append-only log to the latest status per (src, dst, sha).

        Returns ``{(src, dst, sha256): entry}`` keeping the last entry seen for
        each move identity, so a later ``done`` supersedes an earlier ``intent``.
        """
        latest: dict = {}
        for entry in self:
            key = (entry["src"], entry["dst"], entry["sha256"])
            latest[key] = entry
        return latest

    def done_dst_index(self) -> dict:
        """``{src: dst}`` for completed moves — O(1) resume lookups.

        Callers resolving "was this source already moved?" once per file must
        NOT use ``latest_status()``: it re-opens and re-parses the whole ledger
        on every call, so a per-file loop over N sources against an L-entry
        ledger costs N full reads and N×L JSON parses. On a 31k-file case that
        measured ~460M parses and ~879 GB of re-reads, pinning one core for
        ~30 minutes inside ``collect_dedup``'s serial phase.

        This index is built once and then maintained incrementally by
        ``record()``, so the same workload is O(N+L).

        Staleness: the index is rebuilt whenever the file's size no longer
        matches what we last accounted for, which covers a ledger written by a
        previous (crashed) run and any external appender. Within a run this
        class is the only writer, and its own appends update the index in place.
        """
        try:
            size = self.path.stat().st_size if self.path.exists() else 0
        except OSError:
            size = 0
        if self._done_index is None or size != self._indexed_size:
            idx: dict = {}
            for entry in self:
                if entry.get("status") == STATUS_DONE:
                    idx[entry["src"]] = entry["dst"]
            self._done_index = idx
            self._indexed_size = size
        return self._done_index


def done_dst_for_src(ledger, src):
    """FINAL dst of a completed (`done`) tracked move for `src`, else None.

    Per-phase resume: before moving a file, skip it when the ledger already
    holds a `done` entry whose `src` matches and whose recorded dst still
    exists. The recorded dst is authoritative — ``move_tracked`` already
    disambiguated it.

    Uses the O(1) ``done_dst_index()`` rather than ``latest_status()``: this is
    called once per source file inside a serial move phase, and re-parsing the
    whole ledger per file makes the phase quadratic in corpus size (see
    ``MoveLedger.done_dst_index``). That matters far more for mail dedup, where
    the move set can run to hundreds of thousands of files.
    """
    if ledger is None:
        return None
    e_dst = ledger.done_dst_index().get(str(src))
    if e_dst is None:
        return None
    dst = Path(e_dst)
    return dst if dst.exists() else None


class MoveResult(NamedTuple):
    """Outcome of a tracked move.

    ``sha256`` is the source's digest, and it IS the destination's content
    hash: the same-device path relocates the very same bytes atomically, and
    the cross-device path verifies the temp copy's hash equals the source hash
    before ``os.replace`` lands it. Callers may therefore reuse ``sha256`` as
    the destination hash instead of re-reading the moved file.
    """
    final: Path
    sha256: str


def move_tracked_result(src, dst, *, reason: str, ledger: MoveLedger,
                        custody) -> MoveResult:
    """
    Move ``src`` → a collision-free destination derived from ``dst``, recording
    ``intent`` before and ``done`` after, plus a chain-of-custody line.

    Algorithm (amendments 1 & 2):
      1. Resolve the collision-free FINAL path first; record ``intent`` against
         that exact path.
      2. Cross-device-safe move: hash src → copy to ``final.tmp.<pid>`` →
         ``fsync`` → re-hash temp, abort if it differs from the source hash →
         replay the source's mode/mtime onto the temp → atomic
         ``os.replace(tmp, final)`` → ``unlink(src)``. (A same-device
         ``os.replace`` would suffice, but the copy+verify path is used
         universally so a cross-device move can never silently lose data.)
         The metadata replay makes the two branches equivalent: ``os.replace``
         keeps the inode and so preserves mode and mtime for free, while a
         fresh-inode copy would otherwise take the umask and the copy time.
      3. Record the custody line and append ``done``.

    Returns a ``MoveResult`` (final destination Path + verified sha256).
    Raises on any verification failure, leaving the source intact and the
    ledger showing ``intent`` (so ``reconcile`` can recover).
    """
    src = Path(src)
    final = resolve_collision_free(dst)

    sha = sha256_of(src)
    ledger.record_intent(src, final, sha, reason)

    final.parent.mkdir(parents=True, exist_ok=True)

    if _same_device(src, final):
        # Same device: a single os.rename is itself atomic and crash-safe.
        os.replace(str(src), str(final))
    else:
        # Stat BEFORE the copy: the source is unlinked at the end of this branch,
        # and its mode/mtime have to be replayed onto the temp before the replace.
        src_st = _lstat_or_none(src)
        tmp = final.with_name(f"{final.name}.tmp.{os.getpid()}")
        _copy_file(src, tmp)
        # Verify the bytes landed intact before destroying the source.
        tmp_sha = sha256_of(tmp)
        if tmp_sha != sha:
            # Partial / corrupt copy — clean the temp and abort. Source is
            # untouched; the ledger still reads `intent` for reconcile.
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise IOError(
                f"move_tracked verify failed for {src} → {final}: "
                f"temp hash {tmp_sha} != source {sha}")
        # Replay the source's mode and timestamps onto the temp BEFORE the
        # replace, so `final` is never visible with copy-default metadata.
        # Ordering: after the hash verify (a restrictive mode would block the
        # read), before the replace (which needs write on the directory, not on
        # the file, so a mode-000 result is still landable).
        _preserve_metadata(src_st, tmp)
        os.replace(str(tmp), str(final))
        # Source byte-for-byte preserved at `final`; remove the original. This
        # is the ONLY source unlink, and only after a verified atomic replace.
        os.remove(str(src))

    custody.record(sha, final)
    ledger.record_done(src, final, sha, reason)
    return MoveResult(final, sha)


def move_tracked(src, dst, *, reason: str, ledger: MoveLedger, custody) -> Path:
    """Tracked move returning only the final destination Path.

    Thin wrapper over ``move_tracked_result`` (see it for the algorithm and
    guarantees) kept for the many call sites that don't need the hash.
    """
    return move_tracked_result(src, dst, reason=reason, ledger=ledger,
                               custody=custody).final


def _lstat_or_none(path):
    """``os.lstat`` that returns None instead of raising. Never fails a move."""
    try:
        return os.lstat(str(path))
    except OSError:
        return None


def _preserve_metadata(src_st, dst: Path) -> None:
    """Replay a source's permission bits and timestamps onto the moved copy.

    The same-device branch of ``move_tracked_result`` is an ``os.replace``, which
    keeps the inode and therefore mode, mtime and ownership for free. The
    cross-device branch builds a NEW inode with ``open``/``write``, so without
    this the destination silently takes the process umask (typically 0644) and
    the wall-clock time of the copy. A file moved to ``output/suspense/`` or
    ``duplicates/`` across a device boundary would come out with different
    metadata than it went in with — and which branch ran depends only on where
    the case tree happens to be mounted, so the same corpus could behave
    differently on two workstations.

    Both fields are load-bearing here, not cosmetic:

    * **mode** — the permission ledger records a file's mode as evidence; a move
      that rewrites it makes that record wrong, and it is exactly the property
      the read-only handling work exists to keep honest.
    * **mtime** — recorded as ``fs_mtime`` in ``metadata_index.json``, used to
      order pages in ``ocr``, and compared as a copy-verification signal in
      ``build_archive``. Replacing it with the copy time destroys the file's
      only remaining date evidence when it carries no EXIF.

    ``st_atime`` is carried along for completeness; ``ns`` variants are used so
    sub-second precision survives. Ownership is deliberately NOT copied: the
    pipeline does not run as root, so ``chown`` to another uid would raise
    ``EPERM`` on every call, and the destination tree is setgid anyway.

    Best-effort by design — a metadata failure must never lose a move that has
    already been byte-verified. Failures are silent here because the caller has
    no logger; the bytes and the ledger are the guarantees this function is not
    allowed to endanger.
    """
    if src_st is None:
        return
    try:
        os.utime(str(dst), ns=(src_st.st_atime_ns, src_st.st_mtime_ns))
    except OSError:
        pass
    try:
        os.chmod(str(dst), stat.S_IMODE(src_st.st_mode))
    except OSError:
        pass


def _copy_file(src: Path, dst: Path, chunk_size: int = 1 << 20) -> None:
    """Stream-copy src → dst and fsync the destination to durable storage.

    Stdlib-only (avoids importing shutil purely for tests that monkeypatch it;
    callers may still patch this symbol to simulate a mid-copy crash).
    """
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        for chunk in iter(lambda: fin.read(chunk_size), b""):
            fout.write(chunk)
        fout.flush()
        os.fsync(fout.fileno())


def classify_entry(entry: dict) -> str:
    """
    Classify a single ledger entry against the reconciliation state table.

    Returns one of the ``R_*`` codes. Hashing is only performed on files that
    exist; the recorded ``sha256`` is the source's pre-move digest.
    """
    if entry.get("status") == STATUS_DONE:
        return R_DONE

    src = Path(entry["src"])
    dst = Path(entry["dst"])
    expected = entry["sha256"]

    src_exists = src.exists()
    dst_exists = dst.exists()

    if dst_exists:
        dst_match = sha256_of(dst) == expected
    else:
        dst_match = False

    # | src    | dst    | hash     | resolution                               |
    if src_exists and not dst_exists:
        return R_REDO                       # redo normally
    if not src_exists and dst_exists and dst_match:
        return R_PROMOTED                   # promote to done
    if src_exists and dst_exists and dst_match:
        return R_DUPLICATE                  # duplicate-recovery; do not double-move
    if dst_exists and not dst_match:
        return R_PARTIAL                    # partial dest; not done
    if not src_exists and not dst_exists:
        return R_UNRESOLVED                 # unresolved loss
    # Defensive: any unhandled combination is treated as unresolved rather than
    # silently passing.
    return R_UNRESOLVED                     # pragma: no cover


class UnresolvedMoveLoss(RuntimeError):
    """Raised by reconcile() when a tracked move's file is at neither src nor dst."""


def reconcile(ledger: MoveLedger, *, partial_policy: str = "quarantine") -> dict:
    """
    Replay the ledger at stage start and classify every unfinished move.

    Implements the spec's full state table. For each ``intent`` entry not yet
    superseded by a ``done``:

      * ``src exists, dst absent``  → ``redo``       (stage will move again)
      * ``src absent, dst match``   → ``promoted``   (append a ``done`` entry)
      * ``src exists, dst match``   → ``duplicate``  (recovery; do NOT double-move)
      * ``dst exists, hash mismatch`` → ``partial``  (partial dest; not done)
      * ``src absent, dst absent``  → UNRESOLVED LOSS (write the report + RAISE)

    ``partial_policy`` is recorded for the operator but this function never
    deletes source material (compliance) — it only logs the partial temp/dest
    under the named policy for manual handling. Promotions are written as new
    ``done`` ledger lines so the audit trail stays append-only.

    Returns a summary dict ``{resolution_code: [entries...]}``. Raises
    ``UnresolvedMoveLoss`` after persisting ``_move_ledger_unresolved.json`` if
    any entry is an unresolved loss — a lost file must surface, never be
    silently skipped (compliance).
    """
    # Collapse to the latest status per move identity so an entry already
    # promoted to `done` in a prior reconcile run is not re-promoted.
    latest = ledger.latest_status()

    summary: dict = {
        R_DONE: [], R_REDO: [], R_PROMOTED: [], R_DUPLICATE: [],
        R_PARTIAL: [], R_UNRESOLVED: [],
    }
    unresolved: list = []
    promotions: list = []

    for entry in latest.values():
        code = classify_entry(entry)
        summary[code].append(entry)
        if code == R_PROMOTED:
            promotions.append(entry)
        elif code == R_UNRESOLVED:
            unresolved.append(entry)
        elif code == R_PARTIAL:
            entry["_partial_policy"] = partial_policy

    # Persist promotions as append-only `done` lines (audit trail intact).
    for entry in promotions:
        ledger.record_done(
            entry["src"], entry["dst"], entry["sha256"], entry["reason"])

    if unresolved:
        report = {
            "schema_version": 1,
            "ts": _now_iso(),
            "ledger": str(ledger.path),
            "unresolved_count": len(unresolved),
            "entries": unresolved,
            "note": (
                "Each entry's file is at neither src nor dst — a tracked move "
                "lost its file. This requires manual investigation; the "
                "pipeline must not silently proceed."
            ),
        }
        report_path = ledger.path.with_name(UNRESOLVED_NAME)
        atomic_write_json(report_path, report)
        raise UnresolvedMoveLoss(
            f"{len(unresolved)} tracked move(s) lost their file (neither src "
            f"nor dst present); see {report_path}")

    return summary
