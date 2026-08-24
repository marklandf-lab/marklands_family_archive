"""Load-time path rebasing for a case served away from the machine that made it.

THE PROBLEM. Every pipeline index records ABSOLUTE paths — the ones the
workstation used ("/data/cases/813_mf/extracted/photos/a.jpg"). Those strings are
the case's id space: archive_map is keyed by them, document_classifications names
them, ocr_index joins on them, the UI round-trips them back as `src`. Copy the
delivered output/ tree to another machine and every one of them is a dead path.

Nothing upstream rebases them, and `--cases-root` does not: it only decides where
the case FOLDER is (CasePaths.from_case_id). So a relocated case fails twice, both
times quietly:

  - build_photo_universe drops any tile whose archive canonical fails
    os.path.exists() — that check is how quarantined items stay hidden, so a whole
    gallery of dead canonicals reads as "no photos" rather than as an error;
  - resolve_media_path refuses every other id with "path outside case" (403),
    which _media then masks behind its fallback resolvers' error text.

THE FIX, in two parts, because a delivery is not a copy:

  1. REBASE — swap the recorded case root for the real one. This is all that
     output/-rooted paths need, and archive canonicals are output/-rooted, so it
     is the whole fix for photos and video.

  2. RELOCATE — a delivery carries output/ and leaves the working trees behind, so
     paths under extracted/ have no counterpart to rebase ONTO. The bytes are
     there, just re-filed: extracted/documents/X.pdf ships as
     output/documents/<category>/X.pdf. Basename is the only link back, so the
     match is basename-exact and fails closed on a collision (a name appearing in
     two categories relocates to neither — better an honest 404 than the wrong
     document).

DIRECTION MATTERS. Rebasing is applied on READ so everything downstream — the
existence checks, the resolvers, the OCR joins, search — sees live paths. The
inverse is applied on WRITE so decision sidecars persist in RECORDED form. That
asymmetry is deliberate: a sidecar keyed to wherever the case happened to sit last
Tuesday orphans every decision the family made the next time the folder moves,
whereas one keyed to the recorded id rebases correctly forever.

Rewrites are whole-prefix and anchored at the start of the string — never a
substring replacement — so a path merely QUOTED inside a summary or a message body
is left alone.
"""
import json
import os
from pathlib import Path

# Top-level directories a case root may contain. Used to recognise a recorded root
# from a sample path: the segment after the case id must be one of these, or the
# case id matched something that merely shares its name.
CASE_SUBDIRS = frozenset({
    "original_files", "extracted", "output", "quarantine", "duplicates",
    "sensitive", "suspense", "logs",
})

# (recorded subtree, delivered subtree, directory names to skip while indexing).
# A delivery re-files these into per-category folders under output/; everything
# else either ships in place or does not ship at all.
DELIVERED_RELOCATIONS = (
    ("extracted/documents", "output/documents", ("_ocr_sidecars", "_manual_review")),
    ("extracted/other/audio", "output/audio", ()),
)


def detect_recorded_root(archive_map=None, case_id=None, samples=()):
    """The case root the indexes were written against, or None if it cannot be told.

    archive_map.json's `archive_root` is the reliable anchor — one unambiguous
    absolute path, written by build_archive, present on every case that delivered
    anything. `samples` (a few path strings from any index) is the fallback for a
    case with no archive_map, and needs `case_id` to find the split point.
    """
    root = (archive_map or {}).get("archive_root")
    if isinstance(root, str) and root:
        root = os.path.normpath(root)
        suffix = os.path.join("output", "archive")
        if root.endswith(os.sep + suffix):
            return root[: -(len(suffix) + 1)]
    if not case_id:
        return None
    marker = f"/{case_id}/"
    for s in samples:
        if not isinstance(s, str) or marker not in s:
            continue
        head, _, tail = s.partition(marker)
        first = tail.split("/", 1)[0]
        if head.startswith("/") and first in CASE_SUBDIRS:
            return f"{head}/{case_id}"
    return None


def build_relocations(recorded_root, case_dir, spec=DELIVERED_RELOCATIONS):
    """Basename map for the trees a delivery re-files. Returns (forward, reverse,
    report): forward is {recorded-relative path -> real local path}, reverse is its
    inverse in absolute form, and report is per-tree counts for the startup log.

    A tree whose recorded location EXISTS locally is skipped entirely — this is a
    full case, not a delivery, and its paths need nothing but the prefix swap.
    """
    case_dir = Path(case_dir)
    forward, reverse, report = {}, {}, []
    for recorded_rel, delivered_rel, skip in spec:
        if (case_dir / recorded_rel).exists():
            continue                                  # working tree came along
        delivered_root = case_dir / delivered_rel
        if not delivered_root.is_dir():
            continue
        by_base = {}
        for dirpath, dirnames, filenames in os.walk(delivered_root):
            dirnames[:] = [d for d in dirnames if d not in skip]
            for name in filenames:
                by_base.setdefault(name, []).append(os.path.join(dirpath, name))
        mapped = collided = 0
        for name, candidates in by_base.items():
            if len(candidates) != 1:
                collided += 1                         # ambiguous → relocate neither
                continue
            forward[f"{recorded_rel}/{name}"] = candidates[0]
            reverse[candidates[0]] = f"{recorded_root}/{recorded_rel}/{name}"
            mapped += 1
        report.append({"recorded": recorded_rel, "delivered": delivered_rel,
                       "mapped": mapped, "ambiguous": collided})
    return forward, reverse, report


