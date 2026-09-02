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

## 🎯 2026-09-02 (end) — Vital documents reads as a reference until you switch Reviewing on

### ▶ Next action

```bash
lsof -nP -iTCP:7766 -sTCP:LISTEN || ./family_archive.sh 813_mf
```

### What shipped

The complaint was that the archive "feels like it is in a perpetual review state".
It did, and the panel's own source already said it should not: *"Two jobs live here
and they are not the same job… This screen is built for the second."* It was not —
every structural choice on it was made for reviewing.

Five collisions, now all gated behind one switch on the page:

| | Reviewing OFF (default) | Reviewing ON |
|---|---|---|
| Order | the checklist's own | sorted by outstanding work |
| Numbers | have a document · signed off · still empty | + undecided · near-misses unreviewed |
| Sentence | one calm line, linking to the queue | the release-gate reminder |
| Columns | Document type · Documents | + Signed off · Undecided · Near-misses |
| Rows | evidence and state | four decision verbs, near-miss drawer |

Off is the default. A row with no buttons says *"Turn Reviewing on to decide about
this document"*, so the switch is discoverable from the one place its absence is
felt. Remembered in `localStorage` (`wy.reviewing`) — the first use of it in this
codebase, wrapped in try/catch.

### ⚠️ This is meant to be deleted

The likely destination is **Vital Documents as a section with two pages under it**
(the checklist, and review) rather than one page in two modes — a mode you cannot
see is this idea's real weakness, and the rail already carries three separate
review entries (Review queue, Guided review, Junk review) that confuse more than
these two do.

So the switch is deliberately thin: one flag, `reviewOn()`, read in a handful of
places in `family.js` and nowhere else. **When the two-page split happens, delete
it — do not grow it.** Keeping both a mode and a navigation split doing the same
job is worse than either.

What it is really for: finding out whether anyone ever browses with Reviewing off.
If it is always on, the split is not worth building.

### Scope

The switch governs the Vital Documents page only. It sits on that page rather than
in the rail on purpose — a control that quietly re-skins the whole archive is the
failure mode worth avoiding. Other surfaces (the Documents lists, Overview) still
carry their own review affordances.

---

## 🎯 2026-09-02 (late night) — discarding documents, and keeping your place

### ▶ Next action

```bash
lsof -nP -iTCP:7766 -sTCP:LISTEN || ./family_archive.sh 813_mf
```

Open: the **review-vs-browsing** conflict — the archive feels like it is in a
perpetual review state. A design pass was requested (mockups before code); it is
the next piece of work, not started.

### Two bugs, both reported from real use

**Discarding documents did nothing and said it worked.** The selection bar's
Discard called `banish`, which moves bytes and refuses anything outside
`output/archive/`. Every document is outside it. A batch banish **skips** per-item
failures and still returns `ok`, so the response was `count: 0, skipped: N`, the UI
toasted "Discarded N item(s)", cleared the selection — and the documents were all
still there. Nothing was ever written, which is why the audit log had no trace.

Fixed in three places, and the third is the general one:

- `doc/discard` verb + `doc_discarded` overlay, filtered inside `document_rows`.
  That builder is the SOLE source of the category lists, their counts **and** the
  search index, so one filter removes a discarded document from all three.
  `build_fts` now reads the overlay too, or search would still find it.
- The selection bar routes a document selection to that verb. Media still banishes.
- **`doVerb` now reports what a batch actually did.** Any batch verb answering
  `{count, skipped}` was reported as a flat success. That is how a reviewer comes
  to believe the archive is dropping their decisions — it is worth keeping.

**A decision collapsed the checklist and lost your place.** The panel is rebuilt
from the top on every verb, and the only thing it remembered was the *near-miss
drawer*. Working a type's candidates — the ordinary case — recorded nothing, so
every decision dropped you back to the collapsed checklist. `VITAL_OPEN` now tracks
the expanded type independently.

### Worth knowing

- **Validate against the index, not the case directory.** The discard verb checks
  the src is a known document rather than checking containment: a document whose
  recorded path did not relocate still has a row (rows come from the index, not the
  disk), and a path check would have made exactly those rows undiscardable. Failing
  closed is right on ambiguity, not on a legitimate action.
