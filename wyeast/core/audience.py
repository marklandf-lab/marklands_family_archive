"""Who is allowed to see an item — the one place that decides.

A case has two audiences, and they want opposite things:

  * "family"   — the people receiving the archive. Sentimental material only.
  * "examiner" — the fiduciary / estate attorney. Everything, including the
                 estate-rescued bulk and platform mail they are paid to find.

`email_triage` rescues estate-material mail that its own family-relevance
triage had already discarded ("your statement is ready", a brokerage notice) by
flipping the verdict back to "keep" and stamping the record `estate_rescued`.
That flag is the *only* per-item record of the disagreement, and until now
nothing on the family side read it — so tens of thousands of marketing and
platform emails were browsable, searchable and shipped in the family archive.

THE ASYMMETRY THIS MODULE EXISTS TO ENFORCE
-------------------------------------------
A forgotten check on the EXAMINER side fails CLOSED: the examiner misses
evidence. Detectable, recoverable, and a human is looking at it.
A forgotten check on the FAMILY side fails OPEN: unscreened material reaches a
grieving family.

So the burden of remembering is put where forgetting is safe:

  * the *default* audience is "family" (the restrictive one), and
  * no caller ever types an index filename — this module owns every path, so a
    reader physically cannot open the wrong audience's file by habit.

THE INVARIANT — "SEEN is a subset of SCREENED"
----------------------------------------------
`sensitive_scan` screens what the family is *shown*, not what the estate
*contains*, and it scopes itself with `filter_email_entries(..., "family")`.
The family's threads, search index and correspondent cards are scoped with the
same call. Because both sides derive from this one predicate, an item the
family can see has necessarily been screened. Widen the family set later and
the scan corpus widens with it, for free.

This is why the predicate is RECORD-level and not THREAD-level. A rescued
message sitting in an otherwise-organic conversation is dropped from the
family's view of that thread; the alternative ("a thread is family-visible
unless *every* message is rescued") would show the family a body that
`sensitive_scan` never looked at. Chain holes are honest — `build_threads`
already reports them via `has_missing_ancestor`.

A record with no `estate_rescued` key at all is family-visible: that is the
pre-rescue-gate behaviour, and it keeps legacy indexes rendering exactly as
they did before this module existed.
"""

import json
import logging
from pathlib import Path

FAMILY = "family"
EXAMINER = "examiner"
AUDIENCES = (FAMILY, EXAMINER)

log = logging.getLogger(__name__)


# ── index loader (injected) ─────────────────────────────────────────────────
# These indexes JOIN on path against the ones tools/_archive_data reads — a thread
# names the message files whose bodies email_index carries — so when a case is
# served away from the machine that produced it, BOTH sides have to be rewritten
# or the join silently empties (threads with no messages, no error anywhere).
# The rewriting lives in wyeast.core.rebase, but importing it here would break
# this module's stdlib purity: wyeast.core is imported by stages running under six
# different venvs, and the guard in tests/unit/test_audience.py exists to keep it
# importable under all of them. So the loader is INJECTED instead — one call, made
# beside the rebaser install in tools/_archive_data.install_rebaser.
_INDEX_LOADER = None


def install_index_loader(fn):
    """Read this module's case indexes through `fn(path) -> parsed`. None restores
    the plain stdlib parse."""
    global _INDEX_LOADER
    _INDEX_LOADER = fn


def _read_index(path):
    if _INDEX_LOADER is not None:
        return _INDEX_LOADER(path)
    return json.loads(Path(path).read_text())


def _check(audience: str) -> str:
    if audience not in AUDIENCES:
        raise ValueError(
            f"unknown audience {audience!r} — expected one of {AUDIENCES}")
    return audience


def _index(paths, filename: str) -> Path:
    """Resolve an index file from either a CasePaths or a bare metadata dir.

    Some tools (build_fts) are written against a metadata directory rather than
    a CasePaths. Accepting both is what lets every one of them route through
    this module instead of concatenating a filename of its own.
    """
    if hasattr(paths, "index"):
        return paths.index(filename)
    return Path(paths) / filename


def is_family_visible(entry: dict) -> bool:
    """True when an email index record may be shown to the family.

    Estate-rescued mail is examiner-only: it was rescued *because* the
    family-relevance triage had already thrown it away.
    """
    return not bool((entry or {}).get("estate_rescued", False))


def filter_email_entries(entries, audience: str = FAMILY) -> list:
    """Scope a list of email_index records to an audience.

    The examiner gets the union — they are the ones paying to see it.
    """
    _check(audience)
    if audience == EXAMINER:
        return list(entries or [])
    return [e for e in (entries or []) if is_family_visible(e)]


