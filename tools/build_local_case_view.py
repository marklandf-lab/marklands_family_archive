#!/usr/bin/env python3
"""Build a LOCAL browse view over a real Wyeast case.

Same navigation as the published prototype, driven by the case's own indexes
instead of invented records. Writes a single self-contained HTML file next to
the case data.

THE FILE THIS WRITES CONTAINS REAL CASE CONTENT — names, subjects, filenames,
phone numbers. It is written beside the case, never into a repo and never
published. Nothing here prints record content to stdout; only counts.

Usage:
    python3 build_local_case_view.py CASE_ID
    python3 build_local_case_view.py CASE_ID --cases-root /Volumes/WyeastUSB/cases

Cases root, in order:  --cases-root -> $WYEAST_CASES_ROOT -> ./cases -> ~/WyeastCases
    — the same order family_archive.sh uses, so anyone who has run the archive
    already knows how this behaves. No case path is written into this file.
"""
import argparse
import json
import io
import urllib.parse, os, re, sys, html
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))

_ap = argparse.ArgumentParser(description="Build a local browse view over one case.")
_ap.add_argument("case_id")
_ap.add_argument("--cases-root", default=None,
                 help="where the case trees live (default: $WYEAST_CASES_ROOT, "
                      "then ./cases beside this script, then ~/WyeastCases)")
_args = _ap.parse_args()

def _cases_root(explicit):
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    env = os.environ.get("WYEAST_CASES_ROOT")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    beside = os.path.join(HERE, "cases")
    if os.path.isdir(beside):
        return beside
    return os.path.expanduser("~/WyeastCases")

CASE = _args.case_id
CASES_ROOT = _cases_root(_args.cases_root)
# The view is written beside the case, which lives outside any checkout.
ROOT = os.path.join(CASES_ROOT, CASE)
if not os.path.isdir(ROOT):
    sys.exit(f"no case at {ROOT} — pass --cases-root or set WYEAST_CASES_ROOT")
MD = os.path.join(ROOT, "output", "metadata")
OUT = os.path.join(ROOT, f"wyeast-browse-{CASE}.html")
# The stylesheet sits beside this script. It was lifted once from the browse
# prototype, which carries real case names in its demo rows and is therefore not
# distributable; this file is pure CSS and carries nothing from any case.
STYLESHEET = os.path.join(HERE, "wyeast-view.css")

def load(name, default=None):
    p = os.path.join(MD, name)
    if not os.path.exists(p):
        return default
    with open(p) as f:
        return json.load(f)

# ── read the case ───────────────────────────────────────────────────────────
summary = load("case_summary.json", {})
dclass  = summary.get("document_classifications", [])
confirmed = load("vital_doc_confirmed.json", [])
rejections = load("vital_doc_rejections.json", {}) or {}
threads = (load("email_threads_index_examiner.json", {}) or {}).get("threads", [])
corr = load("correspondent_frequency.json", []) or []
inventory = load("account_inventory.json", {}) or {}
decisions = load("family_decisions.json", {}) or {}

# The pipeline runs TWO type taxonomies that were never reconciled: the
# classifier's financial/legal sub-categories, and the vital-document targets.
# Four names appear in both and disagree badly. Normalising both through one
# label map is what merges them into a single tree.
TYPE_LABEL = {
    # estate & legal
    "will_testament":"Will / testament","trust_instrument":"Trust instrument",
    "certificate_of_trust":"Certificate of trust","power_of_attorney":"Power of attorney",
    "beneficiary_designation":"Beneficiary designation","deed_title":"Property deed / title",
    "tod_pod_registration":"Transfer-on-death registration","letters_testamentary":"Letters testamentary",
    "letter_of_instruction":"Letter of instruction","court_filing":"Court filing",
    "litigation":"Litigation","other":"Other legal",
    # business
    "business_formation":"Business formation","business_operating_agreement":"Business operating agreement",
    "buy_sell_agreement":"Buy-sell agreement","estate_ein_confirmation":"Estate EIN confirmation",
    # money
    "receipts_bills_orders":"Receipts, bills & orders","retirement_investments":"Retirement & investments",
    "budgets":"Budgets & planning","banking":"Banking","paystubs":"Pay stubs",
    "financial_statement":"Financial statement","safe_deposit":"Safe deposit box",
    "credentials":"Account credentials","insurance":"Insurance","life_insurance":"Life insurance",
    # tax
    "taxes":"Tax records","tax_return":"Tax return","gift_tax_return":"Gift / estate tax return",
    "k1_estate_related":"Schedule K-1","cost_basis_valuation":"Cost-basis valuation",
    "foreign_asset_disclosure":"Foreign asset disclosure",
    # identity & titled property
    "birth_certificate":"Birth certificate","death_certificate":"Death certificate",
    "marriage_certificate":"Marriage certificate","passport_id":"Passport / ID",
    "military_record":"Military record","vehicle_title":"Vehicle title",
}
UNTYPED = "Type not determined"
def tlabel(t):
    return TYPE_LABEL.get(t, str(t).replace("_"," ").capitalize()) if t else UNTYPED

VITAL_LABEL = {
    "will_testament":"Will / testament","power_of_attorney":"Power of attorney",
    "deed_title":"Property deed / title","vehicle_title":"Vehicle title",
    "life_insurance":"Life insurance","birth_certificate":"Birth certificate",
    "death_certificate":"Death certificate","marriage_certificate":"Marriage certificate",
    "passport_id":"Passport / ID","financial_statement":"Financial statement",
    "tax_return":"Tax return","military_record":"Military record",
    "credentials":"Account credentials","safe_deposit":"Safe deposit box",
    "trust_instrument":"Trust agreement","certificate_of_trust":"Certificate of trust",
    "letters_testamentary":"Letters testamentary","beneficiary_designation":"Beneficiary designation",
    "tod_pod_registration":"Transfer-on-death registration","business_operating_agreement":"Business operating agreement",
    "buy_sell_agreement":"Buy-sell agreement","estate_ein_confirmation":"Estate EIN confirmation",
    "gift_tax_return":"Gift / estate tax return","foreign_asset_disclosure":"Foreign asset disclosure",
    "k1_estate_related":"Schedule K-1","cost_basis_valuation":"Cost-basis valuation",
    "letter_of_instruction":"Letter of instruction",
}
def vlabel(t): return VITAL_LABEL.get(t, t.replace("_", " ").capitalize())

CAT_LABEL = {
    "financial":"Financial","legal":"Legal","medical":"Medical",
    "personal_correspondence":"Personal correspondence","work_correspondence":"Work correspondence",
    "newsletters_lists":"Newsletters & lists","creative_writing":"Creative writing",
    "recipe":"Recipes","miscellaneous":"Miscellaneous",
}
def clabel(c): return CAT_LABEL.get(c, str(c).replace("_", " ").capitalize())

# ── documents ───────────────────────────────────────────────────────────────
# Most of "document_classifications" is email BODIES, not documents — the ratio
# is printed when this runs. The real documents are the ones the OCR/office
# stage actually read.
docs_raw = [d for d in dclass if d.get("method") != "email_body"]

vital_by_path = {}
for v in confirmed:
    vital_by_path.setdefault(v["path"], []).append(v["target"])

near_by_path = {}
for target, items in rejections.items():
    if not isinstance(items, list):
        continue
    for it in items:
        near_by_path.setdefault(it.get("path"), []).append((target, it.get("reason_code") or "unspecified"))

REASON_LABEL = {
    "llm_no":"the model said it is not this type",
    "no_text":"no readable text",
    "duplicate":"a duplicate of something already found",
    "unspecified":"reason not recorded",
}

def folder_of(path):
    parts = [p for p in str(path).split("/") if p]
    for anchor in ("extracted", "original_files"):
        if anchor in parts:
            i = parts.index(anchor)
            tail = parts[i+1:-1]
            return " / ".join(tail[:2]) if tail else anchor
    return parts[-2] if len(parts) > 1 else "—"

READ_AS = {"tesseract_pdf":"PDF (scanned)","xlsx":"Spreadsheet","xls":"Spreadsheet",
           "docx":"Word document","pptx":"Slides","csv":"Spreadsheet","rtf":"Text document"}

# Vitality is a PRIORITY on a document, not a kind of document. The kind is the
# type; these three say how urgently somebody should read it.
VITAL, NEAR, PLAIN = "Vital", "Near miss", "Not vital"

def priority_of(d):
    p = d.get("file")
    if p in vital_by_path: return VITAL
    if p in near_by_path:  return NEAR
    return PLAIN

def type_of(d):
    """One type per document, from whichever taxonomy actually named it."""
    p = d.get("file")
    if p in vital_by_path:
        return tlabel(sorted(vital_by_path[p])[0])
    if d.get("subcategory"):
        return tlabel(d.get("subcategory"))
    return UNTYPED

# Delivered copies are matched to index paths BY FILENAME, the way the archive
# server does it. A basename that collides is left unlinked rather than linked
# to a possibly-wrong file.
DELIVERED = {}
_dup = set()
_docroot = os.path.join(ROOT, "output", "documents")
for _r, _d, _fs in os.walk(_docroot):
    for _f in _fs:
        rel = os.path.relpath(os.path.join(_r, _f), ROOT)
        if _f in DELIVERED:
            _dup.add(_f)
        else:
            DELIVERED[_f] = rel
for _f in _dup:
    DELIVERED.pop(_f, None)

RECORDED_ROOT = "/data/cases/" + CASE
def urlpath(rel):
    """A relative path the browser can fetch. 163 delivered files have a '#' in
    the name, which a browser reads as the start of a fragment."""
    return urllib.parse.quote(str(rel or ""))

def rebase(p):
    p = str(p or "")
    return p.replace(RECORDED_ROOT, ROOT, 1) if p.startswith(RECORDED_ROOT) else p

# Two sidecars per document, and the view has never been used. The .txt is the
# flat text; the .view.json beside it is the structured extraction — headings,
# paragraphs and real tables — that the pipeline already wrote for every office
# document it read: spreadsheets, Word files, decks, RTF and CSV. Handing the
# browser a raw .xlsx only offers a download; this renders.
ocr_sidecar, view_sidecar = {}, {}
for _r in (load("ocr_index.json") or []):
    sc = rebase(_r.get("sidecar"))
    if sc and os.path.exists(sc):
        ocr_sidecar[_r.get("file")] = os.path.relpath(sc, ROOT)
        if sc.endswith(".txt") and os.path.exists(sc[:-4] + ".view.json"):
            view_sidecar[_r.get("file")] = os.path.relpath(sc[:-4] + ".view.json", ROOT)

# where the same name exists in both taxonomies, do they pick the same documents?
_subnames = {d.get("subcategory") for d in docs_raw if d.get("subcategory")}
CLASH = []
for _n in sorted(_subnames & set(rejections.keys())):
    _by_sub = {d["file"] for d in docs_raw if d.get("subcategory") == _n}
    _by_vit = {p for p, ts in vital_by_path.items() if _n in ts}
    CLASH.append([tlabel(_n), len(_by_sub), len(_by_vit), len(_by_sub & _by_vit)])

folders = Counter(folder_of(d.get("file")) for d in docs_raw)
top_folders = {f for f, _ in folders.most_common(30)}