- The fixture's documents are at `/work/docs/...`, outside the case dir. Any
  path-shaped check on documents will behave differently in tests than in a real
  case; check both.

---

## 🎯 2026-09-02 (night) — the review row answers the question it asks

### ▶ Next action

```bash
lsof -nP -iTCP:7766 -sTCP:LISTEN || ./family_archive.sh 813_mf
```

Design directions were mocked up first and the choice was made from them —
**Direction A, with one idea borrowed from B**. The mockups (all three, with the
diagnosis) are an Artifact; ask the user for the link if the next decision here
needs the reasoning.

### What shipped

**A new verb, because one answer did not exist.** "Yes, this is it" ruled on ONE
document type; "No" was recorded against the DOCUMENT and dropped it from every
type it matched. Under a heading reading *Will / testament*, "No" looked like
"not a will" and meant "not a vital document at all". A reviewer who knew a file
was not a will — but not what it was instead — had to reject it everywhere or
reassign it to a guess.

- `vital/not-type` — rejects ONE pairing, keyed by the `target::path` item id.
  Overlay `vital_doc_not_type`, reversible, audited, never touches
  `vital_doc_confirmed.json`. Confirm and promote clear it (opposite rulings).
- **One vocabulary everywhere.** Candidate rows, near-miss rows and the queue used
  to say *Yes, this is it / No / Another type…*, *Mark as vital / Not a vital
  document / Reassign…* and *Confirm / Dismiss / Reassign…* — three names for the
  same three actions. All three now read: **Yes, this is it · Not this type · Not
  a vital document · It's a different type…** Hover text carries the scope.
- **The type picker keeps the current type, pre-selected** (the idea borrowed from
  Direction B). Picking it signs the item off instead of being a no-op the server
  refuses — reassign and confirm are the same assertion, *what is this?*

### Known gap, deliberately

Near-miss rows get no "Not this type". A near-miss is not claimed as that type in
the first place, and suppressing one per type would need a second overlay keyed by
(target, path) — near-misses are computed from `vital_doc_candidates.json`, not
from confirmed items. If reviewers ask for it, that is the shape it takes.

### Testing this without writing to the case

The UI was driven with `window.fetch` stubbed to intercept POSTs, so the wiring
was verified (right endpoint, right payload) with nothing reaching the server; the
verb itself is covered by unit tests against a real case tree, including undo. The
audit log did not move. Use the same trick rather than clicking a real verb.

---

## 🎯 2026-09-02 (late) — the reassign scope dialog says what it does

### ▶ Next action

```bash
lsof -nP -iTCP:7766 -sTCP:LISTEN || ./family_archive.sh 813_mf
```

### What shipped

Reassigning a vital document that matched more than one type asked **"Reassign in
how many categories? — All N categories / Just this one"** over a bare count. It
never said *which* categories, and "just this one" could be read as the type you
are moving OUT of or the one you had just picked to move INTO. Those are opposite
actions and nothing on screen settled it.

It now names them: which type you are looking at, which others hold the same
document, where it is going, and — the part that actually distinguishes the two
choices — **what each one leaves alone**. The narrow choice is the default now;
Enter used to fire the one that rewrote every category at once.

The queue's own reassign modal had the same ambiguity in its "Apply to" select
("Only this item"). It says "Only its *[type]* entry" now. It cannot name the
other categories — a pager item does not carry the document's full match list —
so it names the one it can.

### The dialog only appears when the answer changes something

