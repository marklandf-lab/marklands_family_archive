"""Full-text search index (SQLite FTS5) for the Wyeast Family Archive.

Builds `output/metadata/_archive_fts_<role>.sqlite` — an FTS5 virtual table over
the FULL recovered text (OCR bodies, transcripts, email + message bodies) plus
lightweight photo/people metadata — so the family/examiner interfaces can answer
"find the letter that mentions the cabin". `build_search` in _archive_data.py only
indexed titles + 160-char snippets; this closes that gap.

Design notes (see docs/specs/family-archive-full-text-search.md):
  * stdlib `sqlite3` with FTS5 — no new dependency, air-gap clean.
  * ROLE GATING lives in ONE place per type: rows are drawn from the already
    role-gated builders in _archive_data (document_rows(role), audio_rows(role),
    message_rows, the photo `universe`, and — for email — the noise-excluded
    thread set in email_threads_index.json), NEVER from the raw indexes. So a
    quarantined/undelivered/noise item can never enter the FAMILY db.
  * EMAILS are built by iterating email_threads_index.threads (NOT email_index):
    email_index carries no thread_id AND iterating it would index noise-log
    threads that appear in no family view — a confidentiality LEAK. We pull each
    thread's message bodies (ocr_text) from email_index keyed by `file`.
  * The sqlite file IS content (plaintext OCR/email). It lives under
    output/metadata/, which export_delivery.py never ships (it delivers only what
    its INCLUDE_TOP allowlist names, and "metadata" is not on it) and which
    resolve_media_path forbids to /media. It must NEVER be copied onto an
    unencrypted family stick.
  * The thread set is per-role (wyeast.core.audience): the family's excludes
    estate-rescued mail, so the family's database cannot return a hit on a body
    they may not see — even though the email_index it joins against still has it.

Runnable as a module for a one-shot build:
    python -m tools.build_fts CASE_ID --role family [--cases-root /cases]
"""

import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from wyeast.core.audience import load_thread_index, thread_index_path  # noqa: E402
from tools._archive_data import (  # noqa: E402
    audio_rows, build_photo_universe, document_rows, event_album_titles,
    load_json, log, message_rows, people_rows, photo_rows,
)

# The FTS5 virtual table. `body` (column index 1) is what snippet() highlights;
# title is searched too. page/kind/ref ride along UNINDEXED for the deep link.
FTS_SCHEMA = (
    "CREATE VIRTUAL TABLE docs USING fts5("
    "title, body, page UNINDEXED, kind UNINDEXED, ref UNINDEXED, "
    "tokenize='unicode61 remove_diacritics 2')"
)

# Source index files whose mtimes decide freshness. A rebuild is triggered when
# ANY of these is newer than the sqlite file (mtimes stored in the meta table).
#
# The conversation index is NOT in this list: it is per-role, so it is added by
# source_mtimes() from the role being built. Tracking the unsuffixed name here
# meant the examiner's FTS freshness was decided by the *family's* thread index
# — rebuild the family bundle and the examiner's database would go stale
# without noticing.
SOURCE_INDEXES = [
    "case_summary.json", "ocr_index.json", "transcription_index.json",
    "email_index.json", "message_index.json",
    "conversation_index.json", "scene_index.json", "face_clustering.json",
    "geo_cluster_index.json", "metadata_index.json", "archive_map.json",
    "video_frame_map.json",
]

_BODY_CAP = 500_000  # per-row body clamp — FTS handles large text, but bound it.


def db_path_for(metadata_dir, role):
    """Path of the FTS sqlite for a role (family + examiner kept distinct so a
    family session can never query examiner-only content)."""
    return Path(metadata_dir) / f"_archive_fts_{role}.sqlite"


def progress_path_for(metadata_dir, role):
    return Path(metadata_dir) / f"_archive_fts_{role}.progress.json"


