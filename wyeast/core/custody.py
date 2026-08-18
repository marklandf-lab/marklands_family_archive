"""
wyeast.core.custody — chain-of-custody logging.

Compliance constraint: key file moves and index writes are recorded in
logs_CASEID/chain_of_custody.log. The line format below is the one already
present in production custody logs and MUST stay stable for auditability:

    <sha256>  <path>  [<ISO-8601 timestamp, seconds precision>]
"""

import hashlib
import os
from datetime import datetime
from pathlib import Path

# fcntl is POSIX-only (the production target is Linux). Import defensively so
# the module still imports on a non-POSIX dev box, matching wyeast.core.moves;
# without flock the O_APPEND write is still atomic for small lines on local
# filesystems.
try:
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None


def sha256_of(path, chunk_size: int = 1 << 20) -> str:
    """SHA-256 hex digest of a file, streamed."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


class ChainOfCustody:
    """Append-only writer for a case's chain_of_custody.log."""

    def __init__(self, log_path):
        self.log_path = Path(log_path)

    def _append(self, line: str) -> None:
        """Durably append one newline-terminated line.

        Uses the house ``O_APPEND``+``flock``+``fsync`` idiom (identical to
        ``MoveLedger.record`` and ``family_archive.append_action``): O_APPEND
        makes each write land atomically at EOF on local POSIX filesystems, the
        exclusive flock serialises concurrent writers so records never
        interleave, and the fsync makes the line durable before we return.

        This gives atomic, durable, non-interleaved appends — NOT immutability.
        The log is an ordinary operator-owned file; the same operator who can edit
        ``family_release.json`` could also append a forged ``EVENT release`` line
        here. Tamper-evidence against that comes from Zone B setting ``chattr +a``
        on this log (runbook), which makes it append-only for a non-root user;
        ``verify()``'s cross-check of the record against the last recorded line is
        only as strong as that flag. (Not deployable on every filesystem — OQ5.)
        """
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.log_path,
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
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

    def record(self, sha256: str, path, timestamp: datetime = None) -> None:
        """Append one custody line for an already-computed digest."""
        ts = (timestamp or datetime.now()).isoformat(timespec="seconds")
        self._append(f"{sha256}  {path}  [{ts}]\n")

    def record_file(self, path) -> str:
        """Hash a file and record it; returns the digest."""
        digest = sha256_of(path)
        self.record(digest, path)
        return digest

    def record_event(self, event: str, detail: str = "",
                     timestamp: datetime = None) -> None:
        """Append one custody line for an ACTION A HUMAN TOOK, not a file move.

            EVENT  <event>  <detail>  [<ISO-8601 timestamp>]

        The file-move format above is untouched — a SHA-256 digest is never the
        literal string "EVENT", so the two line kinds stay trivially separable
        for any reader of an existing log.

        This exists for release: when an examiner puts material into the family's
        delivery that the machine did not clear, the examiner IS the screen, and
        that is defensible only if it is recorded. A fiduciary can be personally
        surcharged; "the pipeline decided" is not a thing they can say to a
        beneficiary, and "I reviewed it on this date and released these items"
        is. Record who, when, how much, and what the machine's screen was set to
        at the time.
        """
        ts = (timestamp or datetime.now()).isoformat(timespec="seconds")
        self._append(f"EVENT  {event}  {detail}  [{ts}]\n")
