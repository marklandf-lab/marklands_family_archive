#!/usr/bin/env python3
"""
gen_email_threads.py — Email Conversation Pages
Digital Estate Recovery Service

Reassembles the kept emails from email_index.json into conversation
threads (JWZ-style: Message-ID / In-Reply-To / References, with a
subject + participants + time-window fallback for mail missing those
headers) and renders one family-readable HTML page per conversation,
plus an index page, under output/email_threads/.

Per-email significance and category from case_summary.json (step 13)
are joined in when available to rank conversations; pages still render
without them. No LLM calls are made — threading is purely structural.

Inline attachments and images are referenced by name and size, never
embedded. Every rendered message links back to its original .eml file.

AUDIENCE. Pages and the conversation index are built per audience
(wyeast.core.audience). The family's exclude estate-rescued mail — the bulk and
platform messages email_triage rescued for the examiner *after* its own
family-relevance triage had discarded them. The examiner's contain everything.
The two get separate index files and separate page directories: they used to
share one filename, so whichever role built last overwrote the other's.

Threads are built, and their ids assigned, from the WHOLE index before the
audience filter runs. That is what makes a thread_id mean the same conversation
to both audiences — and it is what lets family_decisions.json's demotions
survive, since they are keyed by thread_id.

Outputs (per audience A):
  output/metadata/email_threads_index_A.json  conversation membership & ranking
  output/email_threads[_A]/index.html         ranked conversation list
  output/email_threads[_A]/t<hash>.html       one page per conversation

Invoked automatically by gen_case_report.py; can also run standalone:

Usage:
    python3 gen_email_threads.py CASE_001
    python3 gen_email_threads.py CASE_001 --audience examiner
    python3 gen_email_threads.py CASE_001 --out /tmp/   # custom output dir
"""

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.parse
import email.utils as _email_utils
from collections import defaultdict
from datetime import datetime
from html import escape as _html_escape
from pathlib import Path

import sys; from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent          # repo root (parent of tools/)
for _p in (str(_ROOT), str(Path(__file__).resolve().parent)):
    if _p not in sys.path: sys.path.insert(0, _p)

from wyeast.core.audience import (
    AUDIENCES, FAMILY, filter_email_entries, is_family_visible,
    thread_index_path, thread_pages_dirname)
from wyeast.core.config import config_dir
from wyeast.core.io import atomic_write_json
from wyeast.core.paths import CasePaths

PIPELINE_CONFIG_PATH = config_dir() / "pipeline_config.json"

_DEFAULT_CONFIG = {
    "enabled": True,
    "subject_fallback_window_days": 14,
    "min_thread_size_to_render": 1,
    "max_conversations_in_report": 10,
}

# ── Threading core (pure functions — no filesystem access) ───────────────────


class _Container:
    """JWZ threading container: one node per Message-ID seen or referenced."""
    __slots__ = ("entry", "parent", "children", "duplicate")

    def __init__(self, entry=None):
        self.entry     = entry   # index entry dict, or None for a phantom
        self.parent    = None
        self.children  = []
        self.duplicate = False   # duplicate copy of another kept message


def _would_cycle(child, candidate_parent) -> bool:
    node = candidate_parent
    while node is not None:
        if node is child:
            return True
        node = node.parent
    return False


def _link(parent, child) -> None:
    if child.parent is not None or parent is child or _would_cycle(child, parent):
        return
    child.parent = parent
    parent.children.append(child)


_SUBJ_PREFIX_RE = re.compile(
    r"^\s*(?:(?:re|fw|fwd|aw|sv|vs)(?:\[\d+\])?\s*:\s*|\[[^\]]{1,40}\]\s*)+",
    re.IGNORECASE,
)


def _norm_subject(subject: str) -> str:
    """Strip reply/forward prefixes and list tags; collapse whitespace; casefold."""
    s = _SUBJ_PREFIX_RE.sub("", subject or "")
    return re.sub(r"\s+", " ", s).strip().casefold()


def _participants(entry: dict) -> set:
    addrs = set()
    for field in ("email_from", "email_to"):
        for _, addr in _email_utils.getaddresses([entry.get(field, "") or ""]):
            if addr:
                addrs.add(addr.lower())
    return addrs


def _dt_entry(entry: dict):
    """datetime from the entry's normalized ISO date, or None."""
    try:
        return datetime.fromisoformat(entry.get("email_date_iso") or "")
    except Exception:
        return None