"All of them" differs from "just this one" ONLY for categories that are neither
the one being moved out of nor the one being moved into. Every multi-category
document on 813_mf is in exactly two — and all 16 are the same pair (Buy-sell
agreement + Business operating agreement) — so moving between those two, which is
the obvious thing to do with them, now asks nothing at all and simply moves it.
Before, it asked a question whose two answers had the same result and whose text
contradicted itself ("becomes Business operating agreement … stops being a
candidate under Business operating agreement").

### The semantics, since they are not guessable from the UI

A vital match is per **(document, type)** pair. One file can be a candidate under
several types at once.

- `scope: "single"` — moves only the pairing you are looking at. The document's
  other type entries are untouched.
- `scope: "global"` — moves **every** pairing of that document to the new type, so
  it stops being a candidate under the others.

`verb_reassign_vital` in `tools/family_archive.py` is the authority; it is a pure
`family_decisions` overlay (`vital_doc_target`), reversible and audited, and never
touches `vital_doc_confirmed.json`.

### Testing this without writing to the case

Opening the target picker and the scope dialog fires nothing — the verb goes only
on the scope dialog's own buttons. Cancel out and the case is untouched. Verified
by count: the audit log did not move for either dialog.

---

## 🎯 2026-09-02 (evening) — Vital Documents is its own section

### ▶ Next action

```bash
lsof -nP -iTCP:7766 -sTCP:LISTEN || ./family_archive.sh 813_mf
```

Nothing half-done. Open candidates remain the email cuts in the 2026-08-31 entry.

### What shipped

**The 27-type checklist is a top-level section instead of a view inside
Documents.** It used to live at `/documents?view=vital`, and because it was a
branch of that page, every way *out* of it landed in the general document list —
you asked to stay in the checklist and got dumped into everything. Three routes
did it: the checklist's own back button, the review pager's escape, and the
Overview card.

- New page `vital` → `/vital`, served by its own section (`/api/vital`, checklist
  only). `/documents?view=vital` still works and `location.replace()`s to it, so
  old links and bookmarks land right and Back does not bounce between the two.
- **Documents is now "Other Documents"** in the rail, its heading, its eyebrow and
  its back button ("← All other documents").
- The review pager's escape goes to `/vital`, not `/documents`.
- The Documents index shows a *pointer* to the checklist, not a second copy of its
  stats — two places showing the same counts is how they end up disagreeing.

### ⚠️ "Other" is a promise that page cannot fully keep

Vital Documents is a cut **across** the archive, not a pile carved out of it. It
searches every document AND every email; the category list holds documents only.
So a will that arrived as an email is *only* in Vital Documents, and one that
arrived as a file is in **both**. The copy on both pages says so on purpose.

I nearly shipped a lead reading "everything except the estate's vital types",
which is simply false. If you edit that copy, keep the overlap visible — the
label already implies a separation the data does not have.

### Watch out

- `/api/vital` (the section) and `/api/vital/near-misses` (the drawer) are
  different endpoints. Dispatch is exact-match, so they do not collide — verified,
  but do not switch that to a prefix match.
- The family role sees this section too; it is not in `EXAMINER_ONLY`, and
  `vital_docs_data` gates the examiner-only columns internally.

---

## 🎯 2026-09-02 (later) — the vital queue now stops between candidates and near-misses

### ▶ Next action

```bash
lsof -nP -iTCP:7766 -sTCP:LISTEN || ./family_archive.sh 813_mf
```

Nothing is half-done. The open candidates are still the email cuts in the
2026-08-31 entry (`linked_by` first). Polish stays off the list.

### What shipped

**The review queue now tells you when you have left the candidates behind.**
Working the vital documents, there was no marker between the ones the pipeline
thought it had found and the weaker matches it did not confirm — the queue simply
carried on, so you could not tell which question you were being asked.

Two changes, one behavioural:

- **The queue is grouped by document type.** Each category's candidates now run
  straight into that same category's near-misses. It used to be every candidate of
  every type, then every near-miss of every type, so the single handover sat
  unmarked in the middle of the whole queue.
- **A card stops at every crossing, of which there are two kinds.**
  Candidates → near-misses within a type: "Finished reviewing candidates for
  *[type]*. Review its *N* near misses?" One type → the next: "Finished *[type]*.
  Next: *[type]* — *N* candidates to confirm." Both offer **Skip to next
  category**. A type with no candidates gets only the first kind, worded "*[type]*
  has no candidates" — that card already names the type, so announcing it twice
  would be noise. No verb button is on either card and no verb key fires from
  them — the item behind has not been read yet. Answering is remembered per
  crossing, so paging back does not re-ask.

**Terminology:** the user-facing label is **Vital documents** everywhere now. The
Documents heading said "Estate documents" for the same thing. Note the word
*estate* is still all over the code in an unrelated sense — "estate-derived text"
means text out of the case that must be escaped before it reaches the page. Do not
sweep those; they are a security note, not a label.

### How it was checked

`./run_tests.sh` and `./jscheck.sh` are necessary and not sufficient here, as ever.
Two new tests pin the grouping and fail against the old code. The queue was then
driven in a real browser: the stop lands at the right item, the right wording for
both the has-candidates and no-candidates cases, Skip jumps a whole type, Back
still works, the escape link out of the pager still lands on Documents.

**Driving the pager is safe only if you never touch a verb.** Advance, Back, Skip
and the handover buttons are all client-side; Confirm / Dismiss / Promote /
Reassign POST immediately and are real decisions on the real case.

### ⚠️ Someone else was working this case while I was in it

The audit log gained 55 entries today between 09:54 and 10:01 local — 39
`confirm_vital`, 15 `dismiss_vital`, 1 `reassign_vital`, in human-paced bursts.
They are not mine: nothing I did in the browser POSTs anything. Almost certainly
the user working the checklist in their own window.

Two things follow, and they will bite the next session too:

- **A count that moved is probably a person, not a bug.** Read the log before
  theorising; the timestamps are UTC and the Mac is UTC-7.
- **Restarting the server interrupts whoever is mid-review.** I restarted it twice
  to pick up code changes. Check `lsof -nP -iTCP:7766` and ask before killing it.

---

## 🎯 2026-09-02 — the broken Document Photos tiles, and what they turned out to be

### ▶ Next action

```bash
lsof -nP -iTCP:7766 -sTCP:LISTEN || ./family_archive.sh 813_mf
```

**No outright defect is open.** The broken-thumbnail one below is fixed and
verified in the running app. What is left is judgement calls — the ranked list in
the 2026-08-31 entry ("Email cuts NOT built", `linked_by` first) — so ask rather
than pick. Polish remains off the list; do not raise it.

### What shipped

**Document Photos no longer lists pictures it cannot show.** The page was drawing
broken-image icons for every item whose file had moved out from under it, and
counting them in the "N photographs and screenshots" header — so the count
promised more than the page could draw.

`scanned_image_rows` (`tools/_archive_data.py`) applied its can-this-be-shown check
only to the **family** role; the examiner got no check at all. Its sibling
`build_photo_universe` has had the right rule all along, with a comment saying it
drops moved-out files for *both* roles precisely so the examiner is not shown a
broken tile. This was that rule missing from one of the two builders — now applied
in both, so the row count and what is drawn agree.

### ⚠️ Correction to the entry below this one

The previous handoff recorded the cause as "a missing thumb-cache entry, not a
decode failure". **That was wrong, and it was never measured** — the thumbnail
cache has nothing to do with it. The picture files themselves were unreachable, so
there was nothing to build a thumbnail from. The extension breakdown in that entry
(mostly `.jpeg`) is real but irrelevant: file type had no bearing on it.

Read the audit log before theorising about a missing file — it says what happened
to each one:

```bash
tail -20 ~/WyeastCases/813_mf/output/metadata/family_actions.ndjson
```

The affected files split three ways there: items the examiner **banished** (their
canonical moves to `output/family_banished/` by design — the archive path is
*meant* to go dead), items **released** from the sensitive-content quarantine whose
bytes are not in this copy at all, and a few never acted on. Only the first group
is working as intended.

### What this is owed to someone else

**BACKEND.md #13** (appended, not inserted — STATE.md and CLAUDE.md cite "#5" by
number, so renumbering would break those references). The archive index names a
number of files a delivered copy does not hold. A banish explains a missing
canonical; a release does not. The front end can only decline to draw the row,
which it now does — the index is still wrong underneath. The item carries a
command that re-derives the current number; run it, do not trust a figure.

