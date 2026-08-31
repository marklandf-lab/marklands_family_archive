# mac_family_archive (marklandf-lab fork) — Project State

_This is the session-handoff doc for AI/dev work on **this fork**, whose purpose is UI
experiments on the family-facing archive. Keep it current when shipping — **inside the
feature branch**, not as a separate push to `main`._

_⚠️ **Nothing in this file may be a number that rots.** No test counts, no "N documents
undecided", no "current through PR #X". Every one of those was true for about an hour on
the day it was written. **Record how to find out, never the answer.**_

## ☀️ START HERE

**Newest first. The section immediately below this one is the most recent thing that
happened** — read down until you have enough, and treat anything dated earlier as history
rather than as instructions.

⚠️ **There is exactly ONE "START HERE" in this file, and this is it.** If you add a
handoff note, put it at the **top** — do not create a second front door.

**Where things stand — run these, do not trust a written figure:**

```bash
gh pr list --repo marklandf-lab/marklands_family_archive --state merged --limit 5
git log --oneline -8                                  # what this file is current through
./run_tests.sh                                        # pytest — necessary, nowhere near sufficient
./jscheck.sh report_assets/family/family.js           # no node on this Mac; this is the only JS check
lsof -nP -iTCP:7766 -sTCP:LISTEN                      # is the app already running?
./family_archive.sh 813_mf                            # start it (cases default to ~/WyeastCases)
```

### The four things that will bite you

1. ⚠️ **A GREEN SUITE DOES NOT MEAN THE UI WORKS.** Every defect fixed on 2026-08-26 was
   invisible to pytest — breadcrumbs that dumped you in the wrong list, a Confirm button
   printed on top of its own label, a page reporting a finished number over an unfinished
   job. None of them broke a function; each returned exactly what it promised. The failure
   only existed in the sequence of clicks. **Run `/walk`** (`.claude/skills/walk/SKILL.md`)
   after any change to `report_assets/family/`, and drive the real app in a browser.

2. ⚠️ **CLICKING IN THE APP MUTATES THE REAL CASE.** The review verbs are live: confirming
   a vital document, banishing a photo and moving a category all write to
   `output/metadata/family_decisions.json` and append to `family_actions.ndjson`. Three
   sign-offs were recorded on 813_mf during the 2026-08-26 session simply from people
   trying the queue. They are audited and reversible — but if a count moves and you did
   not expect it, read the log rather than assuming a bug:
   ```bash
   tail -20 ~/WyeastCases/813_mf/output/metadata/family_actions.ndjson
   ```

3. ⚠️ **A SYNC WILL DESTROY THIS FORK'S UI WORK.** `sync_from_wyeast.sh` replaces
   `report_assets/` wholesale. It now warns loudly instead of reverting in silence — read
   its output, do not skim it. See CLAUDE.md, "This fork's purpose".

4. ⚠️ **THIS REPO IS PUBLIC, AND PII HAS LEAKED INTO IT BEFORE.** Real names and phone
   numbers reached both this fork and WyeastCorp's repo as example text in comments and
   test fixtures. Before pushing anything, run the scan in `/ship` step 2. The rule is in
   CLAUDE.md, "What must never enter a commit". Warn the user; do not silently scrub.

---

## 🎯 2026-08-31 — Reports, Documents as an index, audio kinds, accounts, Messages fixed

**All of it is merged.** PR #5 and PR #6 are in `main`; there are no open PRs and
no unpushed branches. Earlier entries in this file that say "not yet pushed" are
describing a state that no longer exists — read them as history.

### ▶ Next action

```bash
lsof -nP -iTCP:7766 -sTCP:LISTEN || ./family_archive.sh 813_mf
```

Then decide the **eleven wrong vital-document sign-offs** (below). Everything
else here is polish, and the user has said polish is wanted but not what kind —
get that before starting on it.

### What shipped

- **Reports** (second in the rail, examiner-only, all print-styled):
  estate document report, family report, pipeline report. See them rather than
  trusting a description: `/reports`.
- **Documents browses as an index** — estate checklist as one branch, categories
  as siblings. `legal` subcategories were being discarded by `document_rows` and
  are now recovered from the delivery path.
- **Recordings** grouped into six kinds, collapsed.
- **Online Accounts** finds services from the mail rather than listing the
  pipeline's 23 social domains.
- **Messages was completely broken** — every conversation failed to open — and
  is fixed. See BACKEND.md #2 for the cause.
- **Four screens stopped overclaiming**: the Overview's "Still missing", the
  Emails band headings, the vital-doc row, the accounts list.

