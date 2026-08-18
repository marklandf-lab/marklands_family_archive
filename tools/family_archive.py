#!/usr/bin/env python3
"""
family_archive.py — the Wyeast Family Archive: a local web app for exploring AND
curating a finished case.
Digital Estate Recovery Service | Zone B | venv-phase1

Where build_explorer.py produces a read-only static bundle, this serves the case
over a local HTTP server (127.0.0.1) so it can render media inline AND act on the
archive through auditable, reversible verbs — the "desktop app reading off disk"
the Family Archive design calls for. No Ollama; stdlib http.server only.

Verbs (examiner role; v1):
  Confirm        — resolve the pipeline's low-confidence guesses (no file move)
  Banish         — reversibly hide a delivered item (out of archive/, drops its views)
  Rename         — rename a person (cluster identity + by_person folder) or any view folder
  Demote/Restore — push an item or email thread off the top of the Overview/sort (and back)
  Remove person  — drop a face cluster from the People view
  Export         — copy a chosen subset to disk (materialize, never destructive)
  Review queue   — for sensitive_scan quarantined items: Release (back into delivery)
                   or Discard (drop from the queue)
  (Move — cross person/event re-file — is intentionally deferred to v2.)

Every verb is audited: byte-level moves go through the move ledger + chain of
custody, and every action appends a human-readable line to
output/metadata/family_actions.ndjson (surfaced in the History view, with Undo).

Usage:
  family_archive.py CASE_001 --role examiner          # default
  family_archive.py CASE_001 --role examiner --port 7766
  family_archive.py CASE_001 --role family            # honors export_gate

Exit codes: 0 served · 1 bad args/missing case · 2 not complete · 3 family blocked
"""

import argparse
import errno
import fcntl
import hashlib
import html
import io
import json
import mimetypes
import os
import re
import sys
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from wyeast.core.audience import (  # noqa: E402
    can_see_conversation, correspondent_path, email_index_path,
    filter_email_entries, load_thread_index)
from wyeast.core.config import cases_root, load_case_config, load_pipeline_config  # noqa: E402
from wyeast.core.custody import ChainOfCustody, sha256_of  # noqa: E402
from wyeast.core.delivery import canonical_for, relative_symlink  # noqa: E402
from wyeast.core.io import atomic_write_json  # noqa: E402
from wyeast.core.moves import MoveLedger, move_tracked  # noqa: E402
from wyeast.core.paths import CasePaths, display_person_folder, sanitize_person_name  # noqa: E402
from wyeast.core import release  # noqa: E402

from tools._archive_data import (  # noqa: E402
    accounts_data, actions_history, assert_complete, assert_family_allowed,
    family_block_reasons, correspondents_data, correspondent_duplicate_candidates,
    _duplicate_cluster_id, delivered_basename_index,
    audio_rows, build_doctext_views, build_photo_universe, build_search, build_stacks,
    confirm_queue_data, conversation_detail, document_rows, documents_index,
    file_identity,
    email_rows, email_thread_detail, event_album_titles, event_albums_data,
    load_json, log,
    message_rows, overview_data, people_rows, person_detail, photo_rows,
    person_display_name, places_data, ranked_key, review_data, _identity_name,
    scanned_image_rows, timeline_rows, timeline_data, on_this_day_data, venues_data,
    tokenize, transcript_detail, video_rows, vital_docs_data, vital_doc_item_id,
    near_miss_rows, vital_doc_label,
    quarantine_pager_items, vital_pager_items,
    junk_rows, transparency_data, guided_review_data,
    apply_face_overlay, resolve_merge, is_video_frame, SCENE_LABELS,
    VITAL_DOC_LABELS,
)
from tools import build_fts  # noqa: E402

ASSETS_SRC = _ROOT / "report_assets"
ACTIONS_FILE = "family_actions.ndjson"
DECISIONS_FILE = "family_decisions.json"
BANISHED_DIR = "family_banished"
EXPORT_DIR = "family_export"
# Curation layer (docs/specs/family-archive-curation-layer.md): a pure, additive,
# audited sidecar of examiner favorites / collections / notes, keyed by EXISTING
# item ids (archive_map key / thread_id / conversation_id / doc file). It touches
# nothing authoritative — never a file move, never a pipeline-index edit — so the
# never-destroy invariant and examiner authority are structurally untouched. Written
# ONLY through atomic_write_json under the same cross-process _doc_lock that guards
# family_decisions.json (R-4). Not authoritative: a curation journal, like the audit
# log. Phase 1 is examiner-first (the verbs require_examiner); the family-role
# extension is deferred (Phase 2, gated on the write-path security model).
CURATION_FILE = "curation_layer.json"
CURATION_SCHEMA_VERSION = 1
# Operator/family free text is escaped at every UI sink AND length-capped here at the
# write path (belt and braces) so a pathological title/note can't bloat the sidecar.
MAX_TITLE_LEN = 120
MAX_NOTE_LEN = 2000
# A single add call can't stuff an unbounded id list into one collection.
MAX_COLLECTION_MEMBERS = 20000
VIEW_DIRS = ("by_person", "all_photos_by_scene", "by_event")
# G-13 junk rescue: junk-routed images live under extracted/photos_junk/; un-junking
# moves one back to its original working location and drops a scene view symlink into
# all_photos_by_scene/<UNJUNK_SCENE_FOLDER>/ so the rescued image re-appears.
PHOTOS_JUNK_DIRNAME = "photos_junk"
SCENES_VIEW_DIR = "all_photos_by_scene"
UNJUNK_SCENE_FOLDER = "_rescued"
QUARANTINE_MANIFEST = "quarantine_manifest.json"
# Sidecar lockfile flock'd around every shared-JSON read-modify-write so two
# server instances (or two processes) can't interleave and lose entries (R-4).
VERBS_LOCKFILE = ".family_verbs.lock"
# Bound the lazy per-conversation cache (was unbounded — every conversation ever
# opened stayed resident for process life). LRU-evicted past this cap (R-2).
_CONVERSATION_CACHE_CAP = 256

# Page → (label, theme). One unified theme (heirloom) across all views — #17
# standardizes on the Overview look (reverses the v1.1 mix-by-context theming).
PAGES = [
    ("overview", "Overview", "heirloom"),
    ("people", "People", "heirloom"),
    ("photos", "Photos & Videos", "heirloom"),
    # Event albums (Move Phase 2): named trip clusters. Non-sensitive grouping →
    # both roles; a card opens the photos gallery filtered ?event=<album_id>.
    ("events", "Events", "heirloom"),
    ("timeline", "Timeline", "heirloom"),
    ("places", "Places", "heirloom"),
    ("documents", "Documents", "heirloom"),
    ("correspondence", "Correspondence", "heirloom"),
    ("emails", "Emails", "heirloom"),
    ("correspondents", "Correspondents", "heirloom"),
    ("messages", "Messages", "heirloom"),
    ("recordings", "Recordings", "heirloom"),
    ("accounts", "Online Accounts", "heirloom"),
    # Curation layer (examiner-first): the named collections index. Examiner-only in
    # Phase 1 (the curation verbs are require_examiner); a collection opens as a
    # photos grid filtered ?collection_curation=<slug>.
    ("collections", "Collections", "heirloom"),
    ("review", "Review queue", "heirloom"),
    # G-12 guided first-session review + G-13 junk rescue — examiner power tools.
    ("guided", "Guided review", "heirloom"),
    ("junk", "Junk review", "heirloom"),
    ("history", "History", "heirloom"),
    # Full-text search results page (family-archive-full-text-search.md). A valid
    # route + /api endpoint, but reached via the rail search box — kept out of the
    # nav link list (HIDDEN_FROM_NAV) so it isn't a browsing section.
    ("search", "Search", "heirloom"),
]
# Pages that route + serve an API but must NOT appear as a nav link in the rail.
HIDDEN_FROM_NAV = {"search"}
# history is examiner-only: its action entries embed quarantine/canonical paths and
# sensitivity filter names (release/discard_quarantine record the full entry), which
# a family session must not read. It is also, today, purely the examiner's curation
# audit trail (family runs no verbs). When the curation layer gives the family its
# own verbs, add a separate REDACTED family activity view (label+ts only).
# guided (G-12) and junk (G-13) are examiner power tools: the guided checklist
# composes the examiner-only review surfaces, and the junk grid exposes raw internal
# working paths (a family session sees neither the page nor its /api).
EXAMINER_ONLY = {"review", "history", "guided", "junk", "collections"}
# Legacy / convenience path aliases resolved before page + api dispatch.
# quarantine folded into the Review queue (#15) — keep the path working.
PAGE_ALIASES = {"voicemails": "recordings", "audio": "recordings", "letters": "correspondence",
                "quarantine": "review"}
LETTER_CATEGORIES = {"personal_correspondence", "work_correspondence"}

# The E5 default-closed allowlist. A family GET on an unreleased case serves ONLY
# the page SHELLS (which carry no family bytes — the legacy/refused banner renders
# inside them), /assets/*, and the status endpoint. EVERYTHING else — /media,
# /thumb, and every other /api/* — is a body and is gated. This is K4: allowlist
# the shell, gate everything else; never enumerate body routes (that is how a
# "forgot a place" hole is born).
_PAGE_KEYS = {k for k, _lbl, _th in PAGES}


def _e5_shell_allowed(path: str) -> bool:
    if path == "/" or path.startswith("/assets/") or path == "/api/release-status":
        return True
    key = path.lstrip("/")
    return key in _PAGE_KEYS or key in PAGE_ALIASES

# Stdlib-pure mirror of wyeast.stages.ocr.DEFAULT_DOC_CATEGORIES — the canonical
# movable document categories, used as the fallback when a case dir lacks
# case_config.json (load_case_config RAISES FileNotFoundError, §13.4). We do NOT
# import the heavy stage modules (they pull TensorFlow/PyTorch and break CI).
# `account_credentials` is injected by the pipeline separately and is NEVER a
# movable target (the account_credentials seal, §13.3) — it is not listed here.
DEFAULT_DOC_CATEGORIES = [
    "financial", "legal", "personal_correspondence",
    "medical", "creative_writing", "recipe", "miscellaneous",
]

# Stdlib-pure mirror of the financial-subcategory second-pass names (§14.4). The
# movable financial-subcategory set for a document SUB-category move, used as the
# fallback when a case dir lacks case_config.json (or the key is absent). DISTINCT
# name from llm_synthesis.DEFAULT_FINANCE_SUBCATEGORIES (a list of {name,hint}
# dicts) to avoid the collision — this is a list of plain STRINGS, and we do NOT
# import the heavy stage module (it pulls heavy deps and breaks CI).
FINANCIAL_SUBCATEGORY_NAMES = [
    "receipts_bills_orders", "paystubs", "insurance",
    "banking", "budgets", "retirement_investments",
]

# The shipped default for vital_docs.vital_per_target_k — mirrors config/
# case_config.json and wyeast.stages.embed's read of it. The retrieval cap on
# candidate hits per vital-doc target; an examiner may raise it per case, so it
# is resolved from the case's own config (see ArchiveCase.vital_per_target_k),
# with this as the fallback when the file/key is absent.
VITAL_PER_TARGET_K_DEFAULT = 8

# Section/API pagination (docs/specs/family-archive-pagination.md). The pure
# builders return FULL lists; the slicing + {rows,total,offset,limit} wrapping
# happens here at the section boundary so a capped view is never presented as the
# whole set, and the search index stays complete (fed the uncapped builders).
MAX_PAGE_LIMIT = 2000  # a client can't request a 30k-row payload in one shot
# Sections whose GET returns a paginated envelope. Non-paginated sections
# (overview/people/places/accounts/review/history) pass through unchanged.
PAGINATED_SECTIONS = {"photos", "videos", "emails", "messages", "documents",
                      "correspondence", "correspondents", "junk"}


def _one(params, key, default=""):
    return params.get(key, default)


def _page_window(params):
    """Resolve (offset, limit) from a flat query-string dict, clamping limit to
    [1, MAX_PAGE_LIMIT] and offset to >= 0. Bad/missing values fall back."""
    def _int(key, default):
        raw = params.get(key)
        if raw in (None, ""):
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default
    offset = max(0, _int("offset", 0))
    limit = _int("limit", MAX_PAGE_LIMIT)
    limit = max(1, min(limit, MAX_PAGE_LIMIT))
    return offset, limit


def _paginate(rows, offset, limit):
    """Slice `rows` into a page envelope. `total` is always the full pre-slice
    count so the UI can render an honest 'Showing 1–N of M'."""
    return {"rows": rows[offset:offset + limit], "total": len(rows),
            "offset": offset, "limit": limit}


def _slugify(title):
    """Sanitize an operator-supplied collection title into a URL/file-safe slug.

    Lowercase, non-alphanumerics → single '-', trimmed, capped. Purely a lookup key
    (the human-readable title is stored separately and shown in the UI); an
    all-symbol title degrades to the 'collection' fallback. Uniqueness is enforced by
    the caller (verb_collection_create) — this only shapes the candidate."""
    s = re.sub(r"[^a-z0-9]+", "-", str(title or "").strip().lower()).strip("-")
    return (s or "collection")[:64]


def apply_curation(rows, curation, id_key="id"):
    """Stamp the curation overlay onto row dicts, keyed by each row's `id_key`.

    Additive and non-destructive: only a row that is favorited, in a collection, or
    carries a note is COPIED (shallow) and stamped with `favorite_curation: True`,
    `collections: [slug,...]`, `note: str`; every other row passes through untouched
    (no keys added) so the {rows,...} envelope and existing callers/tests are
    unchanged. Never mutates the cached input dicts — a curated row is a copy, so the
    per-generation `_photo_rows` cache stays clean across requests/roles.

    An empty/absent curation layer is a fast no-op (returns `rows` unchanged), so a
    case that was never curated pays nothing. The curation FAVORITE is deliberately a
    DISTINCT key (`favorite_curation`) from the owner-gallery `favorite` (G-1) so the
    two never clobber each other."""
    if not curation:
        return rows
    favs = curation.get("favorites") or {}
    notes = curation.get("notes") or {}
    membership = {}
    for slug, coll in (curation.get("collections") or {}).items():
        for m in (coll.get("members") or []):
            membership.setdefault(str(m), []).append(slug)
    if not favs and not notes and not membership:
        return rows
    out = []
    for r in rows:
        iid = str(r.get(id_key))
        fav = iid in favs
        cols = membership.get(iid)
        note = (notes.get(iid) or {}).get("text") if iid in notes else None
        if fav or cols or note:
            r = dict(r)
            r["favorite_curation"] = fav
            r["collections"] = sorted(cols) if cols else []
            r["note"] = note
        out.append(r)
    return out


def _apply_photo_view(rows, params):
    """Server-side sort + date-range + owner-gallery filters (favorite/album/
    hidden) for the Photos page. These narrow the FULL set BEFORE the page slice
    (a client-side filter over loaded pages would miss the un-loaded tail), so the
    paginated `total` reflects the filtered count. The event-album filter
    (?event=<album_id>) is server-side too (Move Phase 2). Scene/place/media stay
    client-side over the loaded pages. Never mutates the cached input list.

    Hidden handling (G-1): the owner's hidden photos are EXCLUDED from the main
    grid by default; `hidden=1` reveals them (they stay in the set) so the
    examiner always has a way to see them — never a silent drop."""
    df, dt = _one(params, "date_from"), _one(params, "date_to")
    if df:
        rows = [r for r in rows if (r.get("ts") or "")[:10] >= df]
    if dt:
        rows = [r for r in rows if (r.get("ts") or "")[:10] <= dt]
    if _one(params, "hidden") != "1":
        rows = [r for r in rows if not r.get("hidden")]
    if _one(params, "favorite") == "1":
        rows = [r for r in rows if r.get("favorite")]
    album = _one(params, "album")
    if album:
        rows = [r for r in rows if album in (r.get("albums") or [])]
    # Event-album filter (Move Phase 2): ?event=<album_id> narrows to one event
    # album by the row's EFFECTIVE event_id (placement overlay applied in
    # photo_rows) BEFORE the page slice, so the album's tail is reachable and the
    # paginated `total` is the true album size. Sibling of the person/scene/album
    # server filters; absent → no narrowing.
    event = _one(params, "event")
    if event:
        rows = [r for r in rows if r.get("event_id") == event]
    # Curation-layer virtual filters (examiner curation, distinct from the owner
    # gallery above): the Favorites (star) chip and the per-collection grid. Applied
    # BEFORE the page slice so the filtered tail is reachable and the paginated
    # `total` reflects the filtered count. Both read the overlay keys stamped by
    # apply_curation; absent → the row simply doesn't match.
    if _one(params, "favorite_curation") == "1":
        rows = [r for r in rows if r.get("favorite_curation")]
    coll = _one(params, "collection_curation")
    if coll:
        rows = [r for r in rows if coll in (r.get("collections") or [])]
    reverse = _one(params, "sort", "newest") != "oldest"
    return sorted(rows, key=lambda r: (r.get("ts") or ""), reverse=reverse)


def _photo_facets(rows):
    """Whole-set summary for the Photos page controls (computed from the FULL,
    unfiltered rows so the affordances reflect data in the un-loaded tail, not
    just page 1): whether any favorites exist, the distinct album names, and the
    hidden count. Attached additively to the photos payload — the {rows,total,
    offset,limit} envelope is unchanged."""
    albums = set()
    favorites = hidden = starred = 0
    for r in rows:
        if r.get("favorite"):
            favorites += 1
        if r.get("hidden"):
            hidden += 1
        if r.get("favorite_curation"):  # examiner curation star (distinct from owner favorite)
            starred += 1
        for a in (r.get("albums") or []):
            albums.add(a)
    facets = {"favorites": favorites, "hidden": hidden, "albums": sorted(albums)}
    # `starred` (curation-layer star count) is added ONLY when non-zero so an
    # un-curated case's facets are byte-for-byte what they were before (additive).
    if starred:
        facets["starred"] = starred
    return facets


def _filter_documents(rows, params):
    """Server-side category/subcategory filter for Documents, applied BEFORE the
    page slice so a category's tail is reachable (the full category index is
    computed separately, from all rows, so counts stay correct)."""
    cat = _one(params, "cat")
    if not cat:
        return rows
    out = [r for r in rows if (r.get("category") or "miscellaneous") == cat]
    sub = _one(params, "subcat")
    if cat == "financial" and sub:
        out = [r for r in out if (r.get("subcategory") or "uncategorized") == sub]
    return out


def _filter_correspondents(rows, params):
    """Server-side ?q= / ?sort= for the Correspondents list (#11: thousands of
    correspondents with no in-list search/sort was close to unusable for
    'find person X' without falling back to full-text search). Applied BEFORE
    the page slice so a filtered tail is reachable and `total` reflects the
    filtered count. `q` matches a case-insensitive substring of name OR
    address; `sort` re-orders (default stays correspondents_data's own
    total-desc-then-name order — never re-sorts a plain unfiltered view)."""
    q = _one(params, "q").strip().lower()
    if q:
        rows = [r for r in rows
                if q in (r.get("name") or "").lower() or q in (r.get("address") or "").lower()]
    sort = _one(params, "sort")
    if sort == "name":
        rows = sorted(rows, key=lambda r: (r.get("name") or "").lower())
    elif sort == "recent":
        rows = sorted(rows, key=lambda r: (r.get("last_seen") or ""), reverse=True)
    return rows


def _filter_emails_search(rows, params):
    """Server-side ?q= / date range / ?sort= for the Emails list search box
    (#11: tens of thousands of threads with no in-list search/sort/date filter
    was close to unusable for 'find X' without falling back to full-text
    search first). Applied BEFORE the page slice, alongside (not instead of)
    the existing ?participant= click-through filter. `q` matches a
    case-insensitive substring of the subject OR any participant; the date
    range narrows by the thread's last activity date. `sort` re-orders
    (default stays email_rows' own significance-desc order)."""
    q = _one(params, "q").strip().lower()
    if q:
        rows = [r for r in rows
                if q in (r.get("subject") or "").lower()
                or any(q in (p or "").lower() for p in (r.get("participants") or []))]
    df, dt = _one(params, "date_from"), _one(params, "date_to")
    if df:
        rows = [r for r in rows if (r.get("date_last") or "")[:10] >= df]
    if dt:
        rows = [r for r in rows if (r.get("date_last") or "")[:10] <= dt]
    sort = _one(params, "sort")
    if sort == "recent":
        rows = sorted(rows, key=lambda r: (r.get("date_last") or ""), reverse=True)
    elif sort == "subject":
        rows = sorted(rows, key=lambda r: (r.get("subject") or "").lower())
    return rows


def _filter_emails_by_participant(rows, params):
    """Server-side ?participant=<address> filter for the Emails list (G-6
    click-through from a correspondent card). Applied BEFORE the page slice so the
    filtered tail is reachable and the paginated `total` reflects the filtered
    count. Thread `participants` come in mixed forms — a bare address or a
    'Display Name <address>' string — so we match the correspondent address as a
    case-insensitive SUBSTRING of each participant (catches both). No filter → all
    rows unchanged."""
    addr = _one(params, "participant").strip().lower()
    if not addr:
        return rows
    return [r for r in rows
            if any(addr in (p or "").lower() for p in (r.get("participants") or []))]


def _filter_videos(rows, params):
    """Server-side ?person=<person_id> / ?scene=<name> filter for the Videos
    section (G-11), applied BEFORE the page slice so a filtered tail is reachable
    and the paginated `total` reflects the filtered count. A person chip matches on
    person_id (exact); a scene chip on the raw scene label (exact). Both may be
    combined (a person AND a scene). No filter → all rows unchanged."""
    pid = _one(params, "person")
    if pid:
        rows = [r for r in rows
                if any((p or {}).get("person_id") == pid for p in (r.get("persons") or []))]
    scene = _one(params, "scene")
    if scene:
        rows = [r for r in rows if scene in (r.get("scenes") or [])]
    return rows


def _video_facets(rows):
    """Whole-set summary for the Videos view controls (computed from the FULL,
    unfiltered rows so the person/scene affordances reflect the un-loaded tail):
    the distinct persons ([{person_id, name}], name from the first row that carries
    it) and the distinct scene labels. Attached additively to the videos payload —
    the {rows,total,offset,limit} envelope is unchanged."""
    persons = {}
    scenes = set()
    for r in rows:
        for p in (r.get("persons") or []):
            pid = (p or {}).get("person_id")
            if pid and pid not in persons:
                persons[pid] = p.get("name") or pid
        for s in (r.get("scenes") or []):
            if s:
                scenes.add(s)
    return {"persons": [{"person_id": k, "name": persons[k]} for k in sorted(persons)],
            "scenes": sorted(scenes)}


class VerbError(Exception):
    """Raised by verbs on a guard failure; carries an HTTP-ish code."""
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


# ── case context ────────────────────────────────────────────────────────────────

