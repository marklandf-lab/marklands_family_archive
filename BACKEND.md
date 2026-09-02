# Back-end items for Richard

Things this fork **cannot fix**, found while working on the family-facing UI.

This repo is the front end: `tools/family_archive.py` serves a case and
`report_assets/family/` draws it. Everything below is upstream of that — the
pipeline that builds a case, the config it reads, or repo administration. Adding
a screen here cannot change any of it.

**Rules for this file, same as STATE.md.** Newest first. No number that rots —
record *how to find out*, never the answer, because every count here was true for
about an hour on the day it was written. No real names, document titles, email
addresses or phone numbers: this repo is public and case content has leaked into
it before (CLAUDE.md, "What must never enter a commit").

---

## 1. GitHub still holds the pre-rewrite commits

**What:** Upstream force-pushed a cleaned `main` on 2026-08-26 and this fork was
rebuilt on top of it. Force-pushing moved the branch tips; it did not delete the
objects. The old commits — and the real names and numbers in them — are still
retrievable by SHA from **both** repos, because forks share an object store.

**Why it needs you:** requires admin on both repos. Nothing in a working tree
fixes it.

**Unblocked by:** submitting `MarkRock / Codebase / 1-github-support-request.md`
from Google Drive. Context and the verification command are in
`2-note-to-wyeast.md` beside it.

**Until then:** treat the data as public. Verify current status with the
`/repos/{owner}/{repo}/contents/...?ref=<old sha>` call in that note.

---

## 2. Conversation ids contain a colon, and colons do not survive delivery

**What.** A conversation id is `imessage:<hex>`, and the per-conversation JSON is
written to `output/metadata/messages/<id>.json`. A colon is illegal in a filename
on Windows and over SMB, so a case that reaches a Mac through either arrives with
that character rewritten. On 813_mf every one of the 569 files carries **U+F022**,
a private-use codepoint, where the colon should be.

**What it broke.** Every conversation in the Messages section — all 552 — failed
to open with "unknown conversation imessage:…", which reads as "this does not
exist" when the file is sitting right there under a name nobody could construct.
The fork now matches on the readable part of the name and works around it, but
that is a patch over damaged data, not a fix.

**Why it needs you.** Two candidate fixes and both are upstream:
- Stop putting a colon in the on-disk filename. The id can keep its colon; the
  file it is written to does not need to carry it. `wyeast/core/safe_names.py`
  already exists for exactly this and is not used here.
- Or make delivery/relocation aware of the rewrite, the way `rebase.py` is aware
  of relocated paths — a delivered case is already known to need fixing up.

The first is much cheaper and stops the problem at the source. Note that any case
already delivered stays broken either way until it is re-delivered, so the reader
tolerance is worth keeping regardless.

**Check it:**
```bash
ls ~/WyeastCases/813_mf/output/metadata/messages | head -3 | cat -v
python3 -c "import os;d=os.path.expanduser('~/WyeastCases/813_mf/output/metadata/messages');fs=os.listdir(d);print(len(fs),'files;',sum(1 for f in fs if any(ord(c)>127 for c in f)),'with a rewritten character')"
```

---

## 3. The audio classifier ignores an acoustic signal the pipeline already has

_Corrected 31 Aug 2026. An earlier version of this item said the missing signal
was acoustic. It is not missing — it was computed and never consulted._

**What.** Each recording's category comes from `llama3.1:8b` reading the
transcript. Two measurements: of the 47 recordings that exist as the same audio
in two file formats, **17 (36%) got a different category for each copy**; and
**all 26 recordings filed as "voicemail" are, on inspection, songs.**

**Why the transcript cannot settle it.** Much of this audio is *practice
recordings* — takes of songs with a lot of banter before, during and after. A
transcript of one reads like people talking, because people are talking. No
amount of prompt tuning separates a rehearsal from a conversation on text alone.

**The signal already exists.** The `audio_events` stage runs `panns-cnn14` over
every recording and writes per-file labels to
`output/metadata/audio_events_index.json` — 1,051 of 1,051 have them on this
case. **All 26 of the mislabelled voicemails carry "Music" as their top acoustic
label, scoring 0.76 to 0.98.** The pipeline knew. Nothing asked it.

On a crude comparison of those stored labels (best music-ish score against
Speech), **456 recordings the LLM filed as speech carry music-dominant audio** —
including 339 of 408 voice memos and 87 of 121 personal recordings. That crude
rule is not being proposed as the rule; the point is that two signals disagree
on a large fraction of the collection and only one of them is read.