### ⚠️ Decide first: the eleven wrong sign-offs

Still recorded, and the estate report now repeats them to anybody it is shown
to — it asserts a property deed and a marriage certificate on the strength of a
power of attorney and a divorce judgment.

**Do not take that sentence on trust.** Open `/documents?view=vital`, expand
`Property deed / title` and `Marriage certificate`, and read the summaries now
on each row. Undo is per-row ("Undo — not this"); one is a reassign rather than
an undo. It writes to the real case, so it is the case owner's call, not a
maintenance task.

### Open, in rough order

- **Polish.** Asked for, unspecified. Spacing, wording, table density.
- The **27-type checklist moved** off the Documents landing to `?view=vital` —
  one extra click on the main review surface. Put it back if it grates.
- **BACKEND.md is the pipeline list** and is current through today (ten items).
  A copy for the upstream maintainer lives in the shared Google Drive folder
  (ask the user for the link — the file link changes on every edit, because the
  Drive tooling here cannot edit a document in place; the folder link is stable).

### Two things this session learned the hard way

- **The pipeline computes things nothing reads.** Acoustic labels for every
  recording, a dedup summary, per-document summaries, `legal` subcategories —
  four separate cases found by accident. Before building anything that needs a
  signal, check whether it is already on disk.
- **A count is not a count.** Messages against conversations, decisions against
  documents, examined against browsable. Three near-misses this session came
  from comparing two units. When a percentage looks surprising, check the
  denominator before believing it.

---

## 🎯 2026-08-28 (later) — Emails opens on an index, not 21,988 rows

### ▶ Next action

Open **http://127.0.0.1:7766/emails** and drill: a group, then a break-down chip,
then a thread, then back up the breadcrumb. The user asked for this shape and has
not yet said what is wrong with it — get that before building further.

### What shipped — same branch `claude/vital-row-summary`, still NOT pushed