class ArchiveCase:
    """Holds paths/role/config + cached indexes + the audit primitives. Verbs
    take an instance, so they are unit-testable without a live socket."""

    def __init__(self, paths: CasePaths, role: str, cfg: dict):
        self.paths = paths
        self.role = role
        self.cfg = cfg
        self.ledger = MoveLedger.for_metadata_dir(paths.metadata_dir)
        self.custody = ChainOfCustody(paths.custody_log)
        self._email_by_file = None  # lazily built (email_index.json is large)
        # Dedicated lock for the email_by_file first-touch build so two threads
        # opening thread details concurrently can't both json.load the ~120 MB
        # index (transient multi-GB RSS). Kept SEPARATE from the verb lock so email
        # reads never serialize behind a mutating verb (R-2).
        self._email_lock = threading.Lock()
        # Per-conversation message JSONs, loaded lazily one file at a time (the
        # email_by_file lesson, but per-file — see conversation_by_id). Bounded
        # LRU (OrderedDict) so a long browse can't retain every conversation for
        # process life (R-2). Guarded by its own small lock.
        self._conversation_cache = OrderedDict()
        self._conversation_lock = threading.Lock()
        # Serializes mutating verbs (acquired in do_POST). The server is threaded
        # so concurrent media/page reads don't block each other, but verbs must
        # not run concurrently (ledger/manifest races).
        self._lock = threading.Lock()
        # Full-text-search index lifecycle: the FTS sqlite is built lazily off the
        # request thread on the first /api/search, guarded so two searches can't
        # build concurrently. `_fts_status` is the pollable in-memory state.
        self._fts_lock = threading.Lock()          # guards the status/thread start
        self._fts_build_lock = threading.Lock()    # serializes the actual build
        # Single-flights the E5 escalation: the server is threaded, so a gallery
        # page firing dozens of concurrent /media on a stamp-tripped tree must not
        # launch dozens of concurrent fingerprint walks (release_status → verify).
        self._release_lock = threading.Lock()
        self._fts_status = {"state": "idle"}
        self.load()

    def vital_per_target_k(self):
        """The retrieval cap the embed stage used for THIS case: how many candidate
        hits per vital-doc target were pulled before LLM confirmation. Read from the
        case's own case_config.json (`vital_docs.vital_per_target_k`) — an examiner
        raises it per case precisely when they suspect a document was missed, so it
        is NOT a constant. Falls back to the shipped default (VITAL_PER_TARGET_K_
        DEFAULT) when case_config.json or the key is absent, matching
        wyeast.stages.embed. `self.cfg` is the PIPELINE config, not the case one, so
        this reads the case file directly."""
        try:
            cfg = load_case_config(self.paths.case_dir)
            k = (cfg.get("vital_docs") or {}).get("vital_per_target_k")
        except FileNotFoundError:
            k = None
        try:
            k = int(k)
        except (TypeError, ValueError):
            k = None
        return k if (k and k > 0) else VITAL_PER_TARGET_K_DEFAULT

    def load(self):
        """(Re)load all index state and publish it as a SINGLE atomic reference
        swap. Index attributes (summary, scene_index, …) are served via
        __getattr__ from this dict, so a concurrent reader can never observe a
        half-rebuilt load — it sees either the whole old generation or the whole
        new one."""
        md = self.paths.metadata_dir
        st = {
            "summary": load_json(md / "case_summary.json", {}) or {},
            "metadata_index": load_json(md / "metadata_index.json", {}) or {},
            "scene_index": load_json(md / "scene_index.json", {}) or {},
            "archive_map": load_json(md / "archive_map.json", {}) or {},
            "geo_index": load_json(md / "geo_cluster_index.json", {}) or {},
            "face_clustering": load_json(md / "face_clustering.json", {}) or {},
            "ocr_index": load_json(md / "ocr_index.json", []) or [],
            "transcription_index": load_json(md / "transcription_index.json", []) or [],
            "sensitive": load_json(md / "sensitive_scan_index.json", {}) or {},
            # ROLE-SCOPED. The conversation index is the email gate (build_fts's
            # header comment explains why: everything the family sees is derived
            # from this thread set). The family's excludes estate-rescued mail;
            # the examiner's is the union. They used to share one filename, so
            # whichever role built last decided what BOTH roles saw next.
            "email_threads": load_thread_index(md, self.role),
            # G-6 correspondents: a LIST of per-address aggregates; [] pre-email
            # cases. Role-scoped too, or the family's Correspondents page ranks
            # marketing senders by volume and every card's click-through lands
            # on an empty thread list.
            "correspondent_freq": load_json(
                correspondent_path(md, self.role), []) or [],
            "video_frame_map": load_json(md / "video_frame_map.json", {}) or {},
            # G-11: per-video person/scene facets ({"videos":[...]} or a bare list);
            # {} when video_index hasn't run (older cases) → no chips/facets.
            "video_index": load_json(md / "video_index.json", {}) or {},
            # message_triage output; [] when the stage hasn't run (older cases).
            "conversation_index": load_json(md / "conversation_index.json", []) or [],
        }
        st["universe"] = build_photo_universe(
            st["scene_index"], st["archive_map"], self.role, st["video_frame_map"],
            rescued=self.decisions.get("junk_rescued"),
            scanned_released=self.decisions.get("scanned_released"))
        # Photo stacks (perceptual dup groups): keeper-keyed stack model plus
        # the CLOSED member allowlist — the only duplicates/ paths /media may
        # ever serve. Both absent (→ {}) for pre-index / unscanned cases.
        st["stacks"], st["dup_member_paths"] = build_stacks(
            load_json(md / "perceptual_dup_groups.json", {}) or {},
            load_json(md / "dup_member_scan.json", {}) or {},
            self.ledger.latest_status(), st["universe"], self.paths.case_dir)
        # Surfaced-audio deliverable set. Audio files (extracted/other/audio) are a
        # first-class family section but carry NO archive_map or video_frame_map
        # entry, so the delivered-set gate in resolve_media_path would 403 every
        # recording. They are surfaced from case_summary.audio_classifications — the
        # exact set the Recordings page serves — so precompute their normalized
        # paths here as a third allowed family-servable category alongside frames.
        st["deliverable_audio"] = {
            os.path.normpath(a["file"])
            for a in (st["summary"].get("audio_classifications", []) or [])
            if a.get("file")
        }
        # Inline office-document views. Lives IN the state dict (not a separate
        # cached attribute) so it swaps atomically with the ocr_index it is
        # derived from — a cache outside the generation would keep serving views
        # for documents a reload has since re-indexed or removed.
        st["doctext_views"] = build_doctext_views(st["ocr_index"])
        self._state = st  # atomic publish

    def reload_after_move(self):
        """Cheaper alternative to load() for a verb whose only effect is moving
        one or more files (Discard/Banish and its undo): profiled against the
        real vitalgoog corpus (docs/BACKLOG.md #13 — "Discard lags on large
        cases, reads as a hung button"), case_summary.json (~209ms) and the
        role-scoped email_threads_index (~172ms) ALONE account for more than
        half of load()'s ~700-830ms warm-cache cost, and neither one's content
        is affected by a file move — nor are metadata_index, scene_index,
        ocr_index, transcription_index, sensitive_scan_index,
        correspondent_freq, video_frame_map, video_index, or
        conversation_index. Only `universe` (an archive path's on-disk
        presence changed) and `stacks`/`dup_member_paths` (the move ledger
        `build_stacks` reads just gained an entry) can differ after a move —
        re-derive just those from the unchanged pieces already in `_state`.

        Same atomic-reference-swap invariant as load(): builds a whole new
        state dict (old data + freshly rebuilt universe/stacks) and publishes
        it in one assignment, so a concurrent reader never sees a half-updated
        mix. Falls back to a full load() if called before any load() (no
        `_state` to build from yet) — should not happen in practice, since a
        move verb only ever runs on an already-loaded case."""
        old = self.__dict__.get("_state")
        if old is None:
            return self.load()
        md = self.paths.metadata_dir
        universe = build_photo_universe(
            old["scene_index"], old["archive_map"], self.role, old["video_frame_map"],
            rescued=self.decisions.get("junk_rescued"),
            scanned_released=self.decisions.get("scanned_released"))
        stacks, dup_member_paths = build_stacks(
            load_json(md / "perceptual_dup_groups.json", {}) or {},
            load_json(md / "dup_member_scan.json", {}) or {},
            self.ledger.latest_status(), universe, self.paths.case_dir)
        st = dict(old)
        st["universe"] = universe
        st["stacks"] = stacks
        st["dup_member_paths"] = dup_member_paths
        self._state = st  # atomic publish

    def __getattr__(self, name):
        # Only reached when normal lookup misses, so real attrs/methods/properties
        # (paths, role, _lock, archive_entries, email_by_file, …) take precedence.
        try:
            return self.__dict__["_state"][name]
        except KeyError:
            raise AttributeError(name)

    @property
    def archive_entries(self):
        return self.archive_map.get("entries", {}) or {}

    @property
    def email_by_file(self):
        """{eml_path: email_index record}, built once on first email-detail use.

        email_index.json is ~120 MB, so it is never loaded unless a thread is
        actually opened (the Emails list runs off the smaller threads index).
        Double-checked locking guards the first-touch build so only ONE thread
        parses the index even under a concurrent open-thread race (R-2)."""
        if self._email_by_file is None:
            with self._email_lock:
                if self._email_by_file is None:
                    # Audience-scoped: the thread set already excludes rescued
                    # mail for the family, so this is belt-and-braces — but it
                    # means a stale or hand-edited thread index cannot make the
                    # server serve a body the family was never meant to see.
                    idx = load_json(email_index_path(self.paths), []) or []
                    self._email_by_file = {
                        r["file"]: r
                        for r in filter_email_entries(idx, self.role)
                        if r.get("file")}
        return self._email_by_file

    @property
    def decisions(self):
        """Fresh read of recorded confirm decisions (verbs mutate the file)."""
        return load_json(self.paths.metadata_dir / DECISIONS_FILE, {}) or {}

    # ── the examiner release gate: live state for E3/E4/E5 + the UI banner ──
    def _release_record_fresh(self):
        """(record_or_None, legacy_unsigned). Re-reads family_release.json from
        disk, cached by mtime_ns (os.replace bumps it on every atomic write) so a
        revoke/re-sign is honored WITHOUT a case.load() — the long-lived-server
        trap. Absent ⇒ legacy_unsigned; corrupt ⇒ a poison record (present-but-
        invalid, NEVER silently treated as absent → legacy)."""
        p = release.release_path(self.paths)
        try:
            key = p.stat().st_mtime_ns
        except FileNotFoundError:
            self._release_rec_cache = None
            return None, True
        cache = getattr(self, "_release_rec_cache", None)
        if not (cache and cache[0] == key):
            try:
                rec = release.load_release(self.paths)
            except release.ReleaseError:
                rec = {"__corrupt__": True}
            cache = (key, rec)
            self._release_rec_cache = cache
        return cache[1], False

    def release_status(self):
        """Live release state (family E5 gate + the UI banner). Runs
        verify(live=True): the cheap stamp fast-path, escalating to the content
        fingerprint on mismatch, with the escalation verdict cached against the
        stamp value so the ~15 s walk runs once per distinct tree state, never per
        request. Examiner always serves, but the status is still reported so the
        examiner UI can show sign state."""
        rec, legacy = self._release_record_fresh()
        if legacy:
            return {"state": "legacy_unsigned", "valid": False, "revoked": False,
                    "stale": False, "signed_by": None, "signed_at": None,
                    "message": "This case predates the release gate. "
                               "Nothing here is released."}
        if rec.get("__corrupt__"):
            return {"state": "invalid", "valid": False, "revoked": False,
                    "stale": False, "signed_by": None, "signed_at": None,
                    "message": "The release record is present but unreadable — "
                               "delivery is closed."}
        cache = self.__dict__.setdefault("_release_escalation", {})
        r = release.verify(self.paths, rec, live=True, escalation_cache=cache,
                           escalation_lock=self._release_lock)
        revoked = bool(rec.get("revoked"))
        return {
            "state": "released" if r.ok else ("revoked" if revoked else "stale"),
            "valid": bool(r.ok),
            "revoked": revoked,
            "stale": (not r.ok) and not revoked,
            "signed_by": (rec.get("actor") or {}).get("name"),
            "signed_at": rec.get("signed_at"),
            "fingerprint_mode": rec.get("fingerprint_mode"),
            "message": None if r.ok else (
                "This release was revoked." if revoked else
                "The family archive changed after it was released; delivery is "
                "paused until it is re-signed."),
        }

    @property
    def curation_layer(self):
        """Fresh read of the curation sidecar (favorites / collections / notes).

        Read fresh on every use (like `decisions`) so a favorite/note/collection
        verb is reflected on the next render without a full case.load() — curation
        never moves files or touches an index, so it needs no index reload. Absent /
        unreadable → the empty layer, so a case that was never curated is
        indistinguishable from before (additive)."""
        cur = load_json(self.paths.metadata_dir / CURATION_FILE, {}) or {}
        cur.setdefault("favorites", {})
        cur.setdefault("collections", {})
        cur.setdefault("notes", {})
        return cur

    def quarantine_entries(self):
        """Full quarantine manifest entries (examiner-only; carries the paths the
        release verb needs — review_data strips these down to basenames)."""
        manifest = load_json(self.paths.metadata_dir / QUARANTINE_MANIFEST, {}) or {}
        return manifest.get("entries", []) or []

    @property
    def removed_persons(self):
        return list((self.decisions.get("removed_persons", {}) or {}).keys())

    def effective_face_clustering(self, decisions=None):
        """G-15 + Move: face_clustering with the family_decisions face-assist overlay
        (person_merges + face_assignments + face_placements) folded in at render time
        — merged losers' members join their winner and drop out of People, assigned
        noise faces join their target cluster, and moved items (face_placements) leave
        their origin cluster for their target (an emptied origin drops out). NEVER
        mutates face_clustering.json (the overlay lives entirely in
        family_decisions.json). Pass `decisions` to reuse a snapshot; otherwise a
        fresh read is used (like the other decision-driven renders)."""
        return apply_face_overlay(self.face_clustering,
                                  decisions if decisions is not None else self.decisions)

    def _state_cached(self, key, builder):
        """Memoize a derived value for the CURRENT state generation. The cache is
        keyed on _state identity, so load()'s atomic reference swap invalidates it
        for free (a mutating verb reloads → next read rebuilds). Lets reads reuse
        the expensive photo/search builds across requests instead of rebuilding on
        every GET."""
        st = self._state
        cache = self.__dict__.setdefault("_derived_cache", {})
        hit = cache.get(key)
        if hit is not None and hit[0] is st:
            return hit[1]
        val = builder(st)
        cache[key] = (st, val)
        return val

    def _photo_rows(self):
        """The full photo gallery rows — an O(#photos) build, so cached per state
        generation AND only computed for the pages that actually use it (overview,
        photos, places, search); the ~9 other pages no longer pay for it."""
        return self._state_cached("photo_rows", lambda st: photo_rows(
            st["universe"], st["metadata_index"], st["geo_index"], {},
            event_album_titles(st["summary"]), stacks=st["stacks"],
            # G-1/G-7: owner favorites/hidden/albums + LLaVA captions ride along.
            llava_map=(st["scene_index"].get("llava_results", {}) or {}),
            # Phase-1.5 Move: scene overlay regroups the gallery facet at render
            # time (read fresh; the verb's case.load() invalidates this cache).
            scene_placements=self.decisions.get("scene_placements"),
            # Phase-2 Move: event overlay re-tags a photo's album so the events
            # view's live count + the ?event= filter reflect the move.
            event_placements=self.decisions.get("event_placements")))

    def actions_index(self):
        """Parse family_actions.ndjson ONCE per (mtime, size) generation, returning
        (rows_newest_first, {undo_token: entry}, {undone_tokens}).

        append_action only ever GROWS the file (O_APPEND, never rewrites in place),
        so keying the cache on (mtime_ns, size) invalidates it exactly when a new
        line lands — audit history stays fresh after every append while find_action
        / is_undone / the History GET stop re-parsing the whole file per call. This
        collapses the two full scans verb_undo used to do (find_action + is_undone)
        into ONE parse (R-5). Correctness first: a stale read after an append is
        impossible because the size always changes."""
        path = self.paths.metadata_dir / ACTIONS_FILE
        try:
            stt = os.stat(path)
            sig = (stt.st_mtime_ns, stt.st_size)
        except OSError:
            sig = None
        cache = self.__dict__.get("_actions_cache")
        if cache is not None and cache[0] == sig:
            return cache[1]
        rows = actions_history(self.paths)  # newest-first
        by_token, undone = {}, set()
        for e in rows:
            if not isinstance(e, dict):   # a corrupt non-object line must not crash
                continue                  # signoff/History/undo (all read this)
            tok = e.get("undo_token")
            if tok and tok not in by_token:
                by_token[tok] = e
            u = e.get("undoes")
            if u:
                undone.add(u)
        result = (rows, by_token, undone)
        self._actions_cache = (sig, result)
        return result

    def search_index_bytes(self):
        """The /api/search payload pre-serialized to bytes, cached per state
        generation. The inverted index is tens of MB, so the json.dumps itself is a
        measurable per-GET cost even though the index OBJECT is already cached per
        generation (via section('search')). Reusing the bytes across repeated
        search-box opens avoids re-serializing the whole world every time (R-5)."""
        return self._state_cached(
            "search_bytes",
            lambda st: json.dumps(self.section("search")).encode("utf-8"))

    # ── full-text search (FTS5) ──────────────────────────────────────────────
    @property
    def fts_db_path(self):
        return build_fts.db_path_for(self.paths.metadata_dir, self.role)

    def fts_search(self, q, offset, limit):
        """Query the FTS index. When the index is missing/stale, kick off a
        background build and return {building:True} so the caller can fall back to
        the lexical index and the UI can poll. Escaping/malformed-query handling
        lives in build_fts.search (never 500s)."""
        if not q or not str(q).strip():
            return {"hits": [], "total": 0}
        db = self.fts_db_path
        if build_fts.is_fresh(db, self.paths):
            return build_fts.search(db, q, offset, limit)
        self._ensure_fts_build()
        status = dict(self._fts_status)
        return {"hits": [], "total": 0, "building": True,
                "progress": {k: status.get(k) for k in ("phase", "rows")}}

    def _ensure_fts_build(self):
        """Start the FTS build in a daemon thread unless one is already running.
        Idempotent — concurrent searches collapse to a single build (R-2)."""
        with self._fts_lock:
            if self._fts_status.get("state") == "building":
                return
            self._fts_status = {"state": "building", "phase": "start",
                                "started": time.time()}
        t = threading.Thread(target=self._run_fts_build, name="fts-build",
                             daemon=True)
        t.start()

    def _run_fts_build(self):
        def cb(info):
            # Merge coarse progress into the pollable status + a progress.json file
            # for external observers (the UI polls the in-memory status via the API).
            self._fts_status.update(info)
            try:
                atomic_write_json(
                    build_fts.progress_path_for(self.paths.metadata_dir, self.role),
                    self._fts_status)
            except Exception:
                pass
        # Serialize the actual build: a daemon build (from _ensure_fts_build) and a
        # direct call (e.g. a synchronous rebuild) must not run build_fts_db
        # concurrently — same-process concurrent builds otherwise collide on the
        # sqlite. The waiter re-checks freshness and skips a redundant rebuild.
        with self._fts_build_lock:
            if build_fts.is_fresh(self.fts_db_path, self.paths):
                self._fts_status = {"state": "idle", "built_at": time.time()}
                return
            try:
                build_fts.build_fts_db(self.paths, self.role, self.cfg, progress=cb)
                self._fts_status = {"state": "idle", "built_at": time.time()}
            except Exception as e:  # a build failure must not wedge future searches
                log(f"FTS build failed: {e}")
                self._fts_status = {"state": "error", "error": str(e)}

    def lexical_search(self, q, offset, limit):
        """Interim search over the (shallow) lexical inverted index — titles +
        160-char snippets only — used WHILE the full FTS index is still building so
        the first search isn't empty. Same hit envelope as build_fts.search, with
        building:True so the UI keeps polling for the full index."""
        idx = self.section("search")  # {records, index}, cached per generation
        toks = tokenize(q)
        if not toks:
            return {"hits": [], "total": 0, "building": True}
        sets = [set(idx["index"].get(t, [])) for t in toks]
        ids = set.intersection(*sets) if sets else set()
        recs = [idx["records"][i] for i in sorted(ids)]
        total = len(recs)
        page = recs[offset:offset + limit]
        hits = [{"title": r.get("t"), "snippet": r.get("s"), "page": r.get("p"),
                 "ref": r.get("h"), "kind": r.get("k")} for r in page]
        return {"hits": hits, "total": total, "building": True}

    # ── data sections (thumbs/media are served live, so builders get no thumbs) ──
    def section(self, page):
        if page == "overview":
            decisions = self.decisions
            removed = set((decisions.get("removed_persons", {}) or {}).keys())
            eff_fc = self.effective_face_clustering(decisions)  # G-15 merges folded
            counts = {
                "photos": len(self.universe),
                "people": len([p for p in (eff_fc.get("person_clusters", {}) or {})
                               if p not in removed]),
                "places": len(places_data(self._photo_rows())["trips"]),
                # Filtered to match the Documents section exactly (excludes
                # email-sourced classifications and, for family, credentials —
                # see document_rows). It used to be the raw document_classifications
                # length, which counted every email as a document too (~66k vs. the
                # ~1.5k the Documents page actually shows).
                "documents": len(document_rows(self.summary, self.ocr_index, self.role,
                                               doc_placements=decisions.get("doc_placements"))),
                "audio": len(self.summary.get("audio_classifications", []) or []),
                "videos": self.summary.get("video_delivered", self.summary.get("total_videos", 0)),
                # Conversations THIS ROLE may see (matches the Messages section
                # exactly — it used to count every non-discard conversation, so
                # the family's count included platform traffic and estate-rescued
                # conversations the list itself withholds); 0 when message_triage
                # hasn't run.
                "messages": sum(1 for c in (self.conversation_index or [])
                                if can_see_conversation(c, self.role)),
                # Thread-grain, matching the Emails section exactly (see email_rows) —
                # emails were excluded from "documents" above but had no Overview
                # tile of their own at all.
                "emails": len(email_rows(self.email_threads, decisions=decisions)),
            }
            # R-7: for the family role, a legitimately ABSENT archive_map.json (the
            # optional build_archive stage was skipped, or no media) leaves the
            # family universe empty — surface a prominent warning in the payload so
            # a zero-media archive isn't mistaken for "there was nothing", and log
            # it loudly. (A present-but-corrupt map is refused at startup.)
            archive_warning = None
            if self.role == "family" and not (self.archive_map.get("entries") or {}) \
                    and not (self.paths.metadata_dir / "archive_map.json").exists():
                archive_warning = ("Archive index is missing — media may be "
                                   "incomplete. Re-run the build_archive stage to "
                                   "restore the full archive.")
                log("WARNING: archive_map.json absent — family archive is serving "
                    "with no media (build_archive likely skipped).")
            return overview_data(self.summary, self.role, counts,
                                 face_clustering=eff_fc, universe=self.universe,
                                 decisions=decisions,
                                 vital_docs=vital_docs_data(self.paths, self.summary, self.role,
                                                            decisions=decisions,
                                                            threads_index=self.email_threads,
                                                            per_target_k=self.vital_per_target_k()),
                                 archive_warning=archive_warning)
        if page == "photos":
            return self._photo_rows()
        if page == "events":
            # Move Phase 2: event-album cards with a DERIVED live member count
            # (event_placements folded into _photo_rows), not the static
            # case_summary photo_count — so an event-move shifts the counts.
            return event_albums_data(self.summary, self._photo_rows())
        if page == "videos":
            # G-11: join person/scene facets from video_index, names via cluster_identities.
            return video_rows(self.archive_map, self.metadata_index, self.video_frame_map,
                              self.role, video_index=self.video_index,
                              cluster_identities=(self.face_clustering.get("cluster_identities", {}) or {}))
        if page == "people":
            return people_rows(self.effective_face_clustering(), self.summary,
                               self.universe, {}, self.role,
                               self.removed_persons, frame_map=self.video_frame_map,
                               archive_map=self.archive_map)
        if page == "timeline":
            # G-5: chapter bands → event groups → capped photo strips, joined to the
            # geo index's temporal clusters. Capped strips keep a 184-chapter case light.
            return timeline_data(self._photo_rows(), self.geo_index, self.summary)
        if page == "places":
            # G-10: the trip aggregates PLUS everyday-place venue clusters (second tab).
            d = places_data(self._photo_rows())
            d["venues"] = venues_data(self._photo_rows(), self.geo_index)["venues"]
            return d
        if page == "documents":
            rows = document_rows(self.summary, self.ocr_index, self.role,
                                 doc_placements=self.decisions.get("doc_placements"))
            return {"index": documents_index(rows), "rows": rows,
                    "vital_docs": vital_docs_data(self.paths, self.summary, self.role,
                                                  decisions=self.decisions,
                                                  threads_index=self.email_threads,
                                                  per_target_k=self.vital_per_target_k())}
        if page == "correspondence":
            # Real (non-email) correspondence docs, grouped by writing medium,
            # plus scanned document/letter IMAGES (not in document_classifications).
            rows = [d for d in document_rows(self.summary, self.ocr_index, self.role,
                                             doc_placements=self.decisions.get("doc_placements"))
                    if d.get("category") in LETTER_CATEGORIES]
            typed, hand = [], []
            for r in rows:
                (hand if r.get("text_kind") == "handwritten" else typed).append(r)
            return {"typed": typed, "handwritten": hand,
                    "scanned": scanned_image_rows(self.scene_index, self.archive_map,
                                                  self.metadata_index, self.role,
                                                  frame_map=self.video_frame_map,
                                                  released=self.decisions.get("scanned_released"))}
        if page == "emails":
            return email_rows(self.email_threads, decisions=self.decisions)
        if page == "correspondents":
            # G-6: ranked correspondent cards (relationship metadata, no bodies —
            # both roles). Click-through filters the Emails list by ?participant=.
            return correspondents_data(self.correspondent_freq, self.email_threads,
                                       self.conversation_index, self.role,
                                       decisions=self.decisions)
        if page == "messages":
            # Conversation-grain list (message_triage's conversation_index.json);
            # [] when the stage hasn't run. Per-message detail is served lazily
            # by conversation_section.
            return message_rows(self.conversation_index, self.role)
        if page == "recordings":
            # All recordings (the old voicemail-only filter is gone — #14).
            return audio_rows(self.summary, self.transcription_index, self.role, self.cfg)
        if page == "accounts":
            return accounts_data(self.summary, self.role)
        if page == "review":
            decisions = self.decisions
            data = review_data(self.paths, self.summary)
            data["confirm_queue"] = confirm_queue_data(
                self.summary, self.scene_index,
                self.effective_face_clustering(decisions), self.geo_index,
                decisions=decisions, frame_map=self.video_frame_map, archive_map=self.archive_map)
            data["quarantine_entries"] = self.quarantine_section()  # #15 folded-in Quarantine group
            data["decisions"] = decisions
            return data
        if page == "guided":
            # G-12: compose the existing review surfaces into an ordered checklist
            # (no new verbs; progress is persisted via the confirm verb). Examiner-only.
            decisions = self.decisions
            return guided_review_data(
                self.paths, self.summary, self.scene_index,
                self.effective_face_clustering(decisions),
                self.geo_index, decisions=decisions,
                frame_map=self.video_frame_map, archive_map=self.archive_map,
                per_target_k=self.vital_per_target_k())
        if page == "junk":
            # G-13: junk-routed images (scene_index.junk_results keys). Examiner-only.
            # The section layer paginates this (813 rows on goog).
            return junk_rows(self.scene_index, self.metadata_index,
                             rescued=self.decisions.get("junk_rescued"))
        if page == "collections":
            # Curation layer: the examiner's named collections. Metadata + member
            # COUNT only (the grid for one collection is served by the photos section
            # filtered ?collection_curation=<slug>); titles are operator free text,
            # escaped at the UI sink. Examiner-only (gated in _page / _api_get).
            cur = self.curation_layer
            rows = [{"slug": slug,
                     "title": (c.get("title") or slug),
                     "count": len(c.get("members") or []),
                     "ts": c.get("ts"), "actor": c.get("actor")}
                    for slug, c in (cur.get("collections") or {}).items()]
            rows.sort(key=lambda r: (r.get("title") or "").lower())
            return {"collections": rows,
                    "favorites_count": len(cur.get("favorites") or {})}
        if page == "history":
            return self.actions_index()[0]  # cached parse (newest-first)
        if page == "search":
            # The whole inverted index — cached per state generation so opening the
            # search box repeatedly doesn't rebuild people+docs+audio+emails+photos
            # every time (a multi-second reload-the-world on a large case).
            return self._state_cached("search_index", lambda st: build_search(
                self._photo_rows(),
                # G-15: fold the face-assist overlay so a merged loser isn't a
                # separate People search hit (cache invalidated on the verb's load()).
                people_rows(apply_face_overlay(st["face_clustering"], self.decisions),
                            st["summary"], st["universe"], {}, self.role,
                            frame_map=st["video_frame_map"], archive_map=st["archive_map"]),
                document_rows(st["summary"], st["ocr_index"], self.role,
                              doc_placements=self.decisions.get("doc_placements")),
                audio_rows(st["summary"], st["transcription_index"], self.role, self.cfg),
                email_rows(st["email_threads"]),
                conversations=message_rows(st["conversation_index"], self.role)))
        raise VerbError(f"unknown section {page}", 404)

    def api_section(self, name, params=None):
        """The JSON payload for GET /api/<name>, adding {rows,total,offset,limit}
        pagination for the large-list sections (docs/specs/family-archive-
        pagination.md). `params` is a flat {str: str} of the query string. The
        pure builders still return full lists; the slicing lives here so a capped
        view is never presented as complete. Non-paginated sections pass through.

        Testable without a live socket (the HTTP handler is a thin wrapper)."""
        params = params or {}
        offset, limit = _page_window(params)
        if name == "overview":
            # G-8: attach the "on this day" card. `today` (MM-DD source) is injected by
            # the request boundary (_api_get) so the builder stays clock-free/testable;
            # absent (e.g. a direct call without it) → no card rather than a wall-clock read.
            data = self.section("overview")
            today = params.get("today")
            if today:
                data["on_this_day"] = on_this_day_data(self._photo_rows(), today)
            return data
        # Load the curation sidecar ONCE per request and overlay it onto whichever
        # section this GET builds (apply_curation is a no-op when the layer is empty).
        curation = self.curation_layer
        if name == "photos":
            full = apply_curation(self.section("photos"), curation, "id")
            rows = _apply_photo_view(full, params)
            page = _paginate(rows, offset, limit)
            # G-1: whole-set facets for the Favorites chip / Albums dropdown /
            # Hidden toggle, plus the curation `starred` count (additive keys — the
            # envelope shape is unchanged).
            page["facets"] = _photo_facets(full)
            return page
        if name == "videos":
            # G-11: ?person=<person_id> / ?scene=<name> narrow the FULL set before
            # the page slice (so a filtered tail is reachable and `total` is true);
            # facets (distinct persons/scenes) are computed from the full set.
            full = apply_curation(self.section("videos"), curation, "id")
            rows = _filter_videos(full, params)
            page = _paginate(rows, offset, limit)
            page["facets"] = _video_facets(full)
            return page
        if name == "emails":
            # G-6: ?participant=<address> narrows the thread list before the slice
            # so a correspondent card's filtered tail is reachable and total is true.
            # #11: ?q=/date_from/date_to/sort layer the in-list search box on top.
            rows = apply_curation(self.section("emails"), curation, "thread_id")
            rows = _filter_emails_by_participant(rows, params)
            rows = _filter_emails_search(rows, params)
            return _paginate(rows, offset, limit)
        if name == "messages":
            return _paginate(apply_curation(self.section("messages"), curation,
                                            "conversation_id"), offset, limit)
        if name == "correspondents":
            # #11: ?q=/sort= narrow/reorder before the slice (find-person-X search).
            return _paginate(_filter_correspondents(self.section(name), params),
                             offset, limit)
        if name == "junk":
            return _paginate(self.section(name), offset, limit)
        if name == "collections":
            # Curation layer: the examiner's named collections (metadata + counts).
            return self.section("collections")
        if name == "documents":
            data = self.section("documents")           # {index, rows(full), vital_docs}
            rows = apply_curation(data["rows"], curation, "file")
            rows = _filter_documents(rows, params)
            page = _paginate(rows, offset, limit)
            page["index"] = data["index"]              # full index (all rows)
            page["vital_docs"] = data["vital_docs"]    # checklist panel (G-2)
            return page
        if name == "correspondence":
            data = self.section("correspondence")      # {typed, handwritten, scanned}
            data["typed"] = apply_curation(data["typed"], curation, "file")
            data["handwritten"] = apply_curation(data["handwritten"], curation, "file")
            data["scanned"] = _paginate(
                apply_curation(data["scanned"], curation, "id"), offset, limit)
            return data
        return self.section(name)

    def transparency_section(self):
        """G-14: the read-only duplicates/accounting transparency panel. Both roles
        get the numbers-only trust summary; the examiner additionally gets the
        suspense count and the significant-attachment noise list. email_noise_log.json
        is large (~363k entries on goog) and examiner-only, so it is loaded lazily
        here (never in load()) and only for the examiner role."""
        md = self.paths.metadata_dir
        dedup_summary = load_json(md / "collect_dedup_summary.json", {}) or {}
        perceptual = load_json(md / "perceptual_dup_groups.json", {}) or {}
        suspense = load_json(md / "suspense_manifest.json", []) or []
        email_noise = None
        if self.role == "examiner":
            email_noise = load_json(md / "email_noise_log.json", []) or []
        return transparency_data(self.summary, dedup_summary, perceptual, suspense,
                                 email_noise, self.role)

    def quarantine_section(self):
        """Examiner-only: list quarantined items with the paths the release verb
        needs."""
        out = []
        for e in self.quarantine_entries():
            filt = e.get("filter") or ""
            row = {
                "name": os.path.basename(e.get("file", "")),
                "filter": filt,
                "timestamp": e.get("timestamp"),
                "locked": False,
                "canonical_path": e.get("canonical_path"),
                "src": e.get("canonical_path"),
            }
            out.append(row)
        return {"entries": out, "total": len(out)}

    def person_detail_section(self, person_id):
        """One person's member images + video appearances (rendered directly, not
        via the frame/doc-excluded gallery). 404 → unknown person.

        G-15: a merged WINNER shows the union of its own + folded-in loser members
        (overlay applied); a merged LOSER 404s — but for a helpful UX we redirect it
        to its surviving winner rather than a dead end (documented deviation)."""
        decisions = self.decisions
        merges = (decisions.get("person_merges", {}) or {})
        winner = resolve_merge(person_id, merges)
        detail = person_detail(self.effective_face_clustering(decisions), self.universe,
                               self.archive_map, self.metadata_index, self.scene_index,
                               self.video_frame_map, winner, self.role, self.removed_persons)
        if detail is None:
            raise VerbError(f"unknown person {person_id}", 404)
        if winner != person_id:
            detail["merged_into"] = winner  # loser → winner redirect hint for the UI
        return detail

    def video_frames_section(self, source_video):
        """G-11 poster-strip playback fallback: the ordered keyframe ids of ONE
        source video, for when the browser can't decode it (HEVC/.mov/.wmv/.avi →
        a black <video>). Returns {"frames": [{"id", "offset"}]} in capture order;
        each `id` is a video_frame_map key servable via /thumb + /media (allowed by
        the delivered-set gate's video_frame_map branch).

        SECURITY: `source_video` is an attacker-influenceable archive_map key, but
        it is NEVER read as a file — only string-matched against video_frame_map
        values. Each returned frame id is contained to the case dir (realpath must
        stay under it) so a frame whose path escapes the tree is refused. For the
        family role, only a DELIVERED video (present in archive_map) exposes a
        strip. RETENTION: frames may have been pruned (video-frame-retention spec) —
        an unmatched/pruned video degrades to an empty list (the frontend then
        falls back to the single poster or a notice), never an error."""
        if not source_video:
            return {"frames": []}
        norm_sv = os.path.normpath(source_video)
        if self.role == "family":
            entries = self.archive_entries
            if source_video not in entries and norm_sv not in entries:
                return {"frames": []}   # only delivered videos expose a strip to family
        case_real = os.path.realpath(self.paths.case_dir)
        frames = []
        for frame, info in (self.video_frame_map or {}).items():
            sv = (info or {}).get("source_video")
            if not sv or os.path.normpath(sv) != norm_sv:
                continue
            # Containment: the frame id must resolve to a real path under the case
            # dir (mirrors resolve_media_path). A frame that escapes the tree (a
            # symlink out, a hand-crafted map) is refused, never returned/served.
            real = os.path.realpath(frame)
            if real != case_real and not real.startswith(case_real + os.sep):
                continue
            # RETENTION (video-frame-retention spec): a frame may have been PRUNED
            # from disk while its map entry lingers. Only surviving frames are
            # returned (else the poster strip's /thumb would 404) — this naturally
            # degrades to the retained poster(s), or empty when all are pruned.
            if not os.path.isfile(real):
                continue
            frames.append({"id": frame, "offset": (info or {}).get("frame_offset_seconds")})
        frames.sort(key=lambda f: (f["offset"] is None,
                                   f["offset"] if f["offset"] is not None else 0.0, f["id"]))
        return {"frames": frames}

    def thread_messages(self, thread_id):
        """Threaded conversation for one email thread, resolved server-side (#3)."""
        sig_by_file = {}
        for d in self.summary.get("document_classifications", []) or []:
            if (d.get("source") or "") == "email" and d.get("file"):
                sig_by_file[d["file"]] = d.get("significance")
        # {thread_id: thread} built once per state generation so the detail lookup
        # is O(1) instead of a linear scan of the ~17k-thread list per GET (R-5).
        thread_map = self._state_cached("email_thread_map", lambda st: {
            t.get("thread_id"): t
            for t in ((st["email_threads"] or {}).get("threads", []) or [])})
        # G-4: basename → delivered-item id index, built once per state generation
        # (role-gated, mirrors the doc-browse exclusions), so each message's
        # attachments deep-link only when the basename resolves uniquely.
        attachment_index = self._state_cached(
            "email_attachment_index",
            lambda st: delivered_basename_index(st["summary"], st["archive_map"], self.role))
        detail = email_thread_detail(self.email_threads, self.email_by_file, sig_by_file,
                                     thread_id, thread_map=thread_map,
                                     attachment_index=attachment_index)
        if detail is None:
            raise VerbError(f"unknown thread {thread_id}", 404)
        detail["demoted"] = str(thread_id) in (self.decisions.get("email_demoted", {}) or {})
        return detail

    def conversation_by_id(self, conversation_id):
        """One conversation's per-file JSON (output/metadata/messages/<id>.json),
        loaded lazily ON DEMAND and cached — the email_by_file lesson, but
        per-FILE: only the requested conversation's JSON is ever read (the
        Messages list runs off the small conversation_index.json). Returns None
        for an unknown/absent conversation. The id is client-supplied, so the
        filename must resolve to a direct child of the messages dir (no
        traversal)."""
        cid = str(conversation_id or "")
        with self._conversation_lock:
            cached = self._conversation_cache.get(cid)
            if cached is not None:
                self._conversation_cache.move_to_end(cid)  # LRU touch
                return cached
        try:
            path = contained_child(self.paths.metadata_dir / "messages", f"{cid}.json")
        except VerbError:
            return None
        conv = load_json(path)
        if conv:
            with self._conversation_lock:
                self._conversation_cache[cid] = conv
                self._conversation_cache.move_to_end(cid)
                while len(self._conversation_cache) > _CONVERSATION_CACHE_CAP:
                    self._conversation_cache.popitem(last=False)  # evict LRU
        return conv

    def _attachment_src(self, src):
        """Resolve a message attachment's source-side path to a servable /media
        src through the SAME machinery photos use (canonical_for + the role gates
        in resolve_media_path). Returns the src key itself when servable (the
        client fetches /media?src=...), else None — the UI then shows the
        attachment's basename only, never a broken link."""
        try:
            resolve_media_path(self, src)
        except VerbError:
            return None
        return src

    def conversation_section(self, conversation_id):
        """Bubble-transcript detail for one conversation (mirrors thread_messages
        for email). 404 → unknown conversation / message_triage not run / this
        audience may not see it.

        THE ROLE GATE IS LOAD-BEARING HERE, not a formality. message_triage writes
        a per-conversation JSON for EVERY conversation — including the ones it
        discarded — and this endpoint used to resolve them by filename alone: a
        path-traversal check, then a 404 if the file was missing. So a
        conversation the Messages list would never show could still be fetched, in
        full, by asking for its id. Gate on the conversation_index (the
        authoritative record) rather than on the detail file, so a stale or
        hand-edited detail JSON cannot talk its way in.
        """
        cid = str(conversation_id or "")
        conv = next((c for c in (self.conversation_index or [])
                     if str(c.get("conversation_id") or "") == cid), None)
        # Deliberately the same 404 as "no such conversation": a family session
        # must not be able to learn that a conversation exists by being told it is
        # forbidden.
        if conv is None or not can_see_conversation(conv, self.role):
            raise VerbError(f"unknown conversation {conversation_id}", 404)

        detail = conversation_detail(self.conversation_by_id(cid),
                                     index_record=conv,
                                     attachment_resolver=self._attachment_src)
        if detail is None:
            raise VerbError(f"unknown conversation {conversation_id}", 404)
        return detail

    def correspondent_duplicates_section(self):
        """P2 #9: examiner-only possible-duplicate-identity suggestions for the
        Correspondents page (detection only — see correspondent_duplicate_candidates
        for the heuristic and correspondent_merges overlay)."""
        return correspondent_duplicate_candidates(self.correspondent_freq, self.decisions)

    def doctext_view(self, src):
        """The inline display blocks for ONE office document, or a 404.

        Office formats (.docx/.xlsx/.pptx/.rtf/.odt/…) have no in-browser
        renderer, so the lightbox used to offer nothing but "Download to open".
        The OCR stage already reads their digital text layer, and now also
        writes a `.view.json` sidecar of typed display blocks beside the `.txt`
        one; this hands that sidecar to the client.

        Two properties make this safe to expose:

        1. `resolve_media_path` is the SAME gate `/media` uses, applied to the
           SAME src. A document the family role may not fetch bytes for cannot
           be read as text here either — this endpoint widens no allowlist.
        2. The blocks carry text only, never document markup (see `_doctext`),
           and the client builds DOM nodes with textContent. There is no path by
           which a hostile estate document injects markup into the archive page,
           which is why this does not need the octet-stream/sandbox treatment
           `/media` gives the raw bytes.

        Absence is routine — legacy .doc, an unrenderable file, or a case OCR'd
        before this existed — so a miss is a plain 404 and the client falls back
        to the download card.
        """
        real = resolve_media_path(self, src)
        view = self.doctext_views.get(file_identity(real))
        if not view:
            return (404, {"error": "no inline view"})
        data = load_json(Path(view), None)
        if not isinstance(data, dict) or not data.get("blocks"):
            return (404, {"error": "no inline view"})
        return (200, {"name": os.path.basename(str(real)),
                      "method": data.get("method") or "",
                      "blocks": data["blocks"]})

    def near_miss_section(self, target, qs):
        """Examiner-only: the near-miss review list for ONE vital-doc target.

        Returns (status, payload). The target is validated against the candidate
        keys rather than trusted as free text — it is only ever used as a dict key
        here, but an unknown key would otherwise return a silent empty list that
        reads as "nothing to review" when it actually means "you asked for the
        wrong thing".

        PAGINATED. The subset is NOT bounded: `vital_per_target_k` is a per-case
        config knob (default 8) an examiner raises precisely when they suspect a
        vital document was missed, so a fixed cap would silently truncate the one
        case that needed the recall. `_paginate` reports the true pre-slice
        `total`, and the ordering (not_evaluated first) is fixed in
        near_miss_rows BEFORE the slice, so page 1 always holds the rows that
        matter most.
        """
        candidates = load_json(
            self.paths.metadata_dir / "vital_doc_candidates.json", None)
        if not isinstance(candidates, dict) or not candidates:
            return 404, {"error": "no vital-document candidates for this case"}
        if not target or target not in candidates:
            return 400, {"error": "unknown vital-document target"}
        rows = near_miss_rows(self.paths, self.summary, self.role, target,
                              decisions=self.decisions,
                              threads_index=self.email_threads)
        offset, limit = _page_window({k: (v[0] if v else "")
                                      for k, v in (qs or {}).items()})
        page = _paginate(rows, offset, limit)
        page["target"] = target
        page["label"] = vital_doc_label(target)
        return 200, page

    def review_pager_section(self, group):
        """Examiner-only: the normalized item list a review-queue PAGER pages
        through, for ONE surface (`group` ∈ {quarantine, vital}). Gathers the real
        rows (the quarantine manifest, or vital_docs_data + near_miss_rows) and
        hands them to the pure builders in _archive_data — the union model + the
        blur/actions decisions live there, unit-tested without a live case
        (review-queue-bulk-triage.md §3/§4).

        Uncapped by design: the point of the pager is to clear the WHOLE queue, and
        the near-miss subset is a per-case knob an examiner raises precisely when
        something may have been missed (near_miss_rows §doc), so a cap here would
        truncate the one case that needed the recall.
        """
        if group == "quarantine":
            items = quarantine_pager_items(self.quarantine_entries())
            return {"group": "quarantine", "items": items, "total": len(items)}
        if group == "vital":
            decisions = self.decisions
            vital = vital_docs_data(self.paths, self.summary, "examiner",
                                    decisions=decisions,
                                    threads_index=self.email_threads,
                                    per_target_k=self.vital_per_target_k())
            # A found item is "resolved" exactly as vital_docs_data / the release
            # gate count it: reviewed (Confirm), promoted (promoting IS the review),
            # or reassigned (an active decision). Everything else is the unconfirmed
            # queue. Reassign lives in decisions, not the item dict, so read it here.
            retarget = decisions.get("vital_doc_target") or {}
            unconfirmed, near = [], []
            for row in vital.get("targets", []) or []:
                t = row.get("target")
                for it in row.get("items", []) or []:
                    resolved = (it.get("reviewed") or it.get("promoted")
                                or it.get("id") in retarget)
                    if not resolved:
                        unconfirmed.append({**it, "target": t})
                # near_miss_rows matches on the ORIGINAL candidate target; the target
                # order from vital_docs_data always includes every candidate key, so
                # iterating it covers the whole near-miss field with no double count
                # (an effective-only target has no candidate bucket → []).
                for r in near_miss_rows(self.paths, self.summary, "examiner", t,
                                        decisions=decisions,
                                        threads_index=self.email_threads):
                    near.append({**r, "target": t})
            items = vital_pager_items(unconfirmed, near)
            return {"group": "vital", "items": items, "total": len(items),
                    # The canonical target set (+ labels) for the reassign picker.
                    "all_targets": vital.get("all_targets", [])}
        raise VerbError(f"unknown review pager group {group}", 400)

    def transcript_section(self, file):
        """G-3: transcript segments + timings for ONE recording (the seek-synced
        detail view). Honors the SAME gate audio_rows applies — transcribe.deliver
        plus, for the family role, the delivered-audio set — so a family session
        never receives a transcript the recordings list would withhold. Reads the
        `.json`/`.vtt` sidecar through the containment check (read_sidecar_text) and
        degrades to empty segments + the plain transcript_text when the sidecar is
        absent/reaped (goog/appl). 404 for an unknown recording."""
        deliver = self.cfg.get("transcribe", {}).get("deliver", True)
        if self.role == "family" and deliver is False:
            raise VerbError("transcripts not delivered", 403)
        norm = os.path.normpath(file or "")
        # Family scope mirrors audio_rows exactly: audio_rows surfaces only the
        # recordings in summary.audio_classifications (= deliverable_audio), so a
        # family session may only fetch a transcript for one of those files. The
        # SET is built from the summary regardless of on-disk presence, so the
        # reaped-media case (goog) still passes the gate and degrades cleanly.
        if self.role != "examiner" and norm not in (self.deliverable_audio or set()):
            raise VerbError("recording not delivered", 403)
        has_audio = False
        try:
            resolve_media_path(self, file)
            has_audio = True
        except VerbError:
            has_audio = False
        detail = transcript_detail(
            self.transcription_index, file,
            lambda p: read_sidecar_text(self, p),
            has_audio=has_audio)
        if detail is None:
            raise VerbError(f"unknown recording {file}", 404)
        return detail