### Two things worth knowing before you verify anything

- **The Chrome extension has no permission for `127.0.0.1`.** Every browser tool
  call against the running app failed with the same error. Verification here went
  through the HTTP API and the render code instead. If you need to see the app on
  screen, grant the site in the extension first, or drive it by hand.
- **A path in this case does not exist on this machine.** Every pipeline index
  records `/data/cases/813_mf/...`, and the app rebases those on read
  (`wyeast/core/rebase.py`). Any script you write against `archive_map.json` or a
  sidecar **by hand** must rebase too — one that does not will report every single
  row as broken and read like a catastrophe. This cost real time this session.

---

## 🎯 2026-08-31 (evening) — email categorisation, and where the boundaries are

Everything through **PR #12** is merged. No open PRs, nothing unpushed.

### ▶ Next action

```bash
lsof -nP -iTCP:7766 -sTCP:LISTEN || ./family_archive.sh 813_mf
```

**Polish is explicitly off the list** — it was asked for three times, never
specified, and the user has since said to drop it. Do not raise it again.

There is no single obvious next task. The candidates, ranked, are in "Still
open" below; **27 broken thumbnails** is the only outright defect among them and
is where I would start. Everything else is a judgement call about what is worth
building, so it is worth asking rather than picking.

### What shipped this evening

