"""
wyeast.core.safe_names — centralized filesystem-safe name sanitization.

One sanitizer for every place the pipeline turns a human/LLM-supplied label
(album titles, scene/person names, event/trip names, attachment/export
filenames) into a path component. Previously this logic was duplicated as
``safe_album_dirname`` in both ``wyeast/core/delivery.py`` and
``wyeast/stages/llm_synthesis.py`` (review M9); both now delegate here so
there is a single, testable policy.

Policy (``safe_component``):
  1. Unicode-normalize (NFKD) and drop combining marks, so "Café" -> "Cafe"
     and full-width / decomposed forms fold to plain ASCII-ish letters rather
     than being stripped wholesale.
  2. Replace every run of characters outside the safe set
     ``[A-Za-z0-9 _-]`` (this includes path separators, control chars, and
     punctuation) with a single space.
  3. Collapse internal whitespace to single underscores.
  4. Cap the length to ``max_len`` bytes/chars (default 80), trimming any
     trailing separator left by the cut.
  5. Fall back to ``fallback`` when nothing usable survives.

This is byte-for-byte equivalent to the old ``safe_album_dirname`` for the
ASCII inputs the pipeline produced before; it is *strictly safer* for control
characters and Unicode (which the old regex stripped, sometimes leaving a
mangled stub).

``safe_relpath`` sanitizes each component of a multi-segment relative path and
rejects absolute paths / parent traversal.

``dedupe_name`` / a caller-held ``set`` give a deterministic collision
strategy (``name``, ``name_2``, ``name_3`` ...), matching the existing
de-dup loops in delivery/s13.

stdlib-pure (enforced by ``test_core_package_is_stdlib_pure``).
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath

# Characters allowed verbatim in a path component. Everything else (path
# separators, control chars, punctuation, emoji) collapses to a space and then
# to an underscore. Identical to the old delivery/s13 _UNSAFE_DIRCHARS_RE.
_UNSAFE_DIRCHARS_RE = re.compile(r"[^A-Za-z0-9 _-]+")

# Default maximum length of a single sanitized component (chars). Matches the
# historical [:80] cap used by safe_album_dirname so existing folder names are
# unchanged.
MAX_COMPONENT_LEN = 80


def _strip_unicode(name: str) -> str:
    """NFKD-normalize and drop combining marks so accented/decomposed letters
    fold to their base form ("Café" -> "Cafe") instead of being stripped to a
    stub. Characters with no ASCII fold (e.g. CJK) survive normalization and
    are removed by the unsafe-char regex in the caller, same as before."""
    if not name:
        return ""
    decomposed = unicodedata.normalize("NFKD", name)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def safe_component(name: str, fallback: str = "untitled", *,
                   max_len: int = MAX_COMPONENT_LEN) -> str:
    """Return a single filesystem-safe path component from an arbitrary label.

    See the module docstring for the policy. Never returns ``""`` (returns
    ``fallback`` instead), never contains a path separator, and is capped at
    ``max_len`` characters.
    """
    normalized = _strip_unicode(name or "")
    cleaned = _UNSAFE_DIRCHARS_RE.sub(" ", normalized).strip()
    cleaned = "_".join(cleaned.split())
    cleaned = cleaned[:max_len].strip("_-") if max_len else cleaned.strip("_-")
    return cleaned or fallback


def safe_relpath(rel: str, fallback: str = "untitled", *,
                 max_len: int = MAX_COMPONENT_LEN) -> PurePosixPath:
    """Sanitize each segment of a RELATIVE path independently.

    Splits on both ``/`` and ``\\`` (so a Windows-style attachment name can't
    smuggle a separator through), sanitizes each non-empty segment with
    ``safe_component``, and joins them back. Absolute paths and ``..``
    traversal segments are dropped, never honored — the result is always a
    relative, traversal-free ``PurePosixPath``. An empty result becomes
    ``fallback``.
    """
    if rel is None:
        return PurePosixPath(fallback)
    raw = str(rel).replace("\\", "/")
    parts = []
    for seg in raw.split("/"):
        seg = seg.strip()
        if not seg or seg in (".", ".."):
            continue
        parts.append(safe_component(seg, fallback, max_len=max_len))
    if not parts:
        return PurePosixPath(fallback)
    return PurePosixPath(*parts)


def dedupe_name(name: str, used: set) -> str:
    """Return ``name`` (or ``name_2``, ``name_3`` ...) such that the result is
    not already in ``used``; adds the chosen name to ``used`` and returns it.

    Deterministic collision strategy shared by the delivery/s13 view-folder
    builders so two labels that sanitize to the same component don't clobber
    each other.
    """
    candidate = name
    n = 2
    while candidate in used:
        candidate = f"{name}_{n}"
        n += 1
    used.add(candidate)
    return candidate