**The fix.** Have the audio classification consult
`audio_events_index.json`. No new model, no new stage, no re-encoding — the
compute is already spent and the answers are already on disk. How to combine
them is yours to design; a floor where a strong music label vetoes a speech
category would fix the visible damage.

Still worth doing alongside it:

* Classify once per *recording* rather than per file, so two encodings cannot
  disagree with each other.
* Make "nothing was transcribed" a flag rather than a category. `non_speech` is
  exactly the set with no transcript — it reports a transcription outcome while
  sitting among content types, and holds 173 acoustically music-dominant files.

**Check it.** The two files record different path roots (the case was
relocated), so this joins them on filename; a handful of recordings share a
basename, which moves the totals by one or two. Enough to see the shape, not to
quote to the decimal:
```bash
python3 - <<'PY'
import json,os
from collections import Counter,defaultdict
md=os.path.expanduser('~/WyeastCases/813_mf/output/metadata/')
ev={os.path.basename(r['file']):{l['label']:l['score'] for l in r.get('top_labels') or []}
    for r in json.load(open(md+'audio_events_index.json'))}
tab=defaultdict(Counter)
for a in json.load(open(md+'case_summary.json'))['audio_classifications']:
    d=ev.get(os.path.basename(a.get('file') or ''))
    if not d: continue
    m=max([d.get(k,0) for k in ('Music','Singing','Musical instrument','Guitar')])
    tab[a.get('category')]['music' if m>d.get('Speech',0) else 'speech']+=1
for c,n in tab.items(): print(c, dict(n))
PY
```

## 4. The pipeline never worked out whose mailbox it is

**What:** `owner_email_addresses` is empty in the case's `case_config.json`, and
`output/metadata/email_triage_summary.json` records `owner_source: "none"`. So
nothing upstream knows which addresses belong to the account holder.

**What it breaks:** sent/received attribution in `correspondent_frequency.json`
is unreliable, and any "who did they correspond with" view is topped by the
account holder themselves.

**What the front end did instead:** the Emails page now *infers* the owner from
what share of threads an address appears in, and names the addresses it guessed
on screen so a wrong guess is visible rather than silent. That is a workaround,
not a fix.

**The fix:** set `owner_email_addresses` in the case config and re-run the email
stages, or make the auto-detection actually run and record its answer.

**Check it:**
```bash
python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/WyeastCases/813_mf/case_config.json')))['owner_email_addresses'])"
python3 -c "import json,os;d=json.load(open(os.path.expanduser('~/WyeastCases/813_mf/output/metadata/email_triage_summary.json')));print(d['owner_addresses'], d['owner_source'])"
```

---

## 5. A dissolution decree is a vital document with nowhere to go

_Updated 31 Aug 2026 with the case owner's decision._

**What.** The checklist searches a fixed list of 27 estate document types. A
dissolution-of-marriage judgment has no type of its own, so on this case one was
signed off under *Marriage certificate* — the nearest bucket, and the wrong one.

**The owner's call, which settles the question we were unsure about:** it IS a
vital document and it should have its own type. So it has NOT been dismissed. It
is deliberately parked under *Marriage certificate* as a placeholder, because
that is the marital-status bucket and nothing among the 27 fits better. The
estate report therefore still reports a marriage certificate this estate does
not have, and will keep doing so until there is somewhere correct to put it.

**What it breaks generally.** Any type the list omits keeps landing in whichever
neighbour is closest, quietly, and the checklist then reports that neighbour as
satisfied. This is not a reviewer error to be trained away — the reviewer was
right that the document mattered.

**The fix.** Add a dissolution / divorce judgment type. While in there, audit the
27 against what an estate actually needs; a trust instrument and a death
certificate are the two to check next.

**To see the current list:**
```bash
python3 -c "import json,os; print(sorted(json.load(open(os.path.expanduser('~/WyeastCases/813_mf/output/metadata/vital_doc_candidates.json'))).keys()))"
```

---

## 6. A vital match has no way to say "here, and only here"

**What.** A vital match is a (document, type) pair, and one document can be a
candidate under several types at once. There is no way to express either of the
two things a reviewer actually wants:

* **"This belongs here and nowhere else."** Reassigning moves the clicked pairing
  and leaves the others; dismissing removes the document from every type it
  matched. Neither narrows the set. On this case, moving a draft will out of
  *Property deed* into *Will / testament* left it appearing TWICE under Will /
  testament — once reassigned, once as the original pending candidate — because
  nothing merges the two.