# ── guards + audit ───────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _doc_lock(case):
    """Cross-PROCESS flock around a shared-JSON read-modify-write.

    CASE._lock serializes mutating verbs WITHIN one process, but two server
    instances (trivially started — the port auto-bumps on EADDRINUSE) each hold
    their own CASE._lock, so their `load_json → mutate → atomic_write_json` on
    family_decisions.json / quarantine_manifest.json can interleave and silently
    lose entries (last writer wins). An exclusive flock on a sidecar lockfile makes
    that read-modify-write atomic across processes too — the same mechanism the
    move ledger already uses for its ndjson appends (R-4).

    Held around the WHOLE read-modify-write, never nested (a second flock on a new
    fd from the same process would deadlock), and never around append_action (which
    flocks its own distinct file)."""
    lock_path = case.paths.metadata_dir / VERBS_LOCKFILE
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def require_examiner(case):
    if case.role != "examiner":
        raise VerbError("this action is examiner-only", 403)


def family_media_roots(case):
    """The only directories a FAMILY-role session may be served media from — the
    delivered/working trees the family builders actually reference:
      output/archive/            delivered canonicals (copy mode)
      extracted/photos/          face-member frames, video stills, symlink-mode
                                 photo targets
      extracted/videos/          delivered video working set / symlink-mode targets
      extracted/other/audio/     surfaced + delivered audio
    Everything else under the case — original_files/, duplicates/, the junk
    buckets (extracted/photos_junk/, extracted/other/*), output/family_banished/,
    output/family_export/, and the processing-only trees — is NOT servable to the
    family. This is an ALLOW-list precisely because a deny-list silently leaks any
    new tree nobody remembered to forbid (the original bug: a family session could
    fetch undelivered originals and just-banished items by direct URL)."""
    ex = case.paths.extracted_dir
    return [
        Path(os.path.realpath(case.paths.archive_dir)),
        Path(os.path.realpath(ex / "photos")),
        Path(os.path.realpath(ex / "videos")),
        Path(os.path.realpath(ex / "other" / "audio")),
    ]


def _under_any(real, roots):
    return any(real == r or r in real.parents for r in roots)


def resolve_media_path(case, src):
    """Resolve a builder id (archive_map key OR an absolute case path) to a real
    file, refusing anything outside the case dir or in a processing-only tree.

    For the family role the resolved file must additionally sit within one of the
    delivered/working roots (family_media_roots) — an allow-list, so undelivered
    or banished material is never served/exported even by a hand-crafted src. The
    examiner role keeps the broader deny-list (it also reaches quarantine via
    resolve_quarantine_media_path)."""
    if not src:
        raise VerbError("missing src", 400)
    # Normalize the builder id BEFORE the archive_map lookup. The map is an exact
    # string→canonical dict, so a non-canonical spelling of a delivered path
    # ('/a/./b.jpg', '/a//b.jpg') used to MISS the map and fall through to serving
    # the raw path directly — the family byte-leak (a banished item's working twin
    # is reachable by a spelling the map can't match). normpath collapses '.', '..',
    # and '//' so every spelling of a delivered file resolves to the same key.
    norm = os.path.normpath(src)
    mapped = canonical_for(norm, case.archive_entries)
    p = mapped if mapped is not None else Path(norm)
    real = Path(os.path.realpath(p))
    case_real = Path(os.path.realpath(case.paths.case_dir))
    if real != case_real and case_real not in real.parents:
        raise VerbError("path outside case", 403)
    rel = real.relative_to(case_real)
    # duplicates/ is forbidden here for BOTH roles: perceptual dup-group
    # members are served ONLY through resolve_dup_member_path's closed
    # allowlist (scan-gated), never via the generic resolver.
    forbidden = {"metadata", "suspense", "sensitive", "quarantine", "duplicates",
                 case.paths.logs_dir.name}
    if forbidden & set(rel.parts):
        raise VerbError("forbidden path", 403)
    if case.role != "examiner":
        if not _under_any(real, family_media_roots(case)):
            # Family session asked for something outside the delivered/working trees
            # (originals, duplicates, junk, banished, export staging, …).
            raise VerbError("forbidden path", 403)
        # Delivered-set gate (defence in depth behind the root allow-list): a
        # family-served file must be one of the three surfaced categories —
        #   (1) a delivered canonical (mapped via archive_map — photos/videos),
        #   (2) a known video keyframe (poster/still; frames carry no archive_map
        #       entry by design — allowed via video_frame_map),
        #   (3) a surfaced audio recording (extracted/other/audio; no archive_map
        #       entry either — allowed via the precomputed deliverable_audio set).
        # Anything else under the working roots is an undelivered stray (e.g. a
        # working copy left behind when only the archive canonical was moved out)
        # and must not be served.
        if (mapped is None and norm not in (case.video_frame_map or {})
                and norm not in (getattr(case, "deliverable_audio", None) or set())):
            raise VerbError("forbidden path", 403)
    if not real.exists() or not real.is_file():
        raise VerbError("not found", 404)
    return real


def resolve_sidecar_path(case, path):
    """Resolve a transcript sidecar (`.json`/`.vtt`) whose path came verbatim from
    transcription_index — an absolute WORKSTATION path — to a real on-disk file,
    or return None if it cannot be trusted/served.

    This is the load-bearing security piece for the /api/transcript endpoint: the
    index path is attacker-influenceable via the `id` query arg, so it is NEVER read
    raw. It must resolve (after realpath, which collapses '.'/'..'/symlinks) to a
    file physically under <case>/extracted/other/audio/ — the same audio family
    media root the recordings themselves come from, and a strict subset of the case
    dir. Anything outside that tree (originals, metadata, /etc/passwd, a symlink out)
    → None, so the endpoint degrades to empty segments rather than leaking a file."""
    if not path:
        return None
    real = Path(os.path.realpath(os.path.normpath(path)))
    audio_root = Path(os.path.realpath(case.paths.extracted_dir / "other" / "audio"))
    if real != audio_root and audio_root not in real.parents:
        return None
    if not real.exists() or not real.is_file():
        return None
    return real


def read_sidecar_text(case, path):
    """Containment-checked text read of a transcript sidecar (see
    resolve_sidecar_path). Returns None for a refused/absent/unreadable file so
    transcript_detail degrades to empty segments."""
    real = resolve_sidecar_path(case, path)
    if real is None:
        return None
    try:
        return real.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def contained_child(root, name):
    """Resolve `name` as a direct child of `root`, or raise VerbError(403).

    Rejects path separators, '.'/'..', and anything whose realpath does not sit
    immediately inside `root` — so a client-supplied folder name can never escape
    the view tree (the rename-folder arbitrary-move hole). Returns the child Path;
    does not require it to exist."""
    if not name or "/" in name or "\\" in name or "\x00" in name \
            or os.path.dirname(name) or name in (".", ".."):
        raise VerbError("forbidden name", 403)
    root_real = Path(os.path.realpath(root))
    child = root / name
    child_real = Path(os.path.realpath(child))
    if child_real.parent != root_real:
        raise VerbError("forbidden name", 403)
    return child


def _quarantine_entry_for_src(case, src):
    """Find the quarantine manifest entry a media `src` refers to. Quarantined
    files have been moved out of the archive into <case>/quarantine/<filter>/, so
    a builder src (the canonical archive path used by the Quarantine group, or the
    original scan path used by the Sensitivity/Human lists) no longer resolves on
    disk. Match against every path the entry records (and basename as a fallback)."""
    if not src:
        return None
    real = os.path.realpath(src)
    base = os.path.basename(src.rstrip("/"))
    for e in case.quarantine_entries():
        paths = [e.get("quarantine_path"), e.get("canonical_path"), e.get("file")]
        for p in paths:
            if p and (p == src or os.path.realpath(p) == real):
                return e
        # Defensive fallback: quarantine filenames are unique within a case.
        if e.get("quarantine_path") and os.path.basename(e["quarantine_path"]) == base:
            return e
    return None


def resolve_dup_member_path(case, src):
    """Resolve a photo-stack member `src` through the CLOSED allowlist built at
    load time (perceptual_dup_groups × move ledger × dup_member_scan verdicts).
    This is the ONLY door into duplicates/ — the map already encodes every
    fail-closed gate (scan coverage, no nudity flag, keeper family-visible),
    so anything absent from it — including other files
    sitting in duplicates/perceptual/ — stays forbidden for both roles. The
    resolved file must still really live under <case>/duplicates/."""
    if not src:
        raise VerbError("missing src", 400)
    target = case.dup_member_paths.get(str(src))
    if target is None:
        raise VerbError("not a surfaced stack member", 404)
    real = Path(os.path.realpath(target))
    dup_root = Path(os.path.realpath(case.paths.case_dir / "duplicates"))
    if real != dup_root and dup_root not in real.parents:
        raise VerbError("forbidden path", 403)
    if not real.exists() or not real.is_file():
        raise VerbError("not found", 404)
    return real


def resolve_quarantine_media_path(case, src):
    """Resolve a media `src` that points at a quarantined item to its real on-disk
    location under <case>/quarantine/. Examiner-only. Used as a fallback by
    /media and /thumb so the Review queue can show quarantined media for
    triage — the generic resolver forbids the quarantine tree outright."""
    require_examiner(case)
    entry = _quarantine_entry_for_src(case, src)
    if entry is None:
        raise VerbError("not a quarantined item", 404)
    qp = Path(os.path.realpath(entry.get("quarantine_path") or ""))
    qroot = Path(os.path.realpath(case.paths.case_dir / "quarantine"))
    if qp != qroot and qroot not in qp.parents:
        raise VerbError("forbidden path", 403)
    if not qp.exists() or not qp.is_file():
        raise VerbError("not found", 404)
    return qp


def append_action(case, action, target, before, after, reversible, undoes=None):
    """Append one human-readable audit line (flock+O_APPEND, like MoveLedger).

    `undoes` (the undo_token of a prior action this entry reverses) is stored at
    the TOP LEVEL of the entry so is_undone()/verb_reset can find it — it used to
    be buried in `before`, so the already-undone guard never fired (undo was
    replayable and Reset re-inverted already-undone actions)."""
    ts = _now()
    token = action + "-" + hashlib.sha1(
        f"{ts}|{action}|{target}|{json.dumps(before, sort_keys=True)}".encode()
    ).hexdigest()[:12]
    entry = {"ts": ts, "actor": case.role, "action": action, "target": str(target),
             "before": before, "after": after, "reversible": bool(reversible),
             "undo_token": token}
    if undoes is not None:
        entry["undoes"] = undoes
    path = case.paths.metadata_dir / ACTIONS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, line)
        # Durable like MoveLedger.record: without this, a crash after a banish's
        # (fsynced) move_tracked but before the page cache flushes loses the action
        # line — the file is moved and the ledger knows, but History doesn't, so
        # the item has no surviving undo token and is unbanishable via the UI (R-6).
        os.fsync(fd)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return entry


def find_action(case, token):
    # One cached parse per generation shared with is_undone (was a full rescan
    # each) → verb_undo does a single ndjson parse instead of two (R-5).
    return case.actions_index()[1].get(token)


def is_undone(case, token):
    return token in case.actions_index()[2]


def build_view_index(case):
    """One walk of all view trees → {realpath(target): [view symlink paths]}. A
    batch verb builds this ONCE and looks each item up, instead of walking every
    view tree per item (a 100-item Discard on a 30k-symlink case was ~100 full
    tree walks / millions of syscalls)."""
    idx: dict = {}
    for view in VIEW_DIRS:
        root = case.paths.output_dir / view
        if not root.exists():
            continue
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                p = Path(dirpath) / fn
                if p.is_symlink():
                    idx.setdefault(os.path.realpath(p), []).append(str(p))
    return idx


def current_views(case, canonical, view_index=None):
    """Every symlink across the view trees currently pointing at `canonical`.

    Pass `view_index` (from build_view_index) to resolve by lookup instead of
    walking — batch callers build it once. A batch's per-item unlinks never
    invalidate other items' entries (distinct canonicals → distinct symlinks)."""
    target = os.path.realpath(canonical)
    if view_index is not None:
        return list(view_index.get(target, []))
    out = []
    for view in VIEW_DIRS:
        root = case.paths.output_dir / view
        if not root.exists():
            continue
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                p = Path(dirpath) / fn
                if p.is_symlink() and os.path.realpath(p) == target:
                    out.append(str(p))
    return out


# ── verbs ────────────────────────────────────────────────────────────────────────