- **An index instead of a list.** Four ways in — significance, subject
  (the pipeline's own per-thread categories, which were never rendered before),
  year, person — each with the server's count for the whole set.
- **One group at a time**, with the dimensions not yet used offered as a
  break-down. Chips link rather than re-sort, so a count can only say what the
  server counted. Filters layer and are addressable in the URL.
- **The band headings used to lie** — each printed its size within the loaded
  page (2,000 of ~22,000) as though it were a total. Gone.
- Fixed `sort` missing from `encodeURL`, which dropped it from the URL on any
  other control change. Correspondent grouping and `?participant=` now both
  honour `correspondent_merges`.

### Open questions on this screen

- **Nobody knows whose mailbox this is.** `owner_email_addresses` is empty in
  `case_config.json` and `email_triage_summary.json` says `owner_source: "none"`,
  so the account's own addresses are INFERRED from thread share and named on the
  page. Filling that in properly is a pipeline re-run, upstream — check the two
  files before assuming it is still true.
- The same person appears once per address in the person break-down until an
  examiner merges them on Correspondents. The merge machinery exists and is now
  honoured here; nothing has been merged on this case.
- Year chips can sum to less than the group total — a thread with no date is in
  the group but in no year. Confirm that reads acceptably before "fixing" it.

---

## 🎯 2026-08-28 — the vital-doc row now carries the evidence its decision needs

### ▶ Next action

```bash
lsof -nP -iTCP:7766 -sTCP:LISTEN || ./family_archive.sh 813_mf
```
Then open **http://127.0.0.1:7766/documents**, expand `Property deed / title`, and read
the rows. Every sign-off there is visibly wrong now that each row says what the document
is — **the sign-offs themselves have not been corrected.** Deciding what to do about them
(undo them in the UI, or leave them for whoever owns the case) is the open question, and
it is the user's call, not a code change.

### What shipped — branch `claude/vital-row-summary`, NOT yet pushed or PR'd

- **The summary is on the decision row.** Confirmed candidates and near-misses both. The
  text already existed — written at classification time, rendered further down the same
  page in the documents table, never where the click happens.
- **Who may read it reuses the gate that was already computed** — the role's browsable-doc
  map and its own conversation index — rather than adding a second gate to drift out of
  step. An item resolving to neither gets nothing, so the family side fails closed.
- Four tests, `tests/unit/test_archive_data.py`, all failing against `main` with
  `KeyError: 'summary'` before the change. Verify that claim by checking the branch out
  against `main` — do not take this file's word for it.

### What this exposed, still open

- **The recorded sign-offs are wrong and are still recorded.** Read them yourself rather
  than trusting this line: expand `Property deed / title` on the running app. The release
  gate trusts these, so they matter. Undo is per-row in the UI ("Undo — not this").
- The **category filter** still renders the whole 27-type checklist above the filtered
  list, and the panel's four numbers still describe the whole collection while the page
  below shows one category. Measure it before deciding it is urgent — it was ~1,400px on
  2026-08-28, and last session's rebuild already shrank it a great deal.
- Everything under the 2026-08-26 "Known issues" below is still open unless noted.

---

## 🎯 2026-08-26 — navigation, three screens rebuilt, and the review queue connected

### ▶ Next action — one command, then open the page

```bash
lsof -nP -iTCP:7766 -sTCP:LISTEN || ./family_archive.sh 813_mf
```
Then open **http://127.0.0.1:7766/documents** and expand a document type. The next piece of
work is further refinement of that screen — the user said so explicitly and has not yet
said which part.

### What shipped — PR #3, merged

- **Breadcrumbs.** Every drill-down used to discard the route that reached it; going
  Correspondents → a person → one email and pressing back landed you in the complete
  unfiltered email list. Eleven paths had this, six with no way back at all. Each view now
  carries a trail in its URL, drawn centrally in `render()` — per-page back links are
  exactly how one page ended up correct and the rest broken.
- **Overview is a working index.** It led with six count tiles that duplicated the left
  rail; now it opens on search.
- **Photos & Videos opens on a photograph** taken on today's date, suppressed the moment
  any filter is set.
- **Documents is a statement of position.** Was ~538 buttons above a page named for
  thousands of documents, with one summary line that read as a finished score. Now: four
  figures, types sorted by what is outstanding, one candidate per row, and each candidate
  says **where the pipeline filed it** — the evidence every decision on the old screen was
  made without.
- **The review queue is reachable.** `/review?group=vital` already existed and is good;
  nothing linked to it. Documents now does, scoped to one type.
- Supporting: the PII rule in CLAUDE.md and `/ship`; the `/walk` click-path checker; a fix
  for a test WyeastCorp's own PII scrub broke.

### History was rewritten on 2026-08-26

Upstream force-pushed a cleaned `main`; this fork was rebuilt on top of it and force-pushed
too. **Any clone or branch older than that date is stale and will conflict** — re-clone
rather than merge. This fork reuses *upstream's* fake names (Alex Rendon, Dawn Merrick, …)
so one fictional person has one identity in both repos.

### Blocked

_Back-end and upstream items now live in **`BACKEND.md`** — the list for Richard.
Add there, not here; this section keeps only what blocks the next UI session._

- **GitHub has not purged the pre-rewrite commits.** Force-pushing moved the branch tips;
  it did not delete the objects. The old commits — and the real names and numbers in them
  — are still retrievable by SHA from **both** repos, because forks share an object store.
  - **On:** Richard (needs admin on both repos).
  - **Unblocked by:** submitting `MarkRock / Codebase / 1-github-support-request.md` from
    Google Drive. Context and the verification command are in `2-note-to-wyeast.md`
    alongside it.
  - **Until then, treat the data as public.** Verify current status with the
    `/repos/{owner}/{repo}/contents/...?ref=<old sha>` call in that note.

### Known issues, not yet filed

- The vital-documents panel still renders all types when a **category filter** is active,
  so filtering to a small category still scrolls past the whole checklist.
- **Conversations in "Most significant" carry no link target** from the ranker, so they
  cannot open their own transcript. They currently point at the Messages section by name,
  which is honest but weak. Fixing it needs a `conversation_id` on the ranked item —
  pipeline work, upstream, not this fork.
- **Duplicate vital candidates each need their own decision** (the same scan saved twice,
  three identical filing-acceptance emails). Collapsing them is pipeline work too.
- The **test fix for WyeastCorp** (`test_api_section_correspondents_search_and_sort`, a
  stale `?q=` after their rename) is merged here and should go back upstream.
- `UPSTREAM.md`'s local-modifications list is still incomplete — see CLAUDE.md,
  "Known drift".

### Where the design thinking lives

Two critique documents, each with a written critique and three full-fidelity alternatives
behind a switcher. They contain real case content, so they are deliberately **not** in this
repo — they are on the Desktop:
`overview-critique-2026-08-26.html`, `documents-critique-2026-08-26.html`.
The user chose variant **B** for Overview and Documents, and variant **A**'s opening for
Photos. The Documents critique also records what is still wrong with that screen, which is
the best starting point for the next round.