* **"This genuinely belongs in two places."** Sometimes right: a will that also
  functions as a letter of instruction, a POA that is also the evidence for
  something else. The model cannot record that as a deliberate decision, so a
  legitimate double is indistinguishable from an unresolved duplicate.

**What it breaks.** The counts. 183 candidate decisions covered 164 distinct
documents on this case, and a reviewer cannot tell which of those doubles are
real. It also makes dismiss dangerous in a way the UI had to be taught to warn
about (now done in the fork: the row names the other types and dismiss confirms
first).

**The fix, roughly.** Some way to resolve a pairing that is narrower than
dismiss — "not this type" as distinct from "not a vital document" — plus a way to
mark a multi-type match as intentional so it stops reading as an error. A
reassign that merges into an existing pairing rather than sitting beside it would
remove the duplicate case entirely.

**Check the current shape:**
```bash
curl -s localhost:7766/api/documents | python3 -c "import json,sys;from collections import Counter;t=json.load(sys.stdin)['vital_docs']['targets'];c=Counter(i['path'] for x in t for i in x['items']);print('documents in >1 pairing:', sum(1 for v in c.values() if v>1), 'of', len(c))"
```

---

## 7. Every near-miss list is truncated, on every type

**What:** the embed stage retrieves at most `vital_per_target_k` candidates per
target before LLM confirmation. On this case that cap is set in the case config
and **every** type reached it — so matching documents past the cap were never
retrieved, and the near-miss total the UI reports is a floor, not the field.

**Why it matters:** the release gate will not clear until every near-miss is
reviewed. Reviewing all of them clears a gate over a set that is known to be
incomplete.

**The fix:** raise the cap and re-run the vital-doc stages, or make retrieval
adaptive per target.

**Check it:** the `per_target_k` and `near_miss_capped` fields on
`/api/documents` → `vital_docs`, against `vital_per_target_k` in the case config.

---

## 8. Nothing can tell a handwritten note from a printed one

**What.** The archive holds 1,221 photographs and screenshots OF documents —
pictures of paper, browsable only under Document Photos. Some are handwritten
notes. Nothing in the pipeline can say which.

Four separate signals, and not one of them separates the two:

* **CLIP** covers all 1,221, but its label is literally
  `"scanned document or handwritten letter"` — a single category that merges the
  distinction. It cannot split what it was never asked to separate.
* **llava** (the vision model) ran on only **177 of the 1,221**, and **none** of
  those 177 descriptions mentions handwriting, cursive or a pen.
* **OCR never touched them.** Zero of the 1,221 appear in `ocr_index.json`, so
  there is no text and no `text_kind`.
* **`text_kind` exists and is the right field** — it is on every document row —
  but it reads `"printed"` for all 4,643 documents in the index. Either the
  handwriting detector never fires or it is not wired up. Worth checking on its
  own account: a case with real handwritten letters would currently report none.

Filenames do not rescue it either: 1,010 of the 1,221 are camera-roll UUIDs, so
even the crude screenshot-versus-photograph split lands on only about a sixth.

**What it costs.** Handwritten notes are among the most personal things in an
estate, and they are the least findable — pooled into 1,221 undifferentiated
thumbnails with no text, no summary and no way to filter.

**The fix.** Cheapest first:

* Run the OCR stage over the scanned images as well as the documents. It already
  computes `text_kind`; it simply never sees these files.
* Or split the CLIP label in two, so the classifier is at least asked the
  question.
* And check why `text_kind` is uniformly `printed` across the whole document
  corpus — that looks like a detector that is not running.

While in there: **llava descriptions exist for 177 of these images and are not
surfaced anywhere.** Another case of the pipeline computing something nothing
reads (see the pattern note below).

**Check it:**
```bash
python3 -c "import json,os;md=os.path.expanduser('~/WyeastCases/813_mf/output/metadata/');ocr=json.load(open(md+'ocr_index.json'));from collections import Counter;print('text_kind across the OCR index:',Counter(e.get('text_kind') for e in ocr))"
```

---

## 9. Five document categories have no sub-taxonomy at all

**What.** `financial` and `legal` are second-passed into subcategories and their
documents are delivered into folders. The other five — `creative_writing`,
`medical`, `personal_correspondence`, `recipe`, `miscellaneous` — are not, so
each browses as one flat list.