def load_email_index(paths, audience: str = FAMILY) -> list:
    """Read email_index.json scoped to `audience`. Missing index -> [].

    The default is deliberately the restrictive audience: a caller that forgets
    to pass one gets the family subset and fails closed.
    """
    _check(audience)
    path = _index(paths, "email_index.json")
    if not path.exists():
        return []
    try:
        entries = _read_index(path)
    except Exception as exc:
        log.warning("could not parse %s: %s", path.name, exc)
        return []
    return filter_email_entries(entries, audience)


# ── Messages (chat / SMS) ────────────────────────────────────────────────────
#
# Conversations carry TWO axes, and the family needs both. `triage_verdict` is
# what message_triage made of the conversation; `estate_rescued` is whether the
# estate gate overrode that verdict.
#
#   keep      — an ordinary personal conversation.
#   platform  — one-way automated traffic (delivery notices, shortcode OTPs).
#   discard   — noise.
#
# The family sees "keep, and not rescued". Two separate reasons, and it is worth
# keeping them separate in your head:
#
#   * RESCUED is examiner-only for the same reason rescued mail is: the estate
#     gate rescued it *because* family-relevance triage had already binned it.
#   * PLATFORM is excluded because message_triage only CHUNKS keep-verdict
#     conversations (message_triage.py: `chunks = ... if verdict == "keep"`),
#     and chunks are the only thing sensitive_scan ever sees. So a platform
#     conversation shown to the family is a conversation shown to the family that
#     was NEVER SCREENED. It was being shown. That is fixed here.
#     (It also matches email, where platform mail never reaches the family at
#     all — it goes to the noise log.)

KEEP = "keep"
PLATFORM = "platform"
DISCARD = "discard"


def conversation_verdict(conv: dict) -> str:
    """A conversation's triage verdict, defaulting to `keep` for legacy records
    written before the verdict existed (matching _archive_data's long-standing
    read)."""
    return (conv or {}).get("triage_verdict") or KEEP


def is_family_visible_conversation(conv: dict) -> bool:
    """True when a conversation may be shown to the family.

    Keep-verdict and not estate-rescued. See the note above for why `platform`
    is not family-visible: it is never chunked, so it is never screened.
    """
    return (conversation_verdict(conv) == KEEP
            and is_family_visible(conv))


def is_examiner_visible_conversation(conv: dict) -> bool:
    """True when a conversation may be shown to the examiner: everything the
    pipeline kept, plus platform traffic (an account-existence signal), but not
    the discarded noise."""
    return conversation_verdict(conv) != DISCARD


def can_see_conversation(conv: dict, audience: str = FAMILY) -> bool:
    """The single gate. Both the LIST and the per-conversation DETAIL endpoint
    must ask this — the detail endpoint used to answer by filename alone, so a
    conversation the family could not list could still be fetched by id."""
    _check(audience)
    if audience == EXAMINER:
        return is_examiner_visible_conversation(conv)
    return is_family_visible_conversation(conv)


def filter_conversations(convs, audience: str = FAMILY) -> list:
    _check(audience)
    return [c for c in (convs or []) if can_see_conversation(c, audience)]


def load_conversation_index(paths, audience: str = FAMILY) -> list:
    """Read conversation_index.json scoped to `audience`. Missing index -> []."""
    _check(audience)
    path = _index(paths, "conversation_index.json")
    if not path.exists():
        return []
    try:
        convs = _read_index(path)
    except Exception as exc:
        log.warning("could not parse %s: %s", path.name, exc)
        return []
    return filter_conversations(convs, audience)


# ── Contact resolution (macos-contact-data-sources spec §5) ─────────────────
#
# A conversation's `participant_contacts` (added by message_triage, spec §6.3)
# carries a `contact_tier` alongside each resolved name: Tier A is a single
# unambiguous directory hit; Tier B is a directory disagreement between
# sources, resolved by source precedence to one displayed name.
#
# Both tiers show the family a name (operator decision 2026-08-15,
# docs/specs/contact-name-surfaces.md §2, reversing spec §4). What the family
# does NOT get is the losing candidates: `participants`/`display_name` and
# these records carry the winner alone, while the full candidate list stays an
# examiner surface (message_triage_summary's `contact_resolution.contested`).
# Naming a thread "Dave Kroening" when a stale card said "David Kroening" is
# the ordinary case; showing the family "Dave Kroening / David Kroening /
# Sandi Hawkey" would be an unreadable hedge, and showing the bare number was
# the cost this decision refused to keep paying.

CONTACT_TIER_FAMILY_VISIBLE = "A"


def family_safe_participant_contacts(conv: dict) -> list:
    """`participant_contacts` with the losing candidates of an ambiguous
    resolution stripped. Absent/empty on legacy or call-only records -> []."""
    out = []
    for p in (conv or {}).get("participant_contacts") or []:
        entry = {k: v for k, v in dict(p).items() if k != "contact_candidates"}
        out.append(entry)
    return out