def source_mtimes(metadata_dir, role):
    """{filename: mtime_ns} for every tracked source index (0 when absent).

    Role-parameterized because the conversation index is role-scoped: the
    freshness key has to name the same file the build actually reads.
    """
    md = Path(metadata_dir)
    names = SOURCE_INDEXES + [thread_index_path(md, role).name]
    out = {}
    for name in names:
        try:
            out[name] = os.stat(md / name).st_mtime_ns
        except OSError:
            out[name] = 0
    return out


def is_fresh(db_path, paths, role=None):
    """True when the sqlite exists and its stored source mtimes match the current
    ones (nothing upstream changed since the build).

    `role` defaults to the one recorded in the database's own meta table, so a
    caller holding only a path still compares against the right (role-scoped)
    conversation index."""
    db_path = Path(db_path)
    if not db_path.exists():
        return False
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            cur = conn.execute("SELECT value FROM meta WHERE key='mtimes'")
            row = cur.fetchone()
            if role is None:
                r = conn.execute(
                    "SELECT value FROM meta WHERE key='role'").fetchone()
                role = r[0] if r else None
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    if not row or role is None:
        return False
    import json
    try:
        stored = json.loads(row[0])
    except (ValueError, TypeError):
        return False
    # Compare as strings — JSON keys are strings and mtime_ns round-trips exactly.
    current = {k: v for k, v in source_mtimes(paths.metadata_dir, role).items()}
    return stored == current


# ── row assembly ─────────────────────────────────────────────────────────────

def _clean(text):
    return (text or "").replace("\x00", " ")[:_BODY_CAP]