def verb_confirm(case, payload):
    require_examiner(case)
    queue = payload.get("queue")
    item_id = payload.get("id")
    decision = payload.get("decision")
    if not queue or item_id is None or decision not in ("accept", "reject"):
        raise VerbError("confirm needs queue, id, decision in {accept,reject}")
    dpath = case.paths.metadata_dir / DECISIONS_FILE
    with _doc_lock(case):  # cross-process RMW guard (R-4)
        decisions = load_json(dpath, {}) or {}
        before = (decisions.get(queue, {}) or {}).get(str(item_id))
        decisions.setdefault(queue, {})[str(item_id)] = {
            "decision": decision, "value": payload.get("value"),
            "ts": _now(), "actor": case.role,
        }
        atomic_write_json(dpath, decisions)
    entry = append_action(case, "confirm", f"{queue}:{item_id}",
                          {"decision": before}, {"decision": decision}, reversible=True)
    return {"ok": True, "undo_token": entry["undo_token"]}


def verb_confirm_batch(case, payload):
    """Record one accept/reject decision for MANY queue items in a single
    request: one decisions-file read + one atomic write for the whole batch,
    instead of the N serialized /api/confirm POSTs the Review multi-select
    used to fire (each rewriting family_decisions.json — the pattern PR #299
    removed from Discard). Each item still gets its own audit entry, so
    History shows — and can individually undo — every decision."""
    require_examiner(case)
    items = payload.get("items") or []
    decision = payload.get("decision")
    if not items or decision not in ("accept", "reject"):
        raise VerbError("confirm/batch needs items[] and decision in {accept,reject}")
    for it in items:
        if not isinstance(it, dict) or not it.get("queue") or it.get("id") is None:
            raise VerbError("each item needs queue and id")
    dpath = case.paths.metadata_dir / DECISIONS_FILE
    ts = _now()
    # Build every decision and capture its `before` FIRST, then persist the
    # decisions file, and only THEN append the audit lines. The old order wrote N
    # durable audit lines inside the loop before the single atomic_write_json — a
    # crash (or a write failure that 500s the POST) forged audit for decisions
    # that never persisted (History showed resolved items that reappear in the
    # queue). Mirror the single verb_confirm: write-then-audit. A crash between
    # the two now loses audit lines for persisted decisions (recoverable), never
    # the reverse (C-4).
    pending = []  # (queue, item_id, before)
    with _doc_lock(case):  # cross-process RMW guard (R-4)
        decisions = load_json(dpath, {}) or {}
        for it in items:
            queue, item_id = it["queue"], str(it["id"])
            before = (decisions.get(queue, {}) or {}).get(item_id)
            decisions.setdefault(queue, {})[item_id] = {
                "decision": decision, "value": it.get("value"),
                "ts": ts, "actor": case.role,
            }
            pending.append((queue, item_id, before))
        atomic_write_json(dpath, decisions)
    tokens = []
    for queue, item_id, before in pending:
        entry = append_action(case, "confirm", f"{queue}:{item_id}",
                              {"decision": before}, {"decision": decision},
                              reversible=True)
        tokens.append(entry["undo_token"])
    return {"ok": True, "count": len(tokens), "undo_tokens": tokens}


def _banish_one(case, src, view_index=None):
    """Move one delivered item to family_banished/ + drop its view symlinks, and
    record the audit entry. Does NOT reload the case — the caller reloads once for
    the whole batch (a full case.load() per item makes multi-select Discard slow
    enough to look broken). Pass `view_index` for O(1) view lookup in a batch.
    Returns the action entry."""
    canonical = resolve_media_path(case, src)
    # Banish only ever HIDES a DELIVERED item: the resolved canonical must live
    # under output/archive/. For the examiner role resolve_media_path also returns
    # originals, extracted frames, etc.; moving one of those into family_banished/
    # would relocate a SOURCE file and break video_frame_map / person-detail
    # stills. Frames/originals are not banishable (C-6).
    archive_real = Path(os.path.realpath(case.paths.archive_dir))
    if canonical != archive_real and archive_real not in canonical.parents:
        raise VerbError("only delivered archive items can be banished", 400)
    views = current_views(case, canonical, view_index=view_index)
    sha = sha256_of(canonical)
    try:
        rel = canonical.relative_to(case.paths.archive_dir)
    except ValueError:
        rel = Path(canonical.name)
    dest = case.paths.output_dir / BANISHED_DIR / rel
    final = move_tracked(canonical, dest, reason="family:banish",
                         ledger=case.ledger, custody=case.custody)
    for v in views:
        try:
            Path(v).unlink()
        except OSError:
            pass
    return append_action(
        case, "banish", canonical,
        {"location": "archive", "canonical": str(canonical), "views": views, "sha256": sha},
        {"location": str(final)}, reversible=True)


def verb_banish(case, payload):
    """Discard one item ({src}) or a whole selection ({srcs:[...]}) in a single
    request. Every call refreshes served state ONCE afterward via
    reload_after_move() (docs/BACKLOG.md #13) rather than a full case.load() —
    a banish only ever changes which archive paths exist on disk and the move
    ledger, so re-deriving just universe/stacks is enough; it skips re-parsing
    case_summary.json/email_threads_index/metadata_index/etc., which measured
    as more than half of load()'s cost on a 190MB-class case and were never
    affected by a banish in the first place. The batch form still amortizes
    this one reload across N items instead of paying it per item (was the
    grid lagging seconds behind the click). Per-item failures are skipped, not
    fatal — a selection may include an already-moved or non-deliverable
    member."""
    require_examiner(case)
    srcs = payload.get("srcs")
    batch = srcs is not None
    if not batch:
        srcs = [payload.get("src")]
    # Walk the view trees ONCE for the whole batch (not per item).
    view_index = build_view_index(case) if batch else None
    tokens, skipped = [], 0
    for src in srcs:
        try:
            entry = _banish_one(case, src, view_index=view_index)
            tokens.append(entry["undo_token"])
        except VerbError:
            if not batch:
                raise            # single-item call keeps its strict error
            skipped += 1
    case.reload_after_move()     # ONCE for the whole batch — see docstring
    out = {"ok": True, "count": len(tokens), "skipped": skipped, "undo_tokens": tokens}
    if len(tokens) == 1:
        out["undo_token"] = tokens[0]   # single-item undo affordance (toast)
    return out


def _unbanish(case, entry):
    canonical = Path(entry["before"]["canonical"])
    banished = Path(entry["after"]["location"])
    restored = move_tracked(banished, canonical, reason="family:unbanish",
                            ledger=case.ledger, custody=case.custody)
    n = 0
    for v in entry["before"].get("views", []):
        try:
            relative_symlink(Path(v), restored)
            n += 1
        except OSError:
            pass
    return {"restored": str(restored), "views": n}


def _unjunk_one(case, jid):
    """Rescue ONE junk-routed image: move it out of extracted/photos_junk/ back to
    its original working location and restore a scene view symlink. Records the audit
    entry; does NOT reload the case (the caller reloads once for the whole batch).
    Returns the action entry. Never destroys — move_tracked only ever moves.

    `jid` is a scene_index.junk_results key = the image's ORIGINAL working path (the
    same id the examiner /thumb resolver uses)."""
    if not jid:
        raise VerbError("missing id", 400)
    junk = case.scene_index.get("junk_results", {}) or {}
    if str(jid) not in junk:
        raise VerbError("not a junk-routed item", 404)
    dest = Path(os.path.normpath(str(jid)))                     # original working location
    junk_file = case.paths.extracted_dir / PHOTOS_JUNK_DIRNAME / dest.name
    # Containment: BOTH the junk source and the restore destination must live under
    # the case dir — never move a file in or out of the tree (mirrors the resolver).
    case_real = os.path.realpath(case.paths.case_dir)
    for p in (junk_file, dest):
        rp = os.path.realpath(p)
        if rp != case_real and not rp.startswith(case_real + os.sep):
            raise VerbError("path outside case", 403)
    if junk_file.is_file():
        # route_junk model: the file was physically MOVED into extracted/photos_junk/
        # (a size-based junk rule). Move it back to its working location and restore a
        # scene-view symlink so it re-enters the gallery.
        restored = move_tracked(junk_file, dest, reason="family:unjunk",
                                ledger=case.ledger, custody=case.custody)
        view = case.paths.output_dir / SCENES_VIEW_DIR / UNJUNK_SCENE_FOLDER / dest.name
        view_str = None
        try:
            relative_symlink(view, restored)
            view_str = str(view)
        except OSError:
            pass
        return append_action(
            case, "unjunk", str(jid),
            {"id": str(jid), "junk_file": str(junk_file)},
            {"location": str(restored), "view": view_str}, reversible=True)
    # scene-classifier model (clip/llava): junk is a LABEL in scene_index.junk_results
    # and the file was NEVER moved — it's still at its working path `dest` (and, when it
    # has an archive entry, already delivered; the gallery just filters it live). There
    # is nothing to move: record a reversible junk_rescued overlay so build_photo_universe
    # and junk_rows stop treating it as junk. NEVER mutate scene_index.json (pipeline
    # output) — a decisions overlay, exactly like the other examiner verbs.
    if not dest.exists():
        raise VerbError(f"junk file not present: {junk_file.name}", 404)
    dpath = case.paths.metadata_dir / DECISIONS_FILE
    with _doc_lock(case):   # cross-process RMW guard (R-4)
        decisions = load_json(dpath, {}) or {}
        decisions.setdefault("junk_rescued", {})[str(jid)] = True
        atomic_write_json(dpath, decisions)
    return append_action(
        case, "unjunk", str(jid),
        {"id": str(jid), "label_only": True},
        {"rescued": True}, reversible=True)


def verb_unjunk(case, payload):
    """Un-junk ONE image ({id}/{src}) or a selection ({ids:[...]}): the audited,
    reversible inverse of route_junk / scene-junk routing. Moves the file out of
    extracted/photos_junk/ back to its delivered working location and restores a
    scene view symlink, via move_tracked + custody + the action log. EXAMINER-ONLY;
    never destroys (undo re-junks). Mirrors verb_banish's batch shape:
    per-item failures in a batch are skipped, not fatal, and the case reloads ONCE."""
    require_examiner(case)
    ids = payload.get("ids")
    batch = ids is not None
    if not batch:
        ids = [payload.get("id") or payload.get("src")]
    tokens, skipped = [], 0
    for jid in ids:
        try:
            entry = _unjunk_one(case, jid)
            tokens.append(entry["undo_token"])
        except VerbError:
            if not batch:
                raise            # single-item call keeps its strict error
            skipped += 1
    case.load()                  # ONCE for the whole batch
    out = {"ok": True, "count": len(tokens), "skipped": skipped, "undo_tokens": tokens}
    if len(tokens) == 1:
        out["undo_token"] = tokens[0]   # single-item undo affordance (toast)
    return out


def _release_scanned_one(case, src):
    """Mark ONE scanned-document/handwritten-letter image (BACKLOG #19) as "not a
    document": a purely-overlay decision, exactly like scene-classifier unjunk's
    label-only branch — the file never moves, so there is nothing to move_tracked.
    scanned_released is read by build_photo_universe (rejoins the gallery) and
    scanned_image_rows (leaves Correspondence). NEVER mutates scene_index.json.
    Does NOT reload the case (the caller reloads once for the whole batch)."""
    if not src:
        raise VerbError("missing id", 400)
    clip = case.scene_index.get("clip_results", {}) or {}
    rec = clip.get(str(src))
    if not rec or rec.get("category") not in SCENE_LABELS:
        raise VerbError("not a scanned-document item", 404)
    dpath = case.paths.metadata_dir / DECISIONS_FILE
    with _doc_lock(case):   # cross-process RMW guard (R-4)
        decisions = load_json(dpath, {}) or {}
        decisions.setdefault("scanned_released", {})[str(src)] = True
        atomic_write_json(dpath, decisions)
    return append_action(
        case, "release_scanned", str(src),
        {"id": str(src)}, {"released": True}, reversible=True)


def verb_release_scanned(case, payload):
    """Release ONE scanned-document/handwritten-letter image ({id}/{src}) or a
    selection ({ids:[...]}) back to Photos — "not a document" (#19). The examiner's
    corrective inverse of the scene classifier's scanned/letter tag: a decisions
    overlay flip, never a file move. EXAMINER-ONLY; reversible (undo re-hides the
    image into Correspondence). Mirrors verb_unjunk's batch shape: per-item
    failures in a batch are skipped, not fatal, and the case reloads ONCE."""
    require_examiner(case)
    ids = payload.get("ids")
    batch = ids is not None
    if not batch:
        ids = [payload.get("id") or payload.get("src")]
    tokens, skipped = [], 0
    for src in ids:
        try:
            entry = _release_scanned_one(case, src)
            tokens.append(entry["undo_token"])
        except VerbError:
            if not batch:
                raise            # single-item call keeps its strict error
            skipped += 1
    case.load()                  # ONCE for the whole batch
    out = {"ok": True, "count": len(tokens), "skipped": skipped, "undo_tokens": tokens}
    if len(tokens) == 1:
        out["undo_token"] = tokens[0]   # single-item undo affordance (toast)
    return out


def _unrelease_scanned(case, entry):
    """Undo a scanned-document release: drop the working path from the
    scanned_released overlay so build_photo_universe/scanned_image_rows treat it
    as a scanned document again. Purely an overlay edit — mirrors _rejunk's
    label-only branch."""
    src = entry["before"]["id"]
    dpath = case.paths.metadata_dir / DECISIONS_FILE
    removed = 0
    with _doc_lock(case):   # cross-process RMW guard (R-4)
        decisions = load_json(dpath, {}) or {}
        overlay = decisions.get("scanned_released") or {}
        if str(src) in overlay:
            del overlay[str(src)]
            removed = 1
            if overlay:
                decisions["scanned_released"] = overlay
            else:
                decisions.pop("scanned_released", None)
            atomic_write_json(dpath, decisions)
    return {"rereleased": str(src), "overlay_removed": removed}


def _rejunk(case, entry):
    """Undo an un-junk: move the file back to extracted/photos_junk/ and drop the
    scene view symlink the rescue created — only if it is still a SYMLINK (never
    unlink a real, source-bearing file: the never-destroy invariant, mirroring
    _unbanish/_requarantine)."""
    before, after = entry["before"], entry["after"]
    if before.get("label_only"):
        # Label-only rescue (scene-classifier junk): nothing was moved — drop the
        # working path from the junk_rescued overlay so the gallery filters it as
        # junk again. Mirror the never-destroy invariant: purely an overlay edit.
        jid = before["id"]
        dpath = case.paths.metadata_dir / DECISIONS_FILE
        removed = 0
        with _doc_lock(case):   # cross-process RMW guard (R-4)
            decisions = load_json(dpath, {}) or {}
            overlay = decisions.get("junk_rescued") or {}
            if str(jid) in overlay:
                del overlay[str(jid)]
                removed = 1
                if overlay:
                    decisions["junk_rescued"] = overlay
                else:
                    decisions.pop("junk_rescued", None)
                atomic_write_json(dpath, decisions)
        return {"rejunked": str(jid), "overlay_removed": removed}
    restored = Path(after["location"])
    junk_file = Path(before["junk_file"])
    junk_file.parent.mkdir(parents=True, exist_ok=True)
    moved = move_tracked(restored, junk_file, reason="family:rejunk",
                         ledger=case.ledger, custody=case.custody)
    removed = 0
    view = after.get("view")
    if view:
        p = Path(view)
        try:
            if p.is_symlink():
                p.unlink()
                removed = 1
        except OSError:
            pass
    return {"rejunked": str(moved), "views_removed": removed}


def verb_rename_person(case, payload, *, record=True):
    require_examiner(case)
    person_id = payload.get("person_id")
    new_name = (payload.get("new_name") or "").strip()
    clusters = case.face_clustering.get("person_clusters", {}) or {}
    if person_id not in clusters:
        raise VerbError(f"unknown person {person_id}", 404)
    fc_path = case.paths.metadata_dir / "face_clustering.json"
    fc = load_json(fc_path, {}) or {}
    identities = fc.setdefault("cluster_identities", {})
    before_ident = identities.get(person_id)
    old_folder = display_person_folder(person_id, identities)
    if new_name:
        identities[person_id] = {"name": new_name}
    else:
        identities.pop(person_id, None)
    new_folder = display_person_folder(person_id, identities)

    by_person = case.paths.output_dir / "by_person"
    old_path, new_path = by_person / old_folder, by_person / new_folder
    # A pre-existing target folder — or a person folder previously renamed
    # out-of-band via verb_rename_folder (disk changed, identity not) — used to
    # make the move a SILENT no-op, desyncing identity ("Alice") from folder
    # (Person_01). Refuse the collision loudly, and check it BEFORE persisting the
    # identity so a refused rename leaves BOTH sides untouched (C-5).
    if old_folder != new_folder and old_path.is_dir() and new_path.exists():
        raise VerbError(f"by_person/{new_folder} already exists", 409)
    atomic_write_json(fc_path, fc)

    moved = None
    if old_folder != new_folder and old_path.is_dir():
        os.rename(old_path, new_path)  # same device, atomic; interior links are relative
        moved = (str(old_path), str(new_path))
    out = {"ok": True, "new_name": new_name, "new_folder": new_folder}
    # record=False when called as an undo/reset inverse: apply the change but do
    # NOT log a new reversible action (that would pollute History and make Reset
    # re-invert a synthetic entry).
    if record:
        entry = append_action(
            case, "rename_person", person_id,
            {"identity": before_ident, "folder": old_folder},
            {"identity": identities.get(person_id), "folder": new_folder, "moved": moved},
            reversible=True)
        out["undo_token"] = entry["undo_token"]
    case.load()
    return out


def _rename_person_to(case, person_id, name, *, record=True):
    """Inverse helper for undo: re-apply a (possibly empty) name."""
    return verb_rename_person(case, {"person_id": person_id, "new_name": name or ""},
                              record=record)


def _person_folder_dir(case, person_id, folder):
    """Locate the on-disk by_person/ folder for a cluster DEFENSIVELY.

    Trusting the computed display name breaks when the folder was renamed
    out-of-band (verb_rename_folder updates disk but not cluster_identities), so
    remove_person would compute the wrong name, find nothing, and record no view
    symlinks for undo. If the computed folder is absent, fall back to scanning
    by_person/ for the folder whose symlinks point at THIS cluster's members
    (their delivered canonicals). C-5."""
    by_person = case.paths.output_dir / "by_person"
    direct = by_person / folder
    if direct.is_dir():
        return direct
    if not by_person.is_dir():
        return direct
    clusters = case.face_clustering.get("person_clusters", {}) or {}
    entries = case.archive_entries
    targets = {os.path.realpath(str(entries.get(m, m)))
               for m in (clusters.get(person_id) or [])}
    if not targets:
        return direct
    for sub in sorted(by_person.iterdir()):
        if not sub.is_dir():
            continue
        for p in sub.iterdir():
            if p.is_symlink() and os.path.realpath(p) in targets:
                return sub
    return direct


def verb_remove_person(case, payload):
    """Dissolve a person grouping (#6): remove the by_person/<folder> dir and drop
    the person from People. Photos STAY (shared faces unaffected). Reversible —
    the folder's symlinks are recorded and recreated on undo/reset."""
    require_examiner(case)
    person_id = payload.get("person_id")
    if person_id not in (case.face_clustering.get("person_clusters", {}) or {}):
        raise VerbError(f"unknown person {person_id}", 404)
    folder = display_person_folder(person_id, case.face_clustering.get("cluster_identities", {}) or {})
    fdir = _person_folder_dir(case, person_id, folder)
    views = []
    if fdir.is_dir():  # video-only persons (e.g. Person_05) have no folder — tolerate
        for p in sorted(fdir.iterdir()):
            # Only ever remove the view SYMLINKS (which is all this folder should
            # contain, and all we record for undo). A stray REAL file here is
            # source-bearing — never unlink it (never-destroy invariant); leaving
            # it also makes the rmdir below fail, so the folder is preserved.
            if p.is_symlink():
                views.append([str(p), os.path.realpath(p)])
                try:
                    p.unlink()
                except OSError:
                    pass
        try:
            fdir.rmdir()
        except OSError:
            pass
    dpath = case.paths.metadata_dir / DECISIONS_FILE
    with _doc_lock(case):  # cross-process RMW guard (R-4)
        decisions = load_json(dpath, {}) or {}
        decisions.setdefault("removed_persons", {})[person_id] = {
            "ts": _now(), "actor": case.role, "folder": folder}
        atomic_write_json(dpath, decisions)
    entry = append_action(case, "remove_person", person_id,
                          {"folder": folder, "views": views}, {}, reversible=True)
    case.load()
    return {"ok": True, "undo_token": entry["undo_token"]}


def verb_merge_persons(case, payload):
    """G-15: fold one face cluster (loser) into another (winner).

    Records a person_merges {loser_pid: winner_pid} entry in family_decisions.json —
    it NEVER edits the pipeline-authored face_clustering.json (a DECISIONS OVERLAY,
    exactly like removed_persons). At render (people_rows / person_detail / confirm
    queue) the loser's members fold into the winner and the loser disappears from
    People. Reversible (undo pops the overlay entry). Merge CHAINS (loser was itself
    a winner of another merge) are allowed and resolved TRANSITIVELY at render; a
    merge that would form a CYCLE is refused."""
    require_examiner(case)
    winner_pid = payload.get("winner_pid")
    loser_pid = payload.get("loser_pid")
    clusters = case.face_clustering.get("person_clusters", {}) or {}
    if winner_pid not in clusters:
        raise VerbError(f"unknown person {winner_pid}", 404)
    if loser_pid not in clusters:
        raise VerbError(f"unknown person {loser_pid}", 404)
    if winner_pid == loser_pid:
        raise VerbError("winner and loser must be different clusters", 400)
    dpath = case.paths.metadata_dir / DECISIONS_FILE
    with _doc_lock(case):  # cross-process RMW guard (R-4)
        decisions = load_json(dpath, {}) or {}
        merges = decisions.setdefault("person_merges", {})
        # Cycle guard: refuse if the winner already resolves (through existing
        # merges) back to the loser — that would make the chain loop. Non-cyclic
        # chains are fine (resolve_merge collapses them at render).
        if resolve_merge(winner_pid, merges) == loser_pid:
            raise VerbError("merge would create a cycle", 409)
        before = merges.get(loser_pid)
        merges[loser_pid] = winner_pid
        atomic_write_json(dpath, decisions)
    entry = append_action(case, "merge_persons", loser_pid,
                          {"winner": before}, {"winner": winner_pid}, reversible=True)
    case.load()
    return {"ok": True, "winner_pid": winner_pid, "loser_pid": loser_pid,
            "undo_token": entry["undo_token"]}


def verb_correspondent_merge_confirm(case, payload):
    """P2 #9: examiner confirms a possible-duplicate-identity suggestion
    (correspondent_duplicate_candidates) — folds every OTHER address in the
    cluster into the highest-volume address (the winner, chosen automatically
    so the examiner reviews identity, not bookkeeping). Recorded as a
    correspondent_merges {loser_addr: winner_addr} overlay in
    family_decisions.json — mirrors person_merges exactly: never mutates
    correspondent_frequency.json, folded in only at render (correspondents_data).
    Reversible; addresses are matched case-insensitively but stored lower-cased
    (the overlay key space)."""
    require_examiner(case)
    addresses = [str(a).strip().lower() for a in (payload.get("addresses") or [])
                 if str(a or "").strip()]
    if len(set(addresses)) < 2:
        raise VerbError("need at least 2 distinct addresses to merge", 400)
    freq_by_addr = {(c.get("address") or "").strip().lower(): c
                    for c in (case.correspondent_freq or [])}
    for a in addresses:
        if a not in freq_by_addr:
            raise VerbError(f"unknown correspondent address {a}", 404)
    winner = max(set(addresses), key=lambda a: freq_by_addr[a].get("total") or 0)
    losers = [a for a in set(addresses) if a != winner]
    cid = _duplicate_cluster_id(addresses)
    dpath = case.paths.metadata_dir / DECISIONS_FILE
    with _doc_lock(case):  # cross-process RMW guard (R-4)
        decisions = load_json(dpath, {}) or {}
        merges = decisions.setdefault("correspondent_merges", {})
        before = {a: merges.get(a) for a in losers}
        for a in losers:
            merges[a] = winner
        confirmed = decisions.setdefault("correspondent_merge_confirmed", [])
        if cid not in confirmed:
            confirmed.append(cid)
        atomic_write_json(dpath, decisions)
    entry = append_action(case, "correspondent_merge_confirm", winner,
                          {"merges": before, "cluster_id": cid},
                          {"winner": winner, "losers": losers}, reversible=True)
    case.load()
    return {"ok": True, "winner": winner, "losers": losers,
            "undo_token": entry["undo_token"]}


def verb_correspondent_merge_reject(case, payload):
    """P2 #9: examiner dismisses a possible-duplicate-identity suggestion as
    NOT the same person — recorded (by cluster_id) so it never resurfaces as a
    suggestion again. Purely suppresses the suggestion; no correspondent card
    or address is ever touched."""
    require_examiner(case)
    addresses = [str(a).strip().lower() for a in (payload.get("addresses") or [])
                 if str(a or "").strip()]
    if len(set(addresses)) < 2:
        raise VerbError("need at least 2 distinct addresses to reject", 400)
    cid = _duplicate_cluster_id(addresses)
    dpath = case.paths.metadata_dir / DECISIONS_FILE
    with _doc_lock(case):  # cross-process RMW guard (R-4)
        decisions = load_json(dpath, {}) or {}
        rejected = decisions.setdefault("correspondent_merge_rejected", [])
        if cid not in rejected:
            rejected.append(cid)
        atomic_write_json(dpath, decisions)
    entry = append_action(case, "correspondent_merge_reject", cid,
                          {}, {"addresses": sorted(set(addresses))}, reversible=True)
    case.load()
    return {"ok": True, "cluster_id": cid, "undo_token": entry["undo_token"]}


def verb_assign_face(case, payload):
    """G-15: assign an unidentified/noise face ({src}) to a person cluster
    ({person_id}).

    Records a face_assignments {src: person_id} entry in family_decisions.json —
    NEVER edits face_clustering.json. At render the face joins the target cluster's
    members and drops out of the confirm queue (its noise item stops reappearing).
    Reversible (undo pops the overlay entry)."""
    require_examiner(case)
    src = payload.get("src")
    person_id = payload.get("person_id")
    if not src:
        raise VerbError("assign_face needs src", 400)
    clusters = case.face_clustering.get("person_clusters", {}) or {}
    if person_id not in clusters:
        raise VerbError(f"unknown person {person_id}", 404)
    if src not in set(case.face_clustering.get("noise_files", []) or []):
        raise VerbError(f"not an unidentified face: {src}", 404)
    dpath = case.paths.metadata_dir / DECISIONS_FILE
    with _doc_lock(case):  # cross-process RMW guard (R-4)
        decisions = load_json(dpath, {}) or {}
        assigns = decisions.setdefault("face_assignments", {})
        before = assigns.get(src)
        assigns[src] = person_id
        atomic_write_json(dpath, decisions)
    entry = append_action(case, "assign_face", src,
                          {"person_id": before}, {"person_id": person_id}, reversible=True)
    case.load()
    return {"ok": True, "src": src, "person_id": person_id,
            "undo_token": entry["undo_token"]}


def _move_person_validate(case, src, to_pid, eff, decisions):
    """Validate ONE person-move against the effective clustering; return
    (before, from_pid) — the PRIOR face_placements value (undo datum) and the
    effective origin cluster. Raises VerbError on any failed precondition. Writes
    nothing (the caller performs the single batched write).

    Hard preconditions (spec §7 + review B):
      - src must NOT be a video frame (a frame is a viewport into its source video).
      - src must be a member in the EFFECTIVE clustering (a person cluster OR
        noise_files) — so an overlay-only member (legacy assignment / a prior move)
        is still movable.
      - src must be role-visible and delivered (resolve_media_path, exactly like
        verb_banish).
      - to must be a real person cluster in the effective view and NOT removed.
      - to != from (a no-op is refused).
    """
    if is_video_frame(src, set(case.video_frame_map or {})):
        raise VerbError("cannot move a video frame's identity", 400)
    clusters = eff.get("person_clusters", {}) or {}
    from_pid = None
    for pid, files in clusters.items():
        if src in files:
            from_pid = pid
            break
    is_noise = src in set(eff.get("noise_files", []) or [])
    if from_pid is None and not is_noise:
        raise VerbError(f"not a movable face: {src}", 404)
    canonical = resolve_media_path(case, src)
    if to_pid not in clusters:
        raise VerbError(f"unknown person {to_pid}", 409)
    if to_pid in set(case.removed_persons):
        raise VerbError(f"{to_pid} was removed — un-remove it first", 409)
    if to_pid == from_pid:
        raise VerbError("item is already in that person", 409)
    before = (decisions.get("face_placements", {}) or {}).get(src)
    return before, from_pid