def build_threads(entries: list[dict], window_days: int = 14) -> list[dict]:
    """Group kept-email index entries into conversation threads.

    Returns a list of thread dicts:
      {
        "messages": [{"entry": <index dict>, "duplicate": bool,
                      "reply_to_file": <file of nearest recovered ancestor> | None}],
        "has_missing_ancestor": bool,   # referenced but unrecovered mail in chain
        "linked_by": "headers" | "heuristic" | "single",
      }
    with messages in chronological order (unparseable dates last).
    """
    table: dict = {}     # message-id -> _Container
    holders: list = []   # containers holding an actual entry, input order

    def _get(mid):
        c = table.get(mid)
        if c is None:
            c = table[mid] = _Container()
        return c

    # Pass 1: place every entry in a container keyed by Message-ID.
    for entry in entries:
        mid = entry.get("message_id") or ""
        if not mid:
            mid = "<noid:%s>" % entry.get("file", id(entry))
        c = table.get(mid)
        if c is not None and c.entry is not None:
            # Same Message-ID kept twice (e.g. Sent + Inbox copies): keep
            # both, threaded together under the first occurrence.
            dup = _Container(entry)
            dup.duplicate = True
            table["%s#dup%d" % (mid, len(holders))] = dup
            _link(c, dup)
            holders.append(dup)
            continue
        c = _get(mid)
        c.entry = entry
        holders.append(c)

    # Pass 2: link the References chain oldest→newest, then the message
    # itself under the last reference (or In-Reply-To). Existing parents are
    # never overridden and cycles are refused.
    for c in holders:
        if c.duplicate:
            continue
        refs = [r for r in (c.entry.get("references") or []) if r]
        prev = None
        for ref in refs:
            node = _get(ref)
            if prev is not None:
                _link(prev, node)
            prev = node
        parent_id = refs[-1] if refs else (c.entry.get("in_reply_to") or "")
        if parent_id:
            _link(_get(parent_id), c)

    # Per-root flatten. A phantom with recovered descendants marks a message
    # that was referenced but never recovered (filtered or absent).
    roots = [c for c in table.values() if c.parent is None]

    counts: dict = {}

    def _count(node) -> int:
        n = 1 if node.entry is not None else 0
        for ch in node.children:
            n += _count(ch)
        counts[id(node)] = n
        return n

    def _walk(node, anc_file, msgs, flags):
        if node.entry is not None:
            msgs.append({
                "entry":         node.entry,
                "duplicate":     node.duplicate,
                "reply_to_file": anc_file,
            })
            anc_file = node.entry.get("file")
        elif counts[id(node)] > 0:
            flags["missing"] = True
        for ch in node.children:
            _walk(ch, anc_file, msgs, flags)

    raw_threads: list[dict] = []
    for root in roots:
        _count(root)
        msgs: list = []
        flags = {"missing": False}
        _walk(root, None, msgs, flags)
        if not msgs:
            continue
        raw_threads.append({
            "messages":             msgs,
            "has_missing_ancestor": flags["missing"],
            "linked_by":            "headers" if len(msgs) > 1 else "single",
        })

    # Fallback: merge header-less singletons into a thread sharing the
    # normalized subject, at least one participant, and a date within the
    # window. Undated singletons never merge (the window can't be checked).
    by_subject: dict = defaultdict(list)
    for t in raw_threads:
        t["_norm_subject"] = _norm_subject(
            t["messages"][0]["entry"].get("email_subject", "")
        )
        by_subject[t["_norm_subject"]].append(t)

    merged_away: set = set()
    for t in raw_threads:
        if id(t) in merged_away or len(t["messages"]) != 1:
            continue
        e = t["messages"][0]["entry"]
        if e.get("references") or e.get("in_reply_to"):
            continue
        subj = t["_norm_subject"]
        if not subj:
            continue
        d = _dt_entry(e)
        if d is None:
            continue
        parts = _participants(e)
        if not parts:
            continue
        for cand in by_subject[subj]:
            if cand is t or id(cand) in merged_away:
                continue
            cand_parts: set = set()
            cand_dates: list = []
            for m in cand["messages"]:
                cand_parts |= _participants(m["entry"])
                cd = _dt_entry(m["entry"])
                if cd is not None:
                    cand_dates.append(cd)
            if not (parts & cand_parts):
                continue
            if not any(abs((d - cd).total_seconds()) <= window_days * 86400
                       for cd in cand_dates):
                continue
            cand["messages"].extend(t["messages"])
            cand["has_missing_ancestor"] |= t["has_missing_ancestor"]
            cand["linked_by"] = "heuristic"
            merged_away.add(id(t))
            break

    threads = [t for t in raw_threads if id(t) not in merged_away]
    for t in threads:
        t.pop("_norm_subject", None)
        t["messages"].sort(key=lambda m: (m["entry"].get("email_date_iso") or "9999",
                                          m["entry"].get("file") or ""))
    return threads