def iter_rows(paths, role, cfg, *, progress=None):
    """Yield (title, body, page, kind, ref) for every searchable item, drawn from
    the role-gated builders so gating stays in one place per type.

    `progress(dict)` is called with coarse phase markers so a UI/observer can poll.
    """
    md = paths.metadata_dir

    def phase(name):
        if progress:
            progress({"phase": name})

    summary = load_json(md / "case_summary.json", {}) or {}
    ocr_index = load_json(md / "ocr_index.json", []) or []
    transcription_index = load_json(md / "transcription_index.json", []) or []

    # ── documents: gate + title via document_rows(role); body = full OCR text,
    # else the full (un-truncated) classification summary. document_rows already
    # drops email items and, for family, account_credentials. ──
    phase("documents")
    ocr_full = {r["file"]: r["ocr_text"] for r in ocr_index
                if r.get("file") and r.get("ocr_text")}
    summary_full = {}
    for d in summary.get("document_classifications", []) or []:
        f = d.get("file")
        if f and f not in summary_full:
            summary_full[f] = d.get("summary") or ""
    for r in document_rows(summary, ocr_index, role):
        f = r.get("file")
        body = ocr_full.get(f) or summary_full.get(f) or r.get("summary") or ""
        yield (r.get("name") or "", _clean(body), "documents", "document", f)

    # ── transcripts: gate via audio_rows(role) (respects transcribe.deliver);
    # body = the FULL transcript_text. ──
    phase("transcripts")
    txt_full = {r["file"]: r["transcript_text"] for r in transcription_index
                if r.get("file") and r.get("transcript_text")}
    for r in audio_rows(summary, transcription_index, role, cfg):
        f = r.get("file")
        body = txt_full.get(f) or r.get("preview") or ""
        yield (r.get("name") or "", _clean(body), "recordings", "audio", f)

    # ── messages: mirror message_rows, ROLE-SCOPED; body = concat of that
    # conversation's message chunk texts from message_index.
    #
    # Same gate as the email block below, and for the same reason: the family's
    # database must not be able to return a hit on a body they may not see. Until
    # this passed `role`, the family's search index carried the full bodies of
    # estate-rescued conversations AND of platform traffic that sensitive_scan
    # had never screened. ──
    phase("messages")
    conversation_index = load_json(md / "conversation_index.json", []) or []
    conv_rows = message_rows(conversation_index, role)
    if conv_rows:
        allowed = {r.get("conversation_id"): r.get("display_name") for r in conv_rows}
        bodies = {}
        for rec in load_json(md / "message_index.json", []) or []:
            cid = rec.get("conversation_id")
            if cid in allowed and rec.get("ocr_text"):
                bodies.setdefault(cid, []).append(rec["ocr_text"])
        for cid, title in allowed.items():
            body = "\n".join(bodies.get(cid, []))
            yield (title or "(conversation)", _clean(body), "messages",
                   "conversation", cid)

    # ── emails: iterate the NOISE-EXCLUDED, ROLE-SCOPED thread set; pull each
    # message's body (ocr_text) from email_index keyed by `file`. Never reach
    # into email_index for threads not in this set (that is the leak vector).
    #
    # The thread set is the gate, and it is now per-role: the family's excludes
    # estate-rescued mail, so the family's FTS database cannot return a hit on a
    # marketing body even though the raw index it joins against still holds one.
    phase("emails")
    threads = (load_thread_index(md, role) or {}).get("threads", []) or []
    if threads:
        # Read the ~120 MB email_index ONCE, keyed by file, keeping only the body
        # (email_by_file discipline — drop the rest so peak RSS stays bounded).
        email_body_by_file = {}
        for rec in load_json(md / "email_index.json", []) or []:
            f = rec.get("file")
            if f:
                email_body_by_file[f] = rec.get("ocr_text") or ""
        for t in threads:
            tid = t.get("thread_id")
            if not tid:
                continue
            parts = [email_body_by_file.get(f, "") for f in (t.get("files") or [])]
            body = "\n".join(p for p in parts if p)
            yield ((t.get("subject") or "(no subject)").strip(),
                   _clean(body), "emails", "email", tid)

    # ── photos + people: lightweight metadata (name/scene/place/caption + person
    # name/summary), gated by the role-scoped photo `universe`. ──
    phase("photos")
    scene_index = load_json(md / "scene_index.json", {}) or {}
    archive_map = load_json(md / "archive_map.json", {}) or {}
    metadata_index = load_json(md / "metadata_index.json", {}) or {}
    geo_index = load_json(md / "geo_cluster_index.json", {}) or {}
    face_clustering = load_json(md / "face_clustering.json", {}) or {}
    video_frame_map = load_json(md / "video_frame_map.json", {}) or {}
    universe = build_photo_universe(scene_index, archive_map, role, video_frame_map)
    llava = scene_index.get("llava_results", {}) or {}
    for r in photo_rows(universe, metadata_index, geo_index, {},
                        event_album_titles(summary), llava_map=llava):
        body = " ".join(filter(None, [
            r.get("name"), r.get("scene"), r.get("place"), r.get("trip"),
            r.get("caption")] + (r.get("albums") or [])[:3]))
        yield (r.get("name") or "", _clean(body), "photos", "photo", r.get("id"))

    phase("people")
    for r in people_rows(face_clustering, summary, universe, {}, role,
                         frame_map=video_frame_map, archive_map=archive_map):
        body = " ".join(filter(None, [r.get("name"), r.get("summary")]))
        yield (r.get("name") or "", _clean(body), "people", "person",
               r.get("person_id"))


# ── build ────────────────────────────────────────────────────────────────────