def scene_move_categories(case, scene_placements=None):
    """The valid target-category set for a scene-move: the distinct gallery scene
    categories present (from the photo universe + any current scene_placements),
    MINUS the SCENE_LABELS buckets. A scanned-document category is excluded because
    build_photo_universe / scanned_image_rows read scene_index DIRECTLY (not the
    overlay), so moving INTO it would drop the item out of the gallery (review C)."""
    if scene_placements is None:
        scene_placements = case.decisions.get("scene_placements") or {}
    cats = {(info or {}).get("category") for info in (case.universe or {}).values()}
    cats |= set(scene_placements.values())
    cats -= set(SCENE_LABELS)
    cats.discard(None)
    return cats


def _move_scene_validate(case, src, to_cat, scene_placements):
    """Validate ONE scene-move — a facet RELABEL within the gallery universe (review
    C), NOT a cross-section re-file. Returns `before` (the prior scene_placements
    value, the undo datum). Raises VerbError on any failed precondition; writes nothing.

    Preconditions:
      - src must be in case.universe (a delivered gallery photo). A src excluded from
        the gallery (e.g. a scanned-document image) is refused — you cannot scene-move
        something that isn't in the gallery.
      - src must resolve (mirror verb_banish; a move never surfaces an undelivered
        item).
      - to must be a real gallery scene category and NOT a SCENE_LABELS bucket (moving
        into scanned-doc would drop the item out of the gallery).
      - to != the item's current (effective) scene (a no-op is refused).
    """
    universe = case.universe or {}
    if src not in universe:
        raise VerbError(f"not a gallery photo: {src}", 404)
    if to_cat in SCENE_LABELS:
        raise VerbError("cannot move into the scanned-document category", 400)
    if to_cat not in scene_move_categories(case, scene_placements):
        raise VerbError(f"unknown scene category {to_cat!r}", 400)
    canonical = resolve_media_path(case, src)
    current = scene_placements.get(src) or (universe[src] or {}).get("category")
    if to_cat == current:
        raise VerbError("item is already in that scene", 409)
    return scene_placements.get(src)


def _move_event_validate(case, src, to, event_placements):
    """Validate ONE event-move — re-file a gallery photo into a different event
    album (Move Phase 2). Returns `before` (the prior event_placements value, the
    undo datum). Raises VerbError on any failed precondition; writes nothing.

    The overlay is applied at render in photo_rows (event_placements overrides a
    row's effective event_id/event), so — like scene-move — this never touches
    geo_cluster_index.json or case_summary.json.

    Preconditions:
      - src must be in case.universe (a delivered gallery photo). A src not in the
        gallery (undelivered / quarantined / a scanned-doc image) is refused.
      - src must resolve (mirror verb_banish; a move never surfaces an undelivered
        item, and never makes one servable).
      - to must be a real album_id present in case_summary event_albums.
      - to != the item's current (effective) event_id (a no-op is refused).
    """
    universe = case.universe or {}
    if src not in universe:
        raise VerbError(f"not a gallery photo: {src}", 404)
    albums = event_album_titles(case.summary)   # {str(album_id): title}
    to = str(to)
    if to not in albums:
        raise VerbError(f"unknown album {to}", 409)
    canonical = resolve_media_path(case, src)
    current = event_placements.get(src)
    if current is None:
        tid = (case.geo_index.get(src, {}) or {}).get("gps_trip_cluster_id")
        current = str(tid) if (tid is not None and str(tid) in albums) else None
    if to == current:
        raise VerbError("item is already in that album", 409)
    return event_placements.get(src)


def doc_move_categories(case):
    """The valid target-category set for a document-move (§13.4): the canonical
    movable document categories from the PIPELINE config
    (`case_config.json document_categories`), falling back to the stdlib-pure
    DEFAULT_DOC_CATEGORIES constant, MINUS `account_credentials` (the sealed
    category is never a movable target, §13.3).

    load_case_config RAISES FileNotFoundError when case_config.json is absent
    (a hand-assembled / older served case dir), so the call is wrapped and falls
    back to the constant — the verb never 500s on a missing config."""
    try:
        cfg = load_case_config(case.paths.case_dir)
        names = [c.get("name") for c in (cfg.get("document_categories") or [])
                 if isinstance(c, dict) and c.get("name")]
    except FileNotFoundError:
        names = None
    if not names:
        names = list(DEFAULT_DOC_CATEGORIES)
    return {n for n in names if n and n != "account_credentials"}


def doc_subcategories(case):
    """The valid target set for a financial SUB-category move (§14.4): the
    financial subcategory names from `case_config.json financial_subcategories`
    (a list of {name,hint} dicts, same shape as document_categories), falling
    back to the stdlib-pure FINANCIAL_SUBCATEGORY_NAMES constant.

    THREE-CASE distinction (review #4 — do NOT copy doc_move_categories' blanket
    `if not names: fallback`):
      - case_config.json absent (FileNotFoundError) OR the key absent → fall back
        to the constant.
      - key PRESENT but an EMPTY list → the EMPTY set. An empty
        financial_subcategories DISABLES the second pass by design, so a sub-move
        has no valid targets and must be hidden/refused — it must NOT fall
        through to the constant.
      - present + non-empty → those names.
    """
    try:
        cfg = load_case_config(case.paths.case_dir)
        raw = cfg.get("financial_subcategories")
    except FileNotFoundError:
        raw = None
    if raw is None:
        # absent file or absent key → fall back to the constant
        raw = [{"name": n} for n in FINANCIAL_SUBCATEGORY_NAMES]
    # present-but-empty list stays empty (second pass disabled) → empty set.
    names = [c.get("name") for c in raw
             if isinstance(c, dict) and c.get("name")]
    return {n for n in names if n}


def _move_document_validate(case, src, to, doc_placements, subcategory=None):
    """Validate ONE document-move and RETURN THE VALUE TO RECORD in doc_placements
    (§13.3/§13.4, §14.3). Raises VerbError on any failed precondition; writes
    nothing. The caller (verb_move) supplies the prior value as the undo datum
    separately — this returns the NEW overlay value, a str (category-only, §13) OR
    the dict {"category":"financial","subcategory":<sub>} (a sub-move, §14).

    The overlay is applied at render in document_rows (the SOLE decoder of the raw
    value) — never touching case_summary.json's document_classifications.

    Preconditions:
      - src validation is MEMBERSHIP, NOT media resolution (§13.4, the #4 fatal
        fix): src must be present in summary.document_classifications with
        source != "email". Delivered doc bytes are frequently reaped, so
        resolve_media_path would 404 EVERY move — we validate membership instead.
        An email-sourced or unknown src is refused.
      - account_credentials seal, write-time (§13.3 guard 1): refuse if the src's
        PIPELINE category is account_credentials (403), or if to ==
        account_credentials (400). (A financial doc is never account_credentials,
        so the seal is inert for a sub-move but the guard stays.)
      - Category-only (subcategory omitted, §13): to must be in the movable
        category set; to != the item's effective category (no-op → 409).
      - Sub-move (subcategory given, §14.3): the effective category must be (or
        become) financial — `to` (if given) must be "financial", else the src's
        current effective category must be financial. subcategory must be in the
        movable financial-subcategory set (doc_subcategories). The effective
        (category, subcategory) tuple != (financial, subcategory) (no-op → 409).
    """
    # Membership validation (NOT resolve_media_path — doc bytes are reaped).
    rec = None
    for d in case.summary.get("document_classifications", []) or []:
        if d.get("file") == src:
            rec = d
            break
    if rec is None:
        raise VerbError(f"not a document: {src}", 404)
    if (rec.get("source") or "").lower() == "email":
        raise VerbError("email items are not category-movable", 400)
    derived = rec.get("category") or "miscellaneous"
    # account_credentials seal (write-time, §13.3 guard 1).
    if derived == "account_credentials":
        raise VerbError("account_credentials documents cannot be re-filed", 403)
    if to == "account_credentials":
        raise VerbError("cannot move a document into account_credentials", 400)

    # Decode the PRIOR placement value → effective (eff_cat, eff_sub), mirroring
    # the document_rows render decode (§14.2). A dict prior value would break a
    # bare string compare (review #1a), so decode before the no-op check.
    pipeline_sub = rec.get("subcategory")
    p = doc_placements.get(src)
    if isinstance(p, dict):
        eff_cat = p.get("category") or derived
        eff_sub = p["subcategory"] if "subcategory" in p else pipeline_sub
    elif isinstance(p, str):
        eff_cat, eff_sub = p, pipeline_sub
    else:
        eff_cat, eff_sub = derived, pipeline_sub
    if eff_cat != "financial":
        eff_sub = None            # subcategory meaningless outside financial

    if subcategory is not None:
        # SUB-MOVE (§14.3): effective category must be or become financial.
        if to is not None and to != "financial":
            raise VerbError("a subcategory can only be set on a financial target", 400)
        if to is None and eff_cat != "financial":
            raise VerbError("subcategory move requires a financial document "
                            "(or to=financial)", 400)
        if subcategory not in doc_subcategories(case):
            raise VerbError(f"unknown financial subcategory {subcategory!r}", 400)
        if (eff_cat, eff_sub) == ("financial", subcategory):
            raise VerbError("document is already in that sub-category", 409)
        return {"category": "financial", "subcategory": subcategory}

    # CATEGORY-ONLY (§13 path, unchanged semantics): records the string `to`.
    if to not in doc_move_categories(case):
        raise VerbError(f"unknown document category {to!r}", 400)
    if to == eff_cat:
        raise VerbError("document is already in that category", 409)
    return to


def verb_move(case, payload):
    """Move verb: correct an item's membership in a taxonomy VIEW by recording a
    placement overlay in family_decisions.json — a DECISIONS OVERLAY, exactly like
    merge_persons / assign_face. It NEVER edits the pipeline-authored indexes
    (face_clustering.json / scene_index.json stay byte-identical).

    Views:
      - "person" (Phase 1): face_placements {src: person_id}. At render
        (apply_face_overlay) src is REMOVED from whatever cluster holds it and
        re-appended to `to`; an emptied origin drops out of People.
      - "scene" (Phase 1.5): scene_placements {src: category}. At render (photo_rows)
        the row's `scene` facet is relabelled — a relabel WITHIN the gallery universe,
        never a cross-section re-file (review C).
      - "event" (Phase 2): event_placements {src: album_id}. At render (photo_rows)
        the row's effective `event_id`/`event` is overridden, re-filing the photo
        into `to`'s album and shifting the event-album view's derived counts.
      - "document" (Phase 2.5): doc_placements {src: category}. At render
        (document_rows) the row's effective category is overridden among the
        family-VISIBLE categories — never crossing the account_credentials family
        seal (§13.3), which is keyed on the DERIVED pipeline category at render.

    Payload (single):  {view, src, to, from?}
    Payload (batch):   {view, srcs:[...], to}
      The batch form validates + places every member under ONE _doc_lock + ONE
      atomic_write_json + ONE case.load(), appends a per-item audit line (so History
      shows/undoes each), and SKIPS an invalid member rather than failing the whole
      request (a selection may legitimately include a non-movable item). The single
      form keeps its strict error and its shipped {ok, src, to, from?, undo_token}
      response; the batch form returns {ok, count, skipped, undo_tokens} (+ a single
      undo_token when count==1, like verb_banish).

    Reversible: undo restores the prior placement (or pops it); _apply_inverse
    dispatches the overlay key by the recorded before.view.
    """
    require_examiner(case)
    view = payload.get("view")
    if view not in ("person", "scene", "event", "document"):
        raise VerbError("only person, scene, event, and document moves are supported", 400)
    to = payload.get("to")
    subcategory = payload.get("subcategory")
    # A document SUB-category move may omit `to` (category stays financial) when a
    # subcategory is given (§14.3); every other move requires `to`.
    if not to and not (view == "document" and subcategory):
        raise VerbError("move needs to", 400)
    srcs = payload.get("srcs")
    batch = srcs is not None
    if not batch:
        srcs = [payload.get("src")]

    key = {"person": "face_placements", "scene": "scene_placements",
           "event": "event_placements", "document": "doc_placements"}[view]
    dpath = case.paths.metadata_dir / DECISIONS_FILE
    # Effective clustering (person view) computed ONCE from the current decisions —
    # every member is validated against the same pre-batch snapshot.
    eff = case.effective_face_clustering() if view == "person" else None

    pending = []   # (src, before, from_pid) to audit AFTER the single write
    skipped = 0
    with _doc_lock(case):  # cross-process RMW guard (R-4) — ONE lock for the batch
        decisions = load_json(dpath, {}) or {}
        overlay = decisions.setdefault(key, {})
        scene_pl = decisions.get("scene_placements", {}) or {}
        event_pl = decisions.get("event_placements", {}) or {}
        doc_pl = decisions.get("doc_placements", {}) or {}
        for src in srcs:
            try:
                if not src:
                    raise VerbError("move needs src", 400)
                if view == "person":
                    before, from_pid = _move_person_validate(case, src, to, eff, decisions)
                    write_value = to
                elif view == "scene":
                    before, from_pid = _move_scene_validate(case, src, to, scene_pl), None
                    write_value = to
                elif view == "event":
                    before, from_pid = _move_event_validate(case, src, to, event_pl), None
                    write_value = to
                else:  # document
                    # The validator RETURNS the value to record (str for a §13
                    # category move, dict for a §14 sub-move); the prior value
                    # (the shape-agnostic undo datum) is the current overlay entry.
                    before, from_pid = doc_pl.get(src), None
                    write_value = _move_document_validate(case, src, to, doc_pl, subcategory)
                overlay[src] = write_value
                pending.append((src, before, from_pid))
            except VerbError:
                if not batch:
                    raise            # single-item call keeps its strict error
                skipped += 1
        atomic_write_json(dpath, decisions)   # ONE write for the whole batch

    # Per-item audit (append_action only GROWS family_actions.ndjson — separate from
    # the decisions file — so one line per placed item is correct). `before` is the
    # PRIOR placement value so undo restores it exactly (pop when there was none).
    # `after` is informational only (the inverse restores `before.from`); carry
    # the subcategory when present so History reads meaningfully for a sub-move.
    after = {"to": to}
    if subcategory is not None:
        after["subcategory"] = subcategory
    tokens, froms = [], []
    for src, before, from_pid in pending:
        entry = append_action(case, "move", src,
                              {"view": view, "from": before}, after,
                              reversible=True)
        tokens.append(entry["undo_token"])
        froms.append(from_pid)
    case.load()                  # ONCE for the whole batch

    if not batch:
        out = {"ok": True, "src": srcs[0], "to": to, "undo_token": tokens[0]}
        if view == "person":
            out["from"] = froms[0]
        if subcategory is not None:
            out["subcategory"] = subcategory
        return out
    out = {"ok": True, "count": len(tokens), "skipped": skipped, "undo_tokens": tokens}
    if subcategory is not None:
        out["subcategory"] = subcategory
    if len(tokens) == 1:
        out["undo_token"] = tokens[0]   # single-item undo affordance (toast)
    return out


def verb_rename_folder(case, payload, *, record=True):
    """Rename any view folder (event album, scene, person folder) on disk."""
    require_examiner(case)
    view = payload.get("view")
    old = payload.get("old_name")
    new = sanitize_person_name(payload.get("new_name") or "")
    if view not in VIEW_DIRS or not old or not new:
        raise VerbError(f"rename_folder needs view in {VIEW_DIRS}, old_name, new_name")
    root = case.paths.output_dir / view
    # Both names must be direct children of the view dir — never a path that
    # escapes it (old_name was previously unchecked: an arbitrary-directory move).
    old_path = contained_child(root, old)
    new_path = contained_child(root, new)
    if not old_path.is_dir():
        raise VerbError(f"no such folder {view}/{old}", 404)
    if new_path.exists():
        raise VerbError(f"{view}/{new} already exists", 409)
    os.rename(old_path, new_path)
    out = {"ok": True, "new_name": new}
    # record=False as an undo/reset inverse: apply but log no new action.
    if record:
        entry = append_action(case, "rename_folder", f"{view}/{old}",
                              {"view": view, "name": old}, {"view": view, "name": new},
                              reversible=True)
        out["undo_token"] = entry["undo_token"]
    return out


def _materialize_items(case, canonicals, dest):
    """Copy already-resolved canonical paths into dest, de-duplicating basenames
    (collections routinely contain same-named files), and write a manifest."""
    from tools.export_delivery import materialize

    dest.mkdir(parents=True, exist_ok=True)
    manifest = []
    used = set()
    for canonical in canonicals:
        name = canonical.name
        if name in used:  # avoid clobbering a same-named earlier item
            stem, suf = os.path.splitext(name)
            n = 1
            while f"{stem}_{n}{suf}" in used:
                n += 1
            name = f"{stem}_{n}{suf}"
        used.add(name)
        out = dest / name
        kind = materialize(canonical, out, allow_hardlink=False)
        manifest.append({"src": str(canonical), "dest": str(out),
                         "sha256": sha256_of(out), "kind": kind})
    mpath = dest / "export_manifest.json"
    atomic_write_json(mpath, {"ts": _now(), "actor": case.role, "items": manifest})
    return manifest, mpath


def _export_dest(case, dest_arg, *, default):
    """Resolve the export destination, confining the FAMILY role to
    output/family_export/. `dest` is caller-controlled JSON; unconfined it is an
    arbitrary-filesystem write (mkdir + shutil.copy2 clobber) reachable from the
    family role. The examiner (trusted, on the workstation — needs to export to an
    external USB) keeps an unconstrained dest."""
    if not dest_arg:
        return default
    if case.role == "examiner":
        return Path(dest_arg)
    base = Path(os.path.realpath(case.paths.output_dir / EXPORT_DIR))
    cand = Path(os.path.realpath(dest_arg))
    if cand != base and base not in cand.parents:
        raise VerbError("export dest must be under output/family_export", 403)
    return Path(dest_arg)


def _assert_family_export_allowed(case):
    """Verb-safe export-gate check (raises VerbError, never sys.exit). Re-reads the
    gate from case_summary.json so a gate flipped to blocked mid-session is honored
    even though the family instance never reloads its indexes."""
    if case.role != "family":
        return
    summary = load_json(case.paths.metadata_dir / "case_summary.json", {}) or case.summary
    reasons = family_block_reasons(summary)
    if reasons:
        raise VerbError("delivery is blocked: " + "; ".join(map(str, reasons)), 403)
    # E4 — a family-role session may export only from a RELEASED bundle. Re-read
    # the record from disk (verify does), so a revoke/stale mid-session is honored
    # even though the family instance never reloads its indexes.
    try:
        rec = release.load_release(case.paths)
    except release.ReleaseError as exc:
        raise VerbError(f"release record unreadable — export blocked ({exc})", 403)
    if rec is None:
        raise VerbError("this case has no release signature — export blocked", 403)
    r = release.verify(case.paths, rec, live=False)
    if not r.ok:
        raise VerbError(f"release signature not current ({r.reason}) — "
                        f"export blocked", 403)


def verb_export(case, payload):
    _assert_family_export_allowed(case)
    items = payload.get("items") or []
    if not items:
        raise VerbError("export needs items[]")
    dest = _export_dest(case, payload.get("dest"),
                        default=case.paths.output_dir / EXPORT_DIR)
    canonicals = []
    for src in items:
        canonical = resolve_media_path(case, src)
        canonicals.append(canonical)
    manifest, mpath = _materialize_items(case, canonicals, dest)
    append_action(case, "export", str(dest),
                  {"count": len(manifest)}, {"manifest": str(mpath)}, reversible=False)
    return {"ok": True, "count": len(manifest), "manifest": str(mpath)}


def _collection_ids(case, kind, key):
    """Resolve a collection (person/scene/place/category, or a curation
    favorites/collection) to its member src ids, server-side and authoritatively
    (the client only knows a sample; the SIDECAR is authoritative for curation
    MEMBERSHIP, resolve_media_path for deliverability at export time)."""
    if kind == "favorites":
        # Every starred item, in id order (deliverability filtered downstream).
        return sorted((case.curation_layer.get("favorites") or {}).keys())
    if kind == "curation_collection":
        coll = (case.curation_layer.get("collections") or {}).get(str(key))
        if coll is None:
            raise VerbError(f"unknown collection {key}", 404)
        return list(coll.get("members") or [])
    if kind == "person":
        # Map video-frame members to their source video (deduped) so a video-only
        # person exports the actual movies, not keyframe stills.
        fmap = case.video_frame_map or {}
        out, seen = [], set()
        for m in (case.face_clustering.get("person_clusters", {}) or {}).get(key, []):
            src = (fmap.get(m) or {}).get("source_video") if m in fmap else m
            if src and src not in seen:
                seen.add(src)
                out.append(src)
        return out
    if kind == "category":
        docs = document_rows(case.summary, case.ocr_index, case.role,
                             doc_placements=case.decisions.get("doc_placements"))
        if ":" in str(key):
            cat, sub = str(key).split(":", 1)
            return [r["file"] for r in docs
                    if r.get("category") == cat and (r.get("subcategory") or "uncategorized") == sub]
        return [r["file"] for r in docs if r.get("category") == key]
    photos = photo_rows(case.universe, case.metadata_index, case.geo_index, {},
                        event_album_titles(case.summary),
                        scene_placements=case.decisions.get("scene_placements"),
                        event_placements=case.decisions.get("event_placements"))
    if kind == "scene":
        return [r["id"] for r in photos if (r.get("scene") or "") == key]
    if kind == "place":
        return [r["id"] for r in photos if key in (r.get("trip"), r.get("place"))]
    if kind == "event":
        # Keyed by album_id (event_id), the effective placement-aware tag — so an
        # event-moved photo exports under its new album (Move Phase 2).
        return [r["id"] for r in photos if (r.get("event_id") or "") == str(key)]
    raise VerbError(f"unknown collection kind {kind!r}")


def verb_export_collection(case, payload):
    """Export every (delivered) member of a person/scene/place/category in one go.

    Member ids are resolved server-side then PRE-FILTERED to what is actually
    deliverable (skip-not-raise): a cluster legitimately includes banished /
    quarantined / undelivered members, and plain verb_export would 404 the whole
    batch on the first missing one. Undeliverable items are skipped."""
    _assert_family_export_allowed(case)
    kind = payload.get("kind")
    key = payload.get("key")
    # The curation "favorites" pseudo-collection needs no key (it's the whole star
    # set); every other kind is keyed.
    if not kind or (key is None and kind != "favorites"):
        raise VerbError("export/collection needs kind and key")
    ids = _collection_ids(case, kind, key)
    if not ids:
        raise VerbError(f"no items for {kind}={key}", 404)
    canonicals, seen, skipped = [], set(), 0
    for src in ids:
        try:
            canonical = resolve_media_path(case, src)
        except VerbError:
            skipped += 1
            continue
        if str(canonical) in seen:
            continue
        seen.add(str(canonical))
        canonicals.append(canonical)
    if not canonicals:
        raise VerbError(f"no deliverable items for {kind}={key}", 404)
    label = "favorites" if kind == "favorites" else (sanitize_person_name(str(key)) or "collection")
    dest = _export_dest(case, payload.get("dest"),
                        default=case.paths.output_dir / EXPORT_DIR / f"{kind}_{label}")
    manifest, mpath = _materialize_items(case, canonicals, dest)
    append_action(case, "export_collection", f"{kind}:{key}",
                  {"requested": len(ids)},
                  {"exported": len(manifest), "skipped": skipped, "manifest": str(mpath)},
                  reversible=False)
    return {"ok": True, "count": len(manifest), "skipped": skipped, "manifest": str(mpath)}


# ── curation layer verbs (examiner-first; additive, audited, reversible) ──────────
#
# Every curation verb: require_examiner → read-modify-write the sidecar under the
# cross-process _doc_lock (R-4) via atomic_write_json → append ONE reversible audit
# line. They touch NOTHING authoritative (no file move, no pipeline-index edit), so
# they are the opposite risk profile of Banish/Move. Undo/Reset reverse them through
# _apply_inverse. Operator free text (titles/notes) is length-capped here and
# escaped at every UI sink.

def _curation_path(case):
    return case.paths.metadata_dir / CURATION_FILE


def _load_curation(case):
    """Read-side of a curation RMW: the sidecar with all three sections present and
    the schema version stamped. Call only while holding _doc_lock(case)."""
    cur = load_json(_curation_path(case), {}) or {}
    cur.setdefault("schema_version", CURATION_SCHEMA_VERSION)
    cur.setdefault("favorites", {})
    cur.setdefault("collections", {})
    cur.setdefault("notes", {})
    return cur


def verb_favorite(case, payload):
    """Toggle a star on one item ({id, on: true|false}). `id` is any existing item
    id (archive_map key / thread_id / conversation_id / doc file) — no new id space."""
    require_examiner(case)
    iid = payload.get("id")
    if iid is None or iid == "":
        raise VerbError("favorite needs id")
    iid = str(iid)
    on = bool(payload.get("on"))
    with _doc_lock(case):
        cur = _load_curation(case)
        favs = cur["favorites"]
        before = favs.get(iid)
        if on:
            favs[iid] = {"ts": _now(), "actor": case.role}
        else:
            favs.pop(iid, None)
        atomic_write_json(_curation_path(case), cur)
    entry = append_action(case, "favorite", iid,
                          {"entry": before}, {"on": on}, reversible=True)
    return {"ok": True, "on": on, "undo_token": entry["undo_token"]}


def verb_collection_create(case, payload):
    """Create a named collection from a title; returns the generated slug. The title
    is operator free text (length-capped); the slug is a unique lookup key."""
    require_examiner(case)
    title = str(payload.get("title") or "").strip()[:MAX_TITLE_LEN]
    if not title:
        raise VerbError("collection/create needs a non-empty title")
    with _doc_lock(case):
        cur = _load_curation(case)
        collections = cur["collections"]
        base = _slugify(title)
        slug, n = base, 2
        while slug in collections:           # uniqueness: base, base-2, base-3, …
            slug = f"{base}-{n}"
            n += 1
        collections[slug] = {"title": title, "ts": _now(), "actor": case.role,
                             "members": []}
        atomic_write_json(_curation_path(case), cur)
    entry = append_action(case, "collection_create", slug,
                          {}, {"slug": slug, "title": title}, reversible=True)
    return {"ok": True, "slug": slug, "title": title, "undo_token": entry["undo_token"]}


def verb_collection_rename(case, payload):
    """Rename a collection's TITLE ({slug, title}); the slug (lookup key) is stable."""
    require_examiner(case)
    slug = str(payload.get("slug") or "")
    title = str(payload.get("title") or "").strip()[:MAX_TITLE_LEN]
    if not slug or not title:
        raise VerbError("collection/rename needs slug and a non-empty title")
    with _doc_lock(case):
        cur = _load_curation(case)
        coll = (cur["collections"] or {}).get(slug)
        if coll is None:
            raise VerbError(f"unknown collection {slug}", 404)
        before = coll.get("title")
        coll["title"] = title
        atomic_write_json(_curation_path(case), cur)
    entry = append_action(case, "collection_rename", slug,
                          {"title": before}, {"title": title}, reversible=True)
    return {"ok": True, "slug": slug, "title": title, "undo_token": entry["undo_token"]}