# ── Presentation helpers ──────────────────────────────────────────────────────

_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _e(s) -> str:
    """Escape untrusted text for HTML, stripping C0 control characters."""
    return _html_escape(_CTRL_RE.sub("", str(s or "")), quote=True)


def _fmt_date(iso: str, raw: str = "") -> str:
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%B %d, %Y at %H:%M UTC")
    except Exception:
        return raw or "Date unknown"


def _human_size(n) -> str:
    if n is None:
        return "size unknown"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    return "size unknown"


# Significance palette — keep in sync with _significance_label in
# gen_case_report.py.
_SIG_LABELS = {
    1: ("Routine", "#6c757d"),
    2: ("Personal", "#17a2b8"),
    3: ("Meaningful", "#28a745"),
    4: ("Important", "#fd7e14"),
    5: ("Major Life Event", "#dc3545"),
}

_EMAIL_CATEGORY_LABELS = {
    "personal_correspondence": "Personal Messages",
    "financial":               "Financial Emails",
    "legal":                   "Legal Correspondence",
    "medical":                 "Medical Correspondence",
    "work_correspondence":     "Work / Professional",
    "newsletters_lists":       "Newsletters & Subscriptions",
    "miscellaneous":           "Other",
}


def _sig_badge(score) -> str:
    if not score:
        return ""
    score = max(1, min(5, int(score)))
    label, color = _SIG_LABELS[score]
    return f'<span class="badge" style="background:{color}">{label}</span>'


def _eml_href(eml_path: str, threads_dir: Path, case_dir: Path) -> str:
    """Link from a thread page back to the original .eml.

    Relative when both ends live inside the case directory (the link then
    survives moving the whole case folder — the deliverable unit); absolute
    file:// otherwise.
    """
    def _is_under(p: Path, root: Path) -> bool:
        try:
            p.resolve().relative_to(root.resolve())
            return True
        except Exception:
            return False

    p = Path(eml_path)
    if _is_under(p, case_dir) and _is_under(threads_dir, case_dir):
        rel = os.path.relpath(str(p), start=str(threads_dir))
        return urllib.parse.quote(rel.replace(os.sep, "/"))
    return "file://" + urllib.parse.quote(str(p))


# ── HTML rendering ────────────────────────────────────────────────────────────