DOCS = []
for d in docs_raw:
    p = d.get("file")
    vt = vital_by_path.get(p)
    nm = near_by_path.get(p)
    fol = folder_of(p)
    DOCS.append({
        "n": os.path.basename(str(p)),
        "cat": clabel(d.get("category")),
        "sub": (d.get("subcategory") or "").replace("_", " ").capitalize() or "—",
        "pri": priority_of(d),
        "type": type_of(d),
        "tested": vlabel(nm[0][0]) if (nm and not vt) else "",
        "why": REASON_LABEL.get(nm[0][1], nm[0][1]) if nm else "",
        "read": READ_AS.get(d.get("method"), d.get("method") or "—"),
        "fol": fol if fol in top_folders else "elsewhere",
        "sig": d.get("significance"),
        "sum": (d.get("summary") or "")[:300],
        "src": urlpath(DELIVERED.get(os.path.basename(str(p)), "")),
        "txt": urlpath(ocr_sidecar.get(p, "")),
        "view": urlpath(view_sidecar.get(p, "")),
    })

# ── mail ────────────────────────────────────────────────────────────────────
inst_domains = {d.lower() for d in inventory.keys()}
by_addr = {str(c.get("address","")).lower(): c for c in corr if c.get("address")}

ADDR = re.compile(r"<([^>]+)>")
def addr_of(participant):
    """participants are 'Name <addr@host>' — the bare string does not join."""
    m = ADDR.search(str(participant))
    return (m.group(1) if m else str(participant)).strip().lower()

def name_of(participant):
    s = str(participant)
    m = ADDR.search(s)
    return (s[:m.start()].strip().strip('"') if m and s[:m.start()].strip() else addr_of(s))

# The pipeline never recorded whose mailbox this is (owner_email_addresses empty,
# owner_source "none"), so sent/received attribution is unusable and bidirectional
# is false for every correspondent. Infer the owner the way the fork's Emails page
# does: the address present in the largest share of threads.
_seen = Counter()
for t in threads:
    for a in {addr_of(p) for p in (t.get("participants") or [])}:
        _seen[a] += 1
OWNERS = {a for a, n in _seen.most_common(3) if threads and n / len(threads) > 0.25}

AUTOMATED = re.compile(r"^(no-?reply|donotreply|notifications?|alerts?|billing|support|info|mailer|news|updates?|team|hello)\b")

def counterpart(t):
    ps = t.get("participants") or []
    for p in ps:
        if addr_of(p) not in OWNERS:
            return p
    return ps[0] if ps else ""

def rel_of(t):
    p = counterpart(t)
    a = addr_of(p)
    dom = a.split("@")[-1]
    local = a.split("@")[0]
    if dom in inst_domains or AUTOMATED.match(local):
        return "Institutions & automated"
    c = by_addr.get(a) or {}
    if (c.get("total") or 0) >= 20:
        return "Frequent correspondents"
    return "Occasional senders"

def display_of(t):
    p = counterpart(t)
    c = by_addr.get(addr_of(p)) or {}
    return c.get("contact_name") or c.get("display_name") or name_of(p) or "—"

# Which account did it arrive in? Never recorded, but 94% of threads contain
# exactly one owner-ish address, and that names the mailbox.
_owner_dom = {a: a.split("@")[-1] for a in OWNERS}
def mailbox_of(t):
    hits = {addr_of(p) for p in (t.get("participants") or [])} & OWNERS
    if len(hits) == 1:
        return _owner_dom[next(iter(hits))]
    return "Could not tell" if not hits else "In more than one account"

# A vital item that IS an email can be joined to its conversation through the
# thread's own file list — the same join the archive uses for vital-doc links.
_thread_of_file = {}
for _t in threads:
    for _f in (_t.get("files") or []):
        _thread_of_file[_f] = _t.get("thread_id")
_vital_in_thread = {}
for _v in confirmed:
    _tid = _thread_of_file.get(_v["path"])
    if _tid:
        _vital_in_thread.setdefault(_tid, set()).add(_v["target"])
VITAL_MAIL_LINKED = len({p for p in (v["path"] for v in confirmed) if p in _thread_of_file})
VITAL_DOCS_UNLINKED = len(confirmed) - VITAL_MAIL_LINKED

MAIL = []
for t in threads:
    cats = t.get("categories") or []
    MAIL.append({
        "n": (t.get("subject") or "(no subject)")[:160],
        "from": str(display_of(t))[:60],
        "rel": rel_of(t),
        "cat": clabel(cats[0]) if cats else "Miscellaneous",
        "sub": "—",
        "msgs": t.get("message_count") or 0,
        "first": (t.get("date_first") or "")[:10],
        "last": (t.get("date_last") or "")[:10],
        "sig": t.get("significance"),
        "src": ("output/email_threads_examiner/" + os.path.basename(str(t.get("page"))))
               if t.get("page") else "",
        "box": mailbox_of(t),
        "who": str(display_of(t))[:60],
        "addr": addr_of(counterpart(t)),
        "dom": addr_of(counterpart(t)).split("@")[-1],
        "vital": sorted(vlabel(x) for x in _vital_in_thread.get(t.get("thread_id"), ())),
        "ts": (t.get("date_last") or "")[:10],
    })

# ── the page ────────────────────────────────────────────────────────────────
# ── the estate axis: sixteen kinds of estate business, over mail ────────────
# estate_materiality scored every item in the case for how it bears on settling
# an estate. The archive has never shown it. Read from the REPORT, not the
# index: the index is ~97MB and carries the below-threshold tail, while the
# report's `highlights` are the band the pipeline itself thinks worth surfacing.
# Items are per message and a row here is a conversation, so a thread takes the
# union of its messages' categories — joined on the `files` list the thread
# already carries. Same source and same join as the fork's estate_relevance_map
# in tools/_archive_data.py; do not invent a second one.
EST_LABEL = {
    "banking_cash_debt": "Banking, cash & debt",
    "community_charitable": "Community & charitable",
    "critical_credentials": "Critical credentials",
    "digital_account_inventory": "Digital accounts",
    "employment_hr_business": "Employment & business",
    "estate_legal": "Estate & legal",
    "financial_or_insurance": "Financial or insurance",
    "identity_government": "Identity & government",
    "insurance": "Insurance",
    "investments_retirement": "Investments & retirement",
    "medical_health": "Medical & health",
    "personal_correspondence_sentimental": "Personal & sentimental",
    "real_estate_property": "Real estate & property",
    "subscriptions_billing_utilities": "Subscriptions, billing & utilities",
    "tax_records": "Tax records",
    "travel_reservations": "Travel & reservations",
}
def elabel(c): return EST_LABEL.get(c, str(c).replace("_", " ").capitalize())

_erep = load("estate_materiality_report.json", {}) or {}
_est_by_file, _escore_by_file = {}, {}
for _h in (_erep.get("highlights") or []):
    if not isinstance(_h, dict) or _h.get("source") != "email":
        continue
    _f = _h.get("file")
    if _f:
        _est_by_file[_f] = _h.get("matched_categories") or []
        _escore_by_file[_f] = _h.get("score") or 0
EST_BY_THREAD, ESCORE_BY_THREAD = {}, {}
for _t in threads:
    _got, _best = set(), 0
    for _f in (_t.get("files") or []):
        _got.update(_est_by_file.get(_f, ()))
        if _f in _escore_by_file:
            _best = max(_best, _escore_by_file[_f])
    if _got:
        _tid = _t.get("thread_id")
        EST_BY_THREAD[_tid] = sorted(elabel(x) for x in _got)
        ESCORE_BY_THREAD[_tid] = _best
for _m, _t in zip(MAIL, threads):
    _tid = _t.get("thread_id")
    _m["est"] = EST_BY_THREAD.get(_tid, [])
    _m["escore"] = ESCORE_BY_THREAD.get(_tid, 0)

# ── messages: real iMessage conversations, not a stand-in ───────────────────
# conversation_index.json is the message side of the case. Direction counts are
# present but worthless for the same reason as mail: owner_handles is empty, so
# every conversation reads as all-received. The view says so rather than showing
# a "sent" column of zeroes.
TRIAGE_LABEL = {"keep": "Kept", "platform": "Platform noise", "discard": "Discarded"}
MSGS = []
for _c in (load("conversation_index.json", []) or []):
    _cid = str(_c.get("conversation_id") or "")
    _nm = _c.get("display_name") or ", ".join(_c.get("participants") or []) or "—"
    _span = _c.get("span") or ["", ""]
    MSGS.append({
        "n": str(_nm)[:160],
        "who": str(_nm)[:60],
        "cat": "Not categorised",
        "sub": "—",
        "msgs": _c.get("message_count") or 0,
        "att": _c.get("attachment_count") or 0,
        "plat": _c.get("platform") or "—",
        "triage": TRIAGE_LABEL.get(_c.get("triage_verdict"), str(_c.get("triage_verdict") or "—")),
        "resc": bool(_c.get("estate_rescued")),
        "first": str(_span[0] or "")[:10],
        "last": str(_span[1] or "")[:10],
        "ts": str(_span[1] or "")[:10],
        "sig": None,
        "est": [], "escore": 0,
        # macOS stores the ':' of the conversation id as U+F022 in the filename,
        # and the URL has to carry it percent-encoded or the fetch 404s.
        "src": ("output/metadata/messages/"
                + urllib.parse.quote(_cid.replace(":", "\uf022") + ".json")) if _cid else "",
    })

# ── Reports and Online Accounts, built by the archive's OWN code ────────────
# Both come out of ~/mac_family_archive rather than being re-derived here. That
# is deliberate: the estate report re-implemented from raw metadata came out at
# 11 types present against the app's 8, because ArchiveCase installs a path
# rebaser before reading any index and the decisions are keyed by path. Anything
# short of the app's own loader silently disagrees with the app. If the fork is
# not on this machine both sections are omitted and the rail says why.
REPORTS, ACCOUNTS, APP_ERR = {}, {}, ""
# When this script lives in the archive's own tools/ directory, that checkout is
# the one to use; otherwise fall back to a sibling clone in the home directory.
_FORK = os.path.dirname(HERE) if os.path.exists(os.path.join(HERE, "family_archive.py")) \
        else os.path.expanduser("~/mac_family_archive")
if os.path.isdir(os.path.join(_FORK, "tools")):
    _buf = io.StringIO()
    try:
        sys.path.insert(0, os.path.join(_FORK, "tools"))
        import contextlib
        with contextlib.redirect_stdout(_buf):
            import family_archive as _FA
            _case = _FA.ArchiveCase(
                _FA.CasePaths.from_case_id(CASE, os.path.dirname(ROOT)),
                "examiner", _FA.load_pipeline_config())
            REPORTS = {_r: _case.api_section("reports", {"r": _r})
                       for _r in ("estate", "family", "pipeline")}
            REPORTS["index"] = _case.api_section("reports", {})
            ACCOUNTS = _case.api_section("accounts")
    except Exception as _e:
        REPORTS, ACCOUNTS = {}, {}
        APP_ERR = "%s: %s" % (type(_e).__name__, _e)
else:
    APP_ERR = "~/mac_family_archive is not on this machine"

with open(STYLESHEET) as f:
    CSS = f.read()