def build_fts_db(paths, role, cfg, *, out_path=None, progress=None):
    """Build the FTS sqlite for a role and atomically publish it. Returns the path.

    O(corpus) once; queries are then milliseconds. Written to a temp file in the
    same dir then os.replace()'d so a concurrent reader never sees a half-built db.
    """
    import json
    out_path = Path(out_path) if out_path else db_path_for(paths.metadata_dir, role)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Unique per PID *and* thread: two concurrent builds in one process (a daemon
    # build racing a direct call) must not share a tmp db, or the second's CREATE
    # VIRTUAL TABLE hits "table docs already exists" and the os.replace races into a
    # "disk I/O error" on the other's open handle.
    tmp = out_path.with_name(f"{out_path.name}.{os.getpid()}.{threading.get_ident()}.building")
    for stale in (tmp, tmp.with_name(tmp.name + "-journal"), tmp.with_name(tmp.name + "-wal")):
        try:
            stale.unlink()
        except OSError:
            pass
    started = time.time()
    if progress:
        progress({"state": "building", "phase": "start", "started": started})
    conn = sqlite3.connect(str(tmp))
    n = 0
    try:
        conn.execute(FTS_SCHEMA)
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("BEGIN")
        for title, body, page, kind, ref in iter_rows(paths, role, cfg, progress=progress):
            conn.execute(
                "INSERT INTO docs(title, body, page, kind, ref) VALUES (?,?,?,?,?)",
                (title, body, page, kind, str(ref) if ref is not None else None))
            n += 1
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('mtimes', ?)",
            (json.dumps(source_mtimes(paths.metadata_dir, role)),))
        conn.execute("INSERT INTO meta(key, value) VALUES ('role', ?)", (role,))
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('rows', ?)", (str(n),))
        conn.commit()
    finally:
        conn.close()
    os.replace(tmp, out_path)
    elapsed = time.time() - started
    log(f"FTS[{role}]: indexed {n} rows in {elapsed:.1f}s → {out_path.name}")
    if progress:
        progress({"state": "ready", "rows": n, "elapsed": elapsed})
    return out_path


# ── query ────────────────────────────────────────────────────────────────────

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _plain_query(q):
    """Fallback for a raw string that is not a valid FTS expression: quote every
    word token so no character is treated as an FTS operator. '' when no tokens."""
    toks = _WORD_RE.findall(q or "")
    if not toks:
        return ""
    return " ".join('"' + t + '"' for t in toks)


def _hit(row):
    return {"snippet": row[0], "title": row[1], "page": row[2],
            "ref": row[3], "kind": row[4]}


def search(db_path, query, offset=0, limit=20):
    """Run an FTS query, returning {hits, total, building:False}.

    The raw user string is tried first as a MATCH expression (so power syntax like
    `cabin OR lake` and prefix `insur*` work); a malformed expression is caught and
    retried as a fully-quoted plain-term query — NEVER a 500."""
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 20
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for match in (query or "", _plain_query(query)):
            if not (match or "").strip():
                continue
            try:
                total = conn.execute(
                    "SELECT count(*) FROM docs WHERE docs MATCH ?", (match,)
                ).fetchone()[0]
                rows = conn.execute(
                    "SELECT snippet(docs, 1, '<mark>', '</mark>', '…', 12) AS snip, "
                    "title, page, ref, kind FROM docs WHERE docs MATCH ? "
                    "ORDER BY rank LIMIT ? OFFSET ?",
                    (match, limit, offset)).fetchall()
                return {"hits": [_hit(r) for r in rows], "total": total,
                        "building": False}
            except sqlite3.OperationalError:
                continue  # malformed FTS expr → try the plain-term fallback
        return {"hits": [], "total": 0, "building": False}
    finally:
        conn.close()


# ── module entrypoint ─────────────────────────────────────────────────────────

def main(argv=None):
    import argparse
    from wyeast.core.config import load_pipeline_config
    from wyeast.core.paths import CasePaths

    ap = argparse.ArgumentParser(description="Build the Family Archive FTS index.")
    ap.add_argument("case_id")
    ap.add_argument("--role", choices=["family", "examiner"], default="examiner")
    ap.add_argument("--cases-root", default=None)
    args = ap.parse_args(argv)
    if args.cases_root:
        paths = CasePaths.from_case_id(args.case_id, args.cases_root)
    else:
        paths = CasePaths.from_case_id(args.case_id)
    cfg = {}
    try:
        cfg = load_pipeline_config()
    except Exception:
        pass
    build_fts_db(paths, args.role, cfg, progress=lambda i: log(f"FTS: {i}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