# Trimmed copy of the gen_case_report.py stylesheet (Georgia 11pt, letter
# pages) plus message-card rules. Keep the palette in sync with that file.
_CSS = """
*, *::before, *::after { box-sizing: border-box; }
body {
    font-family: Georgia, "Times New Roman", serif;
    font-size: 11pt;
    line-height: 1.6;
    color: #1a1a1a;
    background: #fff;
    margin: 0 auto;
    padding: 1.5rem 2rem;
    max-width: 52rem;
}
@page { size: letter; margin: 1in 0.9in; }
@media print {
    a { color: inherit; text-decoration: none; }
    body { font-size: 10pt; }
}
h1 { font-size: 1.5rem; color: #1a3a5c; margin-bottom: 0.25rem; }
h2 {
    font-size: 1.25rem;
    color: #1a3a5c;
    border-bottom: 2px solid #1a3a5c;
    padding-bottom: 0.3rem;
    margin-top: 0;
    margin-bottom: 1rem;
}
p { margin: 0 0 0.75rem; }
.service-name {
    font-size: 0.85rem;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    color: #6c757d;
    margin-bottom: 0.5rem;
}
table { width: 100%; border-collapse: collapse; font-size: 0.9rem; margin-bottom: 1rem; }
th {
    background: #1a3a5c;
    color: #fff;
    text-align: left;
    padding: 0.5rem 0.75rem;
    font-weight: normal;
    font-size: 0.85rem;
}
td { padding: 0.45rem 0.75rem; border-bottom: 1px solid #e0e0e0; vertical-align: top; }
tr:nth-child(even) td { background: #f8f9fa; }
tr:last-child td { border-bottom: none; }
.badge {
    display: inline-block;
    padding: 0.15em 0.55em;
    border-radius: 3px;
    color: #fff;
    font-size: 0.78rem;
    font-family: Arial, sans-serif;
    white-space: nowrap;
}
.crumb { font-size: 0.85rem; margin-bottom: 1rem; }
.thread-meta { color: #555; font-size: 0.92rem; }
.msg {
    border: 1px solid #d0d7de;
    border-radius: 6px;
    margin-bottom: 1rem;
    page-break-inside: avoid;
}
.msg-head {
    background: #f0f4f8;
    padding: 0.6rem 1rem;
    font-size: 0.85rem;
    border-bottom: 1px solid #e0e0e0;
}
.msg-head div { margin-bottom: 0.1rem; }
.msg-tags { margin-top: 0.3rem; color: #555; }
.msg-body {
    padding: 0.8rem 1rem;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font-size: 0.95rem;
}
.msg-missing {
    border: 1px dashed #adb5bd;
    border-radius: 6px;
    color: #6c757d;
    background: #fafafa;
    padding: 0.6rem 1rem;
    margin-bottom: 1rem;
    font-size: 0.88rem;
}
.att {
    font-size: 0.82rem;
    color: #555;
    border-top: 1px solid #e8e8e8;
    padding: 0.4rem 1rem;
}
.eml-link { font-size: 0.78rem; }
footer {
    border-top: 1px solid #ccc;
    padding-top: 0.75rem;
    font-size: 0.78rem;
    color: #888;
    margin-top: 3rem;
    text-align: center;
}
"""


def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
{_CSS}
</style>
</head>
<body>
{body}
</body>
</html>"""


def _footer(case_id: str, report_date: str) -> str:
    return f"""
<footer>
    Digital Estate Recovery Service &nbsp;|&nbsp;
    Case: {_e(case_id)} &nbsp;|&nbsp;
    Generated: {report_date} &nbsp;|&nbsp;
    All original files preserved. No content was permanently deleted.
</footer>"""


def resolved_names(entries) -> dict:
    """address -> address-book name, harvested from email_triage's `from_display`.

    email_triage resolves each message's SENDER against the case's address
    books and stamps the winner on the record. Recipients carry no such stamp,
    but the same person is usually a sender somewhere in the corpus, so one
    pass over every entry gives a map that names them on both sides of a
    thread. Header-derived and address-shaped values are skipped: this map
    exists to hold names the header did NOT have.
    """
    out: dict = {}
    for e in entries or []:
        display = (e.get("from_display") or "").strip()
        if not display or "@" in display:
            continue
        for _n, addr in _email_utils.getaddresses([e.get("email_from", "") or ""]):
            if addr:
                out.setdefault(addr.lower(), display)
                break
    return out


def _participants_display(msgs: list[dict], resolved: dict = None) -> list[str]:
    """Display names (falling back to addresses) in first-appearance order.

    An address-book name outranks the header's display name — same rule as
    email_triage's own sender naming (docs/specs/contact-name-surfaces.md §5),
    so a thread reads with the same names as the Messages surface.
    """
    seen: dict = {}
    for m in msgs:
        e = m["entry"]
        for field in ("email_from", "email_to"):
            for name, addr in _email_utils.getaddresses([e.get(field, "") or ""]):
                if not addr:
                    continue
                key = addr.lower()
                book = (resolved or {}).get(key, "")
                if book:
                    seen[key] = (book, addr)
                elif key not in seen or (name and not seen[key][0]):
                    seen[key] = (name.strip(), addr)
    return [name if name else addr for name, addr in seen.values()]


def _participants_full(msgs: list[dict], resolved: dict = None) -> list[str]:
    """'Name <addr>' strings for the JSON index, first-appearance order.

    An address-book name (see resolved_names) outranks the header's.
    """
    seen: dict = {}
    for m in msgs:
        e = m["entry"]
        for field in ("email_from", "email_to"):
            for name, addr in _email_utils.getaddresses([e.get(field, "") or ""]):
                if not addr:
                    continue
                key = addr.lower()
                book = (resolved or {}).get(key, "")
                if book:
                    seen[key] = f"{book} <{addr}>"
                elif key not in seen or (name and "<" not in seen[key]):
                    seen[key] = f"{name.strip()} <{addr}>" if name.strip() else addr
    return list(seen.values())


def _names_summary(names: list[str], limit: int = 3) -> str:
    if not names:
        return "—"
    shown = ", ".join(_e(n) for n in names[:limit])
    extra = len(names) - limit
    return shown + (f" +{extra} more" if extra > 0 else "")


def _date_range_label(date_first: str, date_last: str) -> str:
    d1, d2 = date_first[:10], date_last[:10]
    if d1 and d2 and d1 != d2:
        return f"{d1} – {d2}"
    return d1 or "—"


def _build_thread_page(case_id: str, t: dict, threads_dir: Path,
                       case_dir: Path, report_date: str,
                       entry_by_file: dict, resolved: dict = None) -> str:
    msgs    = t["messages"]
    subject = t["subject"]

    summary_bits = [f"{len(msgs)} message{'s' if len(msgs) != 1 else ''}"]
    names = _participants_display(msgs, resolved)
    if names:
        summary_bits.append("between " + _names_summary(names, limit=4))
    drange = _date_range_label(t["date_first"], t["date_last"])
    if drange != "—":
        summary_bits.append(drange)
    summary_line = ", ".join(summary_bits) + "."

    cards = ""
    if t["has_missing_ancestor"]:
        cards += """