def verb_collection_delete(case, payload):
    """Delete a collection ({slug}) — DROPS THE COLLECTION, NEVER THE ITEMS (mirrors
    the MCP 'deleting a collection does not delete items' rule; the items keep every
    other view/favorite/note). Reversible: undo restores the whole collection dict."""
    require_examiner(case)
    slug = str(payload.get("slug") or "")
    if not slug:
        raise VerbError("collection/delete needs slug")
    with _doc_lock(case):
        cur = _load_curation(case)
        coll = (cur["collections"] or {}).pop(slug, None)
        if coll is None:
            raise VerbError(f"unknown collection {slug}", 404)
        atomic_write_json(_curation_path(case), cur)
    entry = append_action(case, "collection_delete", slug,
                          {"collection": coll}, {}, reversible=True)
    return {"ok": True, "slug": slug, "undo_token": entry["undo_token"]}


def verb_collection_add(case, payload):
    """Add item ids to a collection ({slug, ids:[...]}). Records ONLY the ids that
    were actually newly added, so undo removes exactly those (and a re-add is
    idempotent)."""
    require_examiner(case)
    slug = str(payload.get("slug") or "")
    ids = payload.get("ids") or []
    if not slug or not isinstance(ids, list):
        raise VerbError("collection/add needs slug and ids[]")
    with _doc_lock(case):
        cur = _load_curation(case)
        coll = (cur["collections"] or {}).get(slug)
        if coll is None:
            raise VerbError(f"unknown collection {slug}", 404)
        members = coll.setdefault("members", [])
        present = set(members)
        added = []
        for i in ids:
            s = str(i)
            if s and s not in present:
                if len(members) >= MAX_COLLECTION_MEMBERS:
                    break
                members.append(s)
                present.add(s)
                added.append(s)
        atomic_write_json(_curation_path(case), cur)
    entry = append_action(case, "collection_add", slug,
                          {"added": added}, {"count": len(added)}, reversible=True)
    return {"ok": True, "slug": slug, "added": len(added),
            "undo_token": entry["undo_token"]}


def verb_collection_remove(case, payload):
    """Remove item ids from a collection ({slug, ids:[...]}) — drops membership only,
    never the items. Records ONLY the ids that were actually present, so undo re-adds
    exactly those."""
    require_examiner(case)
    slug = str(payload.get("slug") or "")
    ids = payload.get("ids") or []
    if not slug or not isinstance(ids, list):
        raise VerbError("collection/remove needs slug and ids[]")
    want = {str(i) for i in ids}
    with _doc_lock(case):
        cur = _load_curation(case)
        coll = (cur["collections"] or {}).get(slug)
        if coll is None:
            raise VerbError(f"unknown collection {slug}", 404)
        members = coll.get("members") or []
        removed = [m for m in members if m in want]
        coll["members"] = [m for m in members if m not in want]
        atomic_write_json(_curation_path(case), cur)
    entry = append_action(case, "collection_remove", slug,
                          {"removed": removed}, {"count": len(removed)}, reversible=True)
    return {"ok": True, "slug": slug, "removed": len(removed),
            "undo_token": entry["undo_token"]}


def verb_note_set(case, payload):
    """Attach / replace a note on an item ({id, text}). Operator free text,
    length-capped; escaped at every UI sink. Reversible (undo restores the prior
    note, or clears it if there was none)."""
    require_examiner(case)
    iid = payload.get("id")
    if iid is None or iid == "":
        raise VerbError("note/set needs id")
    iid = str(iid)
    text = str(payload.get("text") or "")[:MAX_NOTE_LEN]
    if not text.strip():
        raise VerbError("note/set needs non-empty text (use note/clear to remove)")
    with _doc_lock(case):
        cur = _load_curation(case)
        notes = cur["notes"]
        before = notes.get(iid)
        notes[iid] = {"text": text, "ts": _now(), "actor": case.role}
        atomic_write_json(_curation_path(case), cur)
    entry = append_action(case, "note_set", iid,
                          {"entry": before}, {"text": text}, reversible=True)
    return {"ok": True, "undo_token": entry["undo_token"]}


def verb_note_clear(case, payload):
    """Remove an item's note ({id}). Reversible (undo restores it)."""
    require_examiner(case)
    iid = payload.get("id")
    if iid is None or iid == "":
        raise VerbError("note/clear needs id")
    iid = str(iid)
    with _doc_lock(case):
        cur = _load_curation(case)
        before = cur["notes"].pop(iid, None)
        atomic_write_json(_curation_path(case), cur)
    entry = append_action(case, "note_clear", iid,
                          {"entry": before}, {}, reversible=True)
    return {"ok": True, "undo_token": entry["undo_token"]}


def _match_quarantine_entry(case, key):
    """Resolve a quarantine action key to exactly one manifest entry.

    `canonical_path` is unique across a case and is the stable action key the
    Review builders now emit, so it is matched FIRST. Basenames are NOT unique
    across filter dirs (two IMG_0001.jpg quarantined under different filters share
    a basename), so the legacy bare-basename fallback could act on the wrong
    entry — it is now accepted ONLY when it resolves to exactly one entry;
    anything ambiguous raises 409 (use canonical_path). Returns the entry or None
    (unknown key). C-3."""
    entries = case.quarantine_entries()
    for e in entries:
        if e.get("canonical_path") == key:
            return e
    matches = [e for e in entries
               if os.path.basename(e.get("file", "")) == key
               or os.path.basename(e.get("quarantine_path", "")) == key]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise VerbError(f"ambiguous quarantine key {key!r}; use canonical_path", 409)
    return None


def _release_one(case, key):
    """Release ONE quarantined item back into the delivery tree. Raises
    VerbError on any per-item failure (unknown key, file already gone) —
    the caller decides whether that is fatal (single-item) or skippable
    (batch, mirrors _banish_one)."""
    entry = _match_quarantine_entry(case, key)
    if entry is None:
        raise VerbError(f"no quarantine entry for {key}", 404)
    canonical = Path(entry["canonical_path"])
    quarantined = Path(entry["quarantine_path"])
    if not quarantined.exists():
        raise VerbError(f"quarantined file missing: {quarantined}", 404)
    canonical.parent.mkdir(parents=True, exist_ok=True)
    restored = move_tracked(quarantined, canonical, reason="family:release",
                            ledger=case.ledger, custody=case.custody)
    recreated = 0
    for v in entry.get("view_paths", []) or []:
        try:
            relative_symlink(Path(v), restored)
            recreated += 1
        except OSError:
            pass
    _rewrite_quarantine(case, drop=entry, add_released=entry)
    act = append_action(case, "release", canonical,
                        {"entry": entry}, {"location": str(restored), "views": recreated},
                        reversible=True)
    return act, str(restored)


def verb_release(case, payload):
    """Release a quarantined item back into the delivery tree (the post-review
    inverse of sensitive_scan's quarantine). Audited via move_tracked + custody
    + action log — NOT release_quarantine.release_entry, which does raw moves.

    #17: also accepts a batch ({canonical_paths: [...]}), mirroring verb_banish
    exactly — every item is released, then the case reloads ONCE for the whole
    selection instead of once per item. Per-item failures are skipped, not
    fatal, in batch mode (a selection may include an already-released or
    missing member); a single-item call keeps its strict 404."""
    require_examiner(case)
    keys = payload.get("canonical_paths")
    batch = keys is not None
    if not batch:
        keys = [payload.get("canonical_path") or payload.get("id")]
        if not keys[0]:
            raise VerbError("release needs canonical_path (or id)")
    tokens, skipped = [], 0
    restored_last = None
    for key in keys:
        try:
            act, restored_last = _release_one(case, key)
            tokens.append(act["undo_token"])
        except VerbError:
            if not batch:
                raise
            skipped += 1
    case.load()
    out = {"ok": True, "count": len(tokens), "skipped": skipped, "undo_tokens": tokens}
    if len(tokens) == 1:
        out["undo_token"] = tokens[0]
        out["restored"] = restored_last
    return out


def _rewrite_quarantine(case, *, drop=None, add_released=None, add_entry=None,
                        drop_released=None):
    """Atomic update of quarantine_manifest.json: move entries between the
    'entries' (pending) and 'released' lists. Keyed on canonical_path."""
    mpath = case.paths.metadata_dir / QUARANTINE_MANIFEST

    def _key(e):
        return e.get("canonical_path")

    with _doc_lock(case):  # cross-process RMW guard (R-4)
        manifest = load_json(mpath, {}) or {}
        entries = manifest.get("entries", []) or []
        released = manifest.get("released", []) or []
        if drop is not None:
            entries = [e for e in entries if _key(e) != _key(drop)]
        if drop_released is not None:
            released = [e for e in released if _key(e) != _key(drop_released)]
        if add_released is not None:
            released.append(add_released)
        if add_entry is not None:
            entries.append(add_entry)
        manifest["entries"] = entries
        manifest["released"] = released
        atomic_write_json(mpath, manifest)


def _requarantine(case, entry):
    """Undo a release: move the file back to quarantine, drop the recreated views,
    and restore its pending manifest entry."""
    canonical = Path(entry["canonical_path"])
    quarantined = Path(entry["quarantine_path"])
    quarantined.parent.mkdir(parents=True, exist_ok=True)
    moved = move_tracked(canonical, quarantined, reason="family:requarantine",
                         ledger=case.ledger, custody=case.custody)
    removed = 0
    for v in entry.get("view_paths", []) or []:
        try:
            p = Path(v)
            # NEVER unlink a real file — only the view SYMLINKS this release
            # recreated. A real file at a recorded view path is source-bearing
            # (never-destroy invariant); leaving it is correct. Mirrors the guard
            # in verb_remove_person. The prior `or p.exists()` could delete a real
            # file that came to occupy the path after the release.
            if p.is_symlink():
                p.unlink()
                removed += 1
        except OSError:
            pass
    _rewrite_quarantine(case, add_entry=entry, drop_released=entry)
    return {"requarantined": str(moved), "views_removed": removed}


def verb_demote_ranked(case, payload):
    """Demote an item from the Overview 'Most Significant' list (#12). NOT a
    discard: nothing moves on disk; it only suppresses the ranking entry.
    Reversible (undo deletes the suppression)."""
    require_examiner(case)
    key = payload.get("key")
    if not key:
        raise VerbError("demote needs key")
    dpath = case.paths.metadata_dir / DECISIONS_FILE
    with _doc_lock(case):  # cross-process RMW guard (R-4)
        decisions = load_json(dpath, {}) or {}
        demoted = decisions.setdefault("ranked_demoted", {})
        before = key in demoted
        demoted[key] = {"ts": _now(), "actor": case.role, "label": payload.get("label")}
        atomic_write_json(dpath, decisions)
    entry = append_action(case, "demote_ranked", key,
                          {"present": before}, {"label": payload.get("label")}, reversible=True)
    return {"ok": True, "undo_token": entry["undo_token"]}


def verb_demote_email(case, payload):
    """Demote one email thread out of the top of the Emails significance sort
    (#email). NOT a discard — nothing moves and the thread stays visible; it just
    drops to the bottom band. A toggle: `restore: true` (or demoting an
    already-demoted thread) lifts it back. Reversible + audited."""
    require_examiner(case)
    tid = payload.get("thread_id")
    if not tid:
        raise VerbError("demote/email needs thread_id")
    tid = str(tid)
    dpath = case.paths.metadata_dir / DECISIONS_FILE
    with _doc_lock(case):  # cross-process RMW guard (R-4)
        decisions = load_json(dpath, {}) or {}
        demoted = decisions.setdefault("email_demoted", {})
        restore = bool(payload.get("restore")) or tid in demoted
        if restore:
            demoted.pop(tid, None)
        else:
            demoted[tid] = {"ts": _now(), "actor": case.role, "subject": payload.get("subject")}
        atomic_write_json(dpath, decisions)
    action = "restore_email" if restore else "demote_email"
    entry = append_action(case, action, tid,
                          {"subject": payload.get("subject")},
                          {"demoted": not restore}, reversible=True)
    return {"ok": True, "demoted": not restore, "undo_token": entry["undo_token"]}


def verb_promote_vital(case, payload):
    """PROMOTE a near-miss to a vital document (examiner-only).

    The affirmative counterpart to reviewing the near-miss list: "the pipeline
    said no, I have read it, and this IS the document". Without it the near-miss
    review is a dead end — the examiner could see the near-miss and not act on it.

    Keyed by the SAME target::path composite the other three vital verbs use, so a
    promoted item can subsequently be dismissed or reassigned with no new
    machinery. Pure family_decisions overlay (`vital_doc_promoted`) — the pipeline
    indexes vital_doc_candidates.json and vital_doc_confirmed.json are NEVER
    mutated, so re-running the stage cannot be confused by a human decision and
    the promotion survives it. Reversible + audited.

    A promoted item is `reviewed` by construction and so clears the release gate:
    the examiner personally asserting the document is a strictly stronger act than
    confirming a match the pipeline proposed.

    An optional `to_target` promotes AND reassigns in one go ("this is a vital
    document, but it's a deed, not a will"). Both overlay writes happen under one
    lock with ONE action entry, so undo reverses the pair — a promote that half
    survived its own reassign would leave the item filed under the wrong category
    with no way back. The promotion record keeps the ORIGINAL target (the bucket
    whose near-miss list it came from) and `vital_doc_target` carries the display
    override, which is exactly how a reassigned CONFIRMED item already works.
    """
    require_examiner(case)
    ids = payload.get("ids")
    batch = ids is not None
    if not batch:
        iid = payload.get("id")
        if not iid:
            raise VerbError("promote needs id")
        ids = [iid]
    to_target = payload.get("to_target")
    if to_target is not None:
        to_target = str(to_target)
        if to_target not in VITAL_DOC_LABELS:  # the canonical target set
            raise VerbError(f"unknown vital target {to_target}", 400)
    # #17: batch promote does not accept to_target — reassigning while
    # multi-selecting conflates two decisions ("these are vital" + "under a
    # DIFFERENT category than they were found in") into one confusing bulk
    # action; reassign stays a per-row "Reassign…" affordance.
    if batch and to_target is not None:
        raise VerbError("batch promote does not support to_target", 400)
    reason = payload.get("reason")
    tokens, skipped = [], 0
    for raw_iid in ids:
        try:
            iid = str(raw_iid)
            # target::path — the id carries both, so a promotion needs no extra lookup.
            if "::" not in iid:
                raise VerbError("promote needs a target::path id")
            target, path = iid.split("::", 1)
            if not target or not path:
                raise VerbError("promote needs a target::path id")
            # Only a REAL candidate hit may be promoted. Without this an arbitrary
            # path could be injected onto the vital checklist through the id alone.
            candidates = load_json(
                case.paths.metadata_dir / "vital_doc_candidates.json", None)
            hits = ((candidates or {}).get(target) or {}).get("hits") or []
            if not any(isinstance(h, dict) and h.get("path") == path for h in hits):
                raise VerbError("not a candidate for that vital-document target")
            dpath = case.paths.metadata_dir / DECISIONS_FILE
            with _doc_lock(case):  # cross-process RMW guard (R-4)
                decisions = load_json(dpath, {}) or {}
                promoted = decisions.setdefault("vital_doc_promoted", {})
                before = promoted.get(iid)
                promoted[iid] = {"target": target, "path": path, "reason": reason,
                                 "ts": _now(), "actor": case.role}
                # Promote and dismiss are opposite rulings on the same document; a
                # promote un-dismisses (dismissal is keyed by path), mirroring confirm.
                (decisions.get("vital_doc_dismissed") or {}).pop(path, None)
                before_target = None
                if to_target is not None:
                    overlay = decisions.setdefault("vital_doc_target", {})
                    before_target = overlay.get(iid)
                    overlay[iid] = to_target
                atomic_write_json(dpath, decisions)
            entry = append_action(case, "promote_vital", iid,
                                  {"promoted": before, "target": before_target,
                                   "retargeted": to_target is not None},
                                  {"promoted": {"reason": reason}, "target": to_target},
                                  reversible=True)
            tokens.append(entry["undo_token"])
        except VerbError:
            if not batch:
                raise
            skipped += 1
    case.load()
    out = {"ok": True, "count": len(tokens), "skipped": skipped, "undo_tokens": tokens}
    if len(tokens) == 1:
        out["undo_token"] = tokens[0]
    return out


def verb_dismiss_vital(case, payload):
    """DISMISS a document as "not a vital document" (examiner-only). "Not a vital
    document" is a statement about the DOCUMENT, not one categorization, so it drops
    the doc from EVERY vital category it matched — the dismissal is keyed by PATH
    (extracted from the target::path item id). NOT a discard: the document itself is
    untouched and still browsable in the Documents list; only its checklist
    membership is suppressed. Pure family_decisions overlay (vital_doc_dismissed,
    keyed by path) — the pipeline index vital_doc_confirmed.json is NEVER mutated.
    Reversible + audited."""
    require_examiner(case)
    ids = payload.get("ids")
    batch = ids is not None
    if not batch:
        iid = payload.get("id")
        if not iid:
            raise VerbError("dismiss needs id")
        ids = [iid]
        single_path = payload.get("path")   # only meaningful/used for a single id
    tokens, skipped = [], 0
    for raw_iid in ids:
        try:
            iid = str(raw_iid)
            # The item id is target::path; a dismiss applies to the whole document,
            # so we suppress by PATH — which drops it from every matched category.
            path = (single_path if not batch else None) \
                or (iid.split("::", 1)[1] if "::" in iid else iid)
            dpath = case.paths.metadata_dir / DECISIONS_FILE
            with _doc_lock(case):  # cross-process RMW guard (R-4)
                decisions = load_json(dpath, {}) or {}
                dismissed = decisions.setdefault("vital_doc_dismissed", {})
                before = path in dismissed
                dismissed[path] = {"ts": _now(), "actor": case.role}
                atomic_write_json(dpath, decisions)
            entry = append_action(case, "dismiss_vital", path,
                                  {"present": before}, {}, reversible=True)
            tokens.append(entry["undo_token"])
        except VerbError:
            if not batch:
                raise
            skipped += 1
    case.load()
    out = {"ok": True, "count": len(tokens), "skipped": skipped, "undo_tokens": tokens}
    if len(tokens) == 1:
        out["undo_token"] = tokens[0]
    return out


def verb_reassign_vital(case, payload):
    """REASSIGN a vital-document match to a different target (examiner-only). "This
    isn't a will, it's a deed" → move the checklist item to another vital target;
    the old target reverts toward "not found" if it is left empty.

    `scope` (default "single") controls how many categories move when the document
    matched more than one vital target:
      - "single": retarget only the clicked item (its target::path id).
      - "global": retarget EVERY confirmed match of this document (all categories),
        expanded to one item_id override per match so the reader stays uniform.

    Pure family_decisions overlay (vital_doc_target: {id: new_target}) — the pipeline
    index vital_doc_confirmed.json is NEVER mutated. Reversible + audited."""
    require_examiner(case)
    iid = payload.get("id")
    to_target = payload.get("to_target")
    if not iid or not to_target:
        raise VerbError("reassign needs id and to_target")
    iid = str(iid)
    if to_target not in VITAL_DOC_LABELS:  # the canonical 13-target set
        raise VerbError(f"unknown vital target {to_target}", 400)
    scope = str(payload.get("scope") or "single").lower()
    if scope not in ("single", "global"):
        raise VerbError("scope must be 'single' or 'global'", 400)
    path = iid.split("::", 1)[1] if "::" in iid else iid
    dpath = case.paths.metadata_dir / DECISIONS_FILE
    with _doc_lock(case):  # cross-process RMW guard (R-4)
        decisions = load_json(dpath, {}) or {}
        retarget = decisions.setdefault("vital_doc_target", {})
        if scope == "global":
            # Every confirmed item_id (across all categories) for this document path.
            confirmed = load_json(case.paths.metadata_dir / "vital_doc_confirmed.json", None)
            confirmed = confirmed if isinstance(confirmed, list) else []
            ids = sorted({vital_doc_item_id(c.get("target"), path)
                          for c in confirmed if c.get("path") == path}) or [iid]
            # No-op iff every match already resolves to to_target.
            changed = any((retarget.get(k) or k.split("::", 1)[0]) != to_target
                          for k in ids)
            if not changed:
                raise VerbError("already assigned to that target", 409)
            before = {"scope": "global", "map": {k: retarget.get(k) for k in ids}}
            for k in ids:
                retarget[k] = to_target
            audit_target = path
        else:
            prior = retarget.get(iid)
            # Effective current target = a prior override, else the ORIGINAL target
            # encoded in the composite id (target::path).
            current = prior or iid.split("::", 1)[0]
            if to_target == current:
                raise VerbError("already assigned to that target", 409)  # no-op
            retarget[iid] = to_target
            before = {"scope": "single", "from": prior}
            audit_target = iid
        atomic_write_json(dpath, decisions)
    entry = append_action(case, "reassign_vital", audit_target,
                          before, {"to": to_target}, reversible=True)
    case.load()
    return {"ok": True, "undo_token": entry["undo_token"]}


def verb_confirm_vital(case, payload):
    """CONFIRM a vital-document match as reviewed and vouched-for (examiner-only).

    The state-changing "yes, this IS the document" counterpart to dismiss/reassign
    — the vital checklist previously had no affirmative action, so the release gate
    could not require the examiner to have looked. Optional reason. Pure
    family_decisions overlay (`vital_doc_reviewed`, keyed by the item id) — the
    pipeline index vital_doc_confirmed.json is NEVER mutated. Reversible + audited.
    """
    require_examiner(case)
    iid = payload.get("id")
    if not iid:
        raise VerbError("confirm/vital needs id")
    iid = str(iid)
    reason = payload.get("reason")
    dpath = case.paths.metadata_dir / DECISIONS_FILE
    with _doc_lock(case):  # cross-process RMW guard (R-4)
        decisions = load_json(dpath, {}) or {}
        reviewed = decisions.setdefault("vital_doc_reviewed", {})
        before = reviewed.get(iid)
        reviewed[iid] = {"reason": reason, "ts": _now(), "actor": case.role}
        # confirm and dismiss are mutually exclusive dispositions of the same
        # document; a confirm un-dismisses (dismissal is keyed by path).
        path = iid.split("::", 1)[1] if "::" in iid else iid
        (decisions.get("vital_doc_dismissed") or {}).pop(path, None)
        atomic_write_json(dpath, decisions)
    entry = append_action(case, "confirm_vital", iid,
                          {"reviewed": before}, {"reviewed": {"reason": reason}},
                          reversible=True)
    return {"ok": True, "undo_token": entry["undo_token"]}


def _discard_quarantine_one(case, key):
    """Discard ONE quarantined item. Raises VerbError on any per-item failure
    — the caller decides whether that is fatal (single-item) or skippable
    (batch, mirrors _banish_one/_release_one)."""
    entry = _match_quarantine_entry(case, key)
    if entry is None:
        raise VerbError(f"no quarantine entry for {key}", 404)
    quarantined = Path(entry["quarantine_path"])
    if not quarantined.exists():
        raise VerbError(f"quarantined file missing: {quarantined}", 404)
    dest = case.paths.output_dir / BANISHED_DIR / "quarantine" / quarantined.name
    moved = move_tracked(quarantined, dest, reason="family:discard-quarantine",
                         ledger=case.ledger, custody=case.custody)
    _rewrite_quarantine(case, drop=entry)
    act = append_action(case, "discard_quarantine", entry["canonical_path"],
                        {"entry": entry, "banished": str(moved)}, {"location": str(moved)},
                        reversible=True)
    return act, str(moved)


def verb_discard_quarantine(case, payload):
    """Discard a quarantined item from view (#9): move quarantine_path →
    family_banished/quarantine/ and drop its manifest entry. Distinct from
    verb_release (→ delivery). Audited + reversible.

    #17: also accepts a batch ({canonical_paths: [...]}), mirroring
    verb_banish/verb_release — every item is discarded, then the case reloads
    ONCE for the whole selection. Per-item failures are skipped, not fatal, in
    batch mode; a single-item call keeps its strict 404."""
    require_examiner(case)
    keys = payload.get("canonical_paths")
    batch = keys is not None
    if not batch:
        keys = [payload.get("canonical_path") or payload.get("id")]
        if not keys[0]:
            raise VerbError("discard needs canonical_path (or id)")
    tokens, skipped = [], 0
    moved_last = None
    for key in keys:
        try:
            act, moved_last = _discard_quarantine_one(case, key)
            tokens.append(act["undo_token"])
        except VerbError:
            if not batch:
                raise
            skipped += 1
    case.load()
    out = {"ok": True, "count": len(tokens), "skipped": skipped, "undo_tokens": tokens}
    if len(tokens) == 1:
        out["undo_token"] = tokens[0]
        out["location"] = moved_last
    return out