- **Documents and Emails now say what each contains.** They had opposite
  inclusion rules and neither admitted it: the document list drops all 55,115
  emails, while the estate checklist ON that page searches them. So an email can
  be a *Will / testament* candidate on Documents and appear nowhere in the list
  below it. Both pages now say so.
- **An email can say it is on the estate checklist.** 300 of 21,988 threads —
  31 candidates, 269 near misses. Row chip, `?estate=candidate|near_miss|any`,
  and Estate in the break-down. Examiner-only.
- **Estate-rescued mail is marked.** 4,759 threads that triage discarded and the
  estate keywords pulled back. The family index does not contain them, so a
  reviewer reading one is reading mail that will never reach the family.
  `?rescued=1`, Audience in the break-down.

### Email cuts NOT built — measured, ready to pick up

Everything below is derivable from data already on the thread rows:

- **`linked_by`** — 1,015 threads were assembled by *guessing*: same subject
  within a 14-day window, rather than real mail headers (8,720 by headers,
  12,253 single messages). Those 1,015 may not be real threads. A data-quality
  cut rather than a browsing one, but it is the field that explains a
  conversation that looks wrong.
- **Conversation size** — 16,349 one-to-one against 5,639 group.
- **Thread length** — 12,253 single messages, 7,924 short (2–5), 1,811 long (6+).

Both of the last two are real but weak: they say little about what a thread *is*.

**Not available without pipeline work:** attachments. Thread rows carry no
attachment field at all — only 41 of 21,988 records mention one anywhere — so
"emails with attachments" cannot be built here.

### Still open

- ~~**27 of the 1,221 Document Photos thumbnails 404**~~ — **FIXED 2026-09-02,
  and the cause given here is wrong: it was nothing to do with the thumb cache.
  See the 2026-09-02 entry above.** Left in place as history, not as a lead.
  (2.2%), measured
  1 Sep 2026 by requesting every one. All 404 — a missing thumb-cache entry, not
  a decode failure. Mostly `.jpeg` (21), not the `.heic` I had assumed twice
  before measuring. The only outright defect on this list. Re-measure before
  starting:
  ```bash
  python3 - <<'PY'
  import json,urllib.request,urllib.parse
  rows=json.load(urllib.request.urlopen('http://127.0.0.1:7766/api/correspondence'))['scanned']['rows']
  bad=[r['name'] for r in rows if not _try(r)] if False else []
  for r in rows:
      u='http://127.0.0.1:7766/thumb?src='+urllib.parse.quote(r['id'],safe='')
      try: urllib.request.urlopen(u,timeout=5).read(1)
      except Exception: bad.append(r['name'])
  print(len(bad),'of',len(rows),'thumbnails fail')
  PY
  ```
- **`linked_by`** — 1,015 threads assembled by guesswork rather than mail
  headers. The best remaining email cut, though a data-quality one.