<div class="msg-missing">
    An earlier message in this conversation was referenced but not recovered.
    It may have been filtered out as bulk mail or absent from the archive.
</div>"""

    thread_subject_norm = _norm_subject(subject)
    for m in msgs:
        e = m["entry"]
        cls = m.get("classification") or {}

        head_rows = (
            f"<div><strong>From:</strong> {_e(e.get('email_from') or '—')}</div>"
            f"<div><strong>To:</strong> {_e(e.get('email_to') or '—')}</div>"
            f"<div><strong>Date:</strong> "
            f"<span title=\"{_e(e.get('email_date'))}\">"
            f"{_e(_fmt_date(e.get('email_date_iso') or '', e.get('email_date') or ''))}"
            f"</span></div>"
        )
        msg_subject = (e.get("email_subject") or "").strip()
        if msg_subject and _norm_subject(msg_subject) != thread_subject_norm:
            head_rows += f"<div><strong>Subject:</strong> {_e(msg_subject)}</div>"

        tags = []
        sig = cls.get("significance")
        if sig:
            tags.append(_sig_badge(sig))
        cat = cls.get("category")
        if cat:
            tags.append(_e(_EMAIL_CATEGORY_LABELS.get(
                cat, cat.replace("_", " ").title())))
        if m.get("duplicate"):
            tags.append("(duplicate copy)")
        reply_to = entry_by_file.get(m.get("reply_to_file") or "")
        if reply_to is not None:
            reply_date = _fmt_date(reply_to.get("email_date_iso") or "",
                                   reply_to.get("email_date") or "")
            tags.append(f"In reply to the message of {_e(reply_date)}")
        tags_html = (f'<div class="msg-tags">{" &nbsp; ".join(tags)}</div>'
                     if tags else "")

        att_html = ""
        atts = e.get("attachments") or []
        if atts:
            lines = []
            for a in atts:
                ct = a.get("content_type") or ""
                if a.get("is_inline") and ct.startswith("image/"):
                    kind = "Inline image"
                elif a.get("is_inline"):
                    kind = "Inline file"
                else:
                    kind = "Attachment"
                lines.append(
                    f"[{kind}: {_e(a.get('filename') or '(unnamed)')}"
                    f" — {_e(ct)}, {_human_size(a.get('size_bytes'))}]"
                )
            att_html = (
                '<div class="att">' + "<br>".join(lines) +
                "<br><em>Attachments were extracted separately during intake "
                "and appear in the documents and photos sections of this "
                "case.</em></div>"
            )

        href = _eml_href(e.get("file") or "", threads_dir, case_dir)
        cards += f"""
<div class="msg">
    <div class="msg-head">
        {head_rows}
        {tags_html}
    </div>
    <div class="msg-body">{_e(e.get('ocr_text'))}</div>
    {att_html}
    <div class="att eml-link"><a href="{href}">View original email file</a></div>
</div>"""

    body = f"""