**What it costs.** `miscellaneous` is 729 documents with no structure at all,
which is the same as no filing. `creative_writing` is 176 documents that turn
out to be almost entirely tabletop role-playing character sheets — a real
collection, invisible as one, sitting in a bucket named for something else.

**The fix.** A `<category>_subcategories` list in the case config for the
categories that warrant one, following the pattern `financial_subcategories` and
`legal_subcategories` already set. Worth looking at what is actually in
`miscellaneous` first — a 729-document catch-all usually means the top-level
taxonomy is missing a category, not that those documents are miscellaneous.

The front end deliberately does NOT invent these. It shows the pipeline's own
filing and nothing more, so a reader sees what the archive decided rather than a
second opinion layered on top of it.

**Check it:**
```bash
curl -s localhost:7766/api/documents | python3 -c "import json,sys; [print('%-26s %5d  subcategories: %d' % (c['category'], c['count'], len(c['subcategories']))) for c in json.load(sys.stdin)['index']]"
```

---

## 10. Duplicate vital candidates each demand their own decision

**What:** the same document saved twice, and several byte-identical notification
emails, arrive as separate candidates. Each needs its own sign-off, inflating the
undecided count and the work behind the release gate.

**The fix:** collapse obvious duplicates before the checklist — the dedup summary
already exists in `output/metadata/collect_dedup_summary.json`; the vital-doc
stage does not consult it.

---

## 11. Ranked conversations carry no link target

**What:** items in the Overview's "Most significant" list have no
`conversation_id`, so they cannot open their own transcript. The UI points at the
Messages section by name instead — honest, but a dead end for the reader.

**The fix:** have the ranker emit `conversation_id` on ranked conversation items.

---

## 12. Two front-end fixes that should go back upstream

Both are merged here and are not upstream. Neither is urgent; both are small.

- **The vital-doc row now carries the document's summary.** The pipeline already
  writes a plain-language summary for every classified document; the checklist
  row that asks an examiner to certify what a document *is* never showed it. See
  PR #5 in this fork — `_vital_doc_summaries` / `_vital_summary_for` in
  `tools/_archive_data.py`, plus the audience gate that goes with them.
- **A correspondents search test** left broken by upstream's own PII scrub (a
  stale query parameter after their rename). Fixed here.

---

## 13. The archive index lists files a delivered copy does not hold

_Appended rather than inserted at the top: STATE.md and CLAUDE.md both cite
"BACKEND.md #5" by number, and renumbering would break those references._

**What:** `archive_map.json` maps every delivered item to a canonical path under
`output/archive/`. On a delivered copy some of those entries point at a path that
holds no file. A missing canonical is *expected* where the examiner banished the
item — a banish moves the canonical to `output/family_banished/` by design. It is
not expected for items **released from the sensitive-content quarantine**: the
action log records the release writing the item into `output/archive/`, the map
agrees with that destination, and no file is there.

**What it breaks:** the map is the case's own statement of what was delivered, so
anything that trusts it is wrong by exactly that many items. The front end can
only decline to draw the row — this fork now drops any scanned row whose canonical
is missing, so these no longer appear as broken tiles on Document Photos. That
hides the symptom; the index is still claiming a file the delivery does not carry.
A reader is told the archive holds something it does not.

**The fix (upstream):** either carry the bytes into the delivery when an item is
released, or, where the content is withheld from a copy on purpose, drop the
`archive_map` entry with it, so the index never names a file the copy lacks.

**How to check it:**

```bash
# archive entries whose file is not in this copy
python3 - <<'PY2'
import json, os
CASE = os.path.expanduser('~/WyeastCases/813_mf'); REC = '/data/cases/813_mf'
am = json.load(open(CASE + '/output/metadata/archive_map.json'))
reb = lambda p: p.replace(REC, CASE, 1) if p.startswith(REC) else p
ent = am['entries']
gone = [k for k, v in ent.items() if not os.path.exists(reb(v))]
print(len(gone), 'of', len(ent), 'archive entries name a file that is not here')
PY2

# then split those into "explained by a banish" and "not explained":
#   grep the basename in output/metadata/family_actions.ndjson — a `banish`
#   accounts for a missing canonical, a `release` does not.
```

---

## How to add to this file

Put the newest item at the top of the numbered list and renumber, or append and
say so — but keep the shape: *what*, *what it breaks*, *the fix*, and a command
that re-derives the current state. An item without a way to check it will be
stale before it is read.