def _apply_inverse(case, entry):
    """Reverse one prior action on disk/decisions. Returns a result dict. Does NOT
    write an action-log line (callers decide) — shared by verb_undo and verb_reset."""
    action = entry["action"]
    if action == "banish":
        return _unbanish(case, entry)
    if action == "unjunk":
        return _rejunk(case, entry)
    if action == "release_scanned":
        return _unrelease_scanned(case, entry)
    if action == "rename_person":
        # cluster_identities values are EITHER {"name": ...} OR a bare string
        # (older enroll output). The old `prev.get("name") if isinstance(prev,
        # dict) else None` computed name=None for a string identity and DELETED
        # it on undo; recover the name from either shape (C-2).
        prev = entry["before"].get("identity")
        name = _identity_name(entry["target"], {entry["target"]: prev})
        return _rename_person_to(case, entry["target"], name, record=False)
    if action == "rename_folder":
        b, a = entry["before"], entry["after"]
        return verb_rename_folder(case, {"view": b["view"], "old_name": a["name"],
                                         "new_name": b["name"]}, record=False)
    if action == "confirm":
        queue, _id = entry["target"].split(":", 1)
        prev = entry["before"].get("decision")
        dpath = case.paths.metadata_dir / DECISIONS_FILE
        with _doc_lock(case):  # cross-process RMW guard (R-4)
            decisions = load_json(dpath, {}) or {}
            if prev is None:
                (decisions.get(queue, {}) or {}).pop(_id, None)
            else:
                decisions.setdefault(queue, {})[_id] = prev
            atomic_write_json(dpath, decisions)
        return {"restored": prev}
    if action == "release":
        return _requarantine(case, entry["before"]["entry"])
    if action == "remove_person":
        n = 0
        for link, target in entry["before"].get("views", []) or []:
            try:
                relative_symlink(Path(link), Path(target))
                n += 1
            except OSError:
                pass
        dpath = case.paths.metadata_dir / DECISIONS_FILE
        with _doc_lock(case):  # cross-process RMW guard (R-4)
            decisions = load_json(dpath, {}) or {}
            (decisions.get("removed_persons", {}) or {}).pop(entry["target"], None)
            atomic_write_json(dpath, decisions)
        case.load()
        return {"restored": entry["target"], "views": n}
    if action == "demote_ranked":
        dpath = case.paths.metadata_dir / DECISIONS_FILE
        with _doc_lock(case):  # cross-process RMW guard (R-4)
            decisions = load_json(dpath, {}) or {}
            (decisions.get("ranked_demoted", {}) or {}).pop(entry["target"], None)
            atomic_write_json(dpath, decisions)
        return {"restored": entry["target"]}
    if action in ("merge_persons", "assign_face", "move"):
        # G-15 / Move overlay inverse: restore the prior value (or pop) in
        # person_merges / face_assignments / face_placements / scene_placements /
        # event_placements. Pure sidecar edit — face_clustering.json, scene_index.json
        # AND geo_cluster_index.json / case_summary.json are untouched.
        # For "move", before.from is the PRIOR placement value (None on a first move →
        # pop), and the overlay key is dispatched by the recorded before.view: a
        # person-move inverts face_placements, a scene-move scene_placements, an
        # event-move event_placements — so a person/scene/event move on the SAME src
        # undo independently.
        if action == "move":
            mview = (entry.get("before") or {}).get("view")
            key = {"scene": "scene_placements",
                   "event": "event_placements",
                   "document": "doc_placements"}.get(mview, "face_placements")
            field = "from"
        else:
            key = {"merge_persons": "person_merges", "assign_face": "face_assignments"}[action]
            field = {"merge_persons": "winner", "assign_face": "person_id"}[action]
        prev = (entry.get("before") or {}).get(field)
        dpath = case.paths.metadata_dir / DECISIONS_FILE
        with _doc_lock(case):  # cross-process RMW guard (R-4)
            decisions = load_json(dpath, {}) or {}
            overlay = decisions.setdefault(key, {})
            if prev is None:
                overlay.pop(entry["target"], None)
            else:
                overlay[entry["target"]] = prev
            atomic_write_json(dpath, decisions)
        case.load()
        return {"restored": entry["target"]}
    if action == "correspondent_merge_confirm":
        # Restore each loser's PRIOR correspondent_merges entry (or pop → it
        # had none) and drop the suggestion's cluster_id from the confirmed
        # list, so an undone merge lets the suggestion resurface exactly like
        # before it was confirmed.
        before = entry.get("before") or {}
        restore = before.get("merges") or {}
        cid = before.get("cluster_id")
        dpath = case.paths.metadata_dir / DECISIONS_FILE
        with _doc_lock(case):  # cross-process RMW guard (R-4)
            decisions = load_json(dpath, {}) or {}
            merges = decisions.setdefault("correspondent_merges", {})
            for addr, prev in restore.items():
                if prev is None:
                    merges.pop(addr, None)
                else:
                    merges[addr] = prev
            confirmed = decisions.get("correspondent_merge_confirmed") or []
            if cid in confirmed:
                confirmed.remove(cid)
            atomic_write_json(dpath, decisions)
        case.load()
        return {"restored": entry["target"]}
    if action == "correspondent_merge_reject":
        # Inverse of a reject: pop the cluster_id so the suggestion resurfaces.
        dpath = case.paths.metadata_dir / DECISIONS_FILE
        with _doc_lock(case):  # cross-process RMW guard (R-4)
            decisions = load_json(dpath, {}) or {}
            rejected = decisions.get("correspondent_merge_rejected") or []
            if entry["target"] in rejected:
                rejected.remove(entry["target"])
            atomic_write_json(dpath, decisions)
        return {"restored": entry["target"]}
    if action in ("demote_email", "restore_email"):
        # Inverse of demote = restore (pop); inverse of restore = re-demote (add).
        dpath = case.paths.metadata_dir / DECISIONS_FILE
        with _doc_lock(case):  # cross-process RMW guard (R-4)
            decisions = load_json(dpath, {}) or {}
            demoted = decisions.setdefault("email_demoted", {})
            tid = str(entry["target"])
            if action == "demote_email":
                demoted.pop(tid, None)
            else:
                demoted[tid] = {"ts": _now(), "actor": case.role,
                                "subject": (entry.get("before") or {}).get("subject")}
            atomic_write_json(dpath, decisions)
        return {"restored": tid}
    if action == "discard_quarantine":
        e = entry["before"]["entry"]
        banished = Path(entry["before"]["banished"])
        qpath = Path(e["quarantine_path"])
        qpath.parent.mkdir(parents=True, exist_ok=True)
        moved = move_tracked(banished, qpath, reason="family:undiscard-quarantine",
                             ledger=case.ledger, custody=case.custody)
        _rewrite_quarantine(case, add_entry=e)
        case.load()
        return {"restored": str(moved)}
    if action == "confirm_vital":
        # Inverse of a vital confirm: restore the item's PRIOR reviewed record (or
        # pop → back to unconfirmed). verb_confirm_vital claims reversible=True but
        # had no inverse here, so undo of a Confirm 409'd ("cannot undo") — the
        # bulk-triage pager's per-item undo bar surfaced the gap. Mirrors
        # dismiss_vital / promote_vital. The confirm's un-dismiss side effect is not
        # restored (its `before` doesn't record a cleared dismissal — same limitation
        # promote_vital's inverse has); the reviewed flag is the decision undo owns.
        iid = entry["target"]
        prev = (entry.get("before") or {}).get("reviewed")
        dpath = case.paths.metadata_dir / DECISIONS_FILE
        with _doc_lock(case):  # cross-process RMW guard (R-4)
            decisions = load_json(dpath, {}) or {}
            reviewed = decisions.setdefault("vital_doc_reviewed", {})
            if prev is None:
                reviewed.pop(iid, None)
            else:
                reviewed[iid] = prev
            atomic_write_json(dpath, decisions)
        return {"restored": iid}
    if action == "dismiss_vital":
        # Inverse of a vital dismiss: pop the suppression so the item returns to
        # the checklist under its (effective) target.
        iid = entry["target"]
        dpath = case.paths.metadata_dir / DECISIONS_FILE
        with _doc_lock(case):  # cross-process RMW guard (R-4)
            decisions = load_json(dpath, {}) or {}
            (decisions.get("vital_doc_dismissed", {}) or {}).pop(iid, None)
            atomic_write_json(dpath, decisions)
        return {"restored": iid}
    if action == "promote_vital":
        # Inverse of a vital promote: drop the promotion so the item returns to
        # the near-miss list it came from. Restores a prior promotion if the
        # examiner had promoted-then-undone-then-promoted the same item.
        # A promote-and-reassign wrote TWO overlay keys, so undo reverses both —
        # otherwise the retarget would outlive the promotion it belonged to and
        # silently re-file an unrelated future promotion of the same item.
        iid = entry["target"]
        before = entry.get("before") or {}
        prev = before.get("promoted")
        dpath = case.paths.metadata_dir / DECISIONS_FILE
        with _doc_lock(case):  # cross-process RMW guard (R-4)
            decisions = load_json(dpath, {}) or {}
            promoted = decisions.setdefault("vital_doc_promoted", {})
            if prev is None:
                promoted.pop(iid, None)
            else:
                promoted[iid] = prev
            if before.get("retargeted"):
                overlay = decisions.setdefault("vital_doc_target", {})
                prev_target = before.get("target")
                if prev_target is None:
                    overlay.pop(iid, None)
                else:
                    overlay[iid] = prev_target
            atomic_write_json(dpath, decisions)
        case.load()
        return {"restored": iid}
    if action == "reassign_vital":
        # Inverse of a vital reassign: restore each affected item's prior override
        # (or pop → back to the ORIGINAL target encoded in the id). A "global"
        # reassign carries a {id: prior} map (one entry per matched category); a
        # "single" (or legacy, no-scope) reassign carries just {from: prior}.
        before = entry.get("before") or {}
        dpath = case.paths.metadata_dir / DECISIONS_FILE
        with _doc_lock(case):  # cross-process RMW guard (R-4)
            decisions = load_json(dpath, {}) or {}
            overlay = decisions.setdefault("vital_doc_target", {})
            if before.get("scope") == "global":
                restore = before.get("map") or {}
            else:
                restore = {entry["target"]: before.get("from")}
            for k, prev in restore.items():
                if prev is None:
                    overlay.pop(k, None)
                else:
                    overlay[k] = prev
            atomic_write_json(dpath, decisions)
        return {"restored": entry["target"]}
    # ── curation layer inverses (sidecar-only; no file moves) ──
    if action in ("favorite", "collection_create", "collection_rename",
                  "collection_delete", "collection_add", "collection_remove",
                  "note_set", "note_clear"):
        target = entry["target"]
        before = entry.get("before") or {}
        with _doc_lock(case):  # cross-process RMW guard (R-4)
            cur = _load_curation(case)
            if action == "favorite":
                prev = before.get("entry")
                if prev is None:
                    cur["favorites"].pop(target, None)
                else:
                    cur["favorites"][target] = prev
            elif action == "collection_create":
                cur["collections"].pop(target, None)
            elif action == "collection_rename":
                coll = cur["collections"].get(target)
                if coll is not None:
                    coll["title"] = before.get("title")
            elif action == "collection_delete":
                prev = before.get("collection")
                if prev is not None:
                    cur["collections"][target] = prev
            elif action == "collection_add":
                coll = cur["collections"].get(target)
                if coll is not None:
                    drop = set(before.get("added") or [])
                    coll["members"] = [m for m in (coll.get("members") or [])
                                       if m not in drop]
            elif action == "collection_remove":
                coll = cur["collections"].get(target)
                if coll is not None:
                    members = coll.get("members") or []
                    present = set(members)
                    for m in (before.get("removed") or []):
                        if m not in present:
                            members.append(m)
                            present.add(m)
                    coll["members"] = members
            elif action in ("note_set", "note_clear"):
                prev = before.get("entry")
                if prev is None:
                    cur["notes"].pop(target, None)
                else:
                    cur["notes"][target] = prev
            atomic_write_json(_curation_path(case), cur)
        return {"restored": target}
    raise VerbError(f"cannot undo {action}", 409)


def verb_undo(case, payload):
    require_examiner(case)
    token = payload.get("undo_token")
    entry = find_action(case, token)
    if not entry:
        raise VerbError("unknown undo_token", 404)
    if not entry.get("reversible") or is_undone(case, token):
        raise VerbError("action is not reversible or already undone", 409)
    result = _apply_inverse(case, entry)
    # Refresh served state after the inverse so the undo is reflected immediately
    # in every view. Some inverses reload internally (remove_person,
    # discard_quarantine), but _unbanish/_requarantine do not — without a reload
    # here an undone banish/release stays absent from `universe` until some other
    # mutating verb happens to reload, so undo looks broken (C-1). One reload per
    # undo is consistent with the mutating verbs.
    case.load()
    # Record `undoes` at the top level so is_undone()/verb_reset skip the undone
    # action (prevents replaying an undo and Reset re-inverting it).
    append_action(case, entry["action"] + "_undo", entry["target"],
                  {"undo_of": token}, result, reversible=False, undoes=token)
    return {"ok": True, "undoes": token, "result": result}


def verb_reset(case, payload):
    """One-shot reset of a case's Family Archive curation back to as-delivered.

    Reverses every reversible, not-already-undone action NEWEST-FIRST (so e.g. a
    rename applied after a banish is undone before the banish), then clears the
    decision + history files. Leaves family_export/ alone (deliberate copies)."""
    require_examiner(case)
    actions = actions_history(case.paths)  # newest-first
    undone = {e.get("undoes") for e in actions if e.get("undoes")}
    reversed_n, failed = 0, 0
    for e in actions:
        if e.get("undoes"):
            continue  # this IS an undo entry
        if not e.get("reversible") or e.get("undo_token") in undone:
            continue  # not reversible (e.g. export) or already undone
        try:
            _apply_inverse(case, e)
            reversed_n += 1
        except Exception:  # keep going; a single failure shouldn't abort the reset
            failed += 1
    # Clear the decision/curation layer (subsumes confirm/demote undo). Under the
    # cross-process doc lock so a concurrent verb on another instance can't be
    # writing family_decisions.json as we remove it (R-4). Not nested: the
    # _apply_inverse loop above already released its per-item locks.
    dpath = case.paths.metadata_dir / DECISIONS_FILE
    cpath = case.paths.metadata_dir / CURATION_FILE
    with _doc_lock(case):
        if dpath.exists():
            dpath.unlink()
        # Curation is a sidecar journal too — clear it on reset (the reversible
        # inverses above already emptied it; unlink drops the empty shell so goog is
        # left as-delivered).
        if cpath.exists():
            cpath.unlink()
    # ARCHIVE the audit trail (never destroy it) rather than unlink — the verbs
    # must stay auditable, and if any inverse FAILED its on-disk effect remains
    # recorded only here. A fresh history then starts with the reset marker.
    apath = case.paths.metadata_dir / ACTIONS_FILE
    if apath.exists():
        slug = _now().replace(":", "").replace("+", "z")
        archived = apath.with_name(f"{ACTIONS_FILE}.reset-{slug}")
        n = 1
        while archived.exists():
            archived = apath.with_name(f"{ACTIONS_FILE}.reset-{slug}-{n}")
            n += 1
        apath.rename(archived)
    case.load()
    append_action(case, "reset", case.paths.case_id,
                  {"reversed": reversed_n, "failed": failed}, {}, reversible=False)
    return {"ok": True, "reversed": reversed_n, "failed": failed}


# ── the examiner release gate (examiner-only) ────────────────────────────────
#
# A named human releases the family bundle; the record says exactly what that
# means. See docs/specs/examiner-release-gate.md. The disposition gate here is
# CLEARANCE, not acknowledgement (K7): every flagged item must be dispositioned
# by a state-changing, logged verb — never a click-through ack.

DEFAULT_ATTESTATION = (
    "I release this family bundle. I personally made the dispositions recorded "
    "below and reviewed the flagged items. I do NOT attest to having read every "
    "family-visible email; the machine screened what the family is shown and "
    "withheld the estate-rescued bulk by category, which remains open to me in "
    "the examiner explorer and search."
)


def _os_user() -> str:
    try:
        import getpass
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER", "unknown")


def _human_review_cleared(case):
    """Union clear-check over every path in human_review_required.json (§4).

    A path is CLEARED iff its archive copy is gone (discarded/moved out), OR the
    examiner kept it (`human_review_reviewed`), OR waived it
    (`human_review_waived`). NOT `human_review_count == 0` — keep and waive both
    leave the item present, so the count never falls. Keyed on the EXACT path
    strings, message-chunk refs (`<path>#chunk=<hex>`) included: a chunk ref has
    no archive entry, so _present is always True and it can clear ONLY via
    keep/waive. Returns (ok, [unresolved]).
    """
    md = case.paths.metadata_dir
    human = load_json(md / "human_review_required.json", {}) or {}
    paths = human.get("paths", []) or []
    arc = (load_json(md / "archive_map.json", {}) or {}).get("entries", {}) or {}
    d = case.decisions
    reviewed = d.get("human_review_reviewed", {}) or {}
    waived = d.get("human_review_waived", {}) or {}
    unresolved = []
    for p in paths:
        a = arc.get(p)
        present = not (a and not os.path.exists(a))
        if present and p not in reviewed and p not in waived:
            unresolved.append(p)
    return (not unresolved), unresolved


def _vital_docs_cleared(case):
    """Union clear-check over every CONFIRMED vital-document item (§4). An item is
    CLEARED iff the examiner explicitly CONFIRMED it (`vital_doc_reviewed`, by item
    id), DISMISSED the document (`vital_doc_dismissed`, by path — drops every
    category the doc matched), or REASSIGNED it (`vital_doc_target`, by item id —
    an active decision). Mirrors `_human_review_cleared`. An absent
    vital_doc_confirmed.json (the vital_doc_confirm stage never ran) ⇒ nothing to
    review ⇒ cleared. Returns (ok, [unresolved item ids])."""
    confirmed = load_json(case.paths.metadata_dir / "vital_doc_confirmed.json", None)
    if not isinstance(confirmed, list):
        return True, []
    d = case.decisions
    dismissed = d.get("vital_doc_dismissed", {}) or {}
    retarget = d.get("vital_doc_target", {}) or {}
    reviewed = d.get("vital_doc_reviewed", {}) or {}
    promoted = d.get("vital_doc_promoted", {}) or {}
    # A PROMOTED near-miss is on the checklist too, so the gate must see it — but
    # it needs no separate confirm: promoting IS the examiner vouching for it.
    # Included here (rather than skipped) so it is stated, not implied.
    promoted_items = [{"target": r.get("target"), "path": r.get("path")}
                      for r in promoted.values() if isinstance(r, dict)]
    unresolved = []
    for c in confirmed + promoted_items:
        path = c.get("path")
        iid = vital_doc_item_id(c.get("target"), path)
        if (path in dismissed or iid in dismissed
                or iid in retarget or iid in reviewed
                # promoting is itself the affirmative review
                or iid in promoted):
            continue
        unresolved.append(iid)
    return (not unresolved), unresolved


def _machine_screen(case):
    """What the machine's screen was SET TO at signing time — recorded plainly so
    the certificate can state it (the honesty that makes it defensible).
    transcribe_deliver is a config toggle outside the metadata fingerprint;
    recording it here leaves a trace of a later flip (§2 caveat b)."""
    # Read the per-CASE config (authoritative for which sensitivity filters the
    # examiner enabled for THIS estate) rather than the server's pipeline cfg.
    cfg = load_json(case.paths.case_config_path, {}) or case.cfg or {}
    filters = ((cfg.get("sensitive_scan") or {}).get("sensitivity_filters") or {})
    enabled = sorted(k for k, v in filters.items()
                     if isinstance(v, dict) and v.get("enabled"))
    return {
        "scan_filters_enabled": enabled,
        "export_gate": case.summary.get("export_gate", {}) or {},
        "transcribe_deliver": bool((cfg.get("transcribe") or {}).get("deliver", True)),
    }


def _signoff_dispositions(case):
    """Per-category counts of what the human personally dispositioned, from the
    action log + the decisions overlay (for the certificate's itemization)."""
    counts = {"quarantine_released": 0, "quarantine_discarded": 0, "banished": 0}
    # One parse of the action log (rows), with the undone-token set from the same
    # snapshot. Skip actions later undone — a banish-then-undo is a net no-op and
    # must not inflate the wet-ink certificate's counts.
    rows, _by_token, undone = case.actions_index()
    for rec in rows:
        if not isinstance(rec, dict) or rec.get("undo_token") in undone:
            continue
        a = rec.get("action")
        # NOTE: these are the logged `action` fields (verb_discard_quarantine
        # records "discard_quarantine", NOT the "discard/quarantine" route key).
        if a == "release":
            counts["quarantine_released"] += 1
        elif a == "discard_quarantine":
            counts["quarantine_discarded"] += 1
        elif a == "banish":
            counts["banished"] += 1
    d = case.decisions
    counts["review_kept"] = len(d.get("human_review_reviewed", {}) or {})
    counts["review_waived"] = len(d.get("human_review_waived", {}) or {})
    # Vital dispositions the human made at review time (not the raw pipeline count).
    counts["vital_confirmed"] = len(d.get("vital_doc_reviewed", {}) or {})
    counts["vital_dismissed"] = len(d.get("vital_doc_dismissed", {}) or {})
    counts["vital_reassigned"] = len(d.get("vital_doc_target", {}) or {})
    return counts


def _release_counts_and_withheld(case):
    """The family/withheld split, for the certificate. The withheld estate-rescued
    mail is examiner-inspectable (PR 1 kept the examiner's full retention), which
    is what makes "I confirmed a category rule over a set I could open" defensible."""
    from wyeast.core.audience import load_email_index, FAMILY, EXAMINER
    try:
        fam = len(load_email_index(case.paths, FAMILY))
        exam = len(load_email_index(case.paths, EXAMINER))
    except Exception:
        fam = exam = 0
    return ({"family_visible_emails": fam},
            {"estate_rescued_emails": max(0, exam - fam)})


def verb_review_keep(case, payload):
    """Retain a human-review item, with an OPTIONAL reason (rev-3 gave it none —
    the higher-liability verb had the thinner record). The shortcut still exists;
    it leaves a mark. Persists to family_decisions.json (statted → re-checkable
    after a --restart, and in the visibility stamp)."""
    require_examiner(case)
    item_id = payload.get("id")
    if not item_id:
        raise VerbError("review/keep needs id (the exact human-review path)")
    item_id = str(item_id)
    reason = payload.get("reason")
    dpath = case.paths.metadata_dir / DECISIONS_FILE
    with _doc_lock(case):
        decisions = load_json(dpath, {}) or {}
        before = (decisions.get("human_review_reviewed", {}) or {}).get(item_id)
        decisions.setdefault("human_review_reviewed", {})[item_id] = {
            "reason": reason, "ts": _now(), "actor": case.role}
        # keep and waive are mutually exclusive dispositions of the same item
        (decisions.get("human_review_waived") or {}).pop(item_id, None)
        atomic_write_json(dpath, decisions)
    entry = append_action(case, "review/keep", item_id,
                          {"reviewed": before}, {"reviewed": {"reason": reason}},
                          reversible=True)
    return {"ok": True, "undo_token": entry["undo_token"]}


def verb_waive_review(case, payload):
    """Waive individual review of a human-review item — a signed, reasoned "I chose
    not to look at this." Costly and visible; the REASON is required."""
    require_examiner(case)
    item_id = payload.get("id")
    reason = (payload.get("reason") or "").strip()
    if not item_id:
        raise VerbError("review/waive needs id (the exact human-review path)")
    if not reason:
        raise VerbError("review/waive requires a reason")
    item_id = str(item_id)
    dpath = case.paths.metadata_dir / DECISIONS_FILE
    with _doc_lock(case):
        decisions = load_json(dpath, {}) or {}
        before = (decisions.get("human_review_waived", {}) or {}).get(item_id)
        decisions.setdefault("human_review_waived", {})[item_id] = {
            "reason": reason, "ts": _now(), "actor": case.role}
        (decisions.get("human_review_reviewed") or {}).pop(item_id, None)
        atomic_write_json(dpath, decisions)
    entry = append_action(case, "review/waive", item_id,
                          {"waived": before}, {"waived": {"reason": reason}},
                          reversible=True)
    return {"ok": True, "undo_token": entry["undo_token"]}


def _write_certificate(case, rec):
    """Render + write output/examiner/release_certificate.md. Examiner subtree —
    never in INCLUDE_TOP, so never delivered to the family."""
    examiner_dir = case.paths.output_dir / "examiner"
    examiner_dir.mkdir(parents=True, exist_ok=True)
    (examiner_dir / release.CERTIFICATE_FILE).write_text(
        release.render_certificate(rec, case.decisions), encoding="utf-8")


def verb_signoff(case, payload):
    """A named human releases the family bundle. Refuses (409) unless the machine
    gate is clean AND every flagged item is dispositioned (§4). Writes
    evidence-first and durable (§1): custody event → family_release.json →
    append_action. A crash after the custody event leaves the gate closed (no
    record) — fails safe."""
    require_examiner(case)
    name = (payload.get("name") or "").strip()
    capacity = (payload.get("capacity") or "").strip()
    judgment = (payload.get("judgment") or "").strip()
    if not name or not capacity:
        raise VerbError("signoff needs name and capacity")
    if not judgment:
        raise VerbError("signoff needs a judgment — in your own words, why release "
                        "this set to the family?")

    # ── the disposition gate (§4): clearance, not acknowledgement ──
    reasons = family_block_reasons(case.summary)
    if reasons:
        raise VerbError("delivery is blocked by the export gate: "
                        + "; ".join(map(str, reasons)), 409)
    review = review_data(case.paths, case.summary)
    q_total = review.get("quarantine_total", 0)
    if q_total:
        raise VerbError(f"{q_total} quarantine item(s) still need release or "
                        f"discard before sign-off", 409)
    hr_ok, unresolved = _human_review_cleared(case)
    if not hr_ok:
        raise VerbError(f"{len(unresolved)} human-review item(s) not yet kept or "
                        f"waived", 409)
    v_ok, v_unresolved = _vital_docs_cleared(case)
    if not v_ok:
        raise VerbError(f"{len(v_unresolved)} vital document(s) not yet confirmed, "
                        f"dismissed, or reassigned", 409)

    mode = payload.get("mode") or release.MODE_STANDARD
    if mode not in (release.MODE_STANDARD, release.MODE_DEEP):
        raise VerbError(f"unknown fingerprint mode {mode!r}")
    fp = release.fingerprint(case.paths, mode)
    stamp = release.visibility_stamp(case.paths)
    counts, withheld = _release_counts_and_withheld(case)
    rec = {
        "case_id": case.paths.case_id,
        "signed_at": _now(),
        "actor": {"name": name, "capacity": capacity, "os_user": _os_user()},
        "attestation": (payload.get("attestation") or DEFAULT_ATTESTATION),
        "judgment": judgment,
        "delivery_fingerprint": fp,
        "fingerprint_mode": mode,
        # Persist the digest's format version so verify() can tell "the format
        # changed, re-sign" apart from "the tree was tampered with".
        "fingerprint_version": release.FINGERPRINT_VERSION,
        "visibility_stamp": stamp,
        "dispositions": _signoff_dispositions(case),
        "counts": counts,
        "withheld": withheld,
        "machine_screen": _machine_screen(case),
        "authoritative_artifact": "signed printout",
        "revoked": False,
    }

    with _doc_lock(case):
        # 1. durable custody event FIRST — the tamper/verify anchor (T0).
        case.custody.record_event(
            "release", f"{fp} actor={name!r} capacity={capacity!r} mode={mode}")
        # 2. the enabling record, only after evidence is on disk.
        atomic_write_json(release.release_path(case.paths), rec)
        # 2b. the wet-ink certificate (examiner subtree, never delivered) — the
        #     authoritative artifact once printed and hand-signed (§5).
        _write_certificate(case, rec)
    # 3. the UI/undo trail (flocks its own file — never under _doc_lock).
    entry = append_action(case, "signoff", case.paths.case_id, {},
                          {"fingerprint": fp, "signed_at": rec["signed_at"]},
                          reversible=True)
    return {"ok": True, "fingerprint": fp, "signed_at": rec["signed_at"],
            "undo_token": entry["undo_token"]}


def verb_signoff_revoke(case, payload):
    """Explicitly revoke a release. A running family server stops serving at its
    next GET (E5).

    Write order is the MIRROR of sign-off, because revoke's safe direction is the
    opposite: sign-off must not OPEN the gate without a durable record, so evidence
    (custody) lands first; revoke must not FAIL TO CLOSE, so the record
    (`revoked:true`) lands FIRST and the custody audit line second. A crash between
    them leaves the gate closed with a missing audit line — fail-safe — rather than
    a serving family surface with an orphaned revoke event (the fail-open the
    original custody-first order had)."""
    require_examiner(case)
    rec = release.load_release(case.paths)
    if rec is None:
        raise VerbError("no release record to revoke", 404)
    if rec.get("revoked"):
        return {"ok": True, "already_revoked": True}
    with _doc_lock(case):
        rec["revoked"] = True
        rec["revoked_at"] = _now()
        # 1. close the gate FIRST (a crash here leaves it closed — fail-safe).
        atomic_write_json(release.release_path(case.paths), rec)
        # 2. the durable custody audit line.
        case.custody.record_event(
            "revoke", f"{rec.get('delivery_fingerprint', '')} by {case.role}")
        _write_certificate(case, rec)          # re-render with the revoked banner
    append_action(case, "signoff/revoke", case.paths.case_id,
                  {"revoked": False}, {"revoked": True}, reversible=False)
    return {"ok": True}


VERBS = {
    "confirm": verb_confirm, "confirm/batch": verb_confirm_batch,
    "banish": verb_banish, "unjunk": verb_unjunk, "release/scanned": verb_release_scanned,
    "rename/person": verb_rename_person,
    "rename/folder": verb_rename_folder, "export": verb_export,
    "export/collection": verb_export_collection, "release": verb_release,
    "discard/quarantine": verb_discard_quarantine, "demote": verb_demote_ranked,
    "demote/email": verb_demote_email,
    # ── vital-document checklist (examiner-only; DECISIONS OVERLAY, never mutates
    #    vital_doc_confirmed.json) ──
    "vital/dismiss": verb_dismiss_vital, "vital/reassign": verb_reassign_vital,
    "vital/confirm": verb_confirm_vital, "vital/promote": verb_promote_vital,
    "remove/person": verb_remove_person, "reset": verb_reset, "undo": verb_undo,
    # ── G-15 face-assist (examiner-only; DECISIONS OVERLAY, never mutates an index) ──
    "merge/persons": verb_merge_persons, "assign/face": verb_assign_face,
    # ── correspondent identity merge suggestions (P2 #9; DECISIONS OVERLAY) ──
    "correspondent/merge": verb_correspondent_merge_confirm,
    "correspondent/reject": verb_correspondent_merge_reject,
    # ── Move verb (Phase 1, person-only; face_placements overlay) ──
    "move": verb_move,
    # ── curation layer (examiner-first) ──
    "favorite": verb_favorite,
    "collection/create": verb_collection_create,
    "collection/rename": verb_collection_rename,
    "collection/delete": verb_collection_delete,
    "collection/add": verb_collection_add,
    "collection/remove": verb_collection_remove,
    "note/set": verb_note_set, "note/clear": verb_note_clear,
    # ── the examiner release gate ──
    "signoff": verb_signoff, "signoff/revoke": verb_signoff_revoke,
    "review/keep": verb_review_keep, "review/waive": verb_waive_review,
}