def participant_contacts(conv: dict, audience: str = FAMILY) -> list:
    """A conversation's participant_contacts scoped to `audience`.

    Examiner gets everything, including the Tier B candidates that lost — they
    are the audience with case context to overrule a pick (spec §5).
    """
    _check(audience)
    if audience == EXAMINER:
        return list((conv or {}).get("participant_contacts") or [])
    return family_safe_participant_contacts(conv)


def filter_message_chunks(chunks, audience: str = FAMILY) -> list:
    """Scope message_index chunk records to an audience.

    Chunks exist only for keep-verdict conversations, so the rescue flag is the
    only thing left to filter on.
    """
    _check(audience)
    if audience == EXAMINER:
        return list(chunks or [])
    return [c for c in (chunks or []) if is_family_visible(c)]


# ── Derived, role-separated artifacts ────────────────────────────────────────
#
# The thread index and the rendered thread pages are per-audience artifacts.
# Before this module they shared one filename, so whichever role built last
# overwrote the other's — an examiner explorer build would leave the union
# sitting in the file the family's archive server reads next.

def thread_index_path(paths, audience: str = FAMILY):
    """Path of the conversation index for an audience."""
    _check(audience)
    return _index(paths, f"email_threads_index_{audience}.json")


def legacy_thread_index_path(paths):
    """The pre-split, unsuffixed conversation index (the union).

    Only the examiner may fall back to it — see `load_thread_index`.
    """
    return _index(paths, "email_threads_index.json")


def thread_pages_dirname(audience: str = FAMILY) -> str:
    """Directory name for an audience's rendered thread pages.

    The family's keeps the historical name: it is a delivered artifact, linked
    from case_report.html and named in export_delivery's allowlist.
    """
    _check(audience)
    return "email_threads" if audience == FAMILY else f"email_threads_{audience}"


def load_thread_index(paths, audience: str = FAMILY) -> dict:
    """Read an audience's conversation index. Never raises.

    The two audiences degrade in opposite directions, on purpose:

      family   — the role-scoped file or NOTHING. It is never allowed to fall
                 back to the legacy union index, because that index contains
                 the estate-rescued mail this whole module exists to withhold.
                 A missing file means an empty Emails section (fail closed).
      examiner — falls back to the legacy union index when the role-scoped file
                 has not been generated yet, so cases processed before the
                 split keep showing the examiner everything (fail open, toward
                 the audience for whom that is safe).
    """
    _check(audience)
    path = thread_index_path(paths, audience)
    if path.exists():
        try:
            return _read_index(path) or {}
        except Exception as exc:
            log.warning("could not parse %s: %s", path.name, exc)
            return {}

    legacy = legacy_thread_index_path(paths)

    if audience == EXAMINER and legacy.exists():
        log.warning(
            "%s not found — falling back to the legacy union index %s. "
            "Rebuild the examiner bundle to refresh it.",
            path.name, legacy.name)
        try:
            return _read_index(legacy) or {}
        except Exception as exc:
            log.warning("could not parse %s: %s", legacy.name, exc)
            return {}

    # Only shout when there IS mail in the case but no index for this audience —
    # otherwise every photos-only case logs a warning about missing emails.
    has_mail = legacy.exists() or _index(paths, "email_index.json").exists()
    if not has_mail:
        log.debug("%s not found and the case has no email index — no mail here.",
                  path.name)
        return {}

    if audience == FAMILY:
        log.warning(
            "%s not found — the family Emails section will be EMPTY. Rebuild it "
            "(tools/gen_email_threads.py, or a family explorer/report build). "
            "The legacy union index is deliberately NOT used as a fallback: it "
            "contains the estate-rescued mail the family must not see.",
            path.name)
    else:
        log.warning(
            "%s not found — the examiner Emails section will be empty. Rebuild "
            "it with tools/gen_email_threads.py --audience examiner.", path.name)
    return {}


def email_index_path(paths):
    """Path of the raw (union) email index. Audience-independent — there is one
    file, and the filtering happens on read. Exposed so that no caller has to
    type the name, even when it wants to do its own bounded read."""
    return _index(paths, "email_index.json")


def correspondent_path(paths, audience: str = FAMILY):
    """Path of the correspondent-frequency cards for an audience.

    Note which way round these are. The FAMILY's is the new, suffixed file; the
    examiner keeps the historical filename. That is deliberate: correspondent_
    frequency.json has always held the union, so leaving that name meaning what
    it has always meant keeps every existing case and consumer correct, and the
    family gets a file that simply DOES NOT EXIST until email_triage has been
    re-run with an audience in mind. Absent means empty, not "here is the union".

    Inverting this — handing the family the legacy filename — would have served a
    stale union to exactly the audience that must never see it.
    """
    _check(audience)
    return _index(paths, "correspondent_frequency.json" if audience == EXAMINER
                  else f"correspondent_frequency_{audience}.json")