CATS = defaultdict(set)
for r in DOCS: CATS[r["cat"]].add(r["sub"])
for r in MAIL: CATS[r["cat"]].add(r["sub"])
CATS = [{"n": k, "subs": sorted(v)} for k, v in sorted(CATS.items(), key=lambda x: -sum(1 for r in DOCS if r["cat"] == x[0]))]

DATA = {
    "case": CASE,
    "generated": summary.get("generated_at", ""),
    "docs": DOCS, "mail": MAIL, "msg": MSGS, "cats": CATS,
    "pris": [VITAL, NEAR, PLAIN],
    "typesByCat": {c: sorted({r["type"] for r in DOCS if r["cat"] == c},
                             key=lambda t: (t == UNTYPED, t))
                   for c in {r["cat"] for r in DOCS}},
    "clash": CLASH,
    "rels": ["Institutions & automated", "Frequent correspondents", "Occasional senders"],
    "boxes": [b for b,_ in Counter(m["box"] for m in MAIL).most_common()],
    "senders": [[a, n] for a, n in Counter(m["who"] for m in MAIL).most_common(40)],
    "estCats": [[e, n] for e, n in Counter(e for m in MAIL for e in m["est"]).most_common()],
    "estThreads": len(EST_BY_THREAD),
    "triages": [t for t, _ in Counter(m["triage"] for m in MSGS).most_common()],
    "reports": REPORTS, "accounts": ACCOUNTS, "appErr": APP_ERR,
    "boxKnownPct": round(100.0 * sum(1 for m in MAIL
                     if m["box"] not in ("Could not tell", "In more than one account"))
                     / max(len(MAIL), 1)),
    "correspondents": len(corr),
    "sendersTotal": len({m["who"] for m in MAIL}),
    "vitalMailLinked": VITAL_MAIL_LINKED,
    "vitalDocsUnlinked": VITAL_DOCS_UNLINKED,
    "folders": sorted({r["fol"] for r in DOCS}),
    "reads": sorted({r["read"] for r in DOCS}),
    "counts": {
        "classified": len(dclass), "emailBodies": len(dclass) - len(docs_raw),
        "docs": len(DOCS), "mail": len(MAIL),
        "msgConvos": len(MSGS), "msgMessages": sum(m["msgs"] for m in MSGS),
        "messages": sum(m["msgs"] for m in MAIL),
        "confirmed": len(confirmed), "rejected": sum(len(v) for v in rejections.values() if isinstance(v, list)),
        "decisions": sum(len(v) for v in decisions.values() if hasattr(v, "__len__")),
        "docsViewable": sum(1 for r in DOCS if r["src"] or r["txt"]),
        "mailViewable": sum(1 for m in MAIL if m["src"]),
        "vitalInMail": len(confirmed) - sum(1 for r in DOCS if r["pri"] == VITAL),
    },
}

page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wyeast — {CASE} — local browse</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
{CSS}
.demo{{background:var(--oxide-soft);border-bottom:1px solid var(--oxide)}}
.demo .tag{{color:var(--oxide)}}
.doc .hdr,.doc .row{{grid-template-columns:minmax(0,1fr) 128px 150px 120px 130px}}
.mail .hdr,.mail .row{{grid-template-columns:minmax(0,1fr) 150px 130px 60px 100px}}
.msg .hdr,.msg .row{{grid-template-columns:minmax(0,1fr) 130px 70px 100px 100px}}
.mix .hdr,.mix .row{{grid-template-columns:minmax(0,1fr) 150px 210px 110px}}
.est{{display:inline-block;font-size:.63rem;color:var(--fir);background:var(--fir-soft);
  border-radius:2px;padding:.02rem .26rem;margin:0 .2rem .1rem 0;white-space:nowrap}}
.missing{{background:var(--brass-soft);border-left:2px solid var(--brass);border-radius:0 2px 2px 0;
  padding:.4rem .5rem;margin:.3rem .35rem;font-size:.72rem;color:var(--ink-2);line-height:1.35}}
.row{{cursor:pointer}}
[hidden]{{display:none!important}}
.pane{{flex:1;min-height:0;overflow-y:auto;padding:1.1rem 1.4rem 3rem;background:var(--paper)}}
.pane-t{{font-family:var(--serif);font-size:1.5rem;font-weight:600;margin:0 0 .2rem}}
.pane-h{{font-family:var(--serif);font-size:1.05rem;font-weight:600;margin:1.5rem 0 .3rem}}
.pane-sub{{color:var(--ink-2);max-width:72ch;margin:.2rem 0 .6rem;line-height:1.5}}
.pane-note{{background:var(--brass-soft);border-left:2px solid var(--brass);padding:.45rem .6rem;
  margin:.5rem 0;font-size:.8rem;color:var(--ink-2);max-width:78ch;line-height:1.45}}
.pane-back{{font-size:.78rem;color:var(--fir);cursor:pointer;background:none;border:0;padding:0;margin-bottom:.6rem}}
.stats{{display:flex;gap:1.6rem;margin:.8rem 0 .4rem;flex-wrap:wrap}}
.stat b{{display:block;font-family:var(--serif);font-size:1.7rem;font-weight:600;line-height:1}}
.stat span{{font-size:.74rem;color:var(--ink-3)}}
.pane table{{border-collapse:collapse;font-size:.79rem;margin:.4rem 0 .2rem;width:100%;max-width:64rem}}
.pane th{{text-align:left;font-family:var(--mono);font-size:.63rem;letter-spacing:.1em;
  text-transform:uppercase;color:var(--ink-3);border-bottom:1px solid var(--rule);padding:.3rem .5rem .25rem}}
.pane td{{border-bottom:1px solid var(--rule);padding:.28rem .5rem;vertical-align:top}}
.pane td.num,.pane th.num{{text-align:right;font-variant-numeric:tabular-nums;font-family:var(--mono);font-size:.75rem}}
.pane tr.click{{cursor:pointer}}
.pane tr.click:hover td{{background:var(--panel)}}
.tag-inv{{font-family:var(--mono);font-size:.58rem;letter-spacing:.08em;color:var(--brass);
  border:1px solid var(--brass);border-radius:2px;padding:0 .2rem;margin-left:.35rem;vertical-align:.08em}}
.lim{{margin:.3rem 0 .3rem 1rem;color:var(--ink-2);font-size:.81rem;line-height:1.5}}
.rcard{{display:block;width:100%;text-align:left;border:1px solid var(--rule);border-radius:3px;
  padding:.7rem .85rem;margin:.5rem 0;background:var(--shell);cursor:pointer}}
.rcard:hover{{border-color:var(--fir)}}
.rcard b{{font-family:var(--serif);font-size:1rem;display:block;margin-bottom:.15rem}}
.rcard span{{color:var(--ink-2);font-size:.81rem;line-height:1.45}}
.nav .ct.star{{color:var(--brass);font-weight:600;font-size:.8rem}}
.rail .note{{font-size:.68rem;color:var(--ink-3);line-height:1.4;padding:0 .35rem}}
.rail .note b{{color:var(--ink-2)}}
.nav.sect{{font-family:var(--mono);font-size:.63rem;letter-spacing:.13em;text-transform:uppercase;
  color:var(--ink-3);margin:.75rem 0 .1rem}}
.nav.sect .tw{{font-size:.58rem;width:.7rem;flex:none}}
.notes{{margin-top:1rem;padding-top:.55rem;border-top:1px solid var(--rule);
  display:flex;flex-direction:column;gap:.45rem}}
.notes .mk{{color:var(--brass);font-weight:600;font-family:var(--mono);font-size:.72rem}}
.more{{padding:.5rem .35rem;font-family:var(--mono);font-size:.73rem;color:var(--ink-3)}}
.gh{{width:100%;font:inherit;color:inherit;text-align:left;cursor:pointer;
  border:0;border-bottom:1px solid var(--rule);border-radius:0}}
.gh:hover .n{{color:var(--fir)}}
.gh .car{{display:inline-block;flex:none;width:.7rem;font-size:.6rem;color:var(--ink-3);
  transition:transform .12s ease}}
.gh.folded .car{{transform:rotate(-90deg)}}
.gh.folded{{margin-bottom:0}}
.fold{{font-family:var(--mono);font-size:.7rem;background:var(--paper);color:var(--ink-3);
  border:1px solid var(--rule);border-radius:2px;padding:.14rem .42rem;cursor:pointer}}
.fold:hover{{border-color:var(--fir);color:var(--fir)}}
.sentrow{{display:flex;align-items:baseline;gap:.55rem;flex-wrap:wrap}}
.sentrow .sentence{{flex:none}}
.scrim{{position:fixed;inset:0;background:rgba(19,26,21,.55);z-index:40;display:flex;
  align-items:center;justify-content:center;padding:clamp(.6rem,2vw,2rem)}}
.modal{{background:var(--shell);border-radius:4px;width:min(1120px,94vw);height:min(880px,90vh);
  display:flex;flex-direction:column;box-shadow:0 12px 48px rgba(0,0,0,.3);overflow:hidden}}