- The **27-type checklist** sits at `/documents?view=vital`, one click in from the
  Documents index. Put it back on the landing page if it grates in use.
- **BACKEND.md** is the pipeline list, current through today. A copy for the
  upstream maintainer is in the shared Google Drive folder — ask the user for the
  link, it changes on every edit.

---

## 🎯 2026-08-31 — Reports, Documents index, audio kinds, accounts, Messages, and the sign-offs cleared

**Everything is merged.** PRs #5, #6, #7 and #8 are all in `main`. No open PRs, no
unpushed branches, working tree clean. Any entry below this one that says "not
yet pushed" is describing a state that no longer exists — read them as history.

### ▶ Next action

```bash
lsof -nP -iTCP:7766 -sTCP:LISTEN || ./family_archive.sh 813_mf
```

**Polish, and you need to ask what kind.** The user has said twice that polish is
wanted and has not said what — spacing, wording, table density are guesses.
Get that steer before starting; do not pick for them.

Nothing else is blocked. The decision that was waiting at the top of this file
all day has been made and carried out (below).

### ⚠️ The eleven wrong sign-offs are DONE — do not redo them

This was the standing item and it is finished. On the real case, audited and
reversible, decisions file backed up first:

- **7 dismissed** under *Property deed / title* — a zoning letter, a research
  response, five emails about deed research.
- **2 reassigned** — a draft will to *Will / testament*, a durable power of
  attorney to *Power of attorney*.
- **1 left signed off** — the ALTA title policy, which is genuinely a deed.
- **1 left in place deliberately** — the dissolution judgment under *Marriage
  certificate*. It IS a vital document (the owner's call) and there is no correct
  type for it, so it is parked as an explicit placeholder. **The estate report
  therefore still reports a marriage certificate this estate does not have.**
  That is known, intended, and waiting on BACKEND.md #5.

Signed off went 14 → 7; undecided unchanged at 169. Re-derive rather than trust
those: `/documents?view=vital`.

### What shipped today

- **Reports** (second in the rail): estate, family and pipeline reports, all
  print-styled. `/reports`.
- **Documents as an index** — estate checklist as one branch, categories as
  siblings. `legal` subcategories were being discarded by the row builder and are
  now recovered from the delivery path (nine of them).
- **Recordings** in six kinds, collapsed.
- **Online Accounts** finds ~38 services from the mail rather than the
  pipeline's 23 social domains.
- **Messages was completely broken** — every conversation failed to open — fixed.
- **Correspondence → Document Photos.** Its "typed" list was an exact duplicate
  of Documents → Personal correspondence; its real content is 1,221 photographs
  of documents that appear nowhere else in the archive.
- **The vital row now says when a document is filed under other types**, and
  dismiss asks first — it is keyed by path and silently removes the document from
  every type it matched.
- **176 document photos gained the vision model's description**, which had never
  been rendered.
- Five screens stopped overclaiming: the Overview's "Still missing", the Emails
  band headings, the vital-doc row, the accounts list, the pipeline report's
  "surfaced" column.

### Open

- **Polish** — asked for, unspecified. Ask.
- The **27-type checklist moved** off the Documents landing to `?view=vital`.
  One extra click on the main review surface; put it back if it grates.
- A handful of **Document Photos thumbnails fail to render** and fall back to the
  filename, mostly `.heic` and `.png`. Pre-existing, not investigated.
- **BACKEND.md is the pipeline list** and is current through today (twelve
  sections, nine numbered items). A copy for the upstream maintainer lives in the
  shared Google Drive folder — ask the user for the link; the file link changes
  on every edit because the Drive tooling cannot edit a document in place.

### Two things worth carrying forward

- **The pipeline computes signals nothing reads.** Five found this session:
  acoustic labels for every recording, a dedup summary, per-document summaries,
  `legal` subcategories, and vision-model descriptions for 176 images. Four of
  the five turned up by accident. Before building something to produce a signal,
  check whether it is already on disk.
- **A count is not a count.** Messages against conversations, decisions against
  documents, examined against browsable. Several near-misses came from comparing
  two units. When a percentage looks surprising, check the denominator first.

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
