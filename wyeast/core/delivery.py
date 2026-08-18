"""
wyeast.core.delivery — canonical archive layout + symlink delivery helpers.

The client delivery tree keeps exactly ONE physical copy of every de-duped
photo/video in output/archive/, reconstructed in the family's original gallery
structure (album-name folders, with an _no_album/<year> fallback). Every other
view (all_photos_by_scene/, by_person/, by_event/) is a RELATIVE symlink into
that archive, so a photo that belongs to many scenes/people/albums costs ~1x
disk instead of N copies.

The build_archive stage builds the archive and writes archive_map.json
({working_set_path: canonical_archive_path}); face_cluster/scene_classify/
video_deliver resolve a working file to its canonical copy and symlink it into
the view folders; sensitive_scan reads the map to quarantine matches. A final
export step materializes the symlinks into real files for the (symlink-hostile)
client handoff.

stdlib-pure so it imports under every venv (enforced by the core import test).
"""

import json
import os
import re
from pathlib import Path

from wyeast.core.errors import load_required_index
from wyeast.core.safe_names import safe_component

ARCHIVE_MAP_NAME = "archive_map.json"

NO_ALBUM_DIR = "_no_album"
UNDATED_DIR = "undated"


def safe_album_dirname(name: str, fallback: str = "album") -> str:
    """Filesystem-safe folder name from an album title.

    Thin alias over the centralized ``wyeast.core.safe_names.safe_component``
    (review M9). The sanitization policy (strip path-unsafe chars, collapse
    whitespace to underscores, cap length, Unicode-fold) now lives in
    safe_names; this previously carried a duplicate of s13's copy. Behaviour is
    equivalent for the ASCII album/event titles the pipeline produces.
    """
    return safe_component(name, fallback)


def _year_from_timestamp(timestamp) -> str | None:
    """Year token (YYYY) from an ISO-8601 timestamp string, or None."""
    if not timestamp or not isinstance(timestamp, str):
        return None
    m = re.match(r"(\d{4})", timestamp)
    return m.group(1) if m else None


def canonical_relpath(record: dict, filename: str) -> Path:
    """Canonical archive path for one file, RELATIVE to the archive root.

    Album membership wins: the file is filed under its first album (sorted, for
    determinism). Files with no album fall back to _no_album/<year> by capture
    timestamp, or _no_album/undated when no usable date exists.

    `record` is the file's metadata_index.json entry; `filename` is the
    basename used in the archive.
    """
    albums = record.get("album_membership") or []
    albums = [a for a in albums if a and str(a).strip()]
    if albums:
        first = sorted(albums)[0]
        return Path(safe_album_dirname(first)) / filename

    year = _year_from_timestamp(record.get("timestamp"))
    if record.get("timestamp_confidence") not in ("high", "medium"):
        year = None
    bucket = year if year else UNDATED_DIR
    return Path(NO_ALBUM_DIR) / bucket / filename


def extra_album_relpaths(record: dict, filename: str) -> list[Path]:
    """Relative archive paths for a file's NON-primary albums (the albums that
    get a symlink rather than the physical copy). Empty for single/no-album."""
    albums = sorted({a for a in (record.get("album_membership") or [])
                     if a and str(a).strip()})
    if len(albums) < 2:
        return []
    return [Path(safe_album_dirname(a)) / filename for a in albums[1:]]


def relative_symlink(link_path: Path, target: Path) -> None:
    """Create link_path as a RELATIVE symlink to target (an existing file).

    Relative so the whole output/ tree relocates intact. Replaces an existing
    link of the same name (idempotent re-runs). Parent dirs are created.
    """
    link_path.parent.mkdir(parents=True, exist_ok=True)
    rel = os.path.relpath(os.path.realpath(target), start=link_path.parent)
    if link_path.is_symlink() or link_path.exists():
        try:
            link_path.unlink()
        except OSError:
            pass
    os.symlink(rel, link_path)


def symlink_into(view_dir: Path, target: Path, name: str | None = None) -> Path:
    """Relative-symlink `target` into `view_dir`, disambiguating name
    collisions (group_, _1, ... like the copy/hardlink helpers). Returns the
    link path created."""
    view_dir.mkdir(parents=True, exist_ok=True)
    base = name or target.name
    link = view_dir / base
    counter = 1
    stem, suffix = Path(base).stem, Path(base).suffix
    while link.exists() or link.is_symlink():
        # An existing link already pointing at the same target is a no-op reuse.
        if link.is_symlink() and os.path.realpath(link) == os.path.realpath(target):
            return link
        link = view_dir / f"{stem}_{counter}{suffix}"
        counter += 1
    relative_symlink(link, target)
    return link


def load_archive_map(metadata_dir: Path) -> dict[str, str]:
    """Load archive_map.json ({working_set_path: canonical_archive_path}).

    Returns {} when the file is ABSENT (stage 03c skipped or no media) — a
    legitimate "nothing to map" state.

    RAISES ``RequiredDataError`` on a present-but-unreadable/unparseable file
    (review H5/M5): a corrupt archive map must never be silently swallowed as "no
    mappings", because doing so lets flagged photos/videos stay in the delivery
    tree (quarantine fails open). Callers distinguish a missing map (fine on its
    own → ``{}``) from a corrupt one (fail-closed → raises) by catching
    ``RequiredDataError`` — it subclasses ``RuntimeError`` so existing broad
    ``except Exception`` handlers (e.g. the s11 export-gate brander) still catch it.
    """
    data = load_required_index(Path(metadata_dir) / ARCHIVE_MAP_NAME, missing_ok=True)
    if data is None:            # absent — legitimate "nothing to map"
        return {}
    return data.get("entries", {})


def canonical_for(working_path, archive_map: dict[str, str]) -> Path | None:
    """Resolve a working-set path to its canonical archive Path, or None when
    the file was not archived (e.g. a video frame, or 03c not run)."""
    target = archive_map.get(str(working_path))
    return Path(target) if target else None
