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

## 2. Audio classification is unreliable, and this corpus is why

**What.** Every recording carries a classifier category. Measured 31 Aug 2026:
of the 47 recordings that exist as the same audio in two file formats, **17
(36%) got a different category for each copy**; and **all 26 recordings
classified as "voicemail" are, on inspection, songs.**

**Why it is hard.** Much of this audio is *practice recordings* — takes of songs
with a lot of banter before, during and after. A transcript of one reads like
people talking, because people are talking. A classifier reasoning from
transcript text alone cannot tell a rehearsal from a conversation, and no amount
of prompt tuning will fix that. Treat it as a corpus shape the pipeline has not
met before, not a regression.

**What it breaks.** The Recordings section groups by this category, so the
grouping inherits the error. A "Voicemail" group promises someone's actual
voice — often the voice of the person who died — and holding songs instead is
worse than no grouping. Separately, `non_speech` is not a content judgment: its
members are exactly the set with no transcript, so it reports a transcription
outcome while sitting among content types.

**The fix.** The missing signal is acoustic, not textual — music detection,
harmonicity, beat regularity would settle most of it before the transcript is
consulted. Two cheap wins needing no new model: classify once per *recording*
rather than per file so two encodings cannot disagree; and make "nothing was
transcribed" a flag rather than a category.

**Check it:**
```bash
curl -s localhost:7766/api/recordings | python3 -c "import json,sys,re; from collections import defaultdict; rows=json.load(sys.stdin); g=defaultdict(set); [g[re.sub(r'\.[^.]+$','',r['name']).lower()].add(r['category']) for r in rows]; print('same audio, two formats, two answers:', len([k for k,v in g.items() if len(v)>1]))"
```

---

## 3. The pipeline never worked out whose mailbox it is

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

## 4. The vital-document checklist has no home for a dissolution decree

**What:** the checklist searches a fixed list of document types. A
dissolution-of-marriage judgment is an estate-relevant document with no type of
its own, so the nearest bucket is the marriage certificate — and on this case one
was duly signed off there. The screen was right that a document mattered; the
list had nowhere correct to put it.

**The fix:** a type for dissolution / divorce judgments in the vital-doc target
list, and a look at what else an estate needs that the 27 do not name (a trust
instrument and a death certificate are worth checking for).

**Check the current list:**
```bash
python3 -c "import json,os;print(sorted(json.load(open(os.path.expanduser('~/WyeastCases/813_mf/output/metadata/vital_doc_candidates.json'))).keys()))"
```

---

## 5. Every near-miss list is truncated, on every type

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

## 6. Duplicate vital candidates each demand their own decision

**What:** the same document saved twice, and several byte-identical notification
emails, arrive as separate candidates. Each needs its own sign-off, inflating the
undecided count and the work behind the release gate.

**The fix:** collapse obvious duplicates before the checklist — the dedup summary
already exists in `output/metadata/collect_dedup_summary.json`; the vital-doc
stage does not consult it.

---

## 7. Ranked conversations carry no link target

**What:** items in the Overview's "Most significant" list have no
`conversation_id`, so they cannot open their own transcript. The UI points at the
Messages section by name instead — honest, but a dead end for the reader.

**The fix:** have the ranker emit `conversation_id` on ranked conversation items.

---

## 8. Two front-end fixes that should go back upstream

Both are merged here and are not upstream. Neither is urgent; both are small.

- **The vital-doc row now carries the document's summary.** The pipeline already
  writes a plain-language summary for every classified document; the checklist
  row that asks an examiner to certify what a document *is* never showed it. See
  PR #5 in this fork — `_vital_doc_summaries` / `_vital_summary_for` in
  `tools/_archive_data.py`, plus the audience gate that goes with them.
- **A correspondents search test** left broken by upstream's own PII scrub (a
  stale query parameter after their rename). Fixed here.

---

## How to add to this file

Put the newest item at the top of the numbered list and renumber, or append and
say so — but keep the shape: *what*, *what it breaks*, *the fix*, and a command
that re-derives the current state. An item without a way to check it will be
stale before it is read.
