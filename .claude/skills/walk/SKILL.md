---
name: walk
description: Click every drill-down path in the Family Archive in a real browser and report anywhere the visitor cannot get back. Use when the user says "walk the archive", "/walk", "check navigation", after any change to family.js/family.css, or when they report being dumped somewhere unexpected after clicking back.
---

# /walk — check that every drill-down has a way back

## Why this exists

The archive's navigation bugs are invisible to `./run_tests.sh`. Nothing is broken in the
code's own terms — every function returns what it promises. It is only broken when a person
clicks four times and lands somewhere useless. The bug that motivated this skill:
Correspondents → Alex Rendon → one email → "back" dumped you in the *complete* email list,
having silently discarded both the correspondent and the section you started from.

It recurred because each instance got patched alone. People got a back link that remembered
its filter; Events, Collections and the video views got nothing. **Check the pattern, not the
page you were told about.**

## How to run it

1. Serve the case. `lsof -nP -iTCP:7766 -sTCP:LISTEN` first — an instance is often already
   running, and a second one against the same case interleaves curation writes. Reuse it.
   Otherwise: `./family_archive.sh <CASE_ID>` (cases default to `~/WyeastCases`).
2. Drive it with the `claude-in-chrome` tools. Not curl: the whole UI is client-rendered, so
   the served HTML is an empty shell and tells you nothing.
3. Walk each path below. At every step read the breadcrumb across the top.

### What a pass looks like

- Every view you reached by clicking shows a trail: `Section › … › where you are`.
- Every crumb but the last is a link, and clicking crumb *i* returns you to that view **with
  the filter/tab/query it had** — not the top of its section.
- Going back up and then down again does not grow the trail (`Places › Places › Places`).
- A view opened cold from a pasted URL shows no trail. That is correct: there is no history
  to show, and inventing a plausible one would be a lie. It should still show the old single
  back link where one existed.

### Clicks that must be walked

| From | Click | Must land as |
|---|---|---|
| Correspondents | a person | `Correspondents › <name>` |
| ↳ that list | one email | `Correspondents › <name> › <subject>` |
| Emails | a thread | `Emails › <subject>` |
| Messages | a conversation | `Messages › <who>` |
| Recordings | a recording | `Recordings › <name>` |
| People | a person | `People › <name>` |
| ↳ person | their videos | `People › <name> › Videos of <name>` |
| Events | an album | `Events › <album>` |
| Collections | a collection / Favorites | `Collections › <title>` |
| Places (Trips tab) | a trip | `Places › <place>` |
| Places (Places tab) | a venue | `Places › <venue>` — and the crumb must return to the **Places** tab |
| Timeline | a day | `Timeline › <date>` |
| Search | any hit | `Search: <query> › <title>` — the crumb must return **with the query** |
| Documents / Review | a vital doc that lives in an email | `<section> › <subject>` |

### The two redesigned screens

**Overview** is a working index: search first, then a two-column layout — what is in
the archive / Most significant on the left, Vital documents / On this day on the right. It
must show **no big count tiles**; those duplicated the left rail and were the whole reason it
was rebuilt. Every figure on it comes from `/api/overview`, `/api/places` or
`/api/transparency` — if you find a number on that screen you cannot trace to a payload, that
is a bug, not a rounding choice.

**Photos & Videos** opens with a hero photograph taken on today's date, then a strip of the
rest of that day across the years, and only then the title and filter bar. Two things to
check every time:
- The hero must come **above** the filter controls. Those controls render into the sticky
  `.pagehead`, so anything appended after `head()` lands below them.
- The hero must **vanish** the moment any filter is set (`?scene=`, `?event=`, `?person=`,
  a date range, an album, a collection…). See `heroSuppressed()`. A visitor who has narrowed
  to an album asked a question; a large unrelated photograph on top of the answer is an
  obstacle. Load `/photos?scene=wedding` and confirm there is no hero.

### The three that break quietly

- **Filter changes.** On a drilled-into view, change a dropdown. Every filter control calls
  `setQ()`, which rebuilds the whole URL from a hand-maintained list in `encodeURL()`. A
  param missing from that list is *deleted* the first time the user touches a control — which
  is how the trail used to vanish the moment you sorted a list. Confirm the crumbs survive.
- **Middle-click / open in new tab.** Album titles and similar are real `<a href>`s. They must
  carry the trail too, or the two routes to one view disagree about where you came from.
  Confirm the href itself contains `from=`, not just the scripted click.
- **Native `<select>` popups do not respond to synthetic keypresses** in browser automation.
  Do not conclude a filter is broken because the dropdown will not move — test the URL-rewrite
  path through a real button instead (the Places tab strip is the reliable one).

## Where the machinery lives

All of it is in `report_assets/family/family.js`: `urlFor`/`go` (writes the trail),
`nextTrail`/`canonURL` (builds it, guards cycles), `breadcrumb`/`backLink` (renders it),
`setCrumb` (a page naming itself). Read the comment block above `TRAIL_MAX` before changing
any of it — it records why the trail is a flat list rather than the obvious nested one.

New pages get breadcrumbs for free: `render()` draws the trail centrally. A new *drill-down*
still has to pass `{label: ...}` to `go()`/`urlFor()`, or its crumb falls back to the bare
section name.

## Reporting

Say which paths you walked and which failed, naming the click and where it actually landed.
If everything passes, say so plainly and list the paths — "no issues found" without the list
is indistinguishable from not having looked.