# ── HTTP handler ─────────────────────────────────────────────────────────────────

CASE: ArchiveCase = None  # set in main()

BASE_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Wyeast Family Archive</title>
<link rel="stylesheet" href="/assets/leaflet.css">
<link rel="stylesheet" href="/assets/family/family.css">
</head>
<body data-page="{page}" data-theme="{theme}" data-role="{role}">
<div id="app"><noscript><p class="notice">This archive needs JavaScript enabled.</p></noscript></div>
<script type="application/json" id="ctx">{ctx}</script>
<script src="/assets/leaflet.js"></script>
<script src="/assets/basemap.js"></script>
<script src="/assets/family/family.js"></script>
</body></html>
"""

# CSP for the HTML pages (distinct from the per-media `sandbox` policy). All
# scripts/styles are same-origin /assets files and the bootstrap ctx is a
# non-executable JSON block, so no 'unsafe-inline' is needed — one forgotten esc()
# in the ~34 innerHTML sinks can no longer become script execution. `data:` on
# img-src covers Leaflet's inline marker data-URIs; everything else is 'self'.
_PAGE_CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; "
             "script-src 'self'; media-src 'self'; frame-src 'self'; "
             "connect-src 'self'; object-src 'none'; base-uri 'none'; "
             "form-action 'none'")


# Content types safe to render inline in the browser. Anything else served from
# /media is forced to download as an opaque octet-stream, so a hostile file in the
# estate (SVG/HTML/…) can never be interpreted as a same-origin document.
_INLINE_MEDIA_PREFIXES = ("image/", "video/", "audio/")
_INLINE_MEDIA_EXACT = {"application/pdf"}
# SVG matches the "image/" prefix but is an XML document that can carry <script>;
# never serve it inline. Forced to attachment + octet-stream like HTML/office files
# so a hostile estate .svg can't be interpreted as a same-origin document. (This is
# a hardening carve-out out of the inline set, NOT a relaxation.)
_INLINE_MEDIA_EXCLUDE = {"image/svg+xml", "image/svg-xml", "image/svg"}

# Sent on EVERY /media + /thumb response. `sandbox` (no tokens) makes the browser
# treat the resource as a unique, script-disabled origin if it is ever loaded as a
# document (top-level or in an <iframe>), neutralising script in a hostile SVG/HTML
# estate file. `nosniff` stops the browser from second-guessing the declared type
# and executing a mislabelled file. This is the load-bearing, client-independent
# half of the lightbox-iframe fix (the sandbox="" attribute in family.js is the
# defence-in-depth half).
_MEDIA_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox",
}


def _media_headers(ctype, extra=None):
    """Merge the media security headers with `extra`, returning (ctype, headers).
    For a non-inline type the content is relabelled octet-stream and marked
    attachment so the browser downloads rather than renders/executes it.

    PDF (F-11) is the one narrow preview exception: it is served inline (so the
    browser's built-in, itself-sandboxed PDF viewer renders it) WITHOUT the
    `Content-Security-Policy: sandbox` header — that CSP would blank the viewer.
    `nosniff` is kept, and the bytes are same-origin /media, so this does not widen
    the hostile-file surface for any OTHER type. SVG/HTML/office/unknown stay
    attachment + application/octet-stream + sandbox, unchanged."""
    c = (ctype or "").split(";")[0].strip().lower()
    inline = (c in _INLINE_MEDIA_EXACT
              or any(c.startswith(p) for p in _INLINE_MEDIA_PREFIXES)) \
        and c not in _INLINE_MEDIA_EXCLUDE
    headers = dict(_MEDIA_SECURITY_HEADERS)
    if extra:
        headers.update(extra)
    if c == "application/pdf":
        # Preview inline in the browser PDF viewer; drop ONLY the sandbox CSP that
        # blocks it (nosniff stays). No other type is relaxed here.
        headers.pop("Content-Security-Policy", None)
        headers["Content-Disposition"] = "inline"
    elif not inline:
        headers["Content-Disposition"] = "attachment"
        ctype = "application/octet-stream"
    return ctype, headers


class FamilyArchiveHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body: bytes, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj).encode("utf-8"))

    # ---- GET ----
    def do_GET(self):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        # DNS-rebinding / cross-origin READ guard. The server binds loopback, but a
        # hostile web page can rebind its own hostname to 127.0.0.1 and then read
        # every GET (pages, /api/*, /media bytes) — exfiltrating the whole estate.
        # The same Host/Origin check the POST path uses must therefore cover reads
        # too; a legitimate browser always sends Host: 127.0.0.1:<port>.
        if not self._local_request_ok():
            return self._json(403, {"error": "cross-origin request refused"})
        # E5 — the family GET surface is DEFAULT-CLOSED. Every body (/media, /thumb,
        # every /api/* except the status endpoint) refuses unless the release record
        # is valid-and-current; only the page shells + assets + status answer. The
        # common path is one or two stats + a small read (the stamp fast-path); the
        # ~15 s fingerprint runs only on a stamp trip, once per distinct tree state.
        if CASE.role == "family" and not _e5_shell_allowed(path):
            status = CASE.release_status()
            if not status.get("valid"):
                return self._json(403, {"error": "not released",
                                        "release_status": status})
        try:
            key = path.lstrip("/")
            if path == "/" or key in {k for k, _, _ in PAGES} or key in PAGE_ALIASES:
                return self._page(path)
            if path.startswith("/assets/"):
                return self._asset(path[len("/assets/"):])
            if path == "/media":
                return self._media(qs, thumb=False)
            if path == "/thumb":
                return self._media(qs, thumb=True)
            if path.startswith("/api/"):
                return self._api_get(path[len("/api/"):], qs)
            self._json(404, {"error": "not found"})
        except VerbError as e:
            self._json(e.code, {"error": str(e)})
        except BrokenPipeError:
            pass
        except Exception as e:  # never crash the server on one bad request
            self._json(500, {"error": str(e)})

    def _page(self, path):
        key = "overview" if path == "/" else path.lstrip("/")
        key = PAGE_ALIASES.get(key, key)
        label, theme = next((lbl, th) for k, lbl, th in PAGES if k == key)
        if key in EXAMINER_ONLY and CASE.role != "examiner":
            return self._json(403, {"error": "examiner only"})
        nav = [{"key": k, "label": lbl, "theme": th} for k, lbl, th in PAGES
               if k not in HIDDEN_FROM_NAV
               and not (k in EXAMINER_ONLY and CASE.role != "examiner")]
        ctx = {"page": key, "role": CASE.role, "case_id": CASE.paths.case_id, "nav": nav}
        # Escape '<' as < so a case_id / nav label containing '</script>' can
        # never break out of the JSON bootstrap block (case_id derives from the
        # drop-folder name, which originates outside Zone B). JSON.parse restores it.
        ctx_json = json.dumps(ctx).replace("<", "\\u003c")
        body = BASE_HTML.format(title=html.escape(label), page=key, theme=theme,
                                role=CASE.role, ctx=ctx_json)
        self._send(200, "text/html; charset=utf-8", body.encode("utf-8"),
                   extra={"Content-Security-Policy": _PAGE_CSP})

    def _api_get(self, name, qs):
        name = PAGE_ALIASES.get(name, name)
        if name == "release-status":
            # Status-only, in the E5 allowlist — the banner UI branches here BEFORE
            # any section fetch so a default-closed page renders the banner, not an
            # empty shell of 403s (§3 banner-ordering coupling).
            return self._json(200, CASE.release_status())
        if name == "search":
            # Full-text search (FTS5). Query the FTS index when it's ready; while it
            # is still building on the first search, fall back to the shallow lexical
            # index so results aren't empty (and flag building:True so the UI polls).
            q = (qs.get("q") or [""])[0]
            try:
                offset = max(0, int((qs.get("offset") or ["0"])[0]))
            except (TypeError, ValueError):
                offset = 0
            try:
                limit = int((qs.get("limit") or ["30"])[0])
            except (TypeError, ValueError):
                limit = 30
            limit = max(1, min(limit, 200))
            result = CASE.fts_search(q, offset, limit)
            if result.get("building"):
                lex = CASE.lexical_search(q, offset, limit)
                lex["building"] = True
                if result.get("progress"):
                    lex["progress"] = result["progress"]
                result = lex
            return self._json(200, result)
        if name == "doctext":
            # Inline office-document view. Gated by resolve_media_path inside —
            # the same allowlist /media applies to the same src — so this reaches
            # exactly the documents the caller could already download.
            return self._json(*CASE.doctext_view((qs.get("src") or [None])[0]))
        if name == "confirm-queue":
            # Examiner-only: the confirm flow is examiner-only, and this exposes raw
            # internal working paths (scene guesses + noise_files) — reconnaissance
            # a family session must not receive.
            if CASE.role != "examiner":
                return self._json(403, {"error": "examiner only"})
            _dec = CASE.decisions
            return self._json(200, confirm_queue_data(
                CASE.summary, CASE.scene_index,
                CASE.effective_face_clustering(_dec), CASE.geo_index,
                decisions=_dec, frame_map=CASE.video_frame_map, archive_map=CASE.archive_map))
        if name == "correspondent-duplicates":
            # P2 #9: examiner-reviewed possible-duplicate-identity suggestions
            # for the Correspondents page. Detection-only (never auto-merges);
            # the correspondent/merge and correspondent/reject verbs record the
            # examiner's decision. Examiner-only — a candidate cluster surfaces
            # raw address lists ahead of any confirmed merge, which a family
            # session has no standing reason to see.
            if CASE.role != "examiner":
                return self._json(403, {"error": "examiner only"})
            return self._json(200, CASE.correspondent_duplicates_section())
        if name == "vital/near-misses":
            # Examiner-only: the reviewable near-misses for ONE vital-doc target —
            # what the checklist's "N near-misses" opens. Exposes raw candidate
            # hits (internal paths + snippets), so it is gated exactly like
            # confirm-queue; near_miss_rows ALSO returns [] for a non-examiner, so
            # the gate is not the only thing standing between family and this data.
            if CASE.role != "examiner":
                return self._json(403, {"error": "examiner only"})
            return self._json(*CASE.near_miss_section(
                (qs.get("target") or [None])[0], qs))
        if name == "review-pager":
            # Examiner-only: the normalized item union a bulk-triage PAGER walks for
            # one surface (?group=quarantine|vital). Same gate as the other review
            # surfaces — quarantine + vital rows carry raw internal paths a family
            # session must never receive; review_pager_section is examiner-scoped too.
            if CASE.role != "examiner":
                return self._json(403, {"error": "examiner only"})
            return self._json(200, CASE.review_pager_section(
                (qs.get("group") or [None])[0]))
        if name == "email/thread":
            tid = (qs.get("id") or [None])[0]
            return self._json(200, CASE.thread_messages(tid))
        if name == "message/conversation":
            cid = (qs.get("id") or [None])[0]
            return self._json(200, CASE.conversation_section(cid))
        if name == "person":
            pid = (qs.get("id") or [None])[0]
            return self._json(200, CASE.person_detail_section(pid))
        if name == "video-frames":
            # G-11: poster-strip fallback frames for one source video. The id is
            # string-matched (never read); each returned frame id is contained to
            # the case dir and servable via /thumb + /media.
            sv = (qs.get("id") or [None])[0]
            return self._json(200, CASE.video_frames_section(sv))
        if name == "transparency":
            # G-14: read-only duplicates/accounting panel. Reachable by BOTH roles —
            # transparency_section gates the examiner-only detail (suspense +
            # significant-attachment noise) internally by role.
            return self._json(200, CASE.transparency_section())
        if name == "doc-categories":
            # Movable document categories for the category-move picker (§13.4):
            # the case-config document_categories (or the stdlib fallback), MINUS
            # account_credentials (the sealed category is never a movable target,
            # §13.3). Examiner-only — doc-move is examiner-gated at the verb.
            if CASE.role != "examiner":
                return self._json(403, {"error": "examiner only"})
            # `financial_subcategories` is the ADDITIVE §14.4 field for the
            # sub-category picker; an EMPTY list means the second pass is disabled
            # (sub-move offered on no rows). Examiner-gated with the categories.
            return self._json(200, {
                "categories": sorted(doc_move_categories(CASE)),
                "financial_subcategories": sorted(doc_subcategories(CASE)),
            })
        if name == "transcript":
            # G-3: seek-synced recording detail (segments + timings). GET returning
            # JSON — covered by the do_GET Origin/Host guard; the load-bearing check
            # is the sidecar containment in resolve_sidecar_path.
            fid = (qs.get("id") or [None])[0]
            return self._json(200, CASE.transcript_section(fid))
        # Flatten qs (parse_qs → {k: [v]}) to the flat dict api_section expects.
        flat = {k: (v[0] if v else "") for k, v in (qs or {}).items()}
        # G-8: read the wall clock ONCE, here at the request boundary (not in the pure
        # builder), and pass today's date down for the Overview "on this day" card.
        if name == "overview":
            flat["today"] = datetime.now().strftime("%Y-%m-%d")
        if name == "videos":
            return self._json(200, CASE.api_section("videos", flat))
        if name in {k for k, _, _ in PAGES}:
            if name in EXAMINER_ONLY and CASE.role != "examiner":
                return self._json(403, {"error": "examiner only"})
            return self._json(200, CASE.api_section(name, flat))
        self._json(404, {"error": "unknown api"})

    def _asset(self, rel):
        # serve report_assets/* with traversal guard
        base = Path(os.path.realpath(ASSETS_SRC))
        target = Path(os.path.realpath(base / rel))
        if base not in target.parents or not target.is_file():
            return self._json(404, {"error": "asset not found"})
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._send(200, ctype, target.read_bytes())

    def _media(self, qs, thumb):
        # parse_qs already percent-decodes the value; do NOT unquote again (a
        # second decode corrupts filenames that legitimately contain '%XX').
        src = (qs.get("src") or [None])[0]
        try:
            real = resolve_media_path(CASE, src)
        except VerbError as e:
            if e.code not in (403, 404):
                raise
            # Two narrow doors past the generic resolver, tried in order:
            # photo-stack members (closed allowlist, both roles), then
            # quarantined items (examiner-only) for Review triage.
            try:
                real = resolve_dup_member_path(CASE, src)
            except VerbError:
                real = resolve_quarantine_media_path(CASE, src)
        if thumb:
            buf = _thumb_bytes(real, cache_dir=CASE.paths.metadata_dir / "_thumb_cache")
            if buf is None:
                return self._json(415, {"error": "no thumbnail"})
            ctype, headers = _media_headers("image/jpeg", {"Cache-Control": "max-age=3600"})
            return self._send(200, ctype, buf, headers)
        # Browsers can't render HEIC/HEIF natively — convert to JPEG on serve so the
        # full-size lightbox works (thumbnails already go through _thumb_bytes). Fall
        # back to raw streaming if conversion is unavailable.
        if os.path.splitext(str(real))[1].lower() in (".heic", ".heif"):
            jpg = _jpeg_bytes(real, 2400)
            if jpg is not None:
                ctype, headers = _media_headers("image/jpeg", {"Cache-Control": "max-age=3600"})
                return self._send(200, ctype, jpg, headers)
        ctype = mimetypes.guess_type(str(real))[0] or "application/octet-stream"
        self._serve_file(real, ctype)

    def _serve_file(self, path, ctype):
        """Stream a file with HTTP Range support so <audio>/<video> can start and
        seek (browsers issue Range requests; a full-200 makes some players flaky).

        The file is OPENED before any status line / headers are written, so a file
        that vanishes mid-request (banished between resolve and serve) yields a clean
        404 rather than a FileNotFoundError raised after headers — which the generic
        do_GET handler would turn into a second _json(500,…) written into the middle
        of an already-started response (a corrupt stream)."""
        try:
            f = open(path, "rb")
        except OSError:
            return self._json(404, {"error": "not found"})
        try:
            size = os.fstat(f.fileno()).st_size
            start, end = parse_range(self.headers.get("Range"), size)
            chunk = 64 * 1024
            if self.headers.get("Range") is not None and start is None:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            # nosniff + CSP:sandbox on every media byte-stream (and octet-stream/attach
            # for non-inline types) so a hostile SVG/HTML estate file can't execute.
            ctype, sec_headers = _media_headers(ctype)
            if start is not None:  # partial
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                start, length = 0, size
                self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "max-age=3600")
            for k, v in sec_headers.items():
                self.send_header(k, v)
            self.end_headers()
            try:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    buf = f.read(min(chunk, remaining))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    remaining -= len(buf)
            except (BrokenPipeError, ConnectionResetError):
                pass
        finally:
            f.close()

    def _local_request_ok(self):
        """Reject cross-site POSTs and DNS-rebinding. The server listens only on
        loopback, but any web page in the same browser can POST cross-site to a
        well-known localhost port — so a state-changing verb must come from OUR
        origin. A browser always sends Origin on a cross-origin POST; absence means
        a non-browser client (curl), which is not a CSRF vector. The Host check
        blocks DNS-rebinding (a hostile domain re-resolved to 127.0.0.1)."""
        port = self.server.server_address[1]
        allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        origin = self.headers.get("Origin")
        if origin is not None and origin not in allowed_origins:
            return False
        host = (self.headers.get("Host") or "").strip()
        if host and host not in allowed_hosts:
            return False
        return True

    # ---- POST ----
    def do_POST(self):
        u = urlparse(self.path)
        if not u.path.startswith("/api/"):
            return self._json(404, {"error": "not found"})
        if not self._local_request_ok():
            return self._json(403, {"ok": False, "error": "cross-origin request refused"})
        verb = u.path[len("/api/"):]
        fn = VERBS.get(verb)
        if not fn:
            return self._json(404, {"error": f"unknown verb {verb}"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            with CASE._lock:  # serialize mutating verbs (server is threaded)
                result = fn(CASE, payload)
            self._json(200, result)
        except VerbError as e:
            self._json(e.code, {"ok": False, "error": str(e)})
        except json.JSONDecodeError as e:
            self._json(400, {"ok": False, "error": f"bad JSON: {e}"})
        except Exception as e:
            self._json(500, {"ok": False, "error": str(e)})


def parse_range(header, size):
    """Parse a single-range `Range: bytes=a-b` header against `size`.

    Returns (start, end) inclusive, or (None, None) when there is no range to
    honor (no/blank header) OR the range is unsatisfiable. Supports open-ended
    (`bytes=N-`) and suffix (`bytes=-N`) forms. Callers distinguish "no header"
    from "unsatisfiable" by checking whether the header was present.
    """
    if not header or not header.startswith("bytes=") or size <= 0:
        return None, None
    spec = header[len("bytes="):].split(",")[0].strip()
    if "-" not in spec:
        return None, None
    a, b = spec.split("-", 1)
    try:
        if a == "":
            n = int(b)
            if n <= 0:
                return None, None
            start, end = max(0, size - n), size - 1
        else:
            start = int(a)
            end = int(b) if b != "" else size - 1
            end = min(end, size - 1)
        if start > end or start >= size or start < 0:
            return None, None
        return start, end
    except ValueError:
        return None, None


_HEIF_REGISTERED = False


def _ensure_heif_registered():
    """Register pillow_heif with PIL so .heic/.heif open. Idempotent; a no-op if
    pillow_heif is absent — keeps the tool import-safe in CI (PIL/pillow_heif are
    never imported at module top level, only inside the thumbnail/serve paths)."""
    global _HEIF_REGISTERED
    if _HEIF_REGISTERED:
        return
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass
    _HEIF_REGISTERED = True


def _jpeg_bytes(path, box):
    """Render an image file to a JPEG byte string, downscaled to fit `box`. Returns
    None on any failure. Handles HEIC/HEIF (registers pillow_heif first)."""
    try:
        from PIL import Image, ImageOps
        _ensure_heif_registered()
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((box, box))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            out = io.BytesIO()
            im.save(out, "JPEG", quality=82)
            return out.getvalue()
    except Exception:
        return None


def _thumb_cache_path(cache_dir, real, box):
    """Cache filename keyed on (realpath, mtime, size, box) — a source edit or a
    box change misses and re-renders; otherwise it's a hit."""
    try:
        stt = os.stat(real)
    except OSError:
        return None
    key = hashlib.sha1(
        f"{os.path.realpath(real)}|{stt.st_mtime_ns}|{stt.st_size}|{box}".encode()
    ).hexdigest()
    return Path(cache_dir) / f"{key}.jpg"


# Per-cache-key in-flight locks so N concurrent requests for the SAME uncached
# thumb decode ONCE (the rest wait, then read the just-written cache file) — the
# dict itself is guarded by a small mutex (R-3).
_THUMB_INFLIGHT_MUTEX = threading.Lock()
_THUMB_INFLIGHT: dict = {}
# Bound on concurrent full-resolution decodes so a gallery scroll can't spawn
# hundreds of ~72 MB RGB decodes at once (thread-per-connection server). Sized to
# leave headroom on the box; never below 1 (R-3).
_THUMB_DECODE_SEM = threading.Semaphore(max(1, min(4, (os.cpu_count() or 3) - 2)))


def _decode_and_cache(path, box, cp):
    """Decode one thumbnail under the concurrency semaphore, then atomically write
    it to the cache path `cp` (or skip caching when cp is None)."""
    with _THUMB_DECODE_SEM:
        buf = _jpeg_bytes(path, box)
    if buf is not None and cp is not None:
        try:
            cp.parent.mkdir(parents=True, exist_ok=True)
            # pid+tid in the tmp name so two threads/processes writing the same
            # cache key never clobber each other's temp file.
            tmp = cp.with_name(cp.name + f".tmp.{os.getpid()}.{threading.get_ident()}")
            tmp.write_bytes(buf)
            os.replace(tmp, cp)          # atomic publish; concurrent readers safe
        except OSError:
            pass
    return buf


def _thumb_bytes(path, box=320, cache_dir=None):
    """JPEG thumbnail bytes, with an optional on-disk cache so the first scroll of
    a large gallery doesn't re-decode every full-resolution original on every
    request (browser cache only helps within one session/URL).

    Two concurrency guards (R-3): a per-cache-key in-flight lock so simultaneous
    requests for the same uncached thumb decode once, and a bounded decode
    semaphore (in _decode_and_cache) so a burst of distinct thumbs can't spawn an
    unbounded number of full-resolution decodes at once."""
    cp = _thumb_cache_path(cache_dir, path, box) if cache_dir is not None else None
    if cp is not None and cp.exists():
        try:
            return cp.read_bytes()
        except OSError:
            pass
    if cp is None:
        return _decode_and_cache(path, box, cp)   # no cache key → no dedup possible
    key = str(cp)
    with _THUMB_INFLIGHT_MUTEX:
        lock = _THUMB_INFLIGHT.get(key)
        if lock is None:
            lock = threading.Lock()
            _THUMB_INFLIGHT[key] = lock
    try:
        with lock:
            # A prior holder of this lock may have just written the cache file —
            # re-check before decoding so only the first thread actually decodes.
            if cp.exists():
                try:
                    return cp.read_bytes()
                except OSError:
                    pass
            return _decode_and_cache(path, box, cp)
    finally:
        # Best-effort cleanup so the in-flight dict doesn't grow without bound.
        with _THUMB_INFLIGHT_MUTEX:
            if _THUMB_INFLIGHT.get(key) is lock:
                _THUMB_INFLIGHT.pop(key, None)


# ── main ─────────────────────────────────────────────────────────────────────────

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Wyeast Family Archive — local server.")
    p.add_argument("case_id")
    p.add_argument("--role", choices=["examiner", "family"], default="examiner")
    p.add_argument("--port", type=int, default=7766)
    p.add_argument("--cases-root", default=None)
    p.add_argument("--force", action="store_true", help="serve even if case looks incomplete")
    return p.parse_args(argv)


def build_case(args):
    cfg = load_pipeline_config()
    root = args.cases_root or str(cases_root(cfg))
    paths = CasePaths.from_case_id(args.case_id, root)
    if not paths.metadata_dir.exists():
        print(f"[family_archive] no metadata dir at {paths.metadata_dir}", file=sys.stderr)
        sys.exit(1)
    # R-7: load_json (used throughout) fails OPEN — a present-but-corrupt
    # archive_map.json parses to {}, silently serving a ZERO-media archive (and, on
    # the examiner side, erasing every delivered/quarantine presence check that
    # keys off archive entries). A LEGITIMATELY ABSENT map is fine (build_archive is
    # optional) and surfaces as a warning in the Overview; a present-but-unparseable
    # one must refuse loudly at startup rather than mislead. Detect it explicitly.
    map_path = paths.metadata_dir / "archive_map.json"
    if map_path.exists():
        try:
            with open(map_path, encoding="utf-8") as fh:
                json.load(fh)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            print(
                f"[family_archive] REFUSING: {map_path} is present but "
                f"unreadable/corrupt ({e}). Serving would silently empty the "
                f"archive (fail-open). Fix or remove the map, or re-run the "
                f"build_archive stage, then try again.", file=sys.stderr)
            sys.exit(1)
    if not args.force:
        assert_complete(paths)
    summary = load_json(paths.metadata_dir / "case_summary.json")
    if summary is None:
        print("[family_archive] case_summary.json missing — cannot serve.", file=sys.stderr)
        sys.exit(1)
    if args.role == "family":
        assert_family_allowed(summary)
        _assert_family_release_startup(paths)   # E3
    return ArchiveCase(paths, args.role, cfg)


def _assert_family_release_startup(paths):
    """E3 — at family startup, distinguish absent ⇒ legacy_unsigned ⇒ START (the
    case is inspectable; E5 gates every body and shows a banner) from
    present-but-invalid ⇒ REFUSE. A tampered/stale/revoked record must never
    silently downgrade to legacy_unsigned. Runs regardless of --force (which skips
    only assert_complete, never the gate)."""
    try:
        rec = release.load_release(paths)
    except release.ReleaseError as exc:
        print(f"[family_archive] REFUSING: {exc}", file=sys.stderr)
        sys.exit(3)
    if rec is None:
        print("[family_archive] no release signature — starting in LEGACY_UNSIGNED "
              "mode: the family surface is CLOSED (bodies gated, a banner shown) "
              "until the case is signed (python3 -m tools.sign_release CASE_ID).",
              file=sys.stderr)
        return
    r = release.verify(paths, rec, live=False)
    if not r.ok:
        print(f"[family_archive] REFUSING: the release record is present but not "
              f"current — {r.reason}. Re-sign the case, or revoke and re-release, "
              f"before serving it to the family.", file=sys.stderr)
        sys.exit(3)
    print(f"[family_archive] release signature current (signed by "
          f"{(rec.get('actor') or {}).get('name')}).", file=sys.stderr)


def main(argv=None):
    global CASE
    args = parse_args(argv)
    CASE = build_case(args)
    # Recover any half-finished move from a prior crash before serving.
    try:
        from wyeast.core.moves import reconcile
        reconcile(CASE.ledger)
    except Exception as e:
        print(f"[family_archive] ledger reconcile warning: {e}", file=sys.stderr)

    port = args.port
    # Threaded so a browser holding open an <audio>/<video> connection doesn't
    # block other requests; mutating verbs are serialized by CASE._lock in do_POST.
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), FamilyArchiveHandler)
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        server = ThreadingHTTPServer(("127.0.0.1", 0), FamilyArchiveHandler)
        port = server.server_address[1]
        # R-4: rebinding SILENTLY let operators run two instances unaware, which can
        # interleave curation writes across processes. Warn loudly and name the
        # actual port so the operator knows a second server may already be running.
        print(
            f"WARNING: port {args.port} is already in use — another Family Archive "
            f"instance may already be running.\n"
            f"WARNING: this instance rebound to port {port}. Running two servers "
            f"against the same case can interleave curation writes; if you did not "
            f"intend to start a second one, stop this instance (Ctrl+C) and use the "
            f"existing server.", file=sys.stderr)
    print(f"Family Archive:  http://127.0.0.1:{port}/")
    print(f"Case:            {CASE.paths.case_id}   (role: {CASE.role})")
    print("Stop with:       Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