<div class="crumb"><a href="index.html">&larr; All conversations</a></div>
<div class="service-name">Digital Estate Recovery Service</div>
<h2>{_e(subject)}</h2>
<p class="thread-meta">{summary_line}</p>
{cards}
{_footer(case_id, report_date)}"""
    return _page(f"Conversation — {_e(subject)}", body)


def _build_index_page(case_id: str, threads: list[dict],
                      report_date: str) -> str:
    multi      = [t for t in threads if len(t["messages"]) > 1]
    singletons = [t for t in threads if len(t["messages"]) == 1]

    def _rows(items):
        rows = ""
        for t in items:
            sig = t.get("significance")
            rows += f"""<tr>
    <td><a href="{t['thread_id']}.html">{_e(t['subject'])}</a></td>
    <td style="text-align:center">{len(t['messages'])}</td>
    <td>{_names_summary(_participants_display(t['messages']))}</td>
    <td>{_date_range_label(t['date_first'], t['date_last'])}</td>
    <td>{_sig_badge(sig) if sig else "—"}</td>
</tr>"""
        return rows

    header = "<tr><th>Subject</th><th style='text-align:center'>Messages</th>" \
             "<th>Participants</th><th>Dates</th><th>Priority</th></tr>"

    multi_html = ""
    if multi:
        multi_html = f"""
<h2>Conversations</h2>
<p>
    Email exchanges with more than one recovered message, ordered by how
    significant our software rated their content.
</p>
<table>{header}{_rows(multi)}</table>"""

    single_html = ""
    if singletons:
        single_html = f"""
<h2>Individual messages</h2>
<p>
    Recovered emails that were not part of a longer exchange.
</p>
<table>{header}{_rows(singletons)}</table>"""

    total_msgs = sum(len(t["messages"]) for t in threads)
    body = f"""
<div class="service-name">Digital Estate Recovery Service</div>
<h1>Email Conversations — {_e(case_id)}</h1>
<p class="thread-meta">Generated {report_date}</p>
<p>
    The {total_msgs:,} recovered personal emails were reassembled into
    conversations so they can be read in order, like a message thread.
    Each conversation page shows every recovered message with a link back
    to the original email file. Attachments are listed by name; the files
    themselves were recovered separately and appear in the documents and
    photos sections of this case.