.mhead{{display:flex;align-items:baseline;gap:.7rem;padding:.6rem .85rem;border-bottom:1px solid var(--rule);flex:none}}
.mtitle{{font-family:var(--serif);font-size:1.02rem;font-weight:600;flex:1;min-width:0;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.mmeta{{font-family:var(--mono);font-size:.68rem;color:var(--ink-3);white-space:nowrap}}
.mbtn{{font-family:var(--mono);font-size:.72rem;border:1px solid var(--rule);border-radius:2px;
  padding:.18rem .5rem;color:var(--ink-2);background:var(--paper);text-decoration:none}}
.mbtn:hover{{border-color:var(--fir);color:var(--fir)}}
.mbody{{flex:1;min-height:0;background:var(--panel);position:relative}}
.mbody iframe{{width:100%;height:100%;border:0;background:#fff}}
.docview{{height:100%;overflow:auto;background:var(--paper);padding:1rem 1.2rem;
  font-size:.86rem;line-height:1.5;color:var(--ink)}}
.dv-sect{{font-family:var(--mono);font-size:.66rem;letter-spacing:.12em;text-transform:uppercase;
  color:var(--ink-3);margin:1.2rem 0 .4rem;padding-bottom:.2rem;border-bottom:1px solid var(--rule)}}
.dv-sect:first-child{{margin-top:0}}
.dv-h{{font-family:var(--serif);margin:.9rem 0 .3rem;line-height:1.25}}
.dv-p{{margin:.45rem 0;white-space:pre-wrap}}
.dv-tw{{overflow-x:auto;margin:.5rem 0}}
.dv-t{{border-collapse:collapse;font-size:.75rem;font-variant-numeric:tabular-nums}}
.dv-t td,.dv-t th{{border:1px solid var(--rule);padding:.15rem .4rem;text-align:left;
  white-space:nowrap;max-width:24rem;overflow:hidden;text-overflow:ellipsis}}
.dv-t th{{background:var(--panel);font-weight:600}}
.dv-more,.dv-none{{font-family:var(--mono);font-size:.7rem;color:var(--ink-3);padding:.3rem .1rem}}
.dv-msg{{margin:.6rem 0;padding-left:.6rem;border-left:2px solid var(--rule)}}
.dv-mh{{display:flex;gap:.5rem;align-items:baseline;font-family:var(--mono);font-size:.68rem;color:var(--ink-3)}}
.dv-mh b{{color:var(--ink-2);font-weight:500}}
.dv-mt{{white-space:pre-wrap;margin-top:.1rem}}
.dv-att{{font-family:var(--mono);font-size:.67rem;color:var(--ink-3);margin-top:.2rem}}
.mnone{{padding:1.4rem;color:var(--ink-2);font-size:.88rem;max-width:60ch;line-height:1.5}}
.mnone code{{font-family:var(--mono);font-size:.8rem;background:var(--paper);padding:.05rem .3rem;border-radius:2px}}
.mfoot{{padding:.4rem .85rem;border-top:1px solid var(--rule);font-family:var(--mono);
  font-size:.67rem;color:var(--ink-3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:none}}
</style></head><body>

<div class="demo">
  <span class="tag">Local · real case data</span>
  <span>Case <b>{html.escape(CASE)}</b> · {DATA['counts']['docs']:,} documents · {DATA['counts']['mail']:,} conversations · {DATA['counts']['messages']:,} messages</span>
  <span class="sep">·</span>
  <span>this file contains real case content — keep it off shared drives and out of any repo</span>
</div>

<div class="top">
  <span class="logo">WYEAST</span>
  <span class="seg" id="mode" title="Click to combine — any one, two or all three"><button type="button" data-m="doc" aria-pressed="true">Documents</button><button type="button" data-m="mail" aria-pressed="false">Mail</button><button type="button" data-m="msg" aria-pressed="false">Messages</button></span>
  <span class="search">Search is not wired up in this local view</span>
  <span class="case">{html.escape(CASE)} · generated {html.escape(str(DATA['generated'])[:10])}</span>
</div>

<div class="frame" id="frame">
  <nav class="rail" id="rail"></nav>
  <div class="main">
    <div class="bar">
      <div class="sentrow">
        <div class="sentence" id="sentence"></div>
        <span id="chips" style="display:flex;gap:.3rem;flex-wrap:wrap"></span>
        <button class="clear" id="clear" type="button" hidden>clear all</button>
      </div>
      <div class="barrow">
        <span class="groupby">group by <select id="groupby"></select></span>
        <span class="groupby">sort <select id="sortby"></select></span>
        <button class="fold" id="foldall" type="button" hidden>collapse all</button>
        <span class="spacer"></span>
        <span class="tally" id="tally"></span>
      </div>
    </div>
    <div class="list" id="list"></div>
    <div class="pane" id="pane" hidden></div>
  </div>
  <aside class="detailpane" id="detail" hidden></aside>
</div>

<script id="case-data" type="application/json">{json.dumps(DATA, ensure_ascii=False)}</script>
<script>
(function(){{
  "use strict";
  var D = JSON.parse(document.getElementById("case-data").textContent);
  var DOCS = D.docs, MAIL = D.mail, MSGS = D.msg || [];
  DOCS.forEach(function(r,i){{ r.id="d"+i; r.kind="doc"; }});
  MAIL.forEach(function(r,i){{ r.id="m"+i; r.kind="mail"; }});
  MSGS.forEach(function(r,i){{ r.id="s"+i; r.kind="msg"; }});
  var CAP = 250;                       // rows drawn per group on a real corpus
  var FOLD_AT = 25;                    // a group bigger than this opens collapsed
  var FOLD_MANY = 12;                  // past this many groups, all of them open collapsed
  var fold = {{}};                       // "mode|group|key" -> true shut, false open, absent = default
  var curKeys = [];                    // the group keys on screen, for collapse/expand all
  function foldKey(k){{ return mode+"|"+group+"|"+k; }}

  var VITAL="Vital", NEAR="Near miss";
  var PRI_CLASS = {{"Vital":"b-vital","Near miss":"b-near","Not vital":"b-rec"}};
  var PRI_HELP = {{"Vital":"one of the 27 estate document types, confirmed",
    "Near miss":"retrieved as one of those types and then rejected — a draft, a copy, or unreadable",
    "Not vital":"everything else the scan read"}};

  // Documents, mail and messages are three kinds you combine, not three modes
  // you switch between. `mode` is derived: the one kind on, or "mix" for several.
  var KIND_ORDER=["doc","mail","msg"];
  var K={{doc:true,mail:false,msg:false}};
  function kindsOn(){{ return KIND_ORDER.filter(function(k){{ return K[k]; }}); }}
  function syncMode(){{ var a=kindsOn(); mode = a.length===1 ? a[0] : "mix"; }}

  var mode="doc", group="cat", sel=null, open={{}}, railOpen={{}};
  var pane="list", report=null;   // "list" | "reports" | "accounts"
  var F = {{ pri:null, type:null, cat:null, sub:null, read:null, rel:null, box:null, who:null, vmail:null, est:null, triage:null, dom:null }};

  function pool(){{
    var out=[];
    if (K.doc) out=out.concat(DOCS);
    if (K.mail) out=out.concat(MAIL);
    if (K.msg) out=out.concat(MSGS);
    return out;
  }}
  function matches(r, skip){{
    if (F.pri  && skip!=="pri"  && r.pri!==F.pri) return false;
    if (F.type && skip!=="type" && r.type!==F.type) return false;
    if (F.cat  && skip!=="cat"  && r.cat!==F.cat) return false;
    if (F.sub  && skip!=="cat"  && r.sub!==F.sub) return false;
    if (F.read && skip!=="read" && r.read!==F.read) return false;
    if (F.rel  && skip!=="rel"  && r.rel!==F.rel) return false;
    if (F.box  && skip!=="box"  && r.box!==F.box) return false;
    if (F.who  && skip!=="who"  && r.who!==F.who) return false;
    if (F.vmail&& skip!=="vmail"&& !(r.vital&&r.vital.length)) return false;
    if (F.est  && skip!=="est"  && !(r.est && r.est.indexOf(F.est)>=0)) return false;
    // A service is a registrable root; a thread's domain may be a subdomain of it.
    if (F.dom  && skip!=="dom"  && !(r.dom===F.dom || String(r.dom||"").slice(-(F.dom.length+1))==="."+F.dom)) return false;
    if (F.triage&&skip!=="triage"&& r.triage!==F.triage) return false;
    return true;
  }}
  function rows(){{ return pool().filter(function(r){{ return matches(r); }}); }}
  function countIf(skip, test){{ return pool().filter(function(r){{ return matches(r,skip)&&test(r); }}).length; }}
  function set(k,v){{
    pane="list"; report=null;
    if (k==="cat"){{ if (F.cat===v&&!F.sub) F.cat=null; else {{F.cat=v;F.sub=null;}} open[v]=F.cat===v; }}
    else if (k==="sub"){{ if (F.sub===v.sub) F.sub=null; else {{F.cat=v.cat;F.sub=v.sub;open[v.cat]=true;}} }}
    else F[k] = (F[k]===v)?null:v;
    sel=null; draw();
  }}

  function navBtn(o){{
    var b=document.createElement("button"); b.type="button";
    b.className="nav"+(o.on?" on":"")+(o.depth?" d"+o.depth:"")+(o.head?" head":"");
    if (o.count===0 && !o.on) b.disabled=true;
    if (o.help) b.title=o.help;
    if (o.twisty!==undefined){{ var t=document.createElement("span"); t.className="tw"; t.textContent=o.twisty?"▾":"▸"; b.appendChild(t); }}
    var l=document.createElement("span"); l.className="lbl"; l.textContent=o.label;
    var c=document.createElement("span"); c.className="ct"; c.textContent=o.count.toLocaleString();
    b.appendChild(l); b.appendChild(c);
    b.addEventListener("click", o.fn); return b;
  }}
  function head(t){{ var h=document.createElement("h4"); h.textContent=t; return h; }}

  // Facets the case cannot actually supply, drawn so a demo can show where the
  // control would sit. Deliberately breaks the "no dead ends" rule, so each one
  // carries an asterisk and each section carries the footnote that explains it.
  // Set DEMO_FACETS to false to take all of them out again.
  var DEMO_FACETS = true;
  function ghostBtn(label, mark, why){{
    var b=document.createElement("button"); b.type="button"; b.className="nav";
    b.title = why;
    var l=document.createElement("span"); l.className="lbl"; l.textContent=label;
    var c=document.createElement("span"); c.className="ct star"; c.textContent=mark;
    b.appendChild(l); b.appendChild(c);
    return b;
  }}

  // A rail heading that nests what follows it. The mark, when there is one, sits
  // on the heading too, so a collapsed section still says it is a placeholder.
  function section(title, key, mark, forceOpen){{
    var isOpen = railOpen[key]!==undefined ? railOpen[key] : !!forceOpen;
    var b=document.createElement("button"); b.type="button"; b.className="nav sect";
    var t=document.createElement("span"); t.className="tw"; t.textContent=isOpen?"▾":"▸";
    var l=document.createElement("span"); l.className="lbl"; l.textContent=title;
    b.appendChild(t); b.appendChild(l);
    if (mark){{ var m=document.createElement("span"); m.className="ct star"; m.textContent=mark; b.appendChild(m); }}
    b.addEventListener("click", function(){{ railOpen[key]=!isOpen; draw(); }});
    return {{ btn:b, open:isOpen }};
  }}
  function missing(t){{ var d=document.createElement("div"); d.className="missing"; d.innerHTML=t; return d; }}

  function drawRail(){{
    var rail=document.getElementById("rail"); rail.textContent="";

    rail.appendChild(head("This case"));
    [["list","Browse the records"],["reports","Reports"],["accounts","Online accounts"]]
      .forEach(function(v){{
        var b=document.createElement("button"); b.type="button";
        b.className="nav"+(pane===v[0]?" on":"");
        b.appendChild(el("span","lbl",v[1]));
        b.addEventListener("click", function(){{
          pane=v[0]; if (v[0]!=="reports") report=null; sel=null; draw(); }});
        rail.appendChild(b);
      }});
    // The facets below filter the record list; they mean nothing over a report.
    if (pane!=="list") return;

    if (K.doc){{
      rail.appendChild(head("How urgent is it"));
      D.pris.forEach(function(pn){{
        rail.appendChild(navBtn({{ label:pn, help:PRI_HELP[pn],
          count: countIf("pri", function(r){{ return r.pri===pn; }}),
          on:F.pri===pn, fn:function(){{ set("pri",pn); }} }}));
      }});
    }}

    if (K.mail){{
      rail.appendChild(head("Which account it arrived in"));
      D.boxes.forEach(function(bx){{
        rail.appendChild(navBtn({{ label:bx, count: countIf("box", function(r){{ return r.box===bx; }}),
          on:F.box===bx, fn:function(){{ set("box",bx); }} }}));
      }});
      var bn=document.createElement("div"); bn.className="missing";
      bn.innerHTML="<b>Derived, not recorded.</b> No thread says which mailbox it came from. This infers it from which of the account holder's own addresses appears in the thread — it lands for about "+D.boxKnownPct+"% of them and says so when it cannot.";
      rail.appendChild(bn);

      rail.appendChild(head("Carries a vital document"));
      rail.appendChild(navBtn({{ label:"Conversations holding a vital item",
        count: countIf("vmail", function(r){{ return r.vital && r.vital.length; }}),
        on:!!F.vmail, fn:function(){{ set("vmail", F.vmail?null:1); }} }}));
      var vn=document.createElement("div"); vn.className="missing";
      vn.innerHTML="<b>Only half the link exists.</b> "+D.vitalMailLinked+" vital items ARE email messages, so they join to their conversation. The other "+D.vitalDocsUnlinked+" are documents — and nothing records which email carried them, so a tax return cannot be traced back to the thread it arrived in.";
      rail.appendChild(vn);

      rail.appendChild(head("Who wrote"));
      rail.appendChild(missing("<b>Partly computable.</b> The pipeline never recorded which addresses belong to the account holder, so <i>sent</i> versus <i>received</i> is empty for all "+D.correspondents.toLocaleString()+" correspondents — “a contact who wrote back” cannot be separated from “a sender who never heard from them”. These three buckets are the most the data supports."));
      D.rels.forEach(function(rl){{
        rail.appendChild(navBtn({{ label:rl, count: countIf("rel", function(r){{ return r.rel===rl; }}),
          on:F.rel===rl, fn:function(){{ set("rel",rl); }} }}));
      }});
    }}

    if (K.mail){{
      rail.appendChild(head("Sender"));
      D.senders.forEach(function(sd){{
        var n=countIf("who", function(r){{ return r.who===sd[0]; }});
        if (!n && F.who!==sd[0]) return;
        rail.appendChild(navBtn({{ label:sd[0], count:n, on:F.who===sd[0],
          fn:function(){{ set("who",sd[0]); }} }}));
      }});
      var sn=document.createElement("div"); sn.className="missing";
      sn.innerHTML="The "+D.senders.length+" most frequent of <b>"+D.sendersTotal.toLocaleString()+"</b> senders. Use the sender column or a group-by to reach the rest.";
      rail.appendChild(sn);
    }}

    if (K.mail){{
      rail.appendChild(head("What estate business it touches"));
      rail.appendChild(missing("<b>Computed months ago, never shown.</b> The pipeline scored every message for how it bears on settling an estate and sorted the hits into sixteen kinds of estate business. <b>"+D.estThreads.toLocaleString()+"</b> of "+D.counts.mail.toLocaleString()+" conversations carry at least one. A thread takes the union of its messages' categories, so it can sit under several — the counts below add up to more than the threads."));
      D.estCats.forEach(function(e){{
        var n=countIf("est", function(r){{ return r.est && r.est.indexOf(e[0])>=0; }});
        if (!n && F.est!==e[0]) return;
        rail.appendChild(navBtn({{ label:e[0], count:n, on:F.est===e[0],
          fn:function(){{ set("est", e[0]); }} }}));
      }});
    }}

    if (K.msg){{
      rail.appendChild(head("How triage judged it"));
      D.triages.forEach(function(t){{
        rail.appendChild(navBtn({{ label:t, count: countIf("triage", function(r){{ return r.triage===t; }}),
          on:F.triage===t, fn:function(){{ set("triage",t); }} }}));
      }});
      rail.appendChild(missing("<b>One iMessage store, "+D.counts.msgMessages.toLocaleString()+" messages.</b> Sent versus received is unusable here for the same reason as mail — the pipeline never recorded which handles belong to the account holder, so every thread reads as entirely received. Messages carry no category and no significance, so those cuts are empty for them."));
    }}

    if (K.doc || K.mail) rail.appendChild(head("Categories, and the types inside them"));
    if (K.doc || K.mail)
    D.cats.forEach(function(c){{
      var n=countIf("cat", function(r){{ return r.cat===c.n; }});
      rail.appendChild(navBtn({{ label:c.n, count:n, twisty:!!open[c.n],
        on:F.cat===c.n&&!F.sub, fn:function(){{ set("cat",c.n); }} }}));
      if (open[c.n]) (D.typesByCat[c.n]||[]).forEach(function(t){{
        var tn=countIf("type", function(r){{ return r.cat===c.n&&r.type===t; }});
        if (!tn && F.type!==t) return;
        rail.appendChild(navBtn({{ label:t, count:tn, depth:1, on:F.type===t,
          fn:function(){{ F.cat=c.n; open[c.n]=true; set("type",t); }} }}));
      }});
    }});

    // ── the nested cuts, then everything that explains them ──────────────
    var notes = [];

    var eSec = section("Entities", "ent", DEMO_FACETS ? "*" : "");
    rail.appendChild(eSec.btn);
    if (eSec.open){{
      if (DEMO_FACETS){{
        ["Me", "Spouse", "ACME Company", "Children"].forEach(function(n){{
          rail.appendChild(ghostBtn(n, "*", "Placeholder — the pipeline records no entity tags, so this filters nothing.")); }});
      }} else {{
        rail.appendChild(missing("<b>Not in this case.</b> Nothing in the pipeline records which person, business, property or account a record belongs to, so “everything about one company” cannot be built from this data."));
      }}
    }}
    if (DEMO_FACETS) notes.push(["*", "<b>Entities are a placeholder.</b> Nothing in the pipeline records which person, business, property or account a record belongs to, so those four filter nothing. “Everything about one company” cannot be built from this case as it stands."]);

    if (K.doc){{
      var sSec = section("Where the file came from", "src", DEMO_FACETS ? "**" : "");
      rail.appendChild(sSec.btn);
      if (sSec.open){{
        if (DEMO_FACETS){{
          ["iMac Hard Drive", "Dropbox", "Gmail", "More Hard Drive"].forEach(function(n){{
            rail.appendChild(ghostBtn(n, "**", "Placeholder — intake kept no record of source, so this filters nothing.")); }});
        }} else {{
          rail.appendChild(missing("<b>Not in this case.</b> All "+D.counts.docs.toLocaleString()+" documents sit in one flat folder — intake copied them out of wherever they were and kept no record of it."));
        }}
      }}
      if (DEMO_FACETS) notes.push(["**", "<b>Where the file came from is a placeholder.</b> All "+D.counts.docs.toLocaleString()+" documents sit in one flat folder — intake copied them out of wherever they were and kept no record of it, so those four filter nothing. “What was on the laptop versus in Dropbox” cannot be answered from this case as it stands."]);

      var rSec = section("How it was read", "read", "", !!F.read);
      rail.appendChild(rSec.btn);
      if (rSec.open) D.reads.forEach(function(x){{
        rail.appendChild(navBtn({{ label:x, count: countIf("read", function(r){{ return r.read===x; }}),
          on:F.read===x, fn:function(){{ set("read",x); }} }}));
      }});

      var ySec = section("Year", "yr", DEMO_FACETS ? "***" : "");
      rail.appendChild(ySec.btn);
      if (ySec.open){{
        if (DEMO_FACETS){{
          for (var y=2026; y>=2015; y--) rail.appendChild(ghostBtn(String(y), "***", "Placeholder — a document record carries no date, so this filters nothing."));
        }} else {{
          rail.appendChild(missing("<b>Not in this case.</b> A document record carries no date — so no year column, no sorting by year, and no Gap Grid. Mail has dates; documents do not."));
        }}
      }}
      if (DEMO_FACETS) notes.push(["***", "<b>The years are a placeholder.</b> A document record carries no date, so they filter nothing — and for the same reason there is no year column, no sorting by year and no Gap Grid over documents. Mail and messages do carry dates; this cut fails only for documents."]);
    }}

    // The footnotes and the taxonomy warning close the rail, below everything
    // they refer to.
    if (notes.length){{
      var fn=document.createElement("div"); fn.className="notes";
      notes.forEach(function(pr){{
        var d=document.createElement("div"); d.className="note";
        d.innerHTML = "<span class=\'mk\'>"+pr[0]+"</span> "+pr[1];
        fn.appendChild(d);
      }});
      rail.appendChild(fn);
    }}

    if (K.doc){{
      var cl=document.createElement("div"); cl.className="missing";
      cl.style.marginTop = ".7rem";
      cl.innerHTML = "<b>Two taxonomies, never reconciled.</b> The classifier and the vital-document pass both name document types, and where they share a name they disagree — "
        + D.clash.map(function(c){{ return c[0]+": classifier "+c[1]+", vital pass "+c[2]+", agreeing on "+c[3]; }}).join("; ")
        + ". The types above merge the two, so each document appears once.";
      rail.appendChild(cl);
    }}
  }}

  // Documents carry no date, so "newest first" is only offered for mail.
  var SORTS={{
    doc:[["name","Name A–Z"],["sig","Significance, high first"],["type","Document type"],["pri","Urgency"]],
    mail:[["recent","Most recent activity first"],["oldest","Oldest activity first"],
          ["msgs","Most messages first"],["sig","Significance, high first"],
          ["estate","Estate relevance, high first"],
          ["subject","Subject A–Z"],["who","Sender A–Z"]],
    msg:[["recent","Most recent activity first"],["oldest","Oldest activity first"],
         ["msgs","Most messages first"],["name","Name A–Z"]],
    mix:[["name","Name A–Z"],["sig","Significance, high first"],["recent","Most recent activity first"]]
  }};
  var SORTFN={{
    name:    function(a,b){{ return String(a.n).localeCompare(String(b.n)); }},
    subject: function(a,b){{ return String(a.n).localeCompare(String(b.n)); }},
    who:     function(a,b){{ return String(a.who||"").localeCompare(String(b.who||"")); }},
    type:    function(a,b){{ return String(a.type||"").localeCompare(String(b.type||"")); }},
    pri:     function(a,b){{ return ["Vital","Near miss","Not vital"].indexOf(a.pri)-["Vital","Near miss","Not vital"].indexOf(b.pri); }},
    sig:     function(a,b){{ return (b.sig||0)-(a.sig||0); }},
    msgs:    function(a,b){{ return (b.msgs||0)-(a.msgs||0); }},
    recent:  function(a,b){{ return String(b.ts||"0000").localeCompare(String(a.ts||"0000")); }},
    oldest:  function(a,b){{ return String(a.ts||"9999").localeCompare(String(b.ts||"9999")); }},
    estate:  function(a,b){{ return (b.escore||0)-(a.escore||0); }}
  }};
  var sortBy="name";

  var GROUPS={{ doc:[["cat","Category"],["type","Document type"],["pri","How urgent"],["read","How it was read"],["sig","Significance 1–5"],["none","Nothing — one list"]],
    mail:[["who","Sender"],["dom","Sender's domain"],["box","Account it arrived in"],["rel","Who wrote"],["cat","Category"],["yr","Year of last activity"],["none","Nothing — one list"]],
    msg:[["who","Who it is with"],["triage","How triage judged it"],
         ["yr","Year of last activity"],["none","Nothing — one list"]],
    mix:[["kind","Documents, mail or messages"],["cat","Category"],["none","Nothing — one list"]] }};
  function keyOf(r,g){{
    if (g==="none") return "All results";
    if (g==="kind") return r.kind==="doc"?"Documents": r.kind==="mail"?"Mail conversations":"Message threads";
    if (g==="yr") return String(r.ts||"").slice(0,4)||"undated";
    return String(r[g]===undefined?"—":r[g]);
  }}

  function drawList(){{
    var host=document.getElementById("list"); host.className="list "+mode; host.textContent="";
    var rs=rows();
    if (!rs.length){{ var e=document.createElement("div"); e.className="empty";
      e.textContent="Nothing matches all of those at once. Remove one of the chips above."; host.appendChild(e); return; }}

    var cols = mode==="doc" ? ["Document","Urgency","Document type","How it was read","Significance"]
             : mode==="mail" ? ["Conversation","From","Account","Msgs","Last activity"]
             : mode==="msg" ? ["Message thread","Triage","Msgs","Attachments","Last activity"]
             : ["Item","Kind","What it is","Last activity"];
    var hdr=document.createElement("div"); hdr.className="hdr";
    cols.forEach(function(c){{ var s=document.createElement("span"); s.textContent=c; hdr.appendChild(s); }});
    host.appendChild(hdr);

    var sfn=SORTFN[sortBy]; if (sfn) rs = rs.slice().sort(sfn);
    var g={{}}, order=[];
    rs.forEach(function(r){{ var k=keyOf(r,group); if(!g[k]){{g[k]=[];order.push(k);}} g[k].push(r); }});
    order.sort(function(a,b){{ return g[b].length-g[a].length; }});

    var many = group!=="none" && order.length>1;
    var shut = {{}};
    order.forEach(function(k){{
      var v = fold[foldKey(k)];
      shut[k] = many && (v===undefined ? (g[k].length>FOLD_AT || order.length>FOLD_MANY) : v);
    }});
    curKeys = order.slice();
    var fa = document.getElementById("foldall");
    fa.hidden = !many;
    if (many){{
      var anyOpen = order.some(function(k){{ return !shut[k]; }});
      fa.textContent = anyOpen ? "collapse all" : "expand all";
      fa.dataset.to = anyOpen ? "1" : "0";
    }}

    order.forEach(function(k){{
      if (group!=="none"){{
        var gh=document.createElement("button"); gh.type="button";
        gh.className="gh"+(shut[k]?" folded":"");
        gh.setAttribute("aria-expanded", shut[k]?"false":"true");
        var car=document.createElement("span"); car.className="car"; car.textContent="\u25be";
        var n=document.createElement("span"); n.className="n"; n.textContent=k;
        var c=document.createElement("span"); c.className="c"; c.textContent=g[k].length.toLocaleString();
        gh.appendChild(car); gh.appendChild(n); gh.appendChild(c);
        gh.addEventListener("click", function(){{ fold[foldKey(k)] = !shut[k]; draw(); }});
        host.appendChild(gh);
      }}
      if (shut[k]) return;
      g[k].slice(0,CAP).forEach(function(r){{
        var b=document.createElement("button"); b.type="button"; b.className="row"+(sel===r.id?" on":"");
        var nm=document.createElement("span"); nm.className="nm";
        var t=document.createElement("span"); t.textContent=r.n; nm.appendChild(t); b.appendChild(nm);
        if (mode==="mix"){{
          var kl = r.kind==="doc" ? "Document" : r.kind==="mail" ? "Mail conversation" : "Message thread";
          var wt = r.kind==="doc" ? r.type : r.kind==="mail" ? r.who : r.triage;
          [kl, wt, r.ts || "no date recorded"].forEach(function(v){{
            var s=document.createElement("span"); s.className="meta"; s.textContent=v; b.appendChild(s); }});
        }} else if (r.kind==="doc"){{
          var bw=document.createElement("span"); var bd=document.createElement("span");
          bd.className="band "+(PRI_CLASS[r.pri]||"b-rec"); bd.textContent=r.pri; bw.appendChild(bd); b.appendChild(bw);
          [r.type, r.read, "significance "+r.sig].forEach(function(v){{ var s=document.createElement("span"); s.className="meta"; s.textContent=v; b.appendChild(s); }});
        }} else if (r.kind==="mail"){{
          [r.who, r.box, String(r.msgs), r.ts].forEach(function(v){{ var s=document.createElement("span"); s.className="meta"; s.textContent=v; b.appendChild(s); }});
        }} else {{
          [r.triage, String(r.msgs), String(r.att), r.ts].forEach(function(v){{ var s=document.createElement("span"); s.className="meta"; s.textContent=v; b.appendChild(s); }});
        }}
        // Clicking the line opens the thing. It selects it as well, so closing the
        // popup leaves the detail pane already showing what you just looked at —
        // the right-hand pane is no longer a stop on the way to opening anything.
        // The popup closes on Escape, on the close button, or by clicking outside.
        b.addEventListener("click", function(){{ sel=r.id; draw(); openDoc(r); }});
        host.appendChild(b);
      }});
      if (g[k].length>CAP){{ var m=document.createElement("div"); m.className="more";
        m.textContent="+ "+(g[k].length-CAP).toLocaleString()+" more in this group — narrow with the sidebar"; host.appendChild(m); }}
    }});
  }}

  function drawDetail(){{
    var pane=document.getElementById("detail"), frame=document.getElementById("frame");
    var r=pool().filter(function(x){{ return x.id===sel; }})[0];
    if (!r){{ pane.hidden=true; frame.className="frame"; return; }}
    pane.hidden=false; frame.className="frame detail"; pane.textContent="";
    var close=document.createElement("button"); close.type="button"; close.className="dclose"; close.textContent="close";
    close.addEventListener("click", function(){{ sel=null; draw(); }}); pane.appendChild(close);
    var t=document.createElement("div"); t.className="dt"; t.textContent=r.n; pane.appendChild(t);
    var ob=document.createElement("button"); ob.type="button"; ob.className="mbtn";
    ob.style.alignSelf="flex-start";
    ob.textContent = r.kind==="doc" ? "Open the document" : "Open the conversation";
    ob.addEventListener("click", function(){{ openDoc(r); }});
    pane.appendChild(ob);
    var dl=document.createElement("div"); dl.className="dl";
    function pair(k,v){{ var a=document.createElement("span"); a.className="k"; a.textContent=k;
      var b=document.createElement("span"); b.textContent=v; dl.appendChild(a); dl.appendChild(b); }}
    pair("Filed under", r.cat+(r.sub&&r.sub!=="—"?" › "+r.sub:""));
    if (r.kind==="doc"){{
      pair("Document type", r.type); pair("Urgency", r.pri);
      if (r.tested) pair("Tested as", r.tested);
      if (r.why) pair("Rejected because", r.why);
      pair("How it was read", r.read);
      pair("Significance", String(r.sig)+" of 5");
    }} else if (r.kind==="mail"){{
      pair("From", r.who); pair("Who wrote", r.rel);
      pair("Account", r.box);
      if (r.vital && r.vital.length) pair("Holds a vital", r.vital.join(" · "));
      if (r.est && r.est.length) pair("Estate business", r.est.join(" · "));
      pair("Messages", String(r.msgs)); pair("Span", r.first+" → "+r.last);
      pair("Significance", String(r.sig));
    }} else {{
      pair("With", r.who); pair("Platform", r.plat);
      pair("Triage", r.triage + (r.resc ? " — rescued by estate keywords" : ""));
      pair("Messages", String(r.msgs)); pair("Attachments", String(r.att));
      pair("Span", r.first+" → "+r.last);
    }}
    pane.appendChild(dl);
    if (r.sum){{ var s=document.createElement("div"); s.className="dnote"; s.textContent=r.sum; pane.appendChild(s); }}
  }}

  function draw(){{
    Array.prototype.forEach.call(document.querySelectorAll("#mode button"), function(b){{
      b.classList.toggle("on", !!K[b.dataset.m]);
      b.setAttribute("aria-pressed", K[b.dataset.m] ? "true" : "false"); }});
    var gs=document.getElementById("groupby");
    if (gs.dataset.mode!==mode){{
      gs.dataset.mode=mode; gs.textContent="";
      GROUPS[mode].forEach(function(o){{ var op=document.createElement("option"); op.value=o[0]; op.textContent=o[1]; gs.appendChild(op); }});
      if (!GROUPS[mode].some(function(o){{ return o[0]===group; }})) group=GROUPS[mode][0][0];
      gs.value=group;
      var ss=document.getElementById("sortby"); ss.textContent="";
      SORTS[mode].forEach(function(o){{ var op=document.createElement("option"); op.value=o[0]; op.textContent=o[1]; ss.appendChild(op); }});
      if (!SORTS[mode].some(function(o){{ return o[0]===sortBy; }})) sortBy=SORTS[mode][0][0];
      ss.value=sortBy;
    }}
    drawRail();
    var bits=[]; bits.push(F.pri ? (F.pri==="Not vital"?"Documents that are not vital":F.pri+" documents")
      : (mode==="doc"?"All documents":mode==="mail"?"All mail":mode==="msg"?"All message threads":"Everything"));
    if (F.type) bits.push("of type <em>"+F.type+"</em>");
    if (F.vmail) bits.push("that <em>carry a vital document</em>");
    if (F.rel) bits.push("from <em>"+F.rel+"</em>");
    if (F.who) bits.push("sent by <em>"+F.who+"</em>");
    if (F.box) bits.push("received in <em>"+F.box+"</em>");
    if (F.cat) bits.push("in <em>"+(F.sub&&F.sub!=="—"?F.cat+" › "+F.sub:F.cat)+"</em>");
    if (F.read) bits.push("read as <em>"+F.read+"</em>");
    if (F.dom) bits.push("from <em>"+F.dom+"</em>");
    if (F.est) bits.push("touching <em>"+F.est+"</em>");
    if (F.triage) bits.push("triaged as <em>"+F.triage+"</em>");
    document.getElementById("sentence").innerHTML=bits.join(" ");

    var host=document.getElementById("chips"); host.textContent=""; var any=false;
    [["pri",F.pri],["type",F.type],["rel",F.rel],["cat",F.cat],["read",F.read],["box",F.box],["who",F.who],["est",F.est],["dom",F.dom],["triage",F.triage],["vmail",F.vmail?"carries a vital item":null]].forEach(function(p){{
      if(!p[1])return; any=true;
      var c=document.createElement("span"); c.className="chip"; c.appendChild(document.createTextNode(p[1]));
      var x=document.createElement("button"); x.type="button"; x.className="x"; x.textContent="×";
      x.addEventListener("click", function(){{ if(p[0]==="cat"){{F.cat=null;F.sub=null;}} else F[p[0]]=null; sel=null; draw(); }});
      c.appendChild(x); host.appendChild(c);
    }});
    document.getElementById("clear").hidden=!any;

    var rs=rows(); var total=pool().length;
    document.getElementById("tally").innerHTML="<b>"+rs.length.toLocaleString()+"</b> of "+total.toLocaleString();
    if (pane==="list"){{ drawList(); drawDetail(); }}
    drawPane();
  }}

  // ── Reports and Online Accounts ─────────────────────────────────────────
  // Both payloads are whatever ~/mac_family_archive's own ArchiveCase produced
  // at generation time. Nothing is recomputed here — this only draws it.
  function num(v){{ return (v===null||v===undefined||v==="") ? "—" : (typeof v==="number" ? v.toLocaleString() : String(v)); }}
  function tbl(host, cols, rows, onRow){{
    var t=document.createElement("table"), hr=document.createElement("tr");
    cols.forEach(function(c){{ var th=document.createElement("th");
      if (c.num) th.className="num"; th.textContent=c.label; hr.appendChild(th); }});
    t.appendChild(hr);
    rows.forEach(function(r){{
      var tr=document.createElement("tr");
      if (onRow){{ tr.className="click"; tr.addEventListener("click", function(){{ onRow(r); }}); }}
      cols.forEach(function(c){{
        var td=document.createElement("td"); if (c.num) td.className="num";
        var v=c.get(r);
        if (v && v.nodeType) td.appendChild(v); else td.textContent = c.num ? num(v) : (v==null?"":String(v));
        tr.appendChild(td);
      }});
      t.appendChild(tr);
    }});
    host.appendChild(t);
  }}
  function stats(host, pairs){{
    var w=el("div","stats");
    pairs.forEach(function(p){{ var d=el("div","stat");
      d.appendChild(el("b","",num(p[0]))); d.appendChild(el("span","",p[1])); w.appendChild(d); }});
    host.appendChild(w);
  }}
  function lims(host, list){{
    (list||[]).forEach(function(x){{ host.appendChild(el("li","lim", String(x))); }});
  }}

  function drawAccounts(host){{
    var A=D.accounts||{{}};
    host.appendChild(el("h2","pane-t","Online Accounts"));
    if (!A.services){{ host.appendChild(el("p","pane-sub",
      "Not available: "+(D.appErr||"the archive's own code could not be reached when this file was built."))); return; }}
    host.appendChild(el("p","pane-sub", A.note ||
      "Login services and security findings recovered from the estate. No monetary balances are extracted by the pipeline."));
    var c=A.credentials||{{}};
    host.appendChild(el("div","pane-note",
      (c.critical_count||0)+" critical and "+(c.informational_count||0)+" informational credential finding(s)."));
    var svcs=A.services||[];
    host.appendChild(el("h3","pane-h","Services found in the mail"));
    host.appendChild(el("p","pane-sub", svcs.length+" services. A row opens that service\u2019s conversations. Found by looking for the kind of mail an account sends \u2014 sign-ins, security alerts, statements \u2014 so it is a strong hint rather than a certainty, and a service the owner never received such mail from will not appear."));
    tbl(host, [
      {{label:"Service", get:function(r){{
        var w=el("span","",r.service);
        if (r.from_pipeline) w.appendChild(el("span","tag-inv","INVENTORY"));
        return w; }}}},
      {{label:"Conversations", num:true, get:function(r){{ return r.threads; }}}},
      {{label:"Account mail", num:true, get:function(r){{ return r.signals; }}}},
      {{label:"", get:function(r){{ return r.filtered_out ? (r.filtered_out+" more filtered out as bulk") : ""; }}}}
    ], svcs, function(r){{
      if (!r.threads) return;                       // nothing to open
      // The count above is the archive's; the link is this view's own sender-
      // domain match, so the two can differ by a few conversations.
      K.doc=false; K.msg=false; K.mail=true; syncMode();
      F.dom=r.service; sel=null; pane="list"; draw();
    }});
    host.appendChild(el("p","pane-sub","The conversation count is the archive\u2019s own. Opening a row filters this view by the sender\u2019s domain, which resolves the counterpart slightly differently \u2014 so the number of conversations you land on can differ from the number in the row by a few."));
    if (A.domains && A.domains.length){{
      host.appendChild(el("h3","pane-h","The pipeline\u2019s own inventory"));
      host.appendChild(el("p","pane-sub", A.domains.length+" domains, built from the raw corpus before the mail was filtered."));
      tbl(host, [{{label:"Domain", get:function(r){{ return r.domain; }}}},
                 {{label:"Emails", num:true, get:function(r){{ return r.count; }}}}], A.domains);
    }}
  }}

  function drawReport(host, key){{
    var R=(D.reports||{{}})[key]; if (!R){{ host.appendChild(el("p","pane-sub","Not available.")); return; }}
    var back=el("button","pane-back","\u2190 All reports"); back.type="button";
    back.addEventListener("click", function(){{ report=null; draw(); }});
    host.appendChild(back);
    if (key==="estate"){{
      host.appendChild(el("h2","pane-t","Estate document report"));
      host.appendChild(el("p","pane-sub","Case "+R.case_id+" \u00b7 prepared "+(R.generated_at||"\u2014")));
      var T=R.totals||{{}};
      stats(host, [[T.present,"types confirmed present"],[T.unconfirmed,"types not yet established"],[T.absent,"types with nothing matched"]]);
      host.appendChild(el("p","pane-sub","Across "+num(T.types)+" document types an estate needs. "+num(T.candidates)+
        " candidate documents were found, of which "+num(T.signed_off)+" have been confirmed and "+num(T.undecided)+
        " are still unreviewed, alongside "+num(T.near_misses)+" weaker matches that have not been reviewed."));
      host.appendChild(el("h3","pane-h","What this report cannot tell you"));
      var ul=document.createElement("ul"); lims(ul, R.limitations); host.appendChild(ul);
      (R.groups||[]).forEach(function(g){{
        host.appendChild(el("h3","pane-h", g.label+" ("+(g.types||[]).length+")"));
        if (g.note) host.appendChild(el("p","pane-sub", g.note));
        if (!(g.types||[]).length){{ host.appendChild(el("p","pane-sub","None.")); return; }}
        tbl(host, [
          {{label:"Document type", get:function(r){{
            var w=el("span","",r.label);
            if (r.capped) w.appendChild(el("span","tag-inv","CAPPED"));
            return w; }}}},
          {{label:"Found", num:true, get:function(r){{ return r.candidates; }}}},
          {{label:"Confirmed", num:true, get:function(r){{ return r.signed_off; }}}},
          {{label:"Unreviewed", num:true, get:function(r){{ return r.undecided; }}}},
          {{label:"Weaker matches", num:true, get:function(r){{ return r.near_misses; }}}}
        ], g.types);
      }});
    }} else if (key==="family"){{
      host.appendChild(el("h2","pane-t","Family report"));
      host.appendChild(el("p","pane-sub","Case "+R.case_id+" \u00b7 prepared "+(R.generated_at||"\u2014")));
      var sp=R.span||{{}};
      host.appendChild(el("p","pane-sub","Covering "+(sp.from||"?")+" to "+(sp.to||"?")+
        (sp.undated ? " \u00b7 "+num(sp.undated)+" items carry no date" : "")));
      stats(host, (R.headline||[]).map(function(h){{ return [h.count, h.label]; }}));
      var P=R.people||{{}};
      host.appendChild(el("h3","pane-h","People"));
      host.appendChild(el("p","pane-sub", num(P.total)+" people \u2014 "+num(P.named)+" named, "+num(P.unnamed)+" unnamed. "+num(R.places)+" places."));
      if (P.top && P.top.length) tbl(host, [
        {{label:"Name", get:function(r){{ return r.name; }}}},
        {{label:"Photographs", num:true, get:function(r){{ return r.photos; }}}}], P.top);
      (R.sections||[]).forEach(function(sec){{
        host.appendChild(el("h3","pane-h", sec.label));
        if (sec.note) host.appendChild(el("p","pane-sub", sec.note));
        tbl(host, [{{label:"", get:function(r){{ return r.label; }}}},
                   {{label:"Count", num:true, get:function(r){{ return r.count; }}}}], sec.items||[]);
      }});
      host.appendChild(el("h3","pane-h","What this report cannot tell you"));
      var ul2=document.createElement("ul"); lims(ul2, R.limitations); host.appendChild(ul2);
    }} else {{
      host.appendChild(el("h2","pane-t","Pipeline report"));
      host.appendChild(el("p","pane-sub","Case "+R.case_id+" \u00b7 prepared "+(R.generated_at||"\u2014")));
      var TT=R.totals||{{}};
      stats(host, [[TT.examined,"items examined"],[TT.surfaced,"reached the archive"]]);
      if (R.surfaced_note) host.appendChild(el("p","pane-sub", R.surfaced_note));
      tbl(host, [
        {{label:"Kind", get:function(r){{ return r.kind; }}}},
        {{label:"Examined", num:true, get:function(r){{ return r.examined; }}}},
        {{label:"Surfaced", num:true, get:function(r){{ return r.surfaced; }}}},
        {{label:"Why the difference", get:function(r){{ return r.note; }}}}
      ], R.rows||[]);
      var ex=R.expansion||{{}}, rd=R.reading||{{}}, sz=R.size||{{}}, run=R.run||{{}};
      host.appendChild(el("h3","pane-h","What was unpacked and read"));
      stats(host, [[ex.archives_found,"archives found"],[ex.files_added,"files added"],
                   [ex.email_attachments,"email attachments"],[rd.documents_read,"documents read"],
                   [rd.text_recovered,"text recovered"],[rd.audio_hours,"hours of audio"]]);
      host.appendChild(el("h3","pane-h","Size"));
      host.appendChild(el("p","pane-sub", (sz.total_human||"\u2014")+" across "+num(sz.files)+" files."));
      if (sz.parts) tbl(host, [
        {{label:"Part", get:function(r){{ return r.name; }}}},
        {{label:"Size", get:function(r){{ return r.human; }}}},
        {{label:"Share", num:true, get:function(r){{ return (r.share!=null? r.share+"%" : "\u2014"); }}}}], sz.parts);
      host.appendChild(el("h3","pane-h","The run"));
      host.appendChild(el("p","pane-sub","Started "+(run.first||"?")+", finished "+(run.last||"?")+" \u2014 "+(run.elapsed||"?")+"."));
      if (run.stages) tbl(host, [
        {{label:"Stage", get:function(r){{ return r.stage; }}}},
        {{label:"At", get:function(r){{ return r.at; }}}}], run.stages);
    }}
  }}

  function drawReports(host){{
    if (report) return drawReport(host, report);
    host.appendChild(el("h2","pane-t","Reports"));
    var idx=(D.reports||{{}}).index;
    if (!idx){{ host.appendChild(el("p","pane-sub",
      "Not available: "+(D.appErr||"the archive\u2019s own code could not be reached when this file was built."))); return; }}
    host.appendChild(el("p","pane-sub","Statements drawn from the archive, meant to be printed and handed over."));
    (idx.reports||[]).forEach(function(r){{
      var b=el("button","rcard"); b.type="button";
      b.appendChild(el("b","",r.label)); b.appendChild(el("span","",r.note||""));
      b.addEventListener("click", function(){{ report=r.key; draw(); }});
      host.appendChild(b);
    }});
  }}

  function drawPane(){{
    var bar=document.querySelector(".bar"), list=document.getElementById("list"),
        pn=document.getElementById("pane"), det=document.getElementById("detail");
    var show = pane!=="list";
    bar.hidden=show; list.hidden=show; pn.hidden=!show;
    if (show){{ det.hidden=true; document.getElementById("frame").className="frame"; }}
    if (!show) return;
    pn.textContent="";
    if (pane==="accounts") drawAccounts(pn); else drawReports(pn);
  }}

  // ── rendering the structured view the pipeline already wrote ────────────
  var TABLE_ROWS = 300;                // rows drawn per table before it is cut
  function el(tag, cls, txt){{
    var e=document.createElement(tag); if(cls) e.className=cls;
    if(txt!==undefined) e.textContent=txt; return e; }}

  function tableOf(b){{
    var rows=(b.rows||[]).slice();
    function blank(r){{ return !r.some(function(c){{ return String(c==null?"":c).trim(); }}); }}
    while (rows.length && blank(rows[0])) rows.shift();
    while (rows.length && blank(rows[rows.length-1])) rows.pop();
    var wrap=el("div","dv-tw"), t=el("table","dv-t");
    rows.slice(0,TABLE_ROWS).forEach(function(r,i){{
      var tr=document.createElement("tr");
      r.forEach(function(c){{
        var cell=document.createElement(i===0 && b.header ? "th" : "td");
        cell.textContent = c==null ? "" : String(c);
        tr.appendChild(cell);
      }});
      t.appendChild(tr);
    }});
    wrap.appendChild(t);
    if (rows.length>TABLE_ROWS) wrap.appendChild(el("div","dv-more",
      "+ "+(rows.length-TABLE_ROWS).toLocaleString()+" more rows — open the original to see them all"));
    return wrap;
  }}

  function renderDoc(j, host){{
    var blocks=(j&&j.blocks)||[];
    if (!blocks.length){{ host.appendChild(el("div","dv-none",
      "The pipeline read this file but recorded no content for it.")); return; }}
    blocks.forEach(function(b){{
      if (b.t==="section") host.appendChild(el("div","dv-sect", b.text||""));
      else if (b.t==="h") host.appendChild(el("h"+Math.min(Math.max((b.level||2)+1,2),5), "dv-h", b.text||""));
      else if (b.t==="table") host.appendChild(tableOf(b));
      else host.appendChild(el("p","dv-p", b.text||""));
    }});
  }}

  function renderTranscript(j, host){{
    var who=(j.participants||[]).join(" · ");
    if (who) host.appendChild(el("div","dv-sect", who));
    var ms=j.messages||[];
    if (!ms.length){{ host.appendChild(el("div","dv-none",
      "No messages recorded in this conversation.")); return; }}
    ms.forEach(function(m){{
      var w=el("div","dv-msg"), h=el("div","dv-mh");
      h.appendChild(el("b","", m.sender||"—"));
      h.appendChild(el("span","", m.ts||""));
      w.appendChild(h);
      w.appendChild(el("div","dv-mt", m.text||""));
      var att=m.attachments||[];
      if (att.length) w.appendChild(el("div","dv-att",
        att.length+" attachment"+(att.length===1?"":"s")+": "+att.map(function(a){{
          return String(a).split("/").pop(); }}).join(", ")));
      host.appendChild(w);
    }});
  }}

  // ── the popup: show the real document or the rendered conversation ──────
  var FILE_MODE = location.protocol === "file:";
  function openDoc(r){{
    var scrim=document.createElement("div"); scrim.className="scrim";
    var m=document.createElement("div"); m.className="modal";
    var h=document.createElement("div"); h.className="mhead";
    var t=document.createElement("span"); t.className="mtitle"; t.textContent=r.n;
    var meta=document.createElement("span"); meta.className="mmeta";
    meta.textContent = r.kind==="doc" ? (r.pri+" · "+r.type)
                     : r.kind==="msg" ? (r.triage+" · "+r.msgs+" messages")
                     : (r.rel+" · "+r.msgs+" messages");
    // A PDF and an HTML conversation render in place; Office files would only
    // download, so those show the OCR text and keep the original one click away.
    var isPdf = /\.pdf$/i.test(r.src || "");
    var isHtml = /\.html?$/i.test(r.src || "");
    // Anything the pipeline extracted structure for is drawn here rather than
    // handed to the browser, which would only offer to download an .xlsx.
    var isMsg = r.kind==="msg" && !!r.src;
    var rich = isMsg ? r.src : (r.view || "");
    var target = rich ? rich : ((isPdf || isHtml) ? r.src : (r.txt || r.src || ""));
    var original = r.src || r.txt || "";
    var showingText = !rich && !!(target && target === r.txt && r.src);
    var open=document.createElement("a"); open.className="mbtn";
    open.textContent = showingText ? "open the original file" : "open in a new tab";
    open.href=original; open.target="_blank"; open.rel="noopener";
    var close=document.createElement("button"); close.className="mbtn"; close.type="button"; close.textContent="close";
    h.appendChild(t); h.appendChild(meta); if(target) h.appendChild(open); h.appendChild(close);

    var body=document.createElement("div"); body.className="mbody";
    if (!target){{
      var d=document.createElement("div"); d.className="mnone";
      d.innerHTML="<b>No viewable copy of this one.</b> It was classified from text the pipeline read, but no delivered file or OCR sidecar could be matched to it by filename — usually because two files share a name, in which case the archive declines to guess.";
      body.appendChild(d);
    }} else if (FILE_MODE){{
      var d2=document.createElement("div"); d2.className="mnone";
      d2.innerHTML="<b>Open this through the launcher to see it inline.</b> Browsers refuse to embed one local file inside another, so the viewer needs a local server. Quit this tab and double-click <code>view-"+D.case+".command</code> beside the case, which starts one and reopens this page.<br><br>“Open in a new tab” above still works right now.";
      body.appendChild(d2);
    }} else if (rich){{
      var dv=document.createElement("div"); dv.className="docview";
      dv.appendChild(el("div","dv-none","Reading…"));
      body.appendChild(dv);
      fetch(rich).then(function(res){{
        if (!res.ok) throw new Error("HTTP "+res.status);
        return res.json();
      }}).then(function(j){{
        dv.textContent="";
        (isMsg ? renderTranscript : renderDoc)(j, dv);
      }}).catch(function(err){{
        dv.textContent="";
        dv.appendChild(el("div","dv-none","Could not read the extracted view: "+err.message));
      }});
    }} else {{
      var f=document.createElement("iframe"); f.src=target;
      f.setAttribute("title", r.n); body.appendChild(f);
    }}

    var foot=document.createElement("div"); foot.className="mfoot";
    foot.textContent = (isMsg ? "showing the conversation the pipeline recorded · source: "
                     : rich ? "showing the structure the pipeline extracted · original: "
                     : showingText ? "showing the text the pipeline read · original: " : "")
                     + (original||"—");
    m.appendChild(h); m.appendChild(body); m.appendChild(foot); scrim.appendChild(m);
    function shut(){{ scrim.remove(); document.removeEventListener("keydown", esc); }}
    function esc(e){{ if(e.key==="Escape") shut(); }}
    close.addEventListener("click", shut);
    scrim.addEventListener("click", function(e){{ if(e.target===scrim) shut(); }});
    document.addEventListener("keydown", esc);
    document.body.appendChild(scrim);
  }}

  document.getElementById("groupby").addEventListener("change", function(e){{ group=e.target.value; draw(); }});
  document.getElementById("sortby").addEventListener("change", function(e){{ sortBy=e.target.value; draw(); }});
  document.getElementById("foldall").addEventListener("click", function(e){{
    var to = e.currentTarget.dataset.to==="1";
    curKeys.forEach(function(k){{ fold[foldKey(k)] = to; }});
    draw();
  }});
  document.getElementById("clear").addEventListener("click", function(){{
    F={{pri:null,type:null,cat:null,sub:null,read:null,rel:null,box:null,who:null,vmail:null,est:null,triage:null,dom:null}}; sel=null; open={{}}; draw(); }});
  Array.prototype.forEach.call(document.querySelectorAll("#mode button"), function(b){{
    b.addEventListener("click", function(){{
      var k=b.dataset.m;
      // Never leave nothing showing: clicking the only kind that is on keeps it on.
      if (K[k] && kindsOn().length===1) return;
      K[k]=!K[k]; syncMode();
      F={{pri:null,type:null,cat:null,sub:null,read:null,rel:null,box:null,who:null,vmail:null,est:null,triage:null,dom:null}}; sel=null; draw(); }}); }});
  draw();
}})();
</script>
</body></html>
"""

with open(OUT, "w") as f:
    f.write(page)

# ── a double-clickable launcher ─────────────────────────────────────────────
# Browsers refuse to embed one file:// document inside another, so the popup
# needs the case served over http. This starts a loopback-only server rooted at
# the case and opens the viewer.
LAUNCH = os.path.join(ROOT, "view-" + CASE + ".command")
with open(LAUNCH, "w") as f:
    f.write(f"""#!/bin/bash
# Opens the local Wyeast browse view for case {CASE}.
# Serves ONLY on 127.0.0.1 — nothing is exposed to the network.
cd "$(dirname "$0")" || exit 1
PORT=8777
while lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; do PORT=$((PORT+1)); done
echo "Serving this case at http://127.0.0.1:$PORT  (close this window to stop)"
python3 -m http.server "$PORT" --bind 127.0.0.1 >/dev/null 2>&1 &
SRV=$!
sleep 1
open "http://127.0.0.1:$PORT/{os.path.basename(OUT)}"
trap 'kill $SRV 2>/dev/null' EXIT
wait $SRV
""")
os.chmod(LAUNCH, 0o755)

c = DATA["counts"]
print(f"case             : {CASE}")
print(f"classified rows  : {c['classified']:,}  ({c['emailBodies']:,} were email bodies, excluded)")
print(f"documents        : {c['docs']:,}")
print(f"  vital confirmed: {c['confirmed']:,} ({c['vitalInMail']:,} of them are mail, not documents)")
print(f"  near misses    : {c['rejected']:,}")
print(f"  urgency        : " + ", ".join(f"{b}={sum(1 for r in DOCS if r['pri']==b):,}" for b in DATA["pris"]))
print(f"  untyped        : {sum(1 for r in DOCS if r['type']==UNTYPED):,} of {len(DOCS):,}")
print(f"  renderable     : {sum(1 for r in DOCS if r['src'].lower().endswith('.pdf')):,} PDF inline, "
      f"{sum(1 for r in DOCS if r['view']):,} via the structured view, "
      f"{sum(1 for r in DOCS if not r['view'] and not r['src'].lower().endswith('.pdf')):,} text-only or nothing")
print(f"  taxonomy clash : " + "; ".join(f"{c[0]} cls={c[1]}/vital={c[2]}/agree={c[3]}" for c in CLASH))
print(f"mail threads     : {c['mail']:,}  ({c['messages']:,} messages)")
print(f"  who wrote      : " + ", ".join(f"{r}={sum(1 for m in MAIL if m['rel']==r):,}" for r in DATA["rels"]))
print(f"  estate axis    : {DATA['estThreads']:,} of {c['mail']:,} threads carry an estate category")
print(f"  top estate cats: " + ", ".join(f"{e}={n:,}" for e, n in DATA["estCats"][:4]))
print(f"message threads  : {c['msgConvos']:,}  ({c['msgMessages']:,} messages)")
print(f"  triage         : " + ", ".join(f"{t}={sum(1 for m in MSGS if m['triage']==t):,}" for t in DATA["triages"]))
print(f"reports          : " + (", ".join(sorted(REPORTS)) if REPORTS else "unavailable — " + APP_ERR))
print(f"online accounts  : " + (f"{len(ACCOUNTS.get('services') or [])} services, "
      f"{len(ACCOUNTS.get('domains') or [])} in the pipeline inventory" if ACCOUNTS else "unavailable"))
print(f"categories       : {len(DATA['cats'])}")
print(f"examiner decisions in this case: {c['decisions']:,}")
print(f"viewable: {c['docsViewable']:,} of {c['docs']:,} documents · {c['mailViewable']:,} of {c['mail']:,} conversations")
print(f"\nwrote {OUT}  ({os.path.getsize(OUT)/1e6:.1f} MB)")
print(f"wrote {LAUNCH}")
print("contains real case content — local only, not published")