class _Identity:
    """The no-op rebaser: a case served from where it was produced. Every method is
    a pass-through so the in-place path costs nothing and changes no id."""

    active = False
    recorded_root = None
    relocation_report = ()

    def to_local(self, obj):
        return obj

    def to_recorded(self, obj):
        return obj

    def load_json_file(self, path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)


IDENTITY = _Identity()

# ── the active rebaser ──────────────────────────────────────────────────────────
# Process-global, and it lives HERE rather than in any one reader because more
# than one module reads a case index: tools/_archive_data.load_json handles the
# metadata/ indexes, wyeast/core/audience handles the audience-scoped email and
# conversation indexes. Those two sides JOIN on path (a thread names the message
# files whose bodies email_index carries), so a rebaser installed for only one of
# them silently empties the join — no error, just threads with no messages. One
# registry, one truth.
_ACTIVE = IDENTITY


def install(rebaser):
    """Point every case-index reader at `rebaser`. None restores the no-op."""
    global _ACTIVE
    _ACTIVE = rebaser or IDENTITY
    return _ACTIVE


def active():
    return _ACTIVE


def load_json_file(path):
    """Parse a case index through the active rebaser. Raises like json.load."""
    return _ACTIVE.load_json_file(path)


class PathRebaser:
    """Rewrites recorded paths to local ones (and back). Construct via `for_case`,
    which returns IDENTITY when there is nothing to rebase."""

    active = True

    def __init__(self, recorded_root, case_dir, forward=None, reverse=None,
                 relocation_report=()):
        self.recorded_root = os.path.normpath(str(recorded_root))
        self.case_dir = os.path.normpath(str(case_dir))
        self._old = self.recorded_root + "/"
        self._new = self.case_dir + "/"
        self._old_len = len(self._old)
        self._new_len = len(self._new)
        self._forward = forward or {}
        self._reverse = reverse or {}
        self.relocation_report = relocation_report

    @classmethod
    def for_case(cls, case_dir, recorded_root, spec=DELIVERED_RELOCATIONS):
        """A rebaser for this case, or IDENTITY when the recorded root is unknown
        or already correct."""
        if not recorded_root:
            return IDENTITY
        recorded_root = os.path.normpath(str(recorded_root))
        case_dir = os.path.normpath(str(case_dir))
        if os.path.normcase(recorded_root) == os.path.normcase(case_dir):
            return IDENTITY
        forward, reverse, report = build_relocations(recorded_root, case_dir, spec)
        return cls(recorded_root, case_dir, forward, reverse, report)

    # ── one string ──────────────────────────────────────────────────────────────

    def local(self, s):
        """Recorded path → the real file here. Relocation first (it knows where the
        bytes actually went), plain prefix swap otherwise."""
        if s[: self._old_len] != self._old:
            return s
        rest = s[self._old_len:]
        moved = self._forward.get(rest)
        return moved if moved is not None else self._new + rest

    def recorded(self, s):
        """The inverse, for anything being written back to disk."""
        back = self._reverse.get(s)
        if back is not None:
            return back
        if s[: self._new_len] == self._new:
            return self._old + s[self._new_len:]
        return s

    # ── whole structures ────────────────────────────────────────────────────────

    def to_local(self, obj):
        return _walk(obj, self.local)

    def to_recorded(self, obj):
        return _walk(obj, self.recorded)

    def load_json_file(self, path):
        """Parse a JSON file with paths rewritten DURING the parse.

        The big indexes run to hundreds of megabytes, so a second pass over the
        parsed tree would both cost real time and briefly double an already heavy
        footprint. `object_pairs_hook` fires bottom-up as each object completes, so
        keys and values are rewritten in the single pass the parser was making
        anyway, and nothing is copied.
        """
        rw = self.local

        def hook(pairs):
            return {(rw(k) if type(k) is str else k): _rw_value(v, rw)
                    for k, v in pairs}

        with open(path, encoding="utf-8") as fh:
            data = json.load(fh, object_pairs_hook=hook)
        # A document whose ROOT is a list or a string never reached the hook.
        return _rw_value(data, rw) if not isinstance(data, dict) else data

    def describe(self):
        moved = sum(r["mapped"] for r in self.relocation_report)
        return (f"rebasing {self.recorded_root} -> {self.case_dir}"
                + (f" ({moved} delivered files relocated)" if moved else ""))


def _rw_value(value, rw):
    """Rewrite a value already emitted by the parser. Nested dicts came through the
    hook and are finished; lists have not been visited, so walk those."""
    t = type(value)
    if t is str:
        return rw(value)
    if t is list:
        return [_rw_value(v, rw) for v in value]
    return value


def _walk(obj, rw):
    """Full recursive rewrite of an in-memory structure, keys included. Used for
    the small sidecars, where a copy costs nothing."""
    t = type(obj)
    if t is str:
        return rw(obj)
    if t is dict:
        return {(rw(k) if type(k) is str else k): _walk(v, rw) for k, v in obj.items()}
    if t is list:
        return [_walk(v, rw) for v in obj]
    return obj