</p>
{multi_html}
{single_html}
{_footer(case_id, report_date)}"""
    return _page(f"Email Conversations — {_e(case_id)}", body)


# ── Generation entry point ────────────────────────────────────────────────────


def _thread_id(root_entry: dict) -> str:
    """A conversation's id, derived from the identity of its root message.

    Replaces the old positional `f"thread_{i:04d}"`, which was assigned by rank
    *after* sorting — so any change to the thread set (like filtering out
    estate-rescued mail) renumbered every conversation, silently re-pointing
    family_decisions.json's demotions at different conversations.

    Derived from the root's Message-ID when it has one, else its file path.
    Callers must compute this from the UNFILTERED thread set, so that both
    audiences give the same conversation the same id.
    """
    key = (root_entry.get("message_id") or "").strip()
    if not key:
        key = "file:%s" % (root_entry.get("file") or "")
    return "t" + hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:12]


def assign_thread_ids(threads: list) -> None:
    """Stamp a stable thread_id on every thread, in place.

    Collisions are astronomically unlikely (48 bits over ~10^4 threads) but a
    silent collision would merge two conversations in the family's curation
    file, so they are resolved rather than trusted away.
    """
    seen: dict = {}
    for t in threads:
        base = _thread_id(t["messages"][0]["entry"])
        n = seen.get(base, 0)
        seen[base] = n + 1
        t["thread_id"] = base if n == 0 else f"{base}-{n}"


def generate(case_id: str, output_dir=None, quiet: bool = False,
             case_dir=None, audience: str = FAMILY):
    """Build conversation pages for a case, scoped to one audience.

    Returns the threads-index summary dict (also written to
    email_threads_index_<audience>.json), or None when there is nothing to do
    (no kept emails, threading disabled, index missing).

    The default audience is "family" — the restrictive one. A caller that
    forgets to pass one therefore withholds estate-rescued mail rather than
    shipping it.
    """

    def _say(msg):
        if not quiet:
            print(msg)

    pcfg = {}
    try:
        with open(PIPELINE_CONFIG_PATH) as f:
            pcfg = json.load(f)
    except Exception:
        pass

    if case_dir is None:
        case_dir = Path(pcfg.get("paths", {}).get("cases", "/cases")) / case_id
    case_dir     = Path(case_dir)
    paths        = CasePaths.from_case_dir(case_dir)
    metadata_dir = case_dir / "output" / "metadata"
    output_dir   = Path(output_dir) if output_dir else case_dir / "output"
    pages_name   = thread_pages_dirname(audience)
    threads_dir  = output_dir / pages_name

    cfg = dict(_DEFAULT_CONFIG)
    cfg.update(pcfg.get("email_threads", {}) or {})
    try:
        with open(case_dir / "case_config.json") as f:
            case_overrides = json.load(f).get("email_threads", {}) or {}
        cfg.update({k: v for k, v in case_overrides.items()
                    if not k.startswith("_")})
    except Exception:
        pass
    cfg.pop("_notes", None)

    if not cfg.get("enabled", True):
        _say("Email threading disabled (email_threads.enabled = false)")
        return None

    index_path = metadata_dir / "email_index.json"
    if not index_path.exists():
        _say(f"No email_index.json at {metadata_dir} — run step 10 first; "
             "skipping conversation pages")
        return None
    try:
        entries = json.loads(index_path.read_text())
    except Exception as e:
        _say(f"WARNING: could not parse {index_path.name}: {e}")
        return None
    if not entries:
        _say("email_index.json is empty — no conversations to build")
        return None

    if not any(e.get("message_id") for e in entries):
        _say(
            "NOTE: email_index.json predates threading-header extraction — "
            "conversations are grouped by subject/participants only. To get "
            "header-accurate threading, re-run step 10 by itself "
            "(./stage 10 CASE_ID): body "
            "text and file paths are unchanged by the re-run, so existing "
            "step 13 classifications still apply and no later stage needs "
            "re-running."
        )

    # Join step 13 per-email classification (significance/category) when
    # available; pages render without it.
    classification_by_file: dict = {}
    case_summary = {}
    summary_path = metadata_dir / "case_summary.json"
    if summary_path.exists():
        try:
            case_summary = json.loads(summary_path.read_text())
        except Exception as e:
            _say(f"WARNING: could not parse case_summary.json: {e}")
    for d in case_summary.get("document_classifications", []):
        if d.get("source") == "email" and d.get("file"):
            classification_by_file[d["file"]] = d
    if not classification_by_file:
        _say("NOTE: no step 13 email classifications found — conversations "
             "will be listed without significance ranking")

    window_days = int(cfg.get("subject_fallback_window_days", 14))

    # Thread, and assign ids, over the WHOLE index — before the audience filter.
    # Both steps have to see every message: threading so that a reply whose
    # parent is estate-rescued still lands in the right conversation, and the
    # ids so that a conversation carries the same id in both audiences' indexes
    # (family_decisions.json's demotions are keyed by thread_id, and the
    # examiner is the one who makes them).
    threads = build_threads(entries, window_days=window_days)
    assign_thread_ids(threads)

    # Now scope to the audience. This is RECORD-level, not thread-level: a
    # rescued message inside an otherwise-organic conversation is dropped from
    # the family's copy of that thread, not tolerated because its neighbours are
    # organic. sensitive_scan screens exactly this set, so anything the family
    # can see has been screened — thread-level filtering would break that.
    if audience != FAMILY:
        visible = entries
    else:
        for t in threads:
            t["messages"] = [m for m in t["messages"]
                             if is_family_visible(m["entry"])]
        threads = [t for t in threads if t["messages"]]
        visible = filter_email_entries(entries, audience)
    withheld = len(entries) - len(visible)

    entry_by_file = {e.get("file"): e for e in visible if e.get("file")}
    # address -> address-book name, from email_triage's per-message resolution.
    # Built from the WHOLE visible set (not per thread) so a person named as a
    # sender anywhere is named as a recipient everywhere.
    resolved = resolved_names(visible)

    # Decorate: display subject, dates, significance, categories.
    for t in threads:
        msgs = t["messages"]
        for m in msgs:
            m["classification"] = classification_by_file.get(
                m["entry"].get("file") or "")
        t["subject"] = ((msgs[0]["entry"].get("email_subject") or "").strip()
                        or "(no subject)")
        dates = sorted(m["entry"].get("email_date_iso") or "" for m in msgs
                       if m["entry"].get("email_date_iso"))
        t["date_first"] = dates[0] if dates else ""
        t["date_last"]  = dates[-1] if dates else ""
        sigs = [int(m["classification"].get("significance") or 0)
                for m in msgs if m.get("classification")]
        t["significance"] = max(sigs) if sigs else None
        t["categories"] = sorted({m["classification"].get("category")
                                  for m in msgs
                                  if m.get("classification")
                                  and m["classification"].get("category")})

    min_size = int(cfg.get("min_thread_size_to_render", 1))
    threads = [t for t in threads if len(t["messages"]) >= min_size]
    # Most significant first; within a band, newest activity first. Ranking no
    # longer determines the thread_id — it is only display order.
    threads.sort(key=lambda t: t["subject"])
    threads.sort(key=lambda t: t["date_last"] or "", reverse=True)
    threads.sort(key=lambda t: -(t["significance"] or 0))

    stats = {
        "emails":              len(visible),
        "threads_multi":       sum(1 for t in threads if len(t["messages"]) > 1),
        "singletons":          sum(1 for t in threads if len(t["messages"]) == 1),
        "linked_by_heuristic": sum(1 for t in threads
                                   if t["linked_by"] == "heuristic"),
        "missing_ancestors":   sum(1 for t in threads
                                   if t["has_missing_ancestor"]),
        "duplicates":          sum(1 for t in threads for m in t["messages"]
                                   if m.get("duplicate")),
    }

    generated_at = datetime.now().isoformat()
    report_date  = datetime.now().strftime("%B %d, %Y")

    summary = {
        "generated_at": generated_at,
        "case_id":      case_id,
        "audience":     audience,
        "withheld_estate_rescued": withheld,
        "config": {
            "subject_fallback_window_days": window_days,
            "min_thread_size_to_render":    min_size,
            "max_conversations_in_report":  int(
                cfg.get("max_conversations_in_report", 10)),
        },
        "stats": stats,
        "threads": [
            {
                "thread_id":     t["thread_id"],
                "page":          f"{pages_name}/{t['thread_id']}.html",
                "subject":       t["subject"],
                "message_count": len(t["messages"]),
                "participants":  _participants_full(t["messages"], resolved),
                "date_first":    t["date_first"],
                "date_last":     t["date_last"],
                "significance":  t["significance"],
                "categories":    t["categories"],
                "linked_by":     t["linked_by"],
                "files":         [m["entry"].get("file") for m in t["messages"]],
            }
            for t in threads
        ],
    }
    atomic_write_json(thread_index_path(paths, audience), summary)

    # Pages are regenerable artifacts: clear only our own previous output. The
    # sweep is "*.html", not "thread_*.html" — a page written by the old
    # positional-id scheme is stale the moment ids become content-derived, and
    # in the family's tree that is a delivered file nothing else would remove.
    threads_dir.mkdir(parents=True, exist_ok=True)
    for old in threads_dir.glob("*.html"):
        old.unlink()

    for t in threads:
        page = _build_thread_page(case_id, t, threads_dir, case_dir,
                                  report_date, entry_by_file, resolved)
        (threads_dir / f"{t['thread_id']}.html").write_text(
            page, encoding="utf-8")
    (threads_dir / "index.html").write_text(
        _build_index_page(case_id, threads, report_date), encoding="utf-8")

    _say(
        f"Email conversations written [{audience}]: {threads_dir}/index.html "
        f"({stats['threads_multi']} conversation(s), "
        f"{stats['singletons']} individual message(s) "
        f"from {stats['emails']} kept emails"
        + (f"; {withheld} estate-rescued message(s) withheld)" if withheld
           else ")")
    )
    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Rebuild email conversation pages from email_index.json"
    )
    parser.add_argument("case_id", help="Case ID, e.g. CASE_001")
    parser.add_argument(
        "--out", default=None,
        help="Output directory for the pages (default: /cases/CASE_ID/output/)"
    )
    parser.add_argument(
        "--audience", choices=list(AUDIENCES), default=FAMILY,
        help="Who the pages are for. 'family' (default) withholds "
             "estate-rescued mail; 'examiner' includes it."
    )
    args = parser.parse_args()
    generate(args.case_id, output_dir=Path(args.out) if args.out else None,
             audience=args.audience)


if __name__ == "__main__":
    main()
