"""Shared data layer for the Wyeast case interfaces.

Pure, import-safe builders (no argparse, no side effects at import) that turn the
pipeline's output/metadata/*.json indexes into the per-section data structures
rendered by BOTH front doors:
  - tools/build_explorer.py  — static file:// bundle (read-only, USB-portable)
  - tools/family_archive.py  — local-server interactive app (inline media + verbs)

Keeping the builders here means role-gating and field selection live in one place.
"""

import hashlib
import json
import os
import re
from datetime import datetime
import sys
from collections import Counter
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from wyeast.core.audience import (  # noqa: E402
    can_see_conversation, filter_message_chunks, install_index_loader,
    load_conversation_index, load_thread_index,
)
from wyeast.core import rebase as _rebase  # noqa: E402

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic"}
THUMB_MAX = 320  # px, longest edge

# Human-readable labels shared with the print report's vocabulary.
SCENE_LABELS = {
    "scanned document or handwritten letter": "Documents & letters",
}
SENSITIVITY_LABELS = {
    "explicit_sexual_imagery": "Explicit imagery",
    "suicidal_ideation": "Self-harm / crisis language",
    "graphic_violence": "Graphic violence",
    "weapons": "Weapons",
    "substance_use": "Substance use",
}

# Shown to the FAMILY role beside the list of files that hold account credentials
# (B7 decision: show filenames + guidance — the family can locate the files to
# secure/close accounts, but the secrets themselves are never surfaced).
CREDENTIAL_FAMILY_GUIDANCE = (
    "These files may contain passwords or account details. Handle them carefully "
    "and consider securing or closing the accounts involved. The examiner has "
    "reviewed them; the passwords themselves are not shown here."
)

# Neutral, human-readable names for the vital-document targets the pipeline
# searches for (keys of vital_doc_candidates.json). Kept as ONE mapping so the
# wording is trivially adjustable in a single place.
#
# NO-DEATH-WORDING POLICY: this repo has a standing policy that documentation /
# UI must not use death/deceased wording. The `death_certificate` entry below is
# an INTENTIONAL, USER-APPROVED EXCEPTION: the policy governs prose describing
# the owner, not the factual name of a legal document type the family must
# locate. A death certificate is a probate document the family needs to find, so
# it is surfaced by its real legal name ("Death certificate") — do NOT euphemize
# it (no "Certificate of passing") and do NOT omit it. Do not introduce
# death/deceased wording anywhere else in the archive.
VITAL_DOC_LABELS = {
    "will_testament": "Will / testament",
    "power_of_attorney": "Power of attorney",
    "deed_title": "Property deed / title",
    "vehicle_title": "Vehicle title",
    "life_insurance": "Life insurance",
    "birth_certificate": "Birth certificate",
    "death_certificate": "Death certificate",  # user-approved exception (see note above)
    "marriage_certificate": "Marriage certificate",
    "passport_id": "Passport / ID",
    "financial_statement": "Financial statement",
    "tax_return": "Tax return",
    "military_record": "Military record",
    "credentials": "Account credentials",
    "safe_deposit": "Safe deposit box",
    # Estate-materiality targets (Approach A) — kept in lockstep with
    # wyeast.embed.DEFAULT_VITAL_TARGETS so the reassign picker offers them.
    "trust_instrument": "Trust agreement",
    "certificate_of_trust": "Certificate of trust",
    "letters_testamentary": "Letters testamentary",
    "beneficiary_designation": "Beneficiary designation",
    "tod_pod_registration": "Transfer-on-death registration",
    "business_operating_agreement": "Business operating agreement",
    "buy_sell_agreement": "Buy-sell agreement",
    "estate_ein_confirmation": "Estate EIN confirmation",
    "gift_tax_return": "Gift / estate tax return",
    "foreign_asset_disclosure": "Foreign asset disclosure (FBAR)",
    "k1_estate_related": "Schedule K-1 (estate)",
    "cost_basis_valuation": "Cost-basis valuation",
    "letter_of_instruction": "Letter of instruction",
}


def vital_doc_label(target):
    """Neutral display name for a vital-document target, with a readable fallback
    for any target not in VITAL_DOC_LABELS (older/newer configs)."""
    if target in VITAL_DOC_LABELS:
        return VITAL_DOC_LABELS[target]
    return (target or "").replace("_", " ").strip().capitalize() or "Document"

# Scene CLIP confidence at/below this is treated as a low-confidence guess.
CONFIRM_SCENE_THRESHOLD = 0.45


def log(msg):
    print(f"[archive] {msg}", flush=True)


# The choke point for reading a metadata/ index — which makes it one of the two
# places a relocated case has to be corrected (wyeast/core/audience is the other,
# and both share the registry in wyeast/core/rebase so they cannot diverge).
# Process-global on purpose: one server process serves exactly one case, and
# threading the rebaser through every builder signature would put the burden of
# remembering it on ~40 call sites, any one of which could quietly forget and
# reintroduce the dead paths. Defaults to the no-op, so an in-place case (and
# every test that does not opt in) behaves byte-for-byte as before.


def install_rebaser(rebaser):
    """Point every case-index reader at `rebaser` for the rest of this process.
    Passing None restores the no-op. ArchiveCase calls this before its first
    load().

    BOTH readers are wired here, together, because they join on path: the thread
    index names the message files whose bodies email_index carries. Wiring only
    one leaves threads with no messages and reports no error at all."""
    rb = _rebase.install(rebaser)
    install_index_loader(rb.load_json_file)
    return rb


def active_rebaser():
    return _rebase.active()


def load_json(path, default=None):
    try:
        return _rebase.load_json_file(path)
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return default


def is_image(path):
    return Path(path).suffix.lower() in IMAGE_EXTS


# ── identity-language scrub ──────────────────────────────────────────────────────
# Display-time neutralizer for LLM summaries that refer to a person as "the
# deceased"/"the departed"/"the decedent" — identity is not definitively
# established and we never label anyone that way (just-the-facts). This is the
# immediate fix for already-generated case_summary.json text; the prompt-level
# fix is forward-only (see docs/BACKLOG.md). Conservative + idempotent: rewrites
# only the "the <deceased|departed|decedent>" noun phrase and its possessive to
# the role-accurate "the owner"; adjectival uses and all other text are untouched.
_IDENTITY_RE = re.compile(r"\bthe\s+(deceased|departed|decedent)(['’]s)?\b", re.IGNORECASE)
_IDENTITY_BARE_POSS_RE = re.compile(r"\b(deceased|departed|decedent)(['’]s)\b", re.IGNORECASE)


def neutralize_summary(text):
    """Replace identity-asserting references with the neutral 'the owner'."""
    if not text:
        return text

    def repl(m):
        lead = "The" if m.group(0)[:1].isupper() else "the"
        poss = "'s" if m.group(2) else ""
        return f"{lead} owner{poss}"

    out = _IDENTITY_RE.sub(repl, text)

    def repl_bare(m):
        lead = "The" if m.group(1)[:1].isupper() else "the"
        return f"{lead} owner's"

    return _IDENTITY_BARE_POSS_RE.sub(repl_bare, out)


# ── completion + role gates ────────────────────────────────────────────────────

def assert_complete(paths):
    """Refuse to run unless the case finished (reconciliation complete)."""
    marker = paths.output_dir / "PIPELINE_COMPLETE"
    if marker.exists():
        return
    rs = load_json(paths.metadata_dir / "_run_state.json", {})
    if rs.get("status") == "complete":
        return
    log(
        "REFUSING: case is not complete — no output/PIPELINE_COMPLETE marker and "
        f"_run_state.json is {rs.get('stage')!r}/{rs.get('status')!r} (need a "
        "completed reconciliation). Re-run the pipeline to completion first, or "
        "pass --force to override."
    )
    sys.exit(2)


def family_block_reasons(summary):
    """Return the export-gate block reasons for a summary, or [] if delivery is
    allowed. The non-exiting core of the gate check — safe to call from inside a
    request thread (verbs), where sys.exit would kill the worker with no response.
    `assert_family_allowed` wraps this for the CLI/startup path."""
    gate = (summary or {}).get("export_gate", {}) or {}
    if gate.get("delivery_blocked"):
        return gate.get("reasons") or [gate.get("waiver_reason", "delivery blocked")]
    return []


def assert_family_allowed(summary):
    """Honor the export gate: a blocked delivery must not be handed off.
    STARTUP/CLI ONLY — exits the process. Verbs must use family_block_reasons()
    and raise VerbError instead (a SystemExit inside a request thread dies silently)."""
    gate = summary.get("export_gate", {}) or {}
    if gate.get("delivery_blocked"):
        reasons = gate.get("reasons") or [gate.get("waiver_reason", "delivery blocked")]
        log("REFUSING family build — export_gate.delivery_blocked is true:")
        for r in reasons:
            log(f"  • {r}")
        log(
            "A family handoff cannot be produced while delivery is blocked. Resolve "
            "the gate (or build the examiner variant for on-workstation review)."
        )
        sys.exit(3)


# ── media ──────────────────────────────────────────────────────────────────────

def thumb_name(src_path):
    return hashlib.sha1(str(src_path).encode("utf-8")).hexdigest()[:16] + ".jpg"


def make_thumb(media_src, dest):
    """Write a downsized JPEG thumbnail. Returns True on success."""
    try:
        from PIL import Image, ImageOps
        try:  # register HEIC/HEIF support if available (idempotent, CI-safe)
            import pillow_heif
            pillow_heif.register_heif_opener()
        except Exception:
            pass

        with Image.open(media_src) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((THUMB_MAX, THUMB_MAX))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            dest.parent.mkdir(parents=True, exist_ok=True)
            im.save(dest, "JPEG", quality=82)
        return True
    except Exception:
        return False


# ── data builders ──────────────────────────────────────────────────────────────

# Extracted video keyframes (the video_frames stage) are CLIP-classified, so they
# appear in scene_index.clip_results, but they are NOT photographs and must never
# show in the gallery. They're authoritatively listed in video_frame_map.json;
# the filename pattern is a belt-and-suspenders fallback if that map is missing.
VIDEO_FRAME_RE = re.compile(r"_f\d{6}\.(jpe?g)$", re.IGNORECASE)


def is_video_frame(src, frame_set):
    return src in frame_set or bool(VIDEO_FRAME_RE.search(str(src)))


# Message chunks (message_triage) are flagged by sensitive_scan under a synthetic
# path "<abs source path>#chunk=<12hex>", where the suffix is chunk_sha256[:12] of
# the message_index.json record. The suffix path is NOT a servable file.
CHUNK_REF_RE = re.compile(r"#chunk=([0-9a-f]{12})$")


def split_chunk_ref(path):
    """Split a '<path>#chunk=<12hex>' chunk reference into (source_path, chunk_id).
    A plain path returns (path, None)."""
    m = CHUNK_REF_RE.search(str(path or ""))
    if m:
        return str(path)[:m.start()], m.group(1)
    return path, None


def file_identity(path):
    """A key equal for every hardlink to the same file.

    build_archive links delivered documents rather than copying them, so the
    working-tree path recorded in ocr_index and the delivered path a client asks
    for are two names for one inode. (st_dev, st_ino) is what makes those compare
    equal; the realpath string is the fallback for anything unstattable.
    """
    try:
        st = os.stat(path)
        return (st.st_dev, st.st_ino)
    except OSError:
        return os.path.realpath(str(path))


def build_doctext_views(ocr_index):
    """{file identity -> .view.json path} for documents with an inline view.

    ocr_index's `file` values are WORKING-tree paths while a client asks for the
    DELIVERED canonical path, and under link_mode=hardlink those are two names
    for one inode — so neither a string compare nor realpath() (which resolves
    symlinks, not hardlinks) would match them. Both the inode identity and the
    realpath string are registered so hardlink and copy-mode archives resolve.

    Sidecars written by an earlier run can be deleted out from under the index,
    so existence is checked here, once per load, rather than per request.
    """
    m = {}
    for rec in ocr_index or []:
        view, f = rec.get("sidecar_view"), rec.get("file")
        if not (view and f) or not os.path.exists(view):
            continue
        m[file_identity(f)] = view
        m[os.path.realpath(f)] = view
    return m


def build_photo_universe(scene_index, archive_map, role, frame_map=None, rescued=None,
                         scanned_released=None):
    """Return {src_path: {category, delivered, archive}} for the photo gallery.

    `scanned_released` is the examiner's scanned_released overlay (family_decisions.json,
    BACKLOG #19) — a scanned-document/handwritten-letter image the examiner marked "not
    a document" rejoins the gallery as an ordinary (uncategorized) photo instead of
    staying filtered into Correspondence. Mirrors `rescued` (junk_rescued): a decisions
    overlay read here, NEVER a scene_index mutation.

    For family builds we keep only delivered photos whose archive file still
    exists on disk (quarantined/sensitive items were moved out of archive/, so
    they fail this existence check and are silently absent — by construction).

    Extracted video keyframes are excluded for BOTH roles (#15): they have no
    archive_map entry, so the family build dropped them already, but the examiner
    build (which skips the archive-exists check) was leaking them into Photographs.

    `rescued` is the examiner's junk_rescued overlay (family_decisions.json) — the
    working paths an examiner un-junked. Scene-classifier junk is filtered LIVE from
    junk_results (the file is never moved), so subtracting the overlay here is what
    makes an un-junked photo reappear in the gallery. NEVER mutates scene_index.
    """
    clip = scene_index.get("clip_results", {}) or {}
    junk = set((scene_index.get("junk_results", {}) or {}).keys())
    if rescued:
        junk -= set(rescued)
    entries = archive_map.get("entries", {}) or {}
    frame_set = set(frame_map or {})
    out = {}
    dropped_junk = dropped_missing = dropped_frames = dropped_docs = restored_scanned = 0
    for src, rec in clip.items():
        if not is_image(src):
            continue
        if is_video_frame(src, frame_set):
            dropped_frames += 1
            continue
        # Scanned documents / handwritten letters are images that aren't
        # photographs — exclude them from the gallery for BOTH roles (they're
        # surfaced in Correspondence via scanned_image_rows). See SCENE_LABELS.
        # A released item (examiner marked "not a document", #19) rejoins the
        # gallery instead — falls through to the ordinary photo path below.
        released_here = rec.get("category") in SCENE_LABELS and src in (scanned_released or ())
        if rec.get("category") in SCENE_LABELS and not released_here:
            dropped_docs += 1
            continue
        delivered = bool(rec.get("delivered", True))
        archive = entries.get(src)
        # If the archive_map says there's a canonical but it's gone from disk, the
        # file was moved out (quarantined/suspense) — drop it for BOTH roles, else
        # the examiner gallery shows a broken tile (its /thumb 404s). #2
        if archive and not os.path.exists(archive):
            dropped_missing += 1
            continue
        if role == "family":
            if src in junk or not delivered:
                dropped_junk += 1
                continue
            if not archive:
                dropped_missing += 1
                continue
        if released_here:
            restored_scanned += 1
        out[src] = {
            "category": "uncategorized" if released_here else (rec.get("category") or "uncategorized"),
            "delivered": delivered,
            "archive": archive,
        }
    if restored_scanned:
        log(f"photo universe: +{restored_scanned} released from scanned documents")
    # Rescued-from-junk items (examiner un-junk overlay). Scene-junk lives in
    # junk_results, which is DISJOINT from clip_results — the loop above never sees
    # it — so a rescue must ADD the item here, gated on a valid on-disk archive entry
    # (no entry / a moved-out or frame file → can't be a gallery tile, stays absent).
    # Applies to BOTH roles: the examiner deliberately rescued it.
    junk_recs = scene_index.get("junk_results", {}) or {}
    restored_junk = 0
    for src in (rescued or ()):
        if src in out or src not in junk_recs:
            continue
        if not is_image(src) or is_video_frame(src, frame_set):
            continue
        archive = entries.get(src)
        if not archive or not os.path.exists(archive):
            continue
        out[src] = {
            "category": (junk_recs.get(src) or {}).get("category") or "uncategorized",
            "delivered": True,
            "archive": archive,
        }
        restored_junk += 1
    if restored_junk:
        log(f"photo universe: +{restored_junk} rescued from junk")
    if role == "family":
        log(
            f"photo universe: {len(out)} delivered "
            f"(excluded {dropped_junk} junk/undelivered, {dropped_missing} not present in archive, "
            f"{dropped_frames} video frames, {dropped_docs} scanned documents)"
        )
    else:
        log(f"photo universe: {len(out)} photos (excluded {dropped_frames} video frames, "
            f"{dropped_docs} scanned documents, {dropped_missing} moved-out/quarantined)")
    return out


def build_stacks(dup_groups, member_scan, ledger_latest, universe, case_dir):
    """Photo stacks: {keeper_src: stack} + the closed member-serving allowlist.

    Joins perceptual_dup_groups.json × the move ledger × dup_member_scan.json
    into the family-archive stack model (family_archive only — the static
    explorer has no live member serving). Returns (stacks, member_paths):
      stacks       {keeper_src: {"n", "kind", "suggested", "members": [...]}}
                   members ordered by capture_time, each
                   {"src", "name", "capture_time", "moved"}
      member_paths {member_src: on-disk path under duplicates/perceptual/}
                   — the ONLY paths the dedicated member route may serve;
                   anything else under duplicates/ stays forbidden.

    Every gate here is FAIL-CLOSED (a member silently drops to keeper-only
    display, indistinguishable from a pre-index case):
      - keeper must itself be family-visible (present in the photo universe —
        delivered, not quarantined/banished for the caller's role);
      - a moved member must resolve through the ledger (reason
        "perceptual_dupe", status "done", unambiguous basename) to a file that
        still exists;
      - a moved member must be COVERED by dup_member_scan.json with
        nudity_flag false — group members skipped the delivered-image scan at
        collect_dedup, so no verdict means not shown;
      - a kept member (keep_all bursts, moved false) is a delivered photo and
        must be in the universe itself; it serves through the normal path.
    """
    groups = (dup_groups or {}).get("groups") or []
    if not groups:
        return {}, {}
    scan_members = (member_scan or {}).get("members")
    case_dir = Path(case_dir)

    # Ledger: pre-move basename -> destination under duplicates/perceptual/.
    # An ambiguous basename (two distinct sources sharing a name) is dropped —
    # serving the wrong bytes is worse than showing keeper-only.
    dst_by_name: dict = {}
    for entry in (ledger_latest or {}).values():
        if entry.get("reason") != "perceptual_dupe" or entry.get("status") != "done":
            continue
        name = os.path.basename(entry.get("src") or "")
        dst = entry.get("dst")
        if not name or not dst:
            continue
        if name in dst_by_name and dst_by_name[name] != dst:
            dst_by_name[name] = None  # ambiguous → fail closed
        else:
            dst_by_name.setdefault(name, dst)

    # Scan verdicts by exact path, with a unique-basename fallback so a case
    # tree that was relocated after the scan still resolves.
    scan_by_name: dict = {}
    for key in (scan_members or {}):
        name = os.path.basename(key)
        scan_by_name[name] = None if name in scan_by_name else key

    def _verdict(dst):
        if not scan_members:
            return None
        rec = scan_members.get(dst)
        if rec is None:
            alt = scan_by_name.get(os.path.basename(dst))
            rec = scan_members.get(alt) if alt else None
        return rec

    stacks: dict = {}
    member_paths: dict = {}
    for g in groups:
        keeper = g.get("keeper")
        if not keeper:
            continue
        keeper_src = keeper if os.path.isabs(keeper) else str(case_dir / keeper)
        if keeper_src not in universe:
            continue  # keeper not family-visible → no stack
        members = []
        for m in g.get("members") or []:
            name = m.get("file")
            if not name:
                continue
            if not m.get("moved"):
                # keep_all burst member: a delivered photo in its own right.
                src = str(case_dir / "extracted" / "photos" / name)
                if src not in universe or src == keeper_src:
                    continue
                members.append({"src": src, "name": name,
                                "capture_time": m.get("capture_time"),
                                "moved": False})
                continue
            dst = dst_by_name.get(name)
            if not dst:
                continue
            rec = _verdict(dst)
            # Require COVERAGE, not merely a clean verdict. run_dup_members_scan
            # writes a record for every member regardless — with NudeNet off it
            # writes {"nudity_scanned": False, "nudity_flag": False} — so
            # "has a record and isn't flagged" admitted members nothing had ever
            # looked at. Group members skip the delivered-image scan at
            # collect_dedup, so this pass is their only screening: no verdict
            # means not shown, exactly as this function's docstring specifies.
            if not rec or rec.get("nudity_scanned") is not True \
                    or rec.get("nudity_flag"):
                continue
            if not os.path.exists(dst):
                continue
            members.append({"src": dst, "name": name,
                            "capture_time": m.get("capture_time"),
                            "moved": True})
        if not members:
            continue
        members.sort(key=lambda r: (r["capture_time"] is None, r["capture_time"] or ""))
        for r in members:
            if r["moved"]:
                member_paths[r["src"]] = r["src"]
        stacks[keeper_src] = {"n": len(members), "kind": g.get("kind") or "unknown",
                              "suggested": g.get("suggested"), "members": members}
    return stacks, member_paths


def event_album_titles(summary):
    """Map {str(album_id): title} from the summary's event_albums. Each album is a
    named trip cluster (album_id == the gps_trip_cluster_id the photos carry), so
    this lets photo_rows tag a photo with the event album it belongs to even though
    the summary drops the per-album file list."""
    out = {}
    for a in (summary or {}).get("event_albums", []) or []:
        aid = a.get("album_id")
        title = a.get("title")
        if aid is not None and title:
            out[str(aid)] = title
    return out


def event_albums_data(summary, photo_rows_):
    """The event-album view (Move Phase 2): one card per configured event album,
    with a LIVE member count DERIVED from the current photo rows — NOT the static
    `event_albums[].photo_count` in case_summary.json, which never changes when a
    photo is event-moved. `count` is the number of photo_rows whose EFFECTIVE
    `event_id` (placement overlay applied by photo_rows) equals this album_id, so a
    move re-tags a photo and shifts the two albums' counts. `sample_ids` is a few
    member ids (newest-first, as photo_rows is sorted) for the card thumbnails.

    Every configured album is returned (even a 0-count one — the operator may have
    moved everything out); cards are sorted by live count desc. Absent
    event_albums → []. Both roles (album grouping is non-sensitive)."""
    counts = Counter()
    samples = {}
    for r in (photo_rows_ or []):
        aid = r.get("event_id")
        if aid is None:
            continue
        counts[aid] += 1
        s = samples.setdefault(aid, [])
        if len(s) < 4 and r.get("id"):
            s.append(r["id"])
    out = []
    for a in (summary or {}).get("event_albums", []) or []:
        aid = a.get("album_id")
        if aid is None:
            continue
        aid = str(aid)
        out.append({
            "album_id": aid,
            "title": a.get("title"),
            "place": a.get("place"),
            "date_range": a.get("date_range"),
            "scenes": a.get("scenes"),
            "count": counts.get(aid, 0),
            "sample_ids": samples.get(aid, []),
        })
    out.sort(key=lambda x: x["count"], reverse=True)
    return out


def _as_str_list(value):
    """A defensive list-of-nonempty-strings coercion for source-conditional
    metadata fields (album_membership, people_tags) that may be absent, a scalar,
    or contain blanks depending on the gallery source."""
    if isinstance(value, list):
        return [str(v) for v in value if v]
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def photo_rows(universe, metadata_index, geo_index, thumbs, event_titles=None,
               stacks=None, llava_map=None, scene_placements=None,
               event_placements=None):
    """Rows for the photo gallery. Beyond the geo/trip fields, this carries the
    OWNER'S OWN gallery layer (G-1) — favorites, hidden flag, album membership,
    source attribution — and a caption (G-7): the owner's own gallery_caption if
    present, else the LLaVA one-sentence description for that image.

    Every gallery-layer field is SOURCE-CONDITIONAL (only iPhoto / Google-Takeout
    sources populate them) and defaults to falsy/empty when absent, so a case that
    never carried them is indistinguishable from before. `llava_map` is
    scene_index.json's `llava_results` (working-path → sentence); pass {} / None
    when unavailable.

    `scene_placements` (Phase 1.5 Move) is the family_decisions scene overlay
    {src: scene_category}: a facet RELABEL within the gallery universe. When set it
    overrides a row's `scene` (`info["category"]`) so the Photos scene facet and
    build_search regroup immediately. Additive — empty/None leaves every scene as
    the pipeline classified it (fast path unchanged).

    `event_placements` (Phase 2 Move) is the family_decisions event overlay
    {src: album_id}: it re-files a photo into a different event album. Each row
    carries `event_id` (the effective album_id string — the placement if present,
    else the photo's own `gps_trip_cluster_id` when that matches a configured
    album, else None) ALONGSIDE `event` (that album's title). The event-album view
    and the ?event=<album_id> server filter key on `event_id`; the placement
    overriding it makes a move change an album's live count. Additive — empty/None
    leaves every photo tagged with its pipeline album (fast path unchanged).
    """
    event_titles = event_titles or {}
    stacks = stacks or {}
    llava_map = llava_map or {}
    scene_placements = scene_placements or {}
    event_placements = event_placements or {}
    rows = []
    for src, info in universe.items():
        md = metadata_index.get(src, {}) or {}
        geo = geo_index.get(src, {}) or {}
        tid = geo.get("gps_trip_cluster_id")
        # Effective event album (Phase 2 Move): the placement wins; otherwise the
        # photo's own trip cluster IF it names a configured album (else None). The
        # title is derived from the effective album_id so a moved photo shows the
        # target album's name.
        derived_aid = str(tid) if (tid is not None and str(tid) in event_titles) else None
        event_id = event_placements.get(src) or derived_aid
        # Caption: the owner's own caption wins; otherwise the pipeline's LLaVA
        # description (only present for CLIP-unsure images → partial coverage).
        # Run through neutralize_summary — it's OUR generated text. Absent (not a
        # blank string) when neither source has it.
        caption = md.get("gallery_caption") or llava_map.get(src)
        caption = neutralize_summary(caption) or None if caption else None
        row = {
            "id": src,
            "name": os.path.basename(src),
            "thumb": thumbs.get(src),
            "scene": scene_placements.get(src) or info["category"],
            "ts": md.get("timestamp"),
            "place": md.get("place"),
            "gps": md.get("gps"),
            "trip": (geo.get("gps_trip_name") or "").strip() or None,
            "event_id": event_id,
            "event": event_titles.get(event_id) if event_id is not None else None,
            "delivered": info["delivered"],
            # ── owner's own gallery layer (G-1) — all source-conditional ──
            "favorite": bool(md.get("photo_library_favorite")),
            "hidden": bool(md.get("photo_library_hidden")),
            "albums": _as_str_list(md.get("album_membership")),
            "source": md.get("gallery_source") or None,
            "caption": caption,
            # ── EXIF facts for the lightbox metadata panel (F-10) ── additive,
            # default None/absent so a case without them is indistinguishable from
            # before. camera_make/model, pixel dimensions, the structured place
            # (`{name,admin1,cc}`) and GPS altitude are present on iPhone/iPad-sourced
            # photos (VERIFIED on goog); `place` and `gps` above already carry the
            # coarse location. All read-only display; never a filter/export key.
            "camera_make": md.get("camera_make") or None,
            "camera_model": md.get("camera_model") or None,
            "width_px": md.get("width_px"),
            "height_px": md.get("height_px"),
            "place_detail": md.get("place_detail") or None,
            "gps_altitude_m": md.get("gps_altitude_m"),
            # people_tags is empty on every case checked (2026-07-05) — plumbed
            # defensively (guarded for absence) but NO UI depends on it yet.
            "people": _as_str_list(md.get("people_tags")),
        }
        stack = stacks.get(src)
        if stack:  # only keepers that head a surfaced stack carry the key
            row["stack"] = stack
        rows.append(row)
    rows.sort(key=lambda r: (r["ts"] or ""), reverse=True)
    return rows


# ── G-15 face-assist DECISIONS OVERLAY (never mutates face_clustering.json) ───────
#
# The examiner verbs merge_persons / assign_face record their decisions in
# family_decisions.json (person_merges: {loser_pid: winner_pid}; face_assignments:
# {noise_src: person_id}) — NOT in the pipeline-authored face_clustering.json. The
# overlay is applied HERE, at render time, exactly like `removed_persons`: a merged
# loser's members fold into its (transitively resolved) winner and the loser drops
# out of People, an assigned noise face joins its target cluster and leaves
# noise_files. This touches nothing authoritative, is trivially reversible (pop the
# decision), and survives a face_cluster re-run gracefully — a stale overlay entry
# whose pid/src no longer exists simply no-ops.


def resolve_merge(pid, merges):
    """Follow a person_merges chain (loser→winner→…) to its final surviving winner.

    merges is {loser_pid: winner_pid}. Chains are resolved transitively (A→B, B→C
    ⇒ A resolves to C); a cycle (should never be recorded — the verb refuses it) is
    broken by the `seen` guard so this always terminates."""
    seen = set()
    while pid in merges and pid not in seen:
        seen.add(pid)
        pid = merges[pid]
    return pid


def _candidate_pids(cands):
    """Collect every Person_NN-looking cluster id out of a geo index
    `face_cluster_merge_candidates` value, whose element shape is unspecified
    (strings, or dicts carrying a person_id/cluster/pid field, possibly nested).
    Defensive: unrecognized shapes contribute nothing (the caller no-ops)."""
    out = set()

    def walk(x):
        if isinstance(x, str):
            if x.startswith("Person_"):
                out.add(x)
        elif isinstance(x, dict):
            for k in ("person_id", "cluster", "pid", "target", "into"):
                v = x.get(k)
                if isinstance(v, str) and v.startswith("Person_"):
                    out.add(v)
            for v in x.values():
                if isinstance(v, (list, dict)):
                    walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)

    walk(cands)
    return out


def apply_face_overlay(face_clustering, decisions):
    """Return a NON-mutating view of face_clustering with the family_decisions
    face-assist overlay folded in (G-15 + the Move verb). The input dict is never
    modified.

    - person_merges {loser: winner}: each loser's members are appended (order-
      preserving, de-duplicated) to the members of its transitively-resolved winner,
      and the loser is dropped from person_clusters (so it disappears from People,
      like a removed person).
    - face_assignments {src: person_id}: the LEGACY noise-assign key (G-15
      verb_assign_face). The noise face `src` is appended to its target cluster
      (target resolved through merges) and removed from noise_files. Kept for
      dual-read: verb_assign_face still writes this key, so on-disk entries keep
      applying and undoing unchanged.
    - face_placements {src: person_id}: the GENERAL person-placement key (the Move
      verb, verb_move). Unlike face_assignments (append-only), a placement REMOVES
      `src` from whatever cluster currently holds it and re-appends it to its
      (merge-resolved) target — the "this photo is filed under the wrong person"
      correction. Runs AFTER the legacy assign step so a src carrying BOTH keys has
      its assigned copy pulled and the placement wins.

    Pass order is load-bearing (spec §4, review A):
      merges fold → legacy face_assignments append → face_placements (remove+append)
      → drop clusters emptied by the net effect → filter noise_files
    noise_files EXCLUDES both face_assignments AND face_placements srcs (else a
    migrated/placed src leaks back into the confirm queue).

    Fast path: no overlay recorded → the original object is returned unchanged."""
    merges = (decisions or {}).get("person_merges", {}) or {}
    assignments = (decisions or {}).get("face_assignments", {}) or {}
    placements = (decisions or {}).get("face_placements", {}) or {}
    if not merges and not assignments and not placements:
        return face_clustering
    clusters = face_clustering.get("person_clusters", {}) or {}
    new_clusters = {pid: list(files) for pid, files in clusters.items()}

    def _append(pid, f):
        lst = new_clusters.get(pid)
        if lst is not None and f not in lst:
            lst.append(f)

    # 1. Fold every merged loser's members into its final winner (chains resolve to
    #    the same surviving winner, so folding each loser independently is correct).
    for loser in list(new_clusters):
        winner = resolve_merge(loser, merges)
        if winner != loser and winner in new_clusters:
            for f in new_clusters[loser]:
                _append(winner, f)
    # Drop the losers only after all folds (a loser is any cluster that resolves to
    # a different pid).
    for loser in list(new_clusters):
        if resolve_merge(loser, merges) != loser:
            new_clusters.pop(loser, None)

    # 2. Legacy face_assignments: append the noise face into its (resolved) target.
    #    MUST run before placements so a placement of a src that also carries a
    #    legacy assignment removes that appended copy and wins.
    assigned = set()
    for src, pid in assignments.items():
        tgt = resolve_merge(pid, merges)
        if tgt in new_clusters:
            _append(tgt, src)
            assigned.add(src)

    # 3. face_placements (Move): remove src from whatever cluster currently holds it,
    #    then append to its (merge-resolved) target. A placement into a merged-away
    #    loser routes to the surviving winner; a placement whose target no longer
    #    exists (post face_cluster re-run) is a no-op — src falls back to its origin
    #    cluster (safe degrade, like the merge overlay).
    nonempty_before = {pid for pid, lst in new_clusters.items() if lst}
    placed = set()
    for src, pid in placements.items():
        tgt = resolve_merge(pid, merges)
        if tgt not in new_clusters:
            continue  # stale target → no-op (src stays in its origin cluster)
        for lst in new_clusters.values():
            while src in lst:      # pull every current copy (origin + any assigned)
                lst.remove(src)
        _append(tgt, src)
        placed.add(src)

    # 4. Drop clusters left EMPTY by a placement pulling out their last member (else
    #    People shows a ghost 0-count person and the confirm queue emits a stale
    #    unnamed_person item). Only clusters that HAD members and now have none are
    #    dropped — an originally-empty cluster is preserved (existing behaviour).
    for pid in list(new_clusters):
        if not new_clusters[pid] and pid in nonempty_before:
            new_clusters.pop(pid, None)

    # 5. noise_files excludes both applied assignments and applied placements.
    noise = [s for s in (face_clustering.get("noise_files", []) or [])
             if s not in assigned and s not in placed]
    out = dict(face_clustering)
    out["person_clusters"] = new_clusters
    out["noise_files"] = noise
    return out


def people_rows(face_clustering, summary, universe, thumbs, role, removed=None,
                frame_map=None, archive_map=None):
    clusters = face_clustering.get("person_clusters", {}) or {}
    identities = face_clustering.get("cluster_identities", {}) or {}
    by_id = {c.get("person_id"): c for c in summary.get("photo_clusters", []) or []}
    removed = set(removed or [])
    frame_set = set(frame_map or {})
    arc_entries = (archive_map or {}).get("entries", {}) or {}

    def present(f):
        # A face member whose archive copy is gone has been discarded/moved out —
        # its thumbnail would 404. For family the universe check already drops it;
        # for examiner (which keeps undelivered) check the archive copy directly so
        # only truly-missing members go (present-but-junk/doc faces are kept).
        arc = arc_entries.get(f)
        return not (arc and not os.path.exists(arc))

    def live_source_video(m):
        # The source video of a frame member, but only while that video is still
        # viewable — else None. `present()` cannot decide this: a frame lives in
        # extracted/ and is never an archive_map key, so its own lookup is always
        # a miss and every frame reads as "present" no matter what happened to the
        # movie it came from. A quarantined/discarded video's frames are still on
        # disk and would render as list-card thumbnails — leaking exactly the
        # imagery the quarantine removed. Mirrors person_detail's guard: family
        # needs the video delivered (in the archive map) and BOTH roles need its
        # archive copy still on disk.
        vsrc = ((frame_map or {}).get(m) or {}).get("source_video")
        if not vsrc:
            return None
        canonical = arc_entries.get(vsrc)
        if role == "family" and not canonical:
            return None
        if canonical and not os.path.exists(canonical):
            return None
        return vsrc

    rows = []
    for pid, files in clusters.items():
        if pid in removed:  # #6 dissolved grouping
            continue
        # Sample faces for the list card / member set: delivered-only for family,
        # never video keyframes (those are "video appearances" in person detail),
        # and never a discarded/moved-out member (no broken face thumb).
        sample = [f for f in files
                  if (role != "family" or f in universe) and not is_video_frame(f, frame_set)
                  and present(f)]
        # Distinct source videos this person appears in (deduped by source_video,
        # like the poster dedupe in video_rows) — shown alongside the photo count.
        video_count = len({sv for m in files if is_video_frame(m, frame_set)
                           for sv in [live_source_video(m)] if sv})
        # A video-only person (photo_count 0, e.g. only ever recorded on video) has
        # nothing in `sample` for the list card's thumbnail strip to draw from —
        # fall back to their own video-frame members, which are thumbnail-able
        # images just like a photo (#12; person_detail already does this).
        card_ids = sample[:6] if sample else [
            f for f in files if is_video_frame(f, frame_set)
            and live_source_video(f) and present(f)][:6]
        if not sample and not video_count:
            # Nothing of this person survives for this role: no showable photo and
            # no viewable video. Their card would carry zero thumbnails under the
            # narrative LLM's photo-oriented summary — text written about imagery
            # the viewer cannot (and, for quarantined material, must not) see.
            continue
        meta = by_id.get(pid, {})
        summary = neutralize_summary(meta.get("summary"))
        name = person_display_name(pid, identities)
        named = bool(_identity_name(pid, identities))
        # #21: the narrative LLM was only ever given the internal cluster id
        # (e.g. "Person_04"), never the name an examiner assigns afterward, so
        # a rename left the summary sentence itself still saying "Photos
        # capture Person_04 in..." even though the card's own title correctly
        # showed the new name. Pure display-time substitution — never rewrites
        # case_summary.json, so a re-run or undo is unaffected.
        if named and summary and re.search(r"\b" + re.escape(pid) + r"\b", summary):
            summary = re.sub(r"\b" + re.escape(pid) + r"\b", name, summary)
        if not sample and video_count:
            # The narrative LLM is only ever given photo descriptions, so for a
            # person with zero delivered photos it had nothing real to describe —
            # it hallucinated generic photo-oriented text ("Photos show...") for
            # someone who has no photos at all. State the honest fact instead.
            summary = "No photographs of this person — appears in {} video{}.".format(
                video_count, "" if video_count == 1 else "s")
        rows.append({
            "person_id": pid,
            "name": name,
            "named": named,
            "photo_count": len(sample),       # photos only (was len(files)) — excludes video frames
            "video_count": video_count,
            "delivered_count": len(sample),
            "significance": meta.get("significance"),
            "summary": summary,
            "thumbs": [thumbs.get(f) for f in card_ids if thumbs.get(f)],
            "sample_ids": card_ids,
            # Role-appropriate full member list (delivered-only for family) so the
            # People view can open one person's whole photo set (capped for size).
            "member_ids": sample[:5000],
        })
    rows.sort(key=lambda r: r["photo_count"], reverse=True)
    return rows


# Non-person sentinel tokens the pipeline writes into video_index.assigned_persons:
# "no_faces" (no face detected) / "unidentified" (faces present but not clustered
# to a known person). Neither is a real Person_NN cluster, so neither becomes a
# person chip — only tokens that map to a face cluster do (G-11).
_VIDEO_PERSON_SENTINELS = {"no_faces", "unidentified", ""}


def _video_index_by_source(video_index):
    """Normalize the loaded video_index.json (a `{"videos": [...]}` dict OR a bare
    list of records) into {source_video: record}. Absent/malformed → {}."""
    if isinstance(video_index, dict):
        videos = video_index.get("videos") or []
    elif isinstance(video_index, list):
        videos = video_index
    else:
        return {}
    out = {}
    for v in videos:
        sv = (v or {}).get("source_video")
        if sv:
            out.setdefault(sv, v)
    return out


def video_rows(archive_map, metadata_index, video_frame_map, role, *, cap=None,
               video_index=None, cluster_identities=None):
    """Delivered videos for the Photographs Photo/Video/All selector (#3). Each
    row's poster is the video's first extracted keyframe (reversed
    video_frame_map). Role-gated + skip a missing canonical (mirrors the photo
    universe). The `id` is the archive_map key → /media plays it (Range).

    G-11 facets: each row also carries `persons` (a list of {person_id, name}
    resolved from video_index.assigned_persons via `cluster_identities`) and
    `scenes` (video_index.assigned_scenes). Both are additive metadata chips, the
    SAME for both roles (the video itself is already delivery-gated); the sentinel
    person tokens (no_faces/unidentified) are dropped so only real face clusters
    become chips. Absent video_index → both lists empty (indistinguishable from a
    pre-video_index case).

    `cap` is opt-in truncation for callers that don't paginate (the static
    explorer may pass one). Default None returns the FULL sorted list — the
    section/API layer (family_archive) does the offset/limit slicing so a capped
    view is never presented as the whole set (see docs/specs/family-archive-pagination.md)."""
    from wyeast.core.media import VIDEO_EXTENSIONS  # 35 dotted exts, config-driven
    entries = archive_map.get("entries", {}) or {}
    identities = cluster_identities or {}
    vindex = _video_index_by_source(video_index)
    poster = {}
    for frame, info in (video_frame_map or {}).items():
        sv = (info or {}).get("source_video")
        if sv and sv not in poster:
            poster[sv] = frame
    rows = []
    for src, canonical in entries.items():
        if os.path.splitext(src)[1].lower() not in VIDEO_EXTENSIONS:
            continue
        if canonical and not os.path.exists(canonical):
            continue  # moved out / missing → would be a broken tile
        if role == "family" and not canonical:
            continue
        md = metadata_index.get(src, {}) or {}
        vrec = vindex.get(src, {}) or {}
        persons, seen_pid = [], set()
        for tok in vrec.get("assigned_persons") or []:
            if tok in _VIDEO_PERSON_SENTINELS or tok in seen_pid:
                continue
            seen_pid.add(tok)
            persons.append({"person_id": tok,
                            "name": person_display_name(tok, identities)})
        scenes, seen_sc = [], set()
        for s in vrec.get("assigned_scenes") or []:
            if not s or s in seen_sc:
                continue
            seen_sc.add(s)
            scenes.append(s)
        rows.append({
            "id": src, "kind": "video", "name": os.path.basename(src),
            "poster": poster.get(src), "ts": md.get("timestamp"), "place": md.get("place"),
            "persons": persons, "scenes": scenes,
        })
    rows.sort(key=lambda r: (r.get("ts") or ""), reverse=True)
    if cap is not None and len(rows) > cap:
        log(f"videos: capping at {cap} of {len(rows)}")
        rows = rows[:cap]
    return rows


def person_detail(face_clustering, universe, archive_map, metadata_index, scene_index,
                  video_frame_map, person_id, role, removed=None):
    """One person's actual cluster members, rendered DIRECTLY (not intersected
    with the photo gallery — which now excludes video frames / scanned docs and
    would empty out video-only people like Person_05).

    Each member is a photo or a video appearance:
      photo: {id, kind:"photo", name, scene, place}
      video: {id (frame still = poster), kind:"video", video_src (source video,
              an archive_map key → /media), video_name, offset}
    Role-gated: examiner sees all; family sees photo members only if delivered
    (in universe) and video members only if the source video is delivered.
    """
    clusters = face_clustering.get("person_clusters", {}) or {}
    if person_id not in clusters or person_id in set(removed or []):
        return None
    identities = face_clustering.get("cluster_identities", {}) or {}
    frame_set = set(video_frame_map or {})
    entries = (archive_map or {}).get("entries", {}) or {}
    clip = (scene_index or {}).get("clip_results", {}) or {}
    photos, videos = [], []
    for m in clusters[person_id]:
        if is_video_frame(m, frame_set):
            info = (video_frame_map or {}).get(m) or {}
            vsrc = info.get("source_video")
            if role == "family" and (not vsrc or vsrc not in entries):
                continue  # source video not delivered
            # Mirror video_rows' canonical-exists guard (BOTH roles): a video
            # whose archive copy was moved out (quarantined / perceptual-dupe
            # loser) must not render a dead "play" — /media on it 404s.
            canonical = entries.get(vsrc) if vsrc else None
            if canonical and not os.path.exists(canonical):
                continue
            videos.append({
                "id": m, "kind": "video", "video_src": vsrc,
                "video_name": os.path.basename(vsrc or ""),
                "offset": info.get("frame_offset_seconds"),
            })
        else:
            if role == "family" and m not in universe:
                continue  # not delivered
            # #20: mirror people_rows' present() check for the examiner role too —
            # a member whose archive copy was moved out (quarantined/discarded)
            # would 404 its thumbnail here just like it would in the list. Without
            # this, an examiner's person detail page counted members people_rows
            # had already excluded, so the header's photo_n could run higher than
            # the same person's photo_count on their own list card.
            arc = entries.get(m)
            if arc and not os.path.exists(arc):
                continue
            md = metadata_index.get(m, {}) or {}
            # metadata falls back to scene_index when the member was excluded
            # from the universe (e.g. a scanned-doc image in this cluster).
            cat = (universe.get(m) or {}).get("category") or (clip.get(m) or {}).get("category")
            photos.append({"id": m, "kind": "photo", "name": os.path.basename(m),
                           "scene": cat, "place": md.get("place")})
    return {
        "person_id": person_id,
        "name": person_display_name(person_id, identities),
        "named": bool(_identity_name(person_id, identities)),
        "photo_count": len(photos),
        "members": photos + videos,  # photographs first, then video appearances
        "photo_n": len(photos),
        "video_n": len(videos),
    }


def scanned_image_rows(scene_index, archive_map, metadata_index, role, *,
                       frame_map=None, cap=None, released=None):
    """Images CLIP-tagged as scanned documents / handwritten letters (category in
    SCENE_LABELS). They are NOT in document_classifications (never OCR'd), so they
    are surfaced here (Correspondence) rather than lost when build_photo_universe
    excludes them from the gallery. Role-gated like the universe.

    Video keyframes are excluded (same as build_photo_universe): a frame CLIP-tagged
    as a scanned document is still a video still, not correspondence.

    `released` is the examiner's scanned_released overlay (BACKLOG #19) — an item
    the examiner marked "not a document" leaves this list (it rejoins the photo
    gallery instead, via build_photo_universe's matching `scanned_released` param)."""
    clip = scene_index.get("clip_results", {}) or {}
    junk = set((scene_index.get("junk_results", {}) or {}).keys())
    frame_set = set(frame_map or {})
    entries = archive_map.get("entries", {}) or {}
    released = released or ()
    rows = []
    for src, rec in clip.items():
        if not is_image(src) or rec.get("category") not in SCENE_LABELS:
            continue
        if src in released:
            continue
        if is_video_frame(src, frame_set):
            continue
        delivered = bool(rec.get("delivered", True))
        archive = entries.get(src)
        if role == "family":
            if src in junk or not delivered:
                continue
            if not archive or not os.path.exists(archive):
                continue
        md = metadata_index.get(src, {}) or {}
        rows.append({
            "id": src,
            "name": os.path.basename(src),
            "scene": SCENE_LABELS.get(rec.get("category")) or rec.get("category"),
            "place": md.get("place"),
            "ts": md.get("timestamp"),
            "delivered": delivered,
        })
    rows.sort(key=lambda r: (r.get("ts") or ""), reverse=True)
    if cap is not None and len(rows) > cap:
        log(f"scanned images: capping at {cap} of {len(rows)}")
        rows = rows[:cap]
    return rows


def _identity_name(pid, identities):
    """cluster_identities entries are {pid: {"name": ...}} or {pid: "name"}."""
    ent = (identities or {}).get(pid)
    if isinstance(ent, dict):
        return (ent.get("name") or "").strip() or None
    if isinstance(ent, str):
        return ent.strip() or None
    return None


def person_display_name(pid, identities):
    return _identity_name(pid, identities) or str(pid).replace("_", " ")


def places_data(rows):
    """Map points (photos with GPS) + per-trip aggregates, from photo rows.

    Each point carries its photo `id` (the canonical src that /media + the
    lightbox resolve) so a marker can drill into the image; each trip carries
    `member_ids` (the photo ids in that location) so a location can be opened as
    a filtered grid or exported as a collection.
    """
    points = []
    trips = {}
    for r in rows:
        gps = r.get("gps")
        if not gps or gps.get("lat") is None or gps.get("lon") is None:
            continue
        trip = r.get("trip")
        if trip and trip.lower().startswith("unknown"):
            trip = None
        points.append({
            "id": r.get("id"),
            "lat": gps["lat"], "lon": gps["lon"],
            "place": r.get("place"), "trip": trip,
            "name": r["name"], "thumb": r.get("thumb"),
        })
        key = trip or r.get("place")
        if key:
            t = trips.setdefault(key, {"name": key, "count": 0, "lat": 0.0, "lon": 0.0,
                                       "member_ids": []})
            t["count"] += 1
            t["lat"] += gps["lat"]
            t["lon"] += gps["lon"]
            if r.get("id"):
                t["member_ids"].append(r["id"])
    trip_list = []
    for t in trips.values():
        if t["count"]:
            t["lat"] /= t["count"]
            t["lon"] /= t["count"]
        trip_list.append(t)
    trip_list.sort(key=lambda t: t["count"], reverse=True)
    return {"points": points, "trips": trip_list}


# Sentinel for document_rows' subcategory override decode (§14.2): a placement
# dict may carry subcategory=None (a legitimate value distinct from "absent"), so
# we cannot use None to mean "no override". `_UNSET` means "the placement did not
# override subcategory → fall back to the pipeline d.get('subcategory')".
_UNSET = object()


def document_rows(summary, ocr_index, role, *, cap=None, doc_placements=None):
    ocr_by_file = {}
    text_kind_by_file = {}
    for rec in ocr_index or []:
        f = rec.get("file")
        if not f:
            continue
        if rec.get("ocr_text"):
            ocr_by_file[f] = rec["ocr_text"]
        if rec.get("text_kind"):
            text_kind_by_file[f] = rec["text_kind"]
    rows = []
    excluded_creds = 0
    excluded_email = 0
    for d in summary.get("document_classifications", []) or []:
        # Emails are surfaced in their own section (email_rows). Excluding them
        # here keeps Documents/Correspondence from being ~96% email, avoids
        # tripling them across sections, and stops them doubling the search index.
        if (d.get("source") or "").lower() == "email":
            excluded_email += 1
            continue
        derived = d.get("category") or "miscellaneous"
        if role == "family" and derived == "account_credentials":
            # LOAD-BEARING: family drop keys on the DERIVED (pipeline) category,
            # never the overlay — a stale placement can't expose a re-classified
            # credential doc. (This is the whole security guard; do not weaken it.)
            # Surfaced separately as a security notice; never browse raw.
            excluded_creds += 1
            continue
        f = d.get("file")
        # §14.2 overlay decode — document_rows is the SOLE decoder of the raw
        # doc_placements value (str OR dict); every other consumer reads the
        # rendered row["category"]/row["subcategory"] strings. The family
        # account_credentials seal above keys on `derived` and fires FIRST, so
        # the overlay can only re-bucket among the family-VISIBLE categories.
        p = (doc_placements or {}).get(f)
        if isinstance(p, dict):
            cat = p.get("category") or derived
            sub_override = p.get("subcategory", _UNSET)   # present-but-None allowed
        elif isinstance(p, str):
            cat, sub_override = p, _UNSET
        else:
            cat, sub_override = derived, _UNSET
        subcat = sub_override if sub_override is not _UNSET else d.get("subcategory")
        if cat != "financial":
            subcat = None            # subcategory is meaningless outside financial
        preview = (ocr_by_file.get(f, "") or "").strip().replace("\n", " ")
        rows.append({
            "file": f,
            "name": d.get("filename") or os.path.basename(f or ""),
            "category": cat,
            "subcategory": subcat,
            "text_kind": text_kind_by_file.get(f),
            "significance": d.get("significance"),
            "summary": neutralize_summary((d.get("summary") or "")[:400]),
            "preview": preview[:240],
        })
    rows.sort(key=lambda r: (r.get("significance") or 0), reverse=True)
    if role == "family" and excluded_creds:
        log(f"documents: excluded {excluded_creds} account_credentials docs from family browse")
    if excluded_email:
        log(f"documents: excluded {excluded_email} email items (surfaced in Emails)")
    if cap is not None and len(rows) > cap:
        log(f"documents: capping at {cap} of {len(rows)} rows")
        rows = rows[:cap]
    return rows


_RECORDING_NAME_LEADING_DASH = re.compile(r"^\s*-\s*")


def _clean_recording_name(name):
    """Strip a leading '- ' artifact from a blank-caller Google Voice export
    filename (e.g. ' - Voicemail - 2010-01-29T17_40_25Z.mp3' — the caller
    portion is empty, not the literal string '-'). Showing it verbatim in the
    Recordings heading reads as a stray dash rather than an honest unnamed
    caller (usability review #26)."""
    name = name or ""
    stripped = _RECORDING_NAME_LEADING_DASH.sub("", name)
    return stripped or name


# ── estate document report (attorney-facing) ─────────────────────────────────

def estate_report_data(vital_docs, case_id=None, generated_at=None):
    """A statement of position on the estate's vital documents, written for
    somebody outside the review — an attorney — rather than for the reviewer.

    The whole point of this view is a distinction the rest of the UI blurs:
    **"we looked and it is not there" is not the same claim as "we have not
    finished looking"**, and on screen they occupy the same empty space. An
    attorney who reads a blank row as "no such document exists" and advises on
    that basis has been misled by us, not by the archive.

    So every type lands in exactly one of three groups, and the third one is
    deliberately hard to qualify for:

      present      at least one candidate has been signed off. We have it.
      unconfirmed  something matched — a candidate, or only weaker near-misses —
                   and nobody has ruled on it. We cannot say either way.
      absent       nothing matched at all, at any strength. Only this group
                   supports the sentence "the archive does not contain one",
                   and even then only as far as retrieval reached (see below).

    A type with a signed-off document AND outstanding candidates is `present`:
    the estate has the document. The outstanding count still rides on the row,
    because "we have a will, and four more candidates nobody has looked at" is
    the true state and the reader is entitled to it.

    Returns totals, the three groups, and a `limitations` list built FROM THE
    DATA rather than written by hand — a caveat that does not update when the
    numbers do is worse than no caveat.
    """
    vital_docs = vital_docs or {}
    if not vital_docs.get("available"):
        return {"available": False, "case_id": case_id, "generated_at": generated_at}

    per_target_k = vital_docs.get("per_target_k")
    rows = []
    for t in vital_docs.get("targets", []) or []:
        items = t.get("items") or []
        signed = sum(1 for i in items if i.get("reviewed"))
        near = t.get("near_miss_count") or 0
        if signed:
            state = "present"
        elif items or near:
            state = "unconfirmed"
        else:
            state = "absent"
        rows.append({
            "target": t.get("target"),
            "label": t.get("label"),
            "state": state,
            "candidates": len(items),
            "signed_off": signed,
            "undecided": len(items) - signed,
            "near_misses": near,
            # This type's candidate list hit the retrieval ceiling, so its counts
            # are a floor. Carried per row because it changes what the row MEANS.
            "capped": bool(t.get("near_miss_capped")),
        })

    def tally(key):
        return [r for r in rows if r["state"] == key]

    groups = [
        {"key": "present", "label": "Confirmed present",
         "note": "A document of this type has been found and signed off.",
         "types": tally("present")},
        {"key": "unconfirmed", "label": "Not yet established",
         "note": "Something matched, but nobody has ruled on it. This is not a "
                 "statement that the document does or does not exist.",
         "types": tally("unconfirmed")},
        {"key": "absent", "label": "Nothing matched",
         "note": "No candidate and no weaker match, at any strength.",
         "types": tally("absent")},
    ]

    totals = {
        "types": len(rows),
        "present": len(tally("present")),
        "unconfirmed": len(tally("unconfirmed")),
        "absent": len(tally("absent")),
        "candidates": sum(r["candidates"] for r in rows),
        "signed_off": sum(r["signed_off"] for r in rows),
        "undecided": sum(r["undecided"] for r in rows),
        "near_misses": sum(r["near_misses"] for r in rows),
    }

    capped = [r for r in rows if r["capped"]]
    # Thousands separators: this text is printed and handed to somebody outside
    # the project, where "1147" reads as a typo rather than a figure.
    def n(x):
        return "{:,}".format(x)
    limitations = []
    if capped and per_target_k:
        limitations.append(
            "Retrieval stopped at {k} candidates per document type. {n} of the {t} "
            "types reached that limit, so for those the counts below are a floor: "
            "documents past the {k}th were never retrieved, and so were never "
            "assessed.".format(k=n(per_target_k), n=n(len(capped)), t=n(len(rows))))
    if totals["undecided"]:
        limitations.append(
            "{n} candidate documents have been found but not yet reviewed.".format(
                n=n(totals["undecided"])))
    if totals["near_misses"]:
        limitations.append(
            "{n} weaker matches have not been reviewed. A document of a type shown "
            "as not established may be among them.".format(n=n(totals["near_misses"])))
    if not totals["absent"]:
        limitations.append(
            "No document type can currently be reported as absent from this "
            "archive: every type still has unreviewed matches.")
    limitations.append(
        "This describes only the material supplied to the archive. It is not a "
        "search of public records, and its absence of a document is not evidence "
        "that none exists elsewhere.")

    return {
        "available": True,
        "case_id": case_id,
        "generated_at": generated_at,
        "per_target_k": per_target_k,
        "totals": totals,
        "groups": groups,
        "limitations": limitations,
    }


def _report_label(slug):
    """A category slug as a person would read it: 'work_correspondence' →
    'Work correspondence'. There is no Python `pretty()` in this module — the
    front end has its own — so report labels are built here rather than shipped
    as slugs and prettified twice with two different results."""
    return (slug or "").replace("_", " ").strip().capitalize() or "Uncategorised"


def _report_span(timeline):
    """(earliest, latest) dated day across the timeline's chapters, or (None, None).

    Read off the chapters rather than re-scanning every item: the chapters are
    already the dated material, grouped, and a chapter that somehow carries no
    dates simply contributes nothing instead of poisoning the range with "".
    """
    lo = hi = None
    for c in (timeline or {}).get("chapters", []) or []:
        a, b = c.get("date_from"), c.get("date_to")
        for d in (a, b):
            if not d:
                continue
            if lo is None or d < lo:
                lo = d
            if hi is None or d > hi:
                hi = d
    return lo, hi


def family_report_data(*, counts, scene_counts, audio_rows_, document_index,
                       email_categories, timeline, people, case_id=None,
                       generated_at=None):
    """An orientation document for somebody who has just been handed the archive.

    Different question from the estate report, and so a different shape. That one
    asks "do we hold the paperwork" and is answerable in three states. This one
    asks "what is in here, and what does it hold" — the thing a family member
    wants before they know what to click.

    Two rules it inherits from the rest of this codebase, because both have
    already burned us here:

      * Count documents from the DOCUMENTS INDEX, never from
        summary.document_classifications. The latter classifies every email as a
        document too — on 813_mf that is the difference between 4,643 and about
        59,000, and the larger number has been shipped to a screen before.

      * Say what is NOT known as prominently as what is. Two thirds of the
        photographs carry no date; most identified faces have no name. A report
        that lists holdings and omits that is describing a more complete archive
        than the one that exists.
    """
    counts = counts or {}
    lo, hi = _report_span(timeline)
    undated = ((timeline or {}).get("undated") or {}).get("count") or 0

    people = people or []
    named = sum(1 for p in people if p.get("named"))

    def rows(pairs):
        return [{"label": lab, "count": n} for lab, n in pairs if n]

    sections = []

    # Photographs by what the classifier saw in them — the most browsable cut,
    # and the one that reads least like an inventory.
    sections.append({
        "key": "photos", "label": "Photographs, by what is in them",
        "note": "A photograph can be in more than one of these.",
        "items": rows(sorted((scene_counts or {}).items(), key=lambda kv: -kv[1])),
    })

    # Recordings by kind, folded through the same mapping the Recordings page uses.
    kind_counts = {}
    for r in audio_rows_ or []:
        k = r.get("kind") or "other"
        kind_counts[k] = kind_counts.get(k, 0) + 1
    sections.append({
        "key": "recordings", "label": "Recordings, by kind",
        "note": "Grouped by the pipeline's classification, which is approximate — "
                "see the limitations below.",
        "items": rows([(audio_kind_label(k), kind_counts.get(k, 0))
                       for k in AUDIO_KIND_ORDER]),
    })

    sections.append({
        "key": "documents", "label": "Documents, by kind",
        "note": "Scanned and digital documents. Emails are counted separately.",
        "items": rows(sorted(
            [(_report_label(c.get("category")), c.get("count") or 0)
             for c in (document_index or [])], key=lambda kv: -kv[1])),
    })

    sections.append({
        "key": "emails", "label": "Email conversations, by subject",
        "note": "A conversation can be in more than one of these.",
        "items": rows(sorted(
            [(_report_label(c.get("name")), c.get("count") or 0)
             for c in (email_categories or [])], key=lambda kv: -kv[1])),
    })

    limitations = []
    photos = counts.get("photos") or 0
    if undated:
        share = " — about {}%".format(round(100.0 * undated / photos)) if photos else ""
        limitations.append(
            "{u:,} items carry no date{share}. They are in the archive and "
            "searchable, but they cannot appear on the timeline or in any figure "
            "above that is grouped by year.".format(u=undated, share=share))
    if people:
        unnamed = len(people) - named
        if unnamed:
            limitations.append(
                "{n:,} of the {t:,} people recognised have not been given a name. "
                "They are distinct faces the archive can group, not strangers — "
                "naming them is a person's job and has not been finished.".format(
                    n=unnamed, t=len(people)))
    limitations.append(
        "Recordings are grouped by an automatic classification that is known to "
        "be unreliable on this collection; treat the recording kinds as a "
        "starting point rather than a fact.")
    limitations.append(
        "This describes only the material that was supplied. Anything never "
        "collected — a device not handed over, an account not exported — is "
        "absent from these figures without appearing as a gap.")

    return {
        "case_id": case_id,
        "generated_at": generated_at,
        "span": {"from": lo, "to": hi, "undated": undated},
        "headline": [
            {"label": "photographs", "count": counts.get("photos") or 0},
            {"label": "videos", "count": counts.get("videos") or 0},
            {"label": "recordings", "count": counts.get("audio") or 0},
            {"label": "documents", "count": counts.get("documents") or 0},
            {"label": "email conversations", "count": counts.get("emails") or 0},
            {"label": "message threads", "count": counts.get("messages") or 0},
        ],
        "people": {"total": len(people), "named": named,
                   "unnamed": len(people) - named,
                   "top": [{"name": p.get("name"), "photos": p.get("photo_count") or 0}
                           for p in people if p.get("named")][:12]},
        "places": counts.get("places") or 0,
        "sections": sections,
        "limitations": limitations,
    }


# ── pipeline report: what was examined, and what reached the archive ─────────

def _pct(part, whole):
    """part as a percentage of whole, or None when the question is meaningless.
    Returned as a number so the page can decide how to render it."""
    if not whole:
        return None
    return round(100.0 * part / whole, 1)


def _human_bytes(n):
    """Bytes as a person reads them. Binary steps, decimal label — what every
    file manager shows, so the figure matches what the user sees in Finder."""
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" % (n, unit)) if unit in ("B", "KB") else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%.1f TB" % n


def _stage_timeline(summaries):
    """(first, last, [(stage, ts)]) across every stage summary that recorded a
    timestamp. Sorted by time, not by pipeline order — a stage that was re-run
    lands where it actually happened, which is the honest picture of a run."""
    seen = []
    for name, doc in sorted((summaries or {}).items()):
        if not isinstance(doc, dict):
            continue
        ts = doc.get("timestamp") or doc.get("ts")
        if not isinstance(ts, str) or not ts:
            continue
        seen.append({"stage": doc.get("step") or doc.get("stage")
                     or name.replace("_summary.json", "").replace(".json", ""),
                     "at": ts})
    seen.sort(key=lambda x: x["at"])
    return (seen[0]["at"] if seen else None,
            seen[-1]["at"] if seen else None,
            seen)


def _elapsed_words(first, last):
    """Plain-language gap between two ISO timestamps, or None. Deliberately
    coarse: this is wall clock between the first and last stage a run recorded,
    which includes any time the machine sat idle between them, so a figure to
    the second would imply a precision it does not have."""
    if not first or not last:
        return None
    try:
        a = datetime.fromisoformat(first.split("+")[0].replace("Z", ""))
        b = datetime.fromisoformat(last.split("+")[0].replace("Z", ""))
    except ValueError:
        return None
    secs = (b - a).total_seconds()
    if secs < 0:
        return None
    days, rem = divmod(int(secs), 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return "%d day%s %d hour%s" % (days, "" if days == 1 else "s",
                                       hours, "" if hours == 1 else "s")
    if hours:
        return "%d hour%s %d minute%s" % (hours, "" if hours == 1 else "s",
                                          mins, "" if mins == 1 else "s")
    return "%d minute%s" % (mins, "" if mins == 1 else "s")


def pipeline_report_data(*, summaries, counts, sizes=None, case_id=None,
                         generated_at=None):
    """What the pipeline went through to produce this archive.

    A different question again from the other two reports: not "do we hold the
    paperwork" and not "what is in here", but "what was looked at, and how much
    of it survived to be looked at again".

    Every row is examined → surfaced with the share, plus a sentence saying where
    the difference went, because the difference is the interesting part and it is
    never the same story twice: photographs mostly lost to duplicates, mail
    mostly to bulk triage, documents to both.

    The comparison is only honest where the two numbers count the same kind of
    thing, and for mail they do not — 80,486 messages become 21,988
    CONVERSATIONS, so a percentage there would be arithmetic on two different
    units. Those rows carry the counts and no share.
    """
    summaries = summaries or {}
    counts = counts or {}

    def sm(name, *keys, default=0):
        doc = summaries.get(name) or {}
        for k in keys:
            if not isinstance(doc, dict):
                return default
            doc = doc.get(k)
            if doc is None:
                return default
        return doc

    collect = summaries.get("collect_dedup_summary.json") or {}
    types = collect.get("type_counts") or {}

    photos_found = types.get("image") or collect.get("source_photos_found") or 0
    videos_found = types.get("video") or collect.get("source_videos_found") or 0
    docs_found = types.get("document") or collect.get("source_docs_found") or 0
    audio_found = sm("transcription_summary.json", "total_audio") or types.get("audio") or 0
    mail_found = sm("email_triage_summary.json", "email_count")
    convs_written = sm("message_triage_summary.json", "conversation_files_written")

    photo_dupes = (collect.get("exact_dupes_moved") or 0) + \
                  (collect.get("perceptual_dupes_moved") or 0)
    doc_dupes = collect.get("docs_exact_dupes_moved") or 0
    video_dupes = collect.get("video_exact_dupes_moved") or 0
    mail_bulk = sm("email_triage_summary.json", "triage", "discarded_bulk")
    mail_platform = sm("email_triage_summary.json", "triage", "discarded_platform")
    mail_rescued = sm("email_triage_summary.json", "triage", "rescued_by_estate_keywords")
    mail_kept = sm("email_triage_summary.json", "kept_count")

    rows = [
        {"kind": "Photographs", "examined": photos_found,
         "surfaced": counts.get("photos") or 0,
         "note": "{:,} were duplicates of another photograph — {:,} byte-identical, "
                 "{:,} the same picture saved again at a different size or "
                 "quality.".format(photo_dupes, collect.get("exact_dupes_moved") or 0,
                                   collect.get("perceptual_dupes_moved") or 0)},
        {"kind": "Videos", "examined": videos_found,
         "surfaced": counts.get("videos") or 0,
         "note": "{:,} were byte-identical duplicates.".format(video_dupes)},
        {"kind": "Documents", "examined": docs_found,
         "surfaced": counts.get("documents") or 0,
         "note": "{:,} were byte-identical duplicates; the rest were read but did "
                 "not resolve to a document the archive shows — see the OCR "
                 "figures below.".format(doc_dupes)},
        {"kind": "Recordings", "examined": audio_found,
         "surfaced": counts.get("audio") or 0,
         "note": "{:,} could not be transcribed.".format(
             sm("transcription_summary.json", "failed"))},
        # Unit change: messages in, conversations out. No share — see docstring.
        {"kind": "Emails", "examined": mail_found,
         "surfaced": counts.get("emails") or 0, "share": None,
         "unit_change": "messages examined, conversations surfaced",
         "note": "{:,} were kept as worth reading and {:,} discarded as bulk "
                 "({:,} more were pulled back out of the discards because they "
                 "mentioned the estate). The kept messages were then grouped into "
                 "conversations, which is what the archive shows.".format(
                     mail_kept, mail_bulk + mail_platform, mail_rescued)},
        {"kind": "Message threads", "examined": convs_written,
         "surfaced": counts.get("messages") or 0,
         "note": "Conversations the pipeline wrote out, against those the archive "
                 "shows; the difference was triaged away as noise."},
    ]
    for r in rows:
        if "share" not in r:
            r["share"] = _pct(r["surfaced"], r["examined"])

    first, last, stages = _stage_timeline(summaries)

    sizes = sizes or {}
    parts = sorted(((k, v) for k, v in (sizes.get("parts") or {}).items() if v),
                   key=lambda kv: -kv[1])
    total_bytes = sizes.get("total") or 0

    audio_secs = sm("transcription_summary.json", "total_duration_seconds") or 0

    return {
        "case_id": case_id,
        "generated_at": generated_at,
        "rows": rows,
        "totals": {
            "examined": sum(r["examined"] for r in rows),
            "surfaced": sum(r["surfaced"] for r in rows),
        },
        "expansion": {
            "archives_found": sm("expandfiles_summary.json", "archives_found"),
            "files_added": sm("expandfiles_summary.json", "files_added"),
            "email_attachments": sm("expandfiles_summary.json", "email_attachments"),
        },
        "reading": {
            "documents_read": sm("ocr_summary.json", "total_documents"),
            "text_recovered": sm("ocr_summary.json", "ocr_results_count"),
            "audio_hours": round(audio_secs / 3600.0, 1) if audio_secs else 0,
        },
        "size": {
            "total_bytes": total_bytes,
            "total_human": _human_bytes(total_bytes),
            "parts": [{"name": k, "bytes": v, "human": _human_bytes(v),
                       "share": _pct(v, total_bytes)} for k, v in parts],
            "files": sizes.get("files") or 0,
        },
        "run": {"first": first, "last": last, "elapsed": _elapsed_words(first, last),
                "stages": stages},
    }


# ── online accounts: services found in the mail ───────────────────────────────

# Free consumer mail providers. A domain here is where PEOPLE have addresses, not
# a service the estate holds an account with, and they are the highest-volume
# domains in any mailbox — left in, they bury the finding under the obvious.
CONSUMER_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "hotmail.com",
    "outlook.com", "live.com", "msn.com", "aol.com", "icloud.com", "me.com",
    "mac.com", "comcast.net", "verizon.net", "att.net", "sbcglobal.net",
    "cox.net", "charter.net", "earthlink.net", "protonmail.com", "proton.me",
}

# Subject lines an account sends and a correspondent does not. This is the
# evidence that a domain is somewhere the owner HELD an account, rather than a
# company that merely emailed them — a shop's newsletter never says "reset your
# password". Deliberately narrow: a false positive here adds a bogus account to
# an estate inventory, which is worse than missing one, because somebody will go
# looking for it.
_ACCOUNT_SUBJECT_RE = re.compile(
    r"password|verify your|verification code|sign[- ]?in|log[- ]?in|welcome to|"
    r"your account|two[- ]factor|2fa|confirm your|activate your|security alert|"
    r"new device|reset your|account statement|e[- ]?statement",
    re.IGNORECASE)


_PARTICIPANT_ADDR_RE = re.compile(r"<([^>]+)>")


def _email_address(participant):
    """The bare address inside a thread participant string. Mixed forms arrive —
    'Display Name <addr>' or a bare address — so take the angle-bracket form when
    there is one. Defined in this module rather than the server because the server
    imports from here, not the other way round."""
    m = _PARTICIPANT_ADDR_RE.search(participant or "")
    return (m.group(1) if m else (participant or "")).strip().lower()


def account_root(domain):
    """The registrable-ish root of a mail domain: 'rs.email.nextdoor.com' →
    'nextdoor.com'.

    Last two labels. Crude — it is wrong for 'co.uk' style suffixes — but the
    alternative is shipping a public-suffix list into a fork whose whole point is
    UI work, and the failure mode is a service listed under a slightly wrong
    name rather than a wrong count. It earns its place by fixing the opposite
    problem, which is real and visible: one service arriving as several rows
    (linkedin.com, e.linkedin.com and em.linkedin.com are one account, and an
    events service was split five ways on 813_mf).
    """
    parts = [p for p in (domain or "").strip().lower().strip(".").split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else (parts[0] if parts else "")


def account_services(threads, inventory=None, owner_addresses=()):
    """Services the estate appears to hold an account with, found in the mail.

    Two sources, deliberately merged rather than shown apart:

      * `inventory` — the pipeline's digital_account_inventory, built from the
        RAW corpus before triage.
      * the threads themselves, which the pipeline's inventory never looked at
        for this purpose. On 813_mf the inventory holds 23 social and newsletter
        domains; the threads carry every bank, brokerage and insurer the estate
        deals with, and not one of them was on the page.

    The count on each row is the number of conversations THE EMAILS PAGE CAN
    ACTUALLY OPEN, not the pipeline's raw figure, because the row is a link and a
    link must deliver what it promises. Those two numbers diverge a long way:
    triage discards bulk notification mail, so a service with hundreds of raw
    notifications can have a handful of readable threads, or none. Where the raw
    figure is higher it rides along as `filtered_out` so the gap is explained
    rather than merely absorbed.
    """
    owner_domains = {(a or "").split("@")[-1].strip().lower()
                     for a in (owner_addresses or []) if "@" in (a or "")}
    owner_roots = {account_root(d) for d in owner_domains}

    pipeline = {}
    for domain, rec in (inventory or {}).items():
        root = account_root(domain)
        if not root:
            continue
        pipeline[root] = pipeline.get(root, 0) + ((rec or {}).get("count") or 0)

    reach = {}
    signals = {}
    for t in threads or []:
        subject = t.get("subject") or ""
        is_signal = bool(_ACCOUNT_SUBJECT_RE.search(subject))
        roots = set()
        for p in (t.get("participants") or []):
            a = _email_address(p)
            if "@" not in a:
                continue
            dom = a.split("@")[-1]
            root = account_root(dom)
            if root and root not in owner_roots and root not in CONSUMER_MAIL_DOMAINS:
                roots.add(root)
        for r in roots:
            reach[r] = reach.get(r, 0) + 1
            if is_signal:
                signals[r] = signals.get(r, 0) + 1

    # A domain earns a row by being something the pipeline already called an
    # account, or by sending mail that is PREDOMINANTLY account-shaped. The
    # second test needs both halves. Counting signals alone put the deceased's
    # own firm at the top of the list on 813_mf — 2,474 threads of ordinary
    # correspondence containing, somewhere, six messages that said "password" —
    # because a person emailing you for years will eventually use every word a
    # service uses. A service's mail is transactional nearly all the way through:
    # a bank here runs 20-40% account-shaped, a colleague runs a fraction of one
    # percent.
    #
    # Set narrow on purpose. It drops a low-volume subscription or two, and that
    # is the right way to be wrong: a false entry in an estate inventory sends
    # somebody hunting for an account that was never there.
    def looks_like_a_service(r):
        n, sig = reach.get(r, 0), signals.get(r, 0)
        return sig >= 2 and n and (sig / float(n)) >= 0.02

    roots = {r for r in reach if looks_like_a_service(r)} | set(pipeline)
    out = []
    for r in roots:
        if r in owner_roots or r in CONSUMER_MAIL_DOMAINS:
            continue
        threads_n = reach.get(r, 0)
        raw = pipeline.get(r)
        out.append({
            "service": r,
            "threads": threads_n,
            "signals": signals.get(r, 0),
            "from_pipeline": r in pipeline,
            # Raw notification mail the triage stage dropped before the Emails
            # section ever saw it. None when there is nothing to explain.
            "filtered_out": (raw - threads_n) if (raw and raw > threads_n) else None,
        })
    # The ones that look most like a held account first, then by how much there
    # is to read. A bank with ten sign-in alerts outranks a newsletter with 700.
    out.sort(key=lambda x: (-x["signals"], -x["threads"], x["service"]))
    return out


# ── audio kinds ───────────────────────────────────────────────────────────────
# The classifier emits seven categories; the Recordings page groups them into six
# KINDS, because "what sort of listening is this" is the question someone browsing
# an estate's audio is actually asking.
#
# Two of the six are deliberately not a straight rename:
#
#   * `voicemail` stays apart from `voice_memo` even though both are one person
#     talking. A voicemail is somebody ELSE's voice — very often the voice of the
#     person who died — and there are a couple of dozen of them against several
#     hundred self-recorded notes. Merged, they vanish.
#
#   * `non_speech` is not a kind of recording at all; it is a processing outcome.
#     On 813_mf its members are EXACTLY the set with no transcript, and their
#     summary is the literal string "No usable speech transcript." Their filenames
#     are numbered album tracks. Calling that bucket "other" would file a few
#     hundred songs under nothing-in-particular, so it is labelled for what it
#     actually reports: transcription returned nothing.
#
# An unrecognised category falls to "other" rather than being dropped — a new
# classifier label must never make recordings disappear from the page.
AUDIO_KINDS = [
    ("voicemail",     "Voicemail",                ("voicemail",)),
    ("voice_note",    "Voice notes",              ("voice_memo",)),
    ("conversation",  "Conversations & meetings", ("personal_recording",
                                                   "interview_or_meeting")),
    ("music",         "Music & performance",      ("music_or_performance",)),
    ("untranscribed", "Nothing was transcribed",  ("non_speech",)),
    ("other",         "Other",                    ("miscellaneous",)),
]
AUDIO_KIND_ORDER = [k for k, _, _ in AUDIO_KINDS]
AUDIO_KIND_LABELS = {k: lab for k, lab, _ in AUDIO_KINDS}
_AUDIO_CATEGORY_TO_KIND = {c: k for k, _, cats in AUDIO_KINDS for c in cats}


def audio_kind(category):
    """The Recordings-page kind slug for one classifier category. Anything the
    mapping does not know — a new label, a blank, None — lands in "other", so the
    page keeps showing the recording instead of silently losing it."""
    return _AUDIO_CATEGORY_TO_KIND.get((category or "").strip().lower(), "other")


def audio_kind_label(kind):
    """Display name for a kind slug."""
    return AUDIO_KIND_LABELS.get(kind, "Other")


def audio_rows(summary, transcription_index, role, cfg):
    deliver = cfg.get("transcribe", {}).get("deliver", True)
    if role == "family" and deliver is False:
        log("audio: transcribe.deliver is false — omitting audio from family build")
        return []
    rec_by_file = {}
    for rec in transcription_index or []:
        f = rec.get("file")
        if f:
            rec_by_file[f] = rec
    rows = []
    for a in summary.get("audio_classifications", []) or []:
        f = a.get("file")
        tr = rec_by_file.get(f, {})
        preview = (tr.get("transcript_text") or a.get("transcript_txt", "") or "").strip()
        rows.append({
            "file": f,
            "name": _clean_recording_name(a.get("filename") or os.path.basename(a.get("file", ""))),
            "category": a.get("category") or "uncategorized",
            # The grouping the Recordings page reads. Kept beside the raw category
            # rather than replacing it: the classifier is demonstrably unsure of
            # itself — on 813_mf, 17 of the 47 recordings that exist in two file
            # formats got a DIFFERENT category for each copy — so the examiner
            # needs to see what it actually said, not only where we filed it.
            "kind": audio_kind(a.get("category")),
            "kind_label": audio_kind_label(audio_kind(a.get("category"))),
            "significance": a.get("significance"),
            "summary": neutralize_summary(a.get("summary")),
            "duration": a.get("duration") if a.get("duration") is not None else tr.get("duration"),
            "language": a.get("language") or tr.get("language"),
            # G-3: additive fields the recording detail / list affordance need — a
            # per-segment count (drives "has timing" hint) and whether ANY transcript
            # text exists (drives the "has transcript" affordance in the list).
            "segment_count": tr.get("segment_count"),
            "has_transcript": bool(preview),
            "preview": preview[:280].replace("\n", " "),
        })
    rows.sort(key=lambda r: (r.get("significance") or 0), reverse=True)
    return rows


def _vtt_timestamp(s):
    """Parse a WEBVTT/SRT timestamp ('HH:MM:SS.mmm', 'MM:SS.mmm', or with a ','
    decimal separator) to float seconds. Returns None on a malformed value."""
    s = (s or "").strip().replace(",", ".")
    if not s:
        return None
    try:
        parts = [float(p) for p in s.split(":")]
    except ValueError:
        return None
    if not parts:
        return None
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


def parse_vtt(text):
    """Minimal stdlib WEBVTT cue parser → [{start, end, text}] in file order.

    Handles the standard shape the transcribe stage emits: a 'WEBVTT' header,
    blank-line-separated cues, each an optional identifier line + a
    'start --> end' timing line + one or more text lines. The header block and
    any cue without a valid timing line are skipped (never raises)."""
    segments = []
    if not text:
        return segments
    blocks = re.split(r"\n[ \t]*\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip() != ""]
        if not lines:
            continue
        timing_idx = None
        for i, ln in enumerate(lines):
            if "-->" in ln:
                timing_idx = i
                break
        if timing_idx is None:
            continue  # WEBVTT header / NOTE / STYLE block
        m = re.match(r"\s*([0-9:.,]+)\s*-->\s*([0-9:.,]+)", lines[timing_idx])
        if not m:
            continue
        start = _vtt_timestamp(m.group(1))
        end = _vtt_timestamp(m.group(2))
        if start is None:
            continue
        body = " ".join(lines[timing_idx + 1:]).strip()
        segments.append({"start": start, "end": end if end is not None else start,
                         "text": body})
    return segments


def transcript_detail(transcription_index, file, read_sidecar, *, has_audio=False):
    """Build the seek-synced recording-detail payload for ONE recording.

    Looks up `file` in the transcription_index, then reads its per-segment timings
    from the `.json` sidecar (preferred — carries start/end per segment) or, failing
    that, parses the `.vtt` sidecar. `read_sidecar(path) -> text | None` is supplied
    by the caller and MUST apply the on-disk containment check (the raw sidecar path
    from the index is never trusted); an absent/unreadable/refused sidecar yields
    empty segments and the caller's UI degrades to the plain `transcript_text`.

    Returns None when `file` is not a known recording (→ 404). `has_audio` is passed
    by the caller (whether the audio itself is servable) so the reaped-media case
    (goog/appl) still returns the text with has_audio=False."""
    rec = None
    for r in transcription_index or []:
        if r.get("file") == file:
            rec = r
            break
    if rec is None:
        return None
    segments = []
    js = rec.get("json_sidecar")
    if js:
        raw = read_sidecar(js)
        if raw:
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                data = None
            for s in (data or {}).get("segments") or []:
                if s.get("start") is None:
                    continue
                try:
                    start = float(s.get("start"))
                    end = float(s.get("end")) if s.get("end") is not None else start
                except (TypeError, ValueError):
                    continue
                segments.append({"start": start, "end": end,
                                 "text": (s.get("text") or "").strip()})
    if not segments:
        vtt = rec.get("vtt_sidecar")
        if vtt:
            raw = read_sidecar(vtt)
            if raw:
                segments = parse_vtt(raw)
    return {
        "file": file,
        "segments": segments,
        "duration": rec.get("duration"),
        "language": rec.get("language"),
        "segment_count": rec.get("segment_count"),
        # The full transcript for the degrade path (reaped sidecar → no segments).
        "transcript_text": rec.get("transcript_text") or "",
        "has_audio": bool(has_audio),
    }


def timeline_rows(photo_rows_):
    out = []
    for r in photo_rows_:
        if not r.get("ts"):
            continue
        out.append({
            "date": r["ts"][:10], "ts": r["ts"], "type": "photo",
            "label": r["name"], "place": r.get("place"),
            "thumb": r.get("thumb"), "id": r["id"],
        })
    out.sort(key=lambda r: r["ts"])
    return out


# ── G-5 timeline / G-8 on-this-day / G-10 venues ──────────────────────────────
#
# These three builders sit alongside places_data/timeline_rows (which the static
# explorer still calls — left untouched). All three are PURE and take the geo
# index (per-file temporal/venue cluster ids) so the join lives in one place.

def _timeline_strip_photo(r):
    """A compact photo stub for a timeline event strip / venue preview — the exact
    subset photoCard() reads (id drives lazyThumb; caption is G-7 alt-text). Kept
    small so the whole-case structure stays a light payload."""
    return {
        "id": r.get("id"),
        "name": r.get("name"),
        "ts": r.get("ts"),
        "place": r.get("place"),
        "scene": r.get("scene"),
        "caption": r.get("caption"),
        "favorite": bool(r.get("favorite")),
    }


def _clean_location_label(label):
    """A location token from `compound_label` ('Portland_Oregon | 2012-06 | event
    133') or `place` → a display string, dropping the pipeline's Unknown_Location
    sentinel. Returns None when there's no real place. Underscores → spaces is left
    to the frontend's prettyPlace()."""
    if not label:
        return None
    token = str(label).split("|")[0].strip()
    if not token or token.lower().startswith("unknown"):
        return None
    return token


def timeline_data(photo_rows_, geo_index, summary, *, strip_cap=8, event_cap=200):
    """G-5: group delivered photos into chapter bands → event groups → photo strips.

    Photos are joined to geo_cluster_index.json per-file records by their working
    path (`id`): `temporal_chapter` bands them, `temporal_event_id` groups within a
    band, and `compound_label`/`place` label the band. Each event carries a CAPPED
    photo strip (`strip_cap`) plus its true `count`, so a 184-chapter/534-event case
    returns a light structure the frontend renders lazily (bands collapsed; strips
    small) rather than every photo inline.

    Timestamp-less / chapter-less media is invisible to the bands (as in
    timeline_rows); its count is surfaced as `undated` so nothing is silently
    hidden — from __clusters__.undated_file_count when present, else counted here.
    """
    geo_index = geo_index or {}
    clusters = geo_index.get("__clusters__", {}) or {}
    chapters = {}
    undated_rows = 0
    for r in photo_rows_:
        geo = geo_index.get(r.get("id"), {}) or {}
        ch = geo.get("temporal_chapter")
        ts = r.get("ts")
        if not ch or not ts:
            undated_rows += 1
            continue
        c = chapters.get(ch)
        if c is None:
            c = chapters[ch] = {"chapter": ch, "events": {}, "places": Counter(),
                                "count": 0, "min_ts": ts, "max_ts": ts}
        c["count"] += 1
        if ts < c["min_ts"]:
            c["min_ts"] = ts
        if ts > c["max_ts"]:
            c["max_ts"] = ts
        loc = _clean_location_label(r.get("place")) or _clean_location_label(geo.get("compound_label"))
        if loc:
            c["places"][loc] += 1
        eid = geo.get("temporal_event_id")
        ekey = "e" + str(eid) if eid is not None else "_none"
        ev = c["events"].get(ekey)
        if ev is None:
            ev = c["events"][ekey] = {"event_id": eid, "count": 0, "photos": [],
                                      "min_ts": ts, "max_ts": ts}
        ev["count"] += 1
        if ts < ev["min_ts"]:
            ev["min_ts"] = ts
        if ts > ev["max_ts"]:
            ev["max_ts"] = ts
        if len(ev["photos"]) < strip_cap:
            ev["photos"].append(_timeline_strip_photo(r))

    out_chapters = []
    for c in chapters.values():
        events = sorted(c["events"].values(), key=lambda e: (e["min_ts"], e["event_id"] is None))
        for e in events:
            e["date_from"] = e.pop("min_ts")[:10]
            e["date_to"] = e.pop("max_ts")[:10]
        dominant = c["places"].most_common(1)
        out_chapters.append({
            "chapter": c["chapter"],
            "label": dominant[0][0] if dominant else None,
            "date_from": c["min_ts"][:10],
            "date_to": c["max_ts"][:10],
            "count": c["count"],
            "event_count": len(events),
            "events": events[:event_cap],
        })
    # Newest chapter first (matches the gallery's newest-first default).
    out_chapters.sort(key=lambda c: c["chapter"], reverse=True)

    undated_count = clusters.get("undated_file_count")
    if undated_count is None:
        undated_count = undated_rows
    return {
        "chapters": out_chapters,
        "chapter_count": len(out_chapters),
        "event_count": sum(c["event_count"] for c in out_chapters),
        "undated": {"count": undated_count},
    }


def on_this_day_data(photo_rows_, today, *, per_year_cap=12):
    """G-8: photos taken on `today`'s month-day across years.

    `today` is injected by the caller (a 'YYYY-MM-DD' string or a date/datetime) so
    this stays pure/testable — the request boundary in family_archive.py is the only
    place that reads the wall clock. Filters the already-built photo rows by
    ts[5:10] == today's MM-DD, groups by year (newest year first), and caps each
    year's strip. Returns count 0 (empty years) when nothing matches — the caller
    decides whether to render or hide the card."""
    mmdd = str(getattr(today, "isoformat", lambda: today)())[5:10] if not isinstance(today, str) else today[5:10]
    years = {}
    total = 0
    for r in photo_rows_:
        ts = r.get("ts")
        if not ts or len(ts) < 10 or ts[5:10] != mmdd:
            continue
        total += 1
        y = ts[:4]
        yr = years.get(y)
        if yr is None:
            yr = years[y] = {"year": y, "count": 0, "photos": []}
        yr["count"] += 1
        if len(yr["photos"]) < per_year_cap:
            yr["photos"].append(_timeline_strip_photo(r))
    year_list = sorted(years.values(), key=lambda y: y["year"], reverse=True)
    return {"mmdd": mmdd, "years": year_list, "total_count": total}


def venues_data(photo_rows_, geo_index, *, min_members=2, sample_cap=8):
    """G-10: everyday-place clusters (geo_cluster's 0.5 km venue DBSCAN) — the house,
    the school, the park — grouped from the delivered photo rows by
    `gps_venue_cluster_id`.

    Each venue reuses the trip-aggregate mechanics: a GPS centroid, the full
    `member_ids` (so a venue opens as a filtered grid / exports as a collection), a
    count, and a small preview strip. Labelled by the DOMINANT place / compound_label
    among members (no reverse geocoding on the air-gapped box). Singleton clusters are
    dropped (`min_members`, default 2 → matches __clusters__.gps_venue_cluster_count).
    """
    geo_index = geo_index or {}
    venues = {}
    for r in photo_rows_:
        geo = geo_index.get(r.get("id"), {}) or {}
        vid = geo.get("gps_venue_cluster_id")
        if vid is None:
            continue
        gps = r.get("gps") or {}
        v = venues.get(vid)
        if v is None:
            v = venues[vid] = {"venue_id": str(vid), "count": 0, "member_ids": [],
                               "photos": [], "places": Counter(),
                               "lat_sum": 0.0, "lon_sum": 0.0, "geo_n": 0,
                               "first_seen": None, "last_seen": None}
        v["count"] += 1
        if r.get("id"):
            v["member_ids"].append(r["id"])
        if len(v["photos"]) < sample_cap:
            v["photos"].append(_timeline_strip_photo(r))
        loc = _clean_location_label(r.get("place")) or _clean_location_label(geo.get("compound_label"))
        if loc:
            v["places"][loc] += 1
        if gps.get("lat") is not None and gps.get("lon") is not None:
            v["lat_sum"] += gps["lat"]
            v["lon_sum"] += gps["lon"]
            v["geo_n"] += 1
        ts = r.get("ts")
        if ts:
            if v["first_seen"] is None or ts < v["first_seen"]:
                v["first_seen"] = ts
            if v["last_seen"] is None or ts > v["last_seen"]:
                v["last_seen"] = ts
    out = []
    for v in venues.values():
        if v["count"] < min_members:
            continue
        dominant = v["places"].most_common(1)
        n = v["geo_n"] or 1
        out.append({
            "venue_id": v["venue_id"],
            "name": dominant[0][0] if dominant else "Unmapped place",
            "count": v["count"],
            "lat": (v["lat_sum"] / n) if v["geo_n"] else None,
            "lon": (v["lon_sum"] / n) if v["geo_n"] else None,
            "member_ids": v["member_ids"],
            "photos": v["photos"],
            "years_span": _years_span(v["first_seen"], v["last_seen"]),
            "first_seen": v["first_seen"],
            "last_seen": v["last_seen"],
        })
    out.sort(key=lambda v: v["count"], reverse=True)
    # #23: distinct GPS/DBSCAN venue clusters within the same city (the house, a
    # friend's house, the school — genuinely different physical places, so they
    # are NOT merged) all carry the identical dominant-city label with no
    # reverse geocoding available to disambiguate them further (see docstring) —
    # reading as "why does Portland show up 6 times?" Disambiguate any name
    # shared by 2+ venues with each one's own capture-year span, which is real,
    # already-computed information (never a fabricated index like "(2)","(3)").
    by_name = {}
    for v in out:
        by_name.setdefault(v["name"], []).append(v)
    for dupes in by_name.values():
        if len(dupes) < 2:
            continue
        for v in dupes:
            y0 = _year(v.get("first_seen"))
            y1 = _year(v.get("last_seen"))
            if y0 is not None and y1 is not None:
                v["name"] = v["name"] + (f" ({y0})" if y0 == y1 else f" ({y0}–{y1})")
    return {"venues": out}


def _ranked_target(item, doc_file_by_name):
    """Attach a click-through target to a 'Most significant' overview item.

    ranked_items carry only type+label (+person_id for clusters); documents have
    no file id, so we recover it by filename. Emails are routed to the Emails
    section (no per-thread id on the ranked entry)."""
    t = item.get("type")
    if t in ("photo_cluster", "person") and item.get("person_id"):
        return {"page": "people", "person_id": item["person_id"]}
    if t == "scene":
        return {"page": "photos", "scene": item.get("label")}
    if t == "document":
        rec = doc_file_by_name.get(item.get("label"))
        if rec and rec[1] == "email":
            return {"page": "emails"}
        if rec and rec[0]:
            return {"page": "documents", "open": True, "file": rec[0]}
        return {"page": "documents"}
    return None


def ranked_key(item):
    """Stable suppression key for a 'Most significant' item — ranked_items carry
    no real id, so key on type + (person_id | label). Shared by overview_data
    (filter) and the demote verb (write)."""
    return f"{item.get('type')}:{item.get('person_id') or item.get('label') or ''}"


def _ranked_thumbs(item, face_clustering, universe, cap=5):
    """Up to `cap` representative thumbnail srcs for a ranked item (1 medium + a
    few small in the UI); empty list → icon. #5"""
    t = item.get("type")
    out = []
    if t in ("photo_cluster", "person") and item.get("person_id"):
        for m in (face_clustering.get("person_clusters", {}) or {}).get(item["person_id"], []):
            if is_video_frame(m, set()):   # never a bare keyframe (defensive; universe also drops them)
                continue
            if not universe or m in universe:
                out.append(m)
            if len(out) >= cap:
                break
    elif t == "scene":
        for src, info in (universe or {}).items():
            if (info.get("category") or "") == item.get("label"):
                out.append(src)
            if len(out) >= cap:
                break
    return out


def _vital_docs_overview(vital_docs):
    """Compact overview-card view of the vital-documents checklist: the found/
    total tally plus a small per-type found/not-found list (labels only, no item
    paths — the full checklist with links lives on the Documents page)."""
    if not vital_docs or not vital_docs.get("available"):
        return {"available": bool(vital_docs and vital_docs.get("available"))}
    targets = vital_docs.get("targets", []) or []
    return {
        "available": True,
        "found_count": vital_docs.get("found_count", 0),
        "total_count": vital_docs.get("total_count", 0),
        "types": [{"label": t["label"], "found": t["found"],
                   # Examiner-only upstream (vital_docs_data omits it for family),
                   # so it stays absent rather than 0 for a family session — the
                   # card must not be able to say "0 unreviewed" to somebody who
                   # is not doing the reviewing.
                   "near_misses": t.get("near_miss_count")}
                  for t in targets],
        # Weaker matches sitting under the types with NO confirmed document. This
        # is what stops the card claiming those types are missing: a type nobody
        # has finished looking at is not a type that is absent. Summed here rather
        # than in the page so the card and the estate report read one number.
        "unfound_near_misses": sum((t.get("near_miss_count") or 0)
                                   for t in targets if not t.get("found")),
    }


def overview_data(summary, role, counts, *, face_clustering=None, universe=None, decisions=None,
                  vital_docs=None, archive_warning=None):
    face_clustering = face_clustering or {}
    universe = universe or {}
    demoted = set((decisions or {}).get("ranked_demoted", {}) or {})
    removed = set((decisions or {}).get("removed_persons", {}) or {})
    creds = summary.get("credentials_report", {}) or {}
    if role == "family":
        # Filenames only, no types/severity detail and certainly no values. The
        # family is shown WHICH files hold credentials (so they can act — secure or
        # close those accounts) but never the secrets, and always with the caution
        # note (B7 decision: show filenames + guidance).
        files = [c.get("file") for c in (creds.get("items") or []) if c.get("file")]
        cred_view = {
            "critical_count": creds.get("critical_count", 0),
            "informational_count": creds.get("informational_count", 0),
            "files": files,
            "guidance": CREDENTIAL_FAMILY_GUIDANCE if files else None,
        }
    else:
        cred_view = creds
    # Resolve a click-through target for each "Most significant" item (#18).
    doc_file_by_name = {}
    for d in summary.get("document_classifications", []) or []:
        nm = d.get("filename") or os.path.basename(d.get("file", ""))
        if nm and nm not in doc_file_by_name:
            doc_file_by_name[nm] = (d.get("file"), (d.get("source") or "").lower())
    ranked_top = []
    for item in (summary.get("ranked_items", []) or []):
        if ranked_key(item) in demoted:  # #12 demoted items removed from the list
            continue
        if item.get("person_id") and item["person_id"] in removed:  # #6 removed person
            continue
        row = dict(item)
        row["target"] = _ranked_target(item, doc_file_by_name)
        row["thumbs"] = _ranked_thumbs(item, face_clustering, universe)  # #5 up to 5 previews
        row["thumb"] = row["thumbs"][0] if row["thumbs"] else None       # back-compat
        row["key"] = ranked_key(item)
        ranked_top.append(row)
        if len(ranked_top) >= 60:
            break
    return {
        "case_id": summary.get("case_id"),
        "role": role,
        "generated_at": summary.get("generated_at"),
        "counts": counts,
        "scene_counts": summary.get("scene_counts", {}),
        "document_counts": summary.get("document_counts", {}),
        "audio_counts": summary.get("audio_counts", {}),
        "export_gate": summary.get("export_gate", {}),
        "ranked_top": ranked_top,
        "limitations": summary.get("limitations", []),
        "stages_completed": summary.get("stages_completed", []),
        "credentials": cred_view,
        "vital_docs": _vital_docs_overview(vital_docs),
        # R-7: non-None (a loud string) when the family archive is serving with a
        # missing archive index — the frontend surfaces it so a zero-media archive
        # isn't mistaken for "there was nothing". None in the normal case.
        "archive_warning": archive_warning,
    }


def vital_doc_item_id(target, path):
    """Stable composite id for one confirmed vital-document item: the ORIGINAL
    pipeline target + its source path. Keying by (target, path) — NOT path alone —
    keeps the two dup-path items (a path confirmed under two targets) distinct, and
    stays stable across a reassign (which changes only the DISPLAY target)."""
    return f"{target}::{path or ''}"


def _vital_thread_links(paths, role, wanted, threads_index=None):
    """Resolve confirmed vital-document paths → the conversation that contains them.

    A vital document is often an EMAIL (on a real mail-heavy case, a majority of
    them: 26 of goog's 47 confirmed items). Those are deliberately absent from the
    documents view — `browsable` skips every email-sourced classification — so the
    checklist used to render them as a bare, unclickable `message_43267.eml`. The
    evidence for "Will: found ✓" was a filename the family could not open.

    They ARE reachable, just through the Emails section, so this maps each wanted
    path to its {thread_id, subject} for a deep link into the conversation view.

    The lookup is deliberately scoped to the CALLER'S OWN audience index:

      * The family's conversation index excludes estate-rescued bulk mail. A vital
        item that lives only in rescued mail therefore resolves to NOTHING here and
        keeps its stub row. That is the correct outcome — linking it would either
        404 or, worse, hand the family a message the audience split exists to
        withhold. (`load_thread_index` fails closed for family; never widen it.)
      * The examiner's index is the union, so the examiner resolves everything.

    Real cases sit on both sides of that line: goog resolves 26/26 for family,
    while dbdoc resolves 17/17 for the examiner but only 5/17 for the family — the
    other 12 are rescued mail. Both are working as intended.

    `wanted` is the small set of confirmed paths, so we make ONE pass over the
    threads and keep only those (an examiner index carries ~64k file entries;
    materialising all of them per request would be wasteful).
    """
    if not wanted:
        return {}
    idx = threads_index if threads_index is not None else load_thread_index(paths, role)
    links = {}
    for t in (idx or {}).get("threads", []) or []:
        tid = t.get("thread_id")
        if not tid:
            continue
        for f in t.get("files", []) or []:
            if f in wanted and f not in links:
                links[f] = {"thread_id": tid, "subject": _thread_label(t.get("subject"))}
    return links


def _thread_label(subject):
    """A conversation subject fit to LABEL a checklist row, or None.

    Real mail yields subjects that are empty or pure reply noise ("Re:"), which
    is no better than the .eml basename it replaces. Strip the reply/forward
    prefixes and return None when nothing survives, so the caller can fall back
    to the same "(no subject)" wording the Emails list uses for that same thread.
    """
    s = (subject or "").replace("\n", " ").strip()
    while True:
        stripped = re.sub(r"^\s*(re|fwd?|aw|sv)\s*:\s*", "", s, flags=re.I)
        if stripped == s:
            break
        s = stripped
    return s.strip() or None


def _vital_message_links(paths, role, wanted):
    """Resolve confirmed vital-document paths that are MESSAGE CHUNKS
    ('<source>#chunk=<12hex>', from message_triage) to their conversation —
    the messages-side counterpart of `_vital_thread_links` for email.

    Backlog #26: the #480 email deep-link fix never got a messages equivalent,
    so a vital-doc item whose evidence came from a chat/SMS database chunk
    rendered as an unlinked stub labelled with its raw path
    (`chat.db#chunk=11bfbfd2961f`) — unopenable, unlike its email siblings.

    message_index.json chunk records already carry `conversation_id` directly
    (no per-thread "files" list to scan, unlike the email thread index), so
    this is a straight lookup once chunks are scoped to the caller's audience
    via `filter_message_chunks` — mirrors `_vital_thread_links`'s own-audience
    rule: a message living only in a rescued or platform conversation resolves
    to nothing for family and keeps its stub row.

    Label prefers the conversation's `display_name` (matches the Messages list
    / `message_rows`), falling back to its participants, then None (the
    frontend's own "(conversation)" wording covers that case).
    """
    if not wanted:
        return {}
    chunks = filter_message_chunks(
        load_json(Path(paths) / "message_index.json", []) or [], role)
    if not chunks:
        return {}
    convs_by_id = {c.get("conversation_id"): c
                   for c in load_conversation_index(paths, role)}
    links = {}
    for rec in chunks:
        f = rec.get("file")
        if not f or f not in wanted or f in links:
            continue
        conv = convs_by_id.get(rec.get("conversation_id")) or {}
        participants = conv.get("participants") or rec.get("participants") or []
        label = (conv.get("display_name") or "").strip() or ", ".join(participants) or None
        links[f] = {"conversation_id": rec.get("conversation_id"), "subject": label}
    return links


def _near_miss_hits(candidates, confirmed, decisions, target, rejections=None):
    """The candidate hits for ONE target that are not on the checklist — ordered.

    THE single definition of "near-miss", shared by the count on the checklist row
    and the rows in the review drawer. Keeping one definition is the point: the
    count and the list disagreeing is precisely the defect this feature exists to
    fix (the count used to be the whole retrieval pool).

    A hit is NOT a near-miss when, for this same target, it was:
      - confirmed by the pipeline, or
      - promoted by the examiner (they already asserted it), or
      - dismissed by the examiner ("not a vital document" — they already ruled).
    Everything else is unreviewed and belongs in the drawer.

    Matching is on the ORIGINAL target throughout, never the effective one: a
    reassigned item ("not a will, it's a deed") keeps its original candidate
    bucket, so counting against the display bucket would over-count the bucket it
    left and under-count the one it joined.

    Each returned row is a COPY of the hit stamped with its disposition from
    `rejections` (vital_doc_rejections.json, keyed by target). Cases processed
    before that file existed — every case on disk today — resolve to
    disposition "unknown" with no reason, and still get path/score/snippet and
    working deep links, so the feature needs no re-run to be useful. That matters:
    some cases cannot be re-run at all.

    Order: `not_evaluated` first regardless of score — "we never read this file"
    is a stronger signal than "we read it and said no" — then score descending.
    Applied here, before any pagination, so the important rows cannot fall off
    the far side of a page boundary.
    """
    hits = ((candidates.get(target) or {}).get("hits")) or []
    if not isinstance(hits, list):
        return []
    decisions = decisions or {}
    dismissed = decisions.get("vital_doc_dismissed") or {}
    promoted = decisions.get("vital_doc_promoted") or {}

    accounted = {c.get("path") for c in confirmed if c.get("target") == target}
    accounted |= {r.get("path") for r in promoted.values()
                  if isinstance(r, dict) and r.get("target") == target}
    # Dismissal is keyed by PATH (it drops the doc from every category), with the
    # legacy per-item composite still honoured — mirrors vital_docs_data.
    def _is_dismissed(p):
        return p in dismissed or vital_doc_item_id(target, p) in dismissed

    by_path = {r.get("path"): r
               for r in ((rejections or {}).get(target) or [])
               if isinstance(r, dict)}

    rows = []
    for h in hits:
        if not isinstance(h, dict):
            continue
        p = h.get("path")
        if p in accounted or _is_dismissed(p):
            continue
        rej = by_path.get(p) or {}
        rows.append({
            "path": p,
            "score": h.get("score"),
            "snippet": h.get("snippet"),
            "disposition": rej.get("disposition") or "unknown",
            "reason_code": rej.get("reason_code"),
            "reason": rej.get("reason"),
        })
    return sorted(rows, key=lambda r: (r["disposition"] != "not_evaluated",
                                       -(r["score"] or 0), r["path"] or ""))


def _vital_doc_summaries(summary):
    """file → the plain-language summary the pipeline already wrote for it.

    Not audience-gated here on purpose: the gate is applied where the item is
    built, by reusing the deep-link decision that was made anyway (see
    `_vital_summary_for`). Keeping the map whole means one build serves both
    roles and the gate stays in ONE place instead of two that can drift.
    Neutralised and capped exactly as document_rows does, so the same document
    reads identically on the checklist and in the documents table.
    """
    out = {}
    for d in summary.get("document_classifications", []) or []:
        f = d.get("file")
        if f and f not in out:
            out[f] = neutralize_summary((d.get("summary") or "")[:400]) or None
    return out


def _vital_summary_for(summaries, path, browsable, *links):
    """The summary to show on one vital-doc row, or None if this role may not
    read it.

    The rule is the one the row already enforces for its LINK: a summary is a
    paraphrase of the document's contents, so anyone allowed the summary must
    already be allowed the thing. `browsable` has had the role's document
    exclusions applied (email-sourced everywhere; account_credentials for
    family), and each link was resolved through the role's OWN conversation
    index — so "can this role open it?" is already computed, and re-deriving it
    from `role` here would be a second gate to keep in step. audience.py's
    asymmetry decides the default: a family-side leak fails open, so an item
    that resolved to nothing gets no summary rather than a best guess.
    """
    if not path:
        return None
    if path not in browsable and not any(links):
        return None
    return summaries.get(path)


def near_miss_rows(paths, summary, role, target, decisions=None, threads_index=None):
    """The reviewable near-miss subset for ONE vital-doc target (examiner-only).

    The checklist row says "3 near-misses"; this is what that number opens. Each
    row carries why it was not confirmed (`disposition`/`reason_code`/`reason`,
    from vital_doc_rejections.json) plus a deep link resolved through EXACTLY the
    same rules the confirmed items use — `browsable` for documents,
    `_vital_thread_links` for conversations. That second path is not optional
    polish: on a real mail-heavy case a MAJORITY of candidate hits are
    email-sourced (57 of 104 on the goog corpus), so a documents-only link would
    be dead for most of the list.

    Returns the FULL ordered list, unpaginated — the caller slices. The subset is
    NOT bounded: `vital_per_target_k` is a per-case config knob (default 8) that an
    examiner raises precisely when they suspect something was missed, so a fixed
    cap here would silently truncate the very case that needed the recall.

    Family never sees raw candidate hits; a non-examiner role gets [].
    """
    if role != "examiner":
        return []
    md = paths.metadata_dir
    confirmed = load_json(md / "vital_doc_confirmed.json", None)
    candidates = load_json(md / "vital_doc_candidates.json", None)
    if not isinstance(candidates, dict):
        return []
    confirmed = confirmed if isinstance(confirmed, list) else []
    # Absent on every case processed before this feature — near-misses then carry
    # disposition "unknown" rather than vanishing.
    rejections = load_json(md / "vital_doc_rejections.json", None)
    rejections = rejections if isinstance(rejections, dict) else {}

    rows = _near_miss_hits(candidates, confirmed, decisions, target, rejections)
    if not rows:
        return []

    browsable = {}
    for d in summary.get("document_classifications", []) or []:
        if (d.get("source") or "").lower() == "email":
            continue
        f = d.get("file")
        if f and f not in browsable:
            browsable[f] = d.get("filename") or os.path.basename(f)

    unlinked = {r["path"] for r in rows if r["path"] and r["path"] not in browsable}
    thread_links = _vital_thread_links(md, role, unlinked, threads_index=threads_index)
    message_links = _vital_message_links(md, role, unlinked - set(thread_links))
    summaries = _vital_doc_summaries(summary)

    out = []
    for r in rows:
        p = r["path"]
        link = thread_links.get(p) or {}
        mlink = message_links.get(p) or {}
        out.append({
            **r,
            # Same composite the confirm/dismiss/reassign/promote verbs key on, so
            # a near-miss can be acted on with no new id scheme.
            "id": vital_doc_item_id(target, p),
            "name": os.path.basename(p or "") or None,
            "file_id": p if p in browsable else None,
            "thread_id": link.get("thread_id"),
            "thread_subject": link.get("subject") or None,
            # Messages-equivalent of the two fields above (#26) — a chat/SMS
            # database chunk deep-links into its conversation the same way.
            "conversation_id": mlink.get("conversation_id"),
            "conversation_subject": mlink.get("subject") or None,
            # The near-miss drawer is a decision surface too — promote/dismiss
            # are the same verbs — so it gets the same evidence as the checklist.
            "summary": _vital_summary_for(summaries, p, browsable, link, mlink),
        })
    return out


def vital_docs_data(paths, summary, role, decisions=None, threads_index=None,
                    per_target_k=None):
    """The vital-documents checklist (spec G-2): for every document type the
    pipeline searched for, was one actually found?

    Reads output/metadata/vital_doc_confirmed.json (a LIST of confirmed items,
    each {path, target, tag, ...}) and vital_doc_candidates.json (a DICT keyed by
    target → {description, hits}; its KEYS are the canonical "searched-for" list,
    so we can render "searched for, not found" rows). Older cases predate the
    vital_doc_confirm stage and have neither file — the feature then reports
    `available: False` rather than crashing.

    Per target we return {target, label, found, items, near_miss_count?}:
      - `found` is True when at least one confirmed item exists for the target.
      - each item carries {path, name, tag, file_id, thread_id, thread_subject,
        conversation_id, conversation_subject}. `file_id` is the confirmed source
        path when it maps to a browsable document (an exact match against
        summary.document_classifications[].file that the given role is allowed to
        open — email-sourced and, for family, account_credentials docs are NOT
        browsable). An EMAIL-sourced item instead carries `thread_id` (+
        `thread_subject` for the label), deep-linking into the Emails section —
        see `_vital_thread_links` for why that resolution is scoped to the
        caller's own audience index. A MESSAGE-sourced item (chat/SMS database
        chunk) carries `conversation_id` (+ `conversation_subject`) the same way
        — see `_vital_message_links`. An item that resolves to NONE of the three
        (no browsable doc, no thread, no conversation this role may see) still
        shows, carrying all as None → a stub row with no link, as before.
      - `near_miss_count` (examiner-only) is the number of candidate hits for this
        target that are NOT on the checklist — see `_near_miss_hits` for the one
        shared definition, and `near_miss_rows` for the list it opens. It replaces
        `candidate_count`, which was len(hits) — the whole retrieval pool including
        everything confirmed — so a fully-confirmed target claimed 8 near-misses
        that did not exist. Family never sees raw candidate hits at all.
      - an item may be PROMOTED (`promoted: True`): a near-miss the examiner
        asserted is the document. It is `reviewed` by construction.
    """
    md = paths.metadata_dir
    confirmed = load_json(md / "vital_doc_confirmed.json", None)
    candidates = load_json(md / "vital_doc_candidates.json", None)
    if confirmed is None and candidates is None:
        # vital_doc_confirm never ran for this case (or both files are unreadable).
        return {"available": False, "targets": [], "found_count": 0, "total_count": 0}
    confirmed = confirmed if isinstance(confirmed, list) else []
    candidates = candidates if isinstance(candidates, dict) else {}

    # Browsable delivered documents this role may deep-link to. Mirrors the
    # exclusions in document_rows so a checklist link never opens something the
    # documents view itself refuses (emails live in the Emails section; family
    # never browses raw account_credentials). file→display-name.
    browsable = {}
    for d in summary.get("document_classifications", []) or []:
        if (d.get("source") or "").lower() == "email":
            continue
        if role != "examiner" and d.get("category") == "account_credentials":
            continue
        f = d.get("file")
        if f and f not in browsable:
            browsable[f] = d.get("filename") or os.path.basename(f)

    # DECISIONS OVERLAY (examiner-only verbs; vital_doc_confirmed.json is NEVER
    # mutated — these are pure family_decisions.json sidecars):
    #   vital_doc_dismissed {path: {...}}     — the DOCUMENT is dropped from the
    #                                           checklist ENTIRELY ("not a vital
    #                                           document" — keyed by PATH, so it
    #                                           leaves every category it matched).
    #   vital_doc_target    {item_id: target} — one item's EFFECTIVE target is
    #                                           REASSIGNED ("not a will, it's a deed").
    # item_id is the stable ORIGINAL-target+path composite (vital_doc_item_id), so a
    # per-item reassign changes only the display bucket while the id — and thus the
    # two dup-path items — stay distinct. (A GLOBAL reassign expands to write one
    # item_id override per matched category, so the reader stays the same.)
    #   vital_doc_promoted  {item_id: {target, path, ...}} — a NEAR-MISS the examiner
    #                                           asserted IS the document after
    #                                           reviewing it ("the pipeline said no,
    #                                           I say yes"). Synthesized into the
    #                                           item list below; vital_doc_confirmed
    #                                           .json is not touched, same as the rest.
    decisions = decisions or {}
    dismissed = decisions.get("vital_doc_dismissed") or {}
    retarget = decisions.get("vital_doc_target") or {}
    reviewed = decisions.get("vital_doc_reviewed") or {}
    promoted = decisions.get("vital_doc_promoted") or {}
    overlay_active = bool(dismissed or retarget or promoted)

    # A promoted near-miss becomes an item indistinguishable from a confirmed one
    # (it flows through the same dismiss/reassign/link logic below), except that it
    # carries `promoted: True` so the UI can say where it came from. Its id is the
    # same target::path composite, so promote/dismiss/reassign all key alike.
    synthesized = []
    for iid, rec in promoted.items():
        if not isinstance(rec, dict):
            continue
        ppath, ptarget = rec.get("path"), rec.get("target")
        if not ppath or not ptarget:
            continue
        # Never double-count: if the pipeline confirmed it too, the confirmed
        # record wins and the promotion is a no-op.
        if any(c.get("target") == ptarget and c.get("path") == ppath
               for c in confirmed):
            continue
        synthesized.append({
            "path": ppath, "target": ptarget, "tag": f"vital_doc:{ptarget}",
            "_promoted": True,
        })

    by_target = {}
    for i in confirmed + synthesized:
        orig_target = i.get("target")
        ipath = i.get("path")
        iid = vital_doc_item_id(orig_target, ipath)
        # "Not a vital document" is a statement about the DOCUMENT, so dismissal is
        # keyed by PATH and drops the item from EVERY vital category it matched.
        # (A legacy per-item composite key is still honoured for backward compat.)
        if overlay_active and (ipath in dismissed or iid in dismissed):
            continue
        eff_target = (retarget.get(iid) if overlay_active else None) or orig_target
        by_target.setdefault(eff_target, []).append((iid, i))

    # Canonical order: the candidate keys (the searched-for list), then any extra
    # target that only shows up in the (overlay-adjusted) confirmed grouping (so a
    # confirmed/reassigned find is never silently dropped just because it lost its
    # candidate bucket).
    order = list(candidates.keys())
    for t in by_target:
        if t not in order:
            order.append(t)

    # Conversation deep links for the items no browsable document can serve —
    # overwhelmingly the email-sourced ones. Resolved only for paths that survived
    # the overlay AND have no document link, so a case with no email vital docs
    # never touches the (large) conversation index.
    unlinked = {i.get("path") for _, i in
                (it for items in by_target.values() for it in items)
                if i.get("path") and i.get("path") not in browsable}
    thread_links = _vital_thread_links(md, role, unlinked, threads_index=threads_index)
    message_links = _vital_message_links(md, role, unlinked - set(thread_links))
    summaries = _vital_doc_summaries(summary)

    targets = []
    found_count = 0
    # Found vital docs the examiner has NOT resolved yet — the "N to confirm" the
    # guided step counts. A found item is resolved when it is reviewed (the Confirm
    # verb), promoted (promoting IS the affirmative review), or reassigned (an
    # active decision); dismissed items never reach `by_target` at all. This mirrors
    # the release gate's `_vital_docs_cleared` EXACTLY, so the guided count reaches
    # 0 precisely when the vital-doc gate clears — confirming a doc drops it here.
    unconfirmed_count = 0
    for t in order:
        items = []
        for iid, i in by_target.get(t, []):
            path = i.get("path")
            link = thread_links.get(path) or {}
            mlink = message_links.get(path) or {}
            resolved = (iid in reviewed or bool(i.get("_promoted"))
                        or iid in retarget)
            if not resolved:
                unconfirmed_count += 1
            items.append({
                "id": iid,
                "path": path,
                "name": os.path.basename(path or "") or (i.get("tag") or ""),
                "tag": i.get("tag"),
                "file_id": path if path in browsable else None,
                # Deep link into the Emails section for an email-sourced item.
                # None when the item is a browsable document, or when this role's
                # conversation index does not contain it (rescued mail, family).
                "thread_id": link.get("thread_id"),
                # The conversation subject, a far better label than the .eml
                # basename (`message_43267.eml` tells a family nothing).
                "thread_subject": link.get("subject") or None,
                # Messages-equivalent deep link (#26): a vital doc sourced from a
                # chat/SMS database chunk, resolved the same way as email above.
                "conversation_id": mlink.get("conversation_id"),
                "conversation_subject": mlink.get("subject") or None,
                # Whether the examiner has explicitly confirmed this item as a
                # reviewed vital doc (drives the release-gate disposition + the
                # UI's confirmed state). Reassign also clears the gate but is
                # shown by its own display target, not this flag.
                # A PROMOTED item is reviewed by construction — the examiner just
                # asserted it personally, which is a stronger act than confirming
                # a match the pipeline proposed.
                "reviewed": iid in reviewed or bool(i.get("_promoted")),
                # This item came from the near-miss list, not from the pipeline.
                "promoted": bool(i.get("_promoted")),
                # WHAT THIS DOCUMENT IS, in the pipeline's own words. The row asks
                # the examiner to certify that a file IS the deed / the will / the
                # marriage certificate, and until now offered a filename and a
                # filing location to decide on — neither of which answers the
                # question. On 813_mf that yielded ten "Property deed / title"
                # sign-offs that are, by their own summaries, a draft will, a
                # durable power of attorney, a zoning letter and four emails about
                # deed research. None of this text is new or newly computed; it was
                # written at classification time and rendered further down the same
                # page. None when this role may not read the underlying item.
                "summary": _vital_summary_for(
                    summaries, path, browsable, link, mlink),
            })
        found = bool(items)  # a target is "found" iff ≥1 item SURVIVES the overlay
        if found:
            found_count += 1
        row = {"target": t, "label": vital_doc_label(t), "found": found, "items": items}
        if role == "examiner":
            cand = candidates.get(t) or {}
            hits = cand.get("hits")
            n_hits = len(hits) if isinstance(hits, list) else 0
            # NEAR-MISSES, not the candidate pool. This used to be len(hits) — the
            # whole retrieval set INCLUDING everything that was confirmed — so a
            # fully-confirmed target reported "8 near-misses" and sent the examiner
            # hunting for misses that did not exist. Subtracting what is already on
            # the checklist (confirmed + promoted, per this target's own bucket)
            # makes the number mean what the UI says it means, and go to 0.
            row["near_miss_count"] = len(
                _near_miss_hits(candidates, confirmed, decisions, t))
            row["description"] = cand.get("description")
            # Whether retrieval hit the per-target ceiling for THIS target: the
            # embed stage pulls at most `vital_per_target_k` hits before LLM
            # confirmation, so a target holding exactly k hits was (very likely)
            # truncated — matching documents beyond the k-th were never retrieved,
            # so the near-miss list is a floor, not the whole field. Surfaced so
            # the Documents view can say "the cap was reached, raise it and re-run
            # to see more". Only meaningful when k is known (per_target_k passed).
            if per_target_k:
                row["near_miss_capped"] = n_hits >= per_target_k
        targets.append(row)

    return {
        "available": True,
        "targets": targets,
        "found_count": found_count,
        "total_count": len(targets),
        # Found vital docs still awaiting an examiner decision (confirm / dismiss /
        # reassign) — the guided step's "to confirm" number. Examiner-only; a family
        # session neither confirms nor sees this. Matches _vital_docs_cleared.
        "unconfirmed_count": unconfirmed_count if role == "examiner" else None,
        # The retrieval ceiling in force for this case (examiner context: how many
        # candidates per type the near-miss lists are drawn from). None when the
        # caller didn't resolve it. Echoed so the UI can label "top N per type".
        "per_target_k": per_target_k if role == "examiner" else None,
        # The canonical target set (+ labels) for the reassign picker's choices.
        "all_targets": [{"target": t, "label": vital_doc_label(t)}
                        for t in VITAL_DOC_LABELS],
    }


def review_data(paths, summary):
    """EXAMINER-ONLY review surfaces. Read directly from the case (never family)."""
    md = paths.metadata_dir
    quarantine = load_json(md / "quarantine_manifest.json", {}) or {}
    sensitive = load_json(md / "sensitive_scan_index.json", {}) or {}
    human = load_json(md / "human_review_required.json", {}) or {}
    recon = load_json(md / "reconciliation_manifest.json", {}) or {}
    suspense = load_json(md / "suspense_manifest.json", {}) or {}
    frame_map = load_json(md / "video_frame_map.json", {}) or {}
    frame_set = set(frame_map)
    arc_entries = (load_json(md / "archive_map.json", {}) or {}).get("entries", {}) or {}

    def _present(path):
        # A flag whose archive copy is gone has been discarded/moved out — its
        # thumbnail would 404. Quarantined items are still covered by the Quarantine
        # group; banished ones are gone by examiner choice. Items not in the archive
        # map at all are kept (nothing to check).
        arc = arc_entries.get(path)
        return not (arc and not os.path.exists(arc))

    # Lazy chunk lookup for flagged message chunks ("<path>#chunk=<12hex>"):
    # message_index.json is loaded ONCE, on the first chunk flag seen, and keyed
    # by chunk_sha256[:12]. Absent index (message_triage not run) → no lookup.
    chunk_by_id = None

    def _chunk_lookup(chunk_id):
        nonlocal chunk_by_id
        if chunk_by_id is None:
            chunk_by_id = {}
            for rec in load_json(md / "message_index.json", []) or []:
                sha = rec.get("chunk_sha256") or ""
                if len(sha) >= 12:
                    chunk_by_id.setdefault(sha[:12], rec)
        return chunk_by_id.get(chunk_id)

    def _attach_chunk(entry, path):
        """A '<path>#chunk=<12hex>' flag points at message TEXT, not a viewable
        file: attach the flagged chunk's rendered text (+ conversation_id) so the
        examiner reads the actual flagged content in the review detail — for a
        suicidal-ideation flag the text, not a file preview, is the thing that
        must be read. Degrades silently when message_index.json is absent or the
        chunk is unknown (entry stays name-only)."""
        _base, chunk = split_chunk_ref(path)
        if chunk is None:
            return entry
        rec = _chunk_lookup(chunk)
        if rec:
            entry["chunk_text"] = (rec.get("ocr_text") or "")[:2000]
            cid = rec.get("conversation_id")
            if cid:
                entry["conversation_id"] = cid
        return entry

    def _review_src(path):
        """Honor the 'a video frame is only ever a viewport into its source video'
        rule on the examiner review lists: a flagged keyframe is shown as its source
        video (so the lightbox opens the movie), never a bare still. Returns
        (name, src). An unmapped keyframe gets no src rather than leaking the still.
        A flagged message chunk ('<path>#chunk=<hex>') is named by its SOURCE file
        but gets no src — the raw export file is not a useful preview (the chunk
        text is attached by _attach_chunk instead)."""
        base, chunk = split_chunk_ref(path)
        if chunk is not None:
            return os.path.basename(base), None
        if is_video_frame(path, frame_set):
            vsrc = (frame_map.get(path) or {}).get("source_video")
            return (os.path.basename(vsrc), vsrc) if vsrc else (os.path.basename(path), None)
        return os.path.basename(path), path

    q_entries = []
    for e in quarantine.get("entries", []) or []:
        q_entries.append({
            "file": os.path.basename(e.get("file", "")),
            "filter": e.get("filter"),
            "timestamp": e.get("timestamp"),
            # Stable, unique action key for release/discard: basenames collide
            # across filter dirs, so the verbs match on canonical_path first (C-3).
            "canonical_path": e.get("canonical_path"),
        })
    # Sensitivity: only entries with a REAL trigger (every record carries a full
    # filter dict even when nothing triggered — without this the list is ~all files
    # and mislabels categories). Emit only triggered filter names; full src except
    # for unmapped frames / chunks. #14
    SENS_CAP = 2000
    sens = []
    sens_total = 0
    for path, rec in (sensitive or {}).items():
        filters = rec.get("sensitivity_filters") or {}
        triggered = [k for k, v in filters.items() if isinstance(v, dict) and v.get("triggered")] \
            if isinstance(filters, dict) else []
        if not (rec.get("human_review_required") or triggered):
            continue
        if not _present(path):   # discarded/moved-out → would render a broken thumb
            continue
        sens_total += 1
        if len(sens) < SENS_CAP:
            name, src = _review_src(path)
            sens.append(_attach_chunk({
                "name": name,
                "src": src,                            # unmapped-frame/chunk → None; frame → source video
                # Stable, unique action key for release/discard (basenames collide
                # across filter dirs); the delivered canonical for this flagged
                # working path, or None when it has no archive entry (C-3).
                "canonical_path": arc_entries.get(path),
                "locked": False,
                "human_review": bool(rec.get("human_review_required")),
                "filters": triggered,
            }, path))
    HUMAN_CAP = 2000
    # Drop discarded/moved-out paths (broken thumbs) before counting, so the tile
    # count matches the visible list.
    human_paths = [p for p in (human.get("paths", []) or []) if _present(p)]
    human_list = []
    for p in human_paths[:HUMAN_CAP]:
        name, src = _review_src(p)
        human_list.append(_attach_chunk({"name": name, "src": src, "locked": False}, p))
    return {
        "quarantine": q_entries,
        "quarantine_total": len(q_entries),
        "sensitive": sens,
        "sensitive_total": sens_total,
        "human_review": human_list,
        "human_review_count": len(human_paths),
        "reconciliation": {
            "needs_examiner_review": recon.get("needs_examiner_review"),
            "attention_counts": recon.get("attention_counts", {}),
            "delivery": recon.get("delivery", {}),
            "original_files_remaining": recon.get("original_files_remaining", {}),
            "review_items": recon.get("review_items", []),
        },
        # suspense_manifest.json is canonically a LIST (schema type: array); older
        # runs wrote a dict with entries/paths. Handle both, mirroring
        # transparency_data() — a bare .get() here crashed the whole review queue
        # ("'list' object has no attribute 'get'").
        "suspense_total": len(
            (suspense.get("entries") or suspense.get("paths") or [])
            if isinstance(suspense, dict) else (suspense or [])),
        "credentials": summary.get("credentials_report", {}),
    }


# ── review-queue bulk-triage pager (review-queue-bulk-triage.md) ──────────────────
#
# The PAGER assembler: normalize the two review surfaces that have real audited
# disposition verbs today — quarantine and vital documents — into ONE item union a
# pure renderer pages through one item at a time. This is deliberately NOT
# review_data (no role/decisions, no vital rows) and NOT guided_review_data (counts
# + links, never the rows). The heavy data-gathering (loading vital_docs_data /
# near_miss_rows, reading the quarantine manifest) stays in the ArchiveCase method
# that calls these; the builders below are pure functions over already-loaded rows
# so both kinds and both vital sub-queues are unit-testable without a live case.

# Video extensions for the pager's blur decision. Imported lazily (the module is
# config-driven and this file must stay import-cheap for the pure builders).
def _is_pager_video(name):
    from wyeast.core.media import VIDEO_EXTENSIONS
    return Path(name or "").suffix.lower() in VIDEO_EXTENSIONS


def quarantine_pager_items(entries, *, media_exists=None):
    """Kind-A (quarantine) pager items from raw quarantine_manifest entries.

    `entries` is the manifest's `entries` list (the PENDING items — released ones
    live in a separate list and never reach here), each carrying at least
    `canonical_path` / `quarantine_path` / `file` / `filter`. `media_exists` is the
    on-disk existence probe (injectable for tests; defaults to os.path.exists).

    Blur is the graphic-exposure guard (§4): TRUE for a servable image/video only.
    A non-media flagged item (a flagged .docx/.txt) is never blurred, and a media
    item whose bytes are ABSENT gets `src=None` — the /media resolver would 404 on
    it, so we must not hand the client a link that renders a broken frame. The id
    is the entry's canonical_path, which is exactly what verb_release /
    verb_discard_quarantine match on.
    """
    exists = media_exists or os.path.exists
    items = []
    for e in entries or []:
        canonical = e.get("canonical_path")
        qpath = e.get("quarantine_path")
        name = os.path.basename(e.get("file") or qpath or canonical or "")
        if is_image(name):
            media_kind = "image"
        elif _is_pager_video(name):
            media_kind = "video"
        else:
            media_kind = "other"
        is_media = media_kind in ("image", "video")
        # Bytes must really be on disk under quarantine/ for /media to resolve them.
        present = bool(qpath and exists(qpath))
        # The resolver keys on the builder src (canonical_path); None when there is
        # nothing servable to show (non-media, or media whose bytes are gone).
        src = canonical if (is_media and present) else None
        filt = e.get("filter")
        items.append({
            "kind": "quarantine",
            "id": canonical,
            "name": name,
            "media_kind": media_kind,
            "src": src,
            "thumb": src,        # same key; None ⇒ the client shows a filename card
            "blur": bool(is_media and present),
            "filters": [filt] if filt else [],
            "actions": ["release", "discard"],
        })
    return items


def vital_pager_items(unconfirmed, near_miss):
    """Kind-B (vital-doc) pager items — BOTH sub-queues, distinguished by `vqueue`.

    `unconfirmed` are the found-but-unresolved confirmed items (each an entry from
    vital_docs_data's `targets[].items` stamped with its display `target`, kept
    only when NOT reviewed / promoted / reassigned). `near_miss` are near_miss_rows
    entries, each stamped with its candidate `target`. Both carry file_id /
    thread_id / conversation_id (never a thumb — vital docs have no thumbnail)
    and their action set differs by sub-queue (§5):

      unconfirmed → confirm / dismiss / reassign
      near_miss   → promote / dismiss / reassign
    """
    items = []
    for it in unconfirmed or []:
        items.append({
            "kind": "vital_doc",
            "vqueue": "unconfirmed",
            "id": it.get("id"),
            "target": it.get("target"),
            "name": it.get("name"),
            "file_id": it.get("file_id"),
            "thread_id": it.get("thread_id"),
            "thread_subject": it.get("thread_subject"),
            "conversation_id": it.get("conversation_id"),
            "conversation_subject": it.get("conversation_subject"),
            "disposition": None,
            "blur": False,
            "actions": ["confirm", "dismiss", "reassign"],
        })
    for r in near_miss or []:
        items.append({
            "kind": "vital_doc",
            "vqueue": "near_miss",
            "id": r.get("id"),
            "target": r.get("target"),
            "name": r.get("name"),
            "file_id": r.get("file_id"),
            "thread_id": r.get("thread_id"),
            "thread_subject": r.get("thread_subject"),
            "conversation_id": r.get("conversation_id"),
            "conversation_subject": r.get("conversation_subject"),
            # Why the pipeline did not confirm it (+ the snippet/score context the
            # examiner reads before promoting), straight from near_miss_rows.
            "disposition": r.get("disposition"),
            "reason": r.get("reason"),
            "snippet": r.get("snippet"),
            "score": r.get("score"),
            "blur": False,
            "actions": ["promote", "dismiss", "reassign"],
        })
    return items


# ── search index ───────────────────────────────────────────────────────────────

def tokenize(text):
    out, cur = [], []
    for ch in (text or "").lower():
        if ch.isalnum():
            cur.append(ch)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return [t for t in out if len(t) > 2]


def build_search(photos, people, docs, audio, emails=None, conversations=None):
    """Pre-tokenized inverted index over everything, for offline lexical search."""
    records = []

    def add(page, title, type_, text, href=None):
        records.append({"p": page, "t": title[:120], "k": type_,
                        "s": (text or "")[:160].replace("\n", " "), "h": href})

    # G-9: photo/document/audio records carry an `href` (their own id) so a search
    # hit can open the item directly (lightbox/doc panel) instead of only landing
    # on the section page — matching the deep-link email/conversation already have.
    for r in photos:
        # G-7: fold the caption (owner's / LLaVA) into the searchable text so a
        # photo is findable by its description, and carry a couple of album names.
        add("photos", r["name"], "photo",
            " ".join(filter(None, [r["name"], r.get("scene"), r.get("place"), r.get("trip"),
                                   r.get("caption")] + (r.get("albums") or [])[:3])),
            href=r.get("id"))
    for r in people:
        add("people", r["name"], "person", " ".join(filter(None, [r["name"], r.get("summary")])))
    for r in docs:
        add("documents", r["name"], "document",
            " ".join(filter(None, [r["name"], r.get("category"), r.get("summary"), r.get("preview")])),
            href=r.get("file"))
    for r in audio:
        add("audio", r["name"], "audio",
            " ".join(filter(None, [r["name"], r.get("category"), r.get("summary"), r.get("preview")])),
            href=r.get("file"))
    for r in (emails or []):
        add("emails", r.get("subject") or "(no subject)", "email",
            " ".join(filter(None, [r.get("subject")] + list(r.get("participants") or [])
                            + list(r.get("categories") or []))),
            href=r.get("thread_id"))
    for r in (conversations or []):
        add("messages",
            r.get("display_name") or ", ".join(r.get("participants") or []) or "(conversation)",
            "conversation",
            " ".join(filter(None, list(r.get("participants") or []) + [r.get("platform")])),
            href=r.get("conversation_id"))

    index = {}
    for i, rec in enumerate(records):
        seen = set()
        for tok in tokenize(rec["t"] + " " + rec["s"]):
            if tok in seen:
                continue
            seen.add(tok)
            index.setdefault(tok, []).append(i)
    log(f"search index: {len(records)} records, {len(index)} terms")
    return {"records": records, "index": index}


# ── server-only builders (used by family_archive.py) ────────────────────────────

def confirm_queue_data(summary, scene_index, face_clustering, geo_index, *,
                       scene_cap=None, face_cap=None, decisions=None, frame_map=None,
                       archive_map=None):
    """Low-confidence pipeline guesses a person can resolve (the Confirm flow).

    Surfaces, with their source so a decision can be recorded:
      - scene classifications below CONFIRM_SCENE_THRESHOLD
      - unidentified faces (face_clustering.noise_files)
      - photos with competing face-cluster merge candidates (geo index)
      - unnamed person clusters (suggest a Rename)
      - event-album guesses (no per-album confidence exists in the pipeline)

    `decisions` is the recorded {queue: {id: {...}}} map (family_decisions.json);
    any item already decided (Confirmed/Dismissed/Named) is omitted so it does
    not reappear after the page is revisited. Undo deletes the decision, which
    returns the item to the queue.

    `scene_cap`/`face_cap` default to UNCAPPED (None) — they used to default to
    300 each, so this list (and every count derived from it: the Review page's
    "To confirm (N)" and Guided review's "confirm" step) silently reported a
    fixed ~620 regardless of the true backlog, and — because decided items are
    filtered out before the cap is applied, so the cap just backfills from the
    next undecided candidate — the number did not visibly decrease as an
    examiner worked through it until enough was cleared to drop under the cap
    per category. An examiner doing real work saw zero progress feedback on the
    queue that gates the release signature. The frontend already paginates this
    list client-side in chunks (CONFIRM_CHUNK), so nothing downstream needs a
    server-side cap; both params are kept for callers that explicitly want one.
    """
    items = []
    decisions = decisions or {}
    frame_set = set(frame_map or {})
    entries = (archive_map or {}).get("entries", {}) or {}

    def decided(queue, _id):
        return str(_id) in (decisions.get(queue) or {})

    def present(src):
        # A media item whose archive copy is gone has been discarded/quarantined/
        # moved out — don't surface it (its thumbnail would 404). Items not in the
        # archive map at all are left as-is (nothing to check against).
        arc = entries.get(src)
        return not (arc and not os.path.exists(arc))

    clip = scene_index.get("clip_results", {}) or {}
    scene_n = 0
    for src, rec in clip.items():
        conf = rec.get("confidence")
        if conf is not None and conf <= CONFIRM_SCENE_THRESHOLD and rec.get("delivered", True):
            if decided("scene", src) or is_video_frame(src, frame_set) or not present(src):
                continue
            if scene_cap is not None and scene_n >= scene_cap:
                log(f"confirm queue: capped scene guesses at {scene_cap}")
                break
            items.append({"queue": "scene", "id": src, "kind": "scene_guess",
                          "guess": rec.get("category"), "confidence": conf})
            scene_n += 1

    noise = [s for s in (face_clustering.get("noise_files", []) or [])
             if not decided("face", s) and not is_video_frame(s, frame_set) and present(s)]
    capped_noise = noise if face_cap is None else noise[:face_cap]
    for src in capped_noise:
        items.append({"queue": "face", "id": src, "kind": "unidentified_face",
                      "guess": None, "confidence": None})
    if face_cap is not None and len(noise) > face_cap:
        log(f"confirm queue: capped unidentified faces at {face_cap} of {len(noise)}")

    # G-15: a face_merge suggestion is "resolved" once the examiner has actually
    # merged one of its candidate clusters away (person_merges overlay) — drop it so
    # it stops reappearing, exactly like a recorded confirm decision.
    merges = (decisions or {}).get("person_merges", {}) or {}

    def _merge_resolved(cands):
        if not merges:
            return False
        toks = _candidate_pids(cands)
        # Resolved if any candidate cluster was merged away, or two candidates now
        # collapse to the same surviving winner.
        if any(t in merges for t in toks):
            return True
        winners = {resolve_merge(t, merges) for t in toks}
        return bool(toks) and len(winners) < len(toks)

    for src, geo in (geo_index or {}).items():
        cands = geo.get("face_cluster_merge_candidates") or []
        if cands and not decided("face_merge", src) and present(src) \
                and not _merge_resolved(cands):
            items.append({"queue": "face_merge", "id": src, "kind": "face_merge",
                          "guess": cands, "confidence": None})

    identities = face_clustering.get("cluster_identities", {}) or {}
    for pid in (face_clustering.get("person_clusters", {}) or {}):
        if not _identity_name(pid, identities) and not decided("name_person", pid):
            items.append({"queue": "name_person", "id": pid, "kind": "unnamed_person",
                          "guess": person_display_name(pid, identities), "confidence": None})

    for alb in summary.get("event_albums", []) or []:
        aid = alb.get("album_id")
        if decided("event", aid):
            continue
        items.append({"queue": "event", "id": aid, "kind": "event_guess",
                      "guess": alb.get("title"), "confidence": None})
    return items


# ── G-13 junk rescue / G-14 transparency / G-12 guided review ────────────────────

def junk_rows(scene_index, metadata_index=None, *, cap=None, rescued=None):
    """EXAMINER-ONLY rows for the Junk-rescue grid (G-13).

    One row per `scene_index.junk_results` key — an image the pipeline routed out
    of the gallery as junk (email banners, logos, icons, tiny/oversized graphics,
    or a size-based route_junk rule). The key IS the image's working path (the `id`
    the examiner /thumb resolver + the Un-junk verb use). Each row carries a
    human-readable `reason` (the CLIP `junk_label`, or `metadata_index`'s
    `junk_reason` from the size-based route_junk rules when present) plus the
    routing `source` and `confidence`.

    Returns the FULL sorted list; the section layer in family_archive.py slices it
    into the {rows,total,offset,limit} envelope (813 rows on goog → paginated). The
    family never sees this (the media allow-list excludes photos_junk by
    construction, and the page/api are examiner-gated like review/history)."""
    junk = (scene_index or {}).get("junk_results", {}) or {}
    metadata_index = metadata_index or {}
    rescued = set(rescued or ())   # examiner-un-junked working paths leave this view
    rows = []
    for src, rec in junk.items():
        if src in rescued:
            continue
        rec = rec or {}
        md = metadata_index.get(src, {}) or {}
        rows.append({
            "id": src,
            "name": os.path.basename(src),
            "reason": md.get("junk_reason") or rec.get("junk_label"),
            "source": rec.get("source"),
            "confidence": rec.get("confidence"),
        })
    rows.sort(key=lambda r: r["name"].lower())
    if cap is not None and len(rows) > cap:
        log(f"junk rows: capping at {cap} of {len(rows)}")
        rows = rows[:cap]
    return rows


def transparency_data(summary, dedup_summary, perceptual_groups, suspense, email_noise, role):
    """Duplicates & accounting transparency panel (G-14) — READ-ONLY, no file moves.

    A trust-builder that says, in NUMBERS ONLY, what the pipeline set aside and that
    nothing was deleted. The family sees the reassurance counts; the examiner
    additionally sees the suspense count and the noise-triaged emails that carried a
    real attachment (a genuine miss-risk with zero surface today).

    Family view:
      exact_duplicates_removed  — case_summary.deduplicated_removed (exact byte
                                  duplicates moved to dupes/, never deleted), falling
                                  back to collect_dedup_summary.exact_dupes_moved
      near_duplicate_groups     — len(perceptual_dup_groups.groups): visually similar
                                  photos grouped (keeper kept, the rest set aside)
      nothing_deleted           — always True (the never-destroy invariant)

    Examiner additionally:
      suspense_count            — corrupt/unreadable files parked in output/suspense/
      significant_attachment_noise — noise-triaged emails whose
                                  has_significant_attachment flag is set (from/subject/
                                  date/triage_reason only — never bodies or file paths),
                                  capped; significant_attachment_total is the true count
    """
    summary = summary or {}
    dedup_summary = dedup_summary or {}
    groups = (perceptual_groups or {}).get("groups") or []
    exact_removed = summary.get("deduplicated_removed")
    if exact_removed is None:
        exact_removed = dedup_summary.get("exact_dupes_moved", 0)
    data = {
        "role": role,
        "exact_duplicates_removed": exact_removed or 0,
        "near_duplicate_groups": len(groups),
        "nothing_deleted": True,
    }
    if role != "examiner":
        return data
    # ── examiner-only detail (never sent to a family session) ──
    if isinstance(suspense, dict):
        suspense_items = suspense.get("entries") or suspense.get("paths") or []
    else:
        suspense_items = suspense or []
    data["suspense_count"] = len(suspense_items)
    SIG_CAP = 500
    sig, sig_total = [], 0
    for e in email_noise or []:
        if not isinstance(e, dict) or not e.get("has_significant_attachment"):
            continue
        sig_total += 1
        if len(sig) < SIG_CAP:
            sig.append({
                "from": e.get("email_from"),
                "subject": (e.get("email_subject") or "").replace("\n", " ").strip(),
                "date": e.get("email_date_iso") or e.get("email_date"),
                "triage_reason": e.get("triage_reason"),
            })
    data["significant_attachment_noise"] = sig
    data["significant_attachment_total"] = sig_total
    return data


def guided_review_data(paths, summary, scene_index, face_clustering, geo_index, *,
                       decisions=None, frame_map=None, archive_map=None,
                       per_target_k=None):
    """Guided first-session review checklist (G-12), EXAMINER-ONLY.

    Pure COMPOSITION of the existing review builders into an ordered checklist —
    NO new verbs. Each step carries a live count (drawn from review_data /
    confirm_queue_data / vital_docs_data / reconciliation_manifest / ocr_summary), a
    deep-link to the page where the action already happens, and a `done` state.

    A step is `done` when its count is 0 OR the examiner has ACKNOWLEDGED it.
    Acknowledgement is persisted under family_decisions.json's `guided_progress`
    key via the EXISTING confirm verb (POST /api/confirm {queue:"guided_progress",
    id:<step_key>}) — so no new verb or state store is introduced (the confirm verb
    already writes decisions[queue][id]). A closing handoff summary re-checks the
    export gate."""
    decisions = decisions or {}
    progress = decisions.get("guided_progress", {}) or {}
    review = review_data(paths, summary)
    confirm_items = confirm_queue_data(summary, scene_index, face_clustering, geo_index,
                                       decisions=decisions, frame_map=frame_map,
                                       archive_map=archive_map)
    confirm_count = len(confirm_items)
    unnamed_count = sum(1 for i in confirm_items if i.get("kind") == "unnamed_person")
    vital = vital_docs_data(paths, summary, "examiner", decisions=decisions,
                            per_target_k=per_target_k)
    vital_available = bool(vital.get("available"))
    vital_targets = vital.get("targets", []) if vital_available else []
    # TWO actionable numbers, both draining to 0 as the examiner works:
    #   `vital_unconfirmed` — found vital docs not yet confirmed/dismissed/
    #     reassigned. The Confirm verb drains this (this is what "vital documents
    #     that have not been marked confirmed" means; the old code used
    #     total-found = MISSING TYPES, which Confirm can't touch, so confirming a
    #     doc moved nothing — the bug this fixes).
    #   `vital_near_misses` — candidate hits awaiting a promote/dismiss decision.
    # The step's `count` (badge + done) is their SUM, so the step clears only when
    # every found doc is resolved AND every near-miss reviewed. `vital_capped`
    # counts types whose retrieval hit the ceiling (the near-miss lists are a floor).
    # (The missing-type STATUS — found_count of total_count — is a fact, not a
    # to-do; it rides in `extra` as context and on the Documents page tally.)
    vital_unconfirmed = (vital.get("unconfirmed_count") or 0) if vital_available else 0
    vital_near_misses = sum(t.get("near_miss_count", 0) for t in vital_targets)
    vital_capped = sum(1 for t in vital_targets if t.get("near_miss_capped"))
    recon = review.get("reconciliation", {}) or {}
    recon_items = recon.get("review_items") or []
    ocr = load_json(paths.metadata_dir / "ocr_summary.json", {}) or {}
    ocr_manual = ocr.get("manual_review_count") or 0

    def acknowledged(key):
        rec = progress.get(key)
        return isinstance(rec, dict) and rec.get("decision") == "accept"

    raw_steps = [
        {"key": "quarantine", "label": "Release or discard quarantined items",
         "count": review.get("quarantine_total", 0), "link": "/review?group=quarantine"},
        {"key": "human_review", "label": "Read the human-review items",
         "count": review.get("human_review_count", 0), "link": "/review?group=human",
         "extra": {"ocr_manual_review": ocr_manual}},
        {"key": "confirm", "label": "Work the confirm queue",
         "count": confirm_count, "link": "/review?group=confirm"},
        {"key": "name_persons", "label": "Name the unnamed people",
         "count": unnamed_count, "link": "/people"},
        {"key": "vital_docs", "label": "Review vital documents",
         # Both queues drive done/badge: confirm the found docs AND review the
         # near-misses. Confirming a doc drops `unconfirmed` → drops this count.
         "count": vital_unconfirmed + vital_near_misses, "link": "/documents",
         "extra": {"unconfirmed": vital_unconfirmed,
                   "near_misses": vital_near_misses,
                   "capped_targets": vital_capped,
                   "per_target_k": per_target_k,
                   "found": vital.get("found_count", 0),
                   "total": vital.get("total_count", 0),
                   "available": vital_available}},
        {"key": "reconciliation", "label": "Resolve reconciliation attention items",
         "count": len(recon_items), "link": "/review",
         "extra": {"needs_examiner_review": recon.get("needs_examiner_review"),
                   "items": recon_items[:50]}},
    ]
    steps = []
    for s in raw_steps:
        ack = acknowledged(s["key"])
        s["acknowledged"] = ack
        s["done"] = ack or (s["count"] == 0)
        steps.append(s)

    gate = summary.get("export_gate", {}) or {}
    reasons = family_block_reasons(summary)
    all_done = all(s["done"] for s in steps)
    return {
        "steps": steps,
        "done_count": sum(1 for s in steps if s["done"]),
        "step_count": len(steps),
        "handoff": {
            "export_gate": gate,
            "delivery_blocked": bool(gate.get("delivery_blocked")),
            "reasons": reasons,
            "all_steps_done": all_done,
            "ready": all_done and not reasons,
        },
    }


def accounts_data(summary, role):
    """'Online Accounts' — mapped to what the pipeline actually produces.

    There are NO monetary balances in the pipeline: this is digital account
    inventory (login services seen in mail), the credentials report, and a count
    of financial documents. Never emits raw secret values.
    """
    domains = []
    for domain, info in (summary.get("digital_account_inventory", {}) or {}).items():
        info = info or {}
        domains.append({
            "domain": domain,
            "count": info.get("count"),
            "sample_subjects": (info.get("sample_subjects") or [])[:3],
        })
    domains.sort(key=lambda d: (d.get("count") or 0), reverse=True)

    creds = summary.get("credentials_report", {}) or {}
    cred_items = []
    for it in creds.get("items", []) or []:
        row = {"file": it.get("file")}
        if role != "family":  # examiner sees types/severity; family gets filename only
            row["types"] = it.get("credential_types")
            row["severity"] = it.get("severity")
        cred_items.append(row)

    credentials = {
        "critical_count": creds.get("critical_count", 0),
        "informational_count": creds.get("informational_count", 0),
        "items": cred_items,
    }
    if role == "family" and cred_items:
        # B7: family sees which files hold credentials, framed with a caution.
        credentials["guidance"] = CREDENTIAL_FAMILY_GUIDANCE
    return {
        "domains": domains,
        "credentials": credentials,
        "financial_docs_count": (summary.get("document_counts", {}) or {}).get("financial", 0),
        "note": "Login services and security findings recovered from the estate. "
                "No monetary balances are extracted by the pipeline.",
    }


def documents_index(rows):
    """Group document rows into a category index for drill-through navigation.

    Returns [{category, count, subcategories:[{name,count}]}] sorted by count
    desc. Sub-categories are only populated for 'financial' (the only category
    the pipeline second-passes); others carry an empty list.
    """
    cats = {}
    for r in rows:
        c = r.get("category") or "miscellaneous"
        cat = cats.setdefault(c, {"category": c, "count": 0, "subs": {}})
        cat["count"] += 1
        if c == "financial":
            sub = r.get("subcategory") or "uncategorized"
            cat["subs"][sub] = cat["subs"].get(sub, 0) + 1
    out = []
    for c in cats.values():
        subs = [{"name": k, "count": v}
                for k, v in sorted(c["subs"].items(), key=lambda kv: -kv[1])]
        out.append({"category": c["category"], "count": c["count"], "subcategories": subs})
    out.sort(key=lambda c: -c["count"])
    return out


def email_rows(threads_index, *, cap=None, decisions=None):
    """Thread-grain rows for the Emails section, from email_threads_index.json.

    Thread grain (not message grain) keeps the list manageable (~17k threads vs
    ~36k messages). The raw .eml `files` are intentionally NOT included here (they
    live under original_files/ and would leak paths + bloat the payload); the
    thread-detail endpoint resolves them server-side by thread_id.

    Threads the examiner has demoted (`family_decisions.json` `email_demoted`,
    keyed by thread_id) are forced to significance 0 so they drop to the bottom
    band of the sort (still listed, just no longer at the top), and flagged
    `demoted: True` so the UI can offer a Restore.
    """
    demoted = set((decisions or {}).get("email_demoted", {}) or {})
    threads = (threads_index or {}).get("threads", []) or []
    rows = []
    for t in threads:
        tid = t.get("thread_id")
        is_demoted = str(tid) in demoted
        rows.append({
            "thread_id": tid,
            "subject": (t.get("subject") or "(no subject)").replace("\n", " ").strip(),
            "participants": t.get("participants") or [],
            "date_first": t.get("date_first"),
            "date_last": t.get("date_last"),
            "significance": 0 if is_demoted else t.get("significance"),
            "demoted": is_demoted,
            "categories": t.get("categories") or [],
            "message_count": t.get("message_count"),
        })
    # Rank by significance (matches gen_email_threads): significance desc, then
    # most-recent activity, then subject — so the cap keeps the most significant.
    rows.sort(key=lambda r: r.get("subject") or "")
    rows.sort(key=lambda r: (r.get("date_last") or ""), reverse=True)
    rows.sort(key=lambda r: -(r.get("significance") or 0))
    if cap is not None and len(rows) > cap:
        log(f"emails: capping at {cap} of {len(rows)} threads")
        rows = rows[:cap]
    return rows


def _year(iso):
    """Leading 4-digit year of an ISO timestamp, or None if unparseable."""
    s = str(iso or "")
    if len(s) < 4:
        return None
    try:
        return int(s[:4])
    except ValueError:
        return None


def _years_span(first, last):
    """Whole-year span between two ISO timestamps (>=0), or None when either end
    is missing/unparseable — drives the correspondent card's 'span of years'."""
    a, b = _year(first), _year(last)
    if a is None or b is None:
        return None
    return max(0, b - a)


_CORR_NAME_STRIP = re.compile(r"[^a-z\s]")
_CORR_WS = re.compile(r"\s+")


def _normalize_corr_name(name):
    n = _CORR_NAME_STRIP.sub("", (name or "").lower().strip())
    return _CORR_WS.sub(" ", n).strip()


def _corr_root_domain(address):
    """The registrable domain (last two dot-labels) of an email address, or ''
    for an address with no @ — used to tell 'one person's several personal
    mailboxes' (distinct providers) from 'one organization's many technical
    subaddresses' (one shared brand domain)."""
    m = re.search(r"@([^@]+)$", address or "")
    if not m:
        return ""
    parts = m.group(1).lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else m.group(1).lower()


def _duplicate_cluster_id(addresses):
    key = ",".join(sorted((a or "").strip().lower() for a in addresses))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def correspondent_duplicate_candidates(correspondent_freq, decisions=None):
    """P2 #9: possible-duplicate-identity suggestions for the Correspondents
    page — detection ONLY, never an auto-merge (an examiner reviews each
    cluster via the correspondent/merge or correspondent/reject verb, which
    records the decision as a DECISIONS OVERLAY exactly like person_merges).

    An exact normalized-display-name match alone is a poor signal on real
    mail: it clusters every subaddress of one bulk sender (Amazon, a
    newsletter, a hotel loyalty program) under its own shared brand domain
    just as readily as it clusters a real person's several personal
    mailboxes. So a candidate cluster must ALSO:
      - span >= 2 distinct root domains (rules out one organization's many
        technical subaddresses under a single domain);
      - normalize to exactly two name tokens (a plausible "First Last", not
        a multi-word brand/program name);
      - include at least one address that is genuinely bidirectional
        (marketing/loyalty mail is receive-only; a real correspondence has
        at least one reply somewhere in the relationship).
    Measured against a real ~2,400-correspondent test corpus, this narrows
    245 raw exact-name-match groups (677 rows, dominated by brand noise like
    17 amazon.com subaddresses) down to 57 plausible person-candidates.

    A cluster already resolved (every one of its addresses collapses to one
    winner via decisions.correspondent_merges) or explicitly rejected
    (its cluster_id is in decisions.correspondent_merge_rejected) is
    excluded so a confirmed/dismissed suggestion never resurfaces."""
    decisions = decisions or {}
    rejected = set(decisions.get("correspondent_merge_rejected") or [])
    merges = decisions.get("correspondent_merges") or {}
    already_merged = set(merges.keys()) | set(merges.values())

    by_name = {}
    for c in correspondent_freq or []:
        addr = (c.get("address") or "").strip()
        if not addr:
            continue
        nm = _normalize_corr_name(c.get("display_name"))
        if nm:
            by_name.setdefault(nm, []).append(c)

    candidates = []
    for nm, rows in by_name.items():
        if len(rows) < 2 or len(nm.split()) != 2:
            continue
        if not any(r.get("bidirectional") for r in rows):
            continue
        addrs = [(r.get("address") or "").strip().lower() for r in rows]
        if any(a in already_merged for a in addrs):
            continue
        domains = {_corr_root_domain(a) for a in addrs}
        if len(domains) < 2:
            continue
        cid = _duplicate_cluster_id(addrs)
        if cid in rejected:
            continue
        addr_rows = sorted(
            ({"address": (r.get("address") or "").strip(),
              "total": r.get("total") or 0,
              "bidirectional": bool(r.get("bidirectional"))} for r in rows),
            key=lambda x: -x["total"])
        candidates.append({
            "cluster_id": cid,
            "name": (rows[0].get("display_name") or nm).strip(),
            "addresses": addr_rows,
            "total_combined": sum(r.get("total") or 0 for r in rows),
        })
    candidates.sort(key=lambda c: -c["total_combined"])
    return candidates


def correspondents_data(correspondent_freq, threads_index=None, conversation_index=None,
                        role="family", decisions=None):
    """Ranked correspondent cards for the Correspondents page (G-6).

    Source is the caller's ALREADY ROLE-SCOPED correspondent frequency file — a
    LIST of per-address aggregates the email stage computed ({address,
    display_name, sent_count, received_count, total, bidirectional, first_seen,
    last_seen, subject_diversity}). Sorted by total volume desc, then name.

    The roles no longer see the same cards, and the reason is not sensitivity —
    it is coherence. The cards are non-sensitive relationship metadata (volumes
    and dates, no bodies), but the family's Emails list excludes estate-rescued
    mail, so a card for a marketing sender would rank near the top by sheer
    volume and its ?participant= click-through would return NOTHING. The family's
    file is therefore built from family-visible mail only (see
    wyeast.core.audience.correspondent_path); the examiner's is the union.

    `threads_index` / `conversation_index` are accepted for signature/spec parity
    (the click-through filters the Emails list server-side by participant — see the
    `?participant=` filter — so the card itself needs only the freq file).

    We do NOT do fuzzy identity merging here on our own initiative — an
    examiner-confirmed correspondent_merges {loser_addr: winner_addr} overlay
    (see correspondent_duplicate_candidates + verb_correspondent_merge_confirm)
    folds a loser address's stats into its resolved winner and drops the loser
    row, exactly like person_merges folds a face cluster. Never mutates
    correspondent_frequency.json itself — a display-time fold only.
    """
    merges = (decisions or {}).get("correspondent_merges") or {}
    by_addr = {}
    for c in correspondent_freq or []:
        addr = (c.get("address") or "").strip()
        if addr:
            by_addr[addr.lower()] = c

    combined = {}
    for addr_lower, c in by_addr.items():
        winner_key = resolve_merge(addr_lower, merges)
        winner_c = by_addr.get(winner_key, c)
        acc = combined.setdefault(winner_key, {
            "address": (winner_c.get("address") or winner_key).strip(),
            "name": (winner_c.get("display_name") or "").strip()
                    or (winner_c.get("address") or winner_key).strip(),
            "sent": 0, "received": 0, "total": 0, "bidirectional": False,
            "first_seen": None, "last_seen": None,
            "subject_diversity": winner_c.get("subject_diversity"),
            "merged_addresses": [],
        })
        sent = c.get("sent_count") or 0
        received = c.get("received_count") or 0
        total = c.get("total")
        if total is None:
            total = sent + received
        acc["sent"] += sent
        acc["received"] += received
        acc["total"] += total
        acc["bidirectional"] = acc["bidirectional"] or bool(c.get("bidirectional"))
        fs, ls = c.get("first_seen"), c.get("last_seen")
        if fs and (acc["first_seen"] is None or fs < acc["first_seen"]):
            acc["first_seen"] = fs
        if ls and (acc["last_seen"] is None or ls > acc["last_seen"]):
            acc["last_seen"] = ls
        if addr_lower != winner_key:
            addr = (c.get("address") or "").strip()
            if addr:
                acc["merged_addresses"].append(addr)

    rows = []
    for acc in combined.values():
        rows.append({
            "address": acc["address"],
            "name": acc["name"],
            "sent": acc["sent"],
            "received": acc["received"],
            "total": acc["total"],
            "bidirectional": acc["bidirectional"],
            "first_seen": acc["first_seen"],
            "last_seen": acc["last_seen"],
            "years_span": _years_span(acc["first_seen"], acc["last_seen"]),
            "subject_diversity": acc["subject_diversity"],
            "merged_addresses": sorted(acc["merged_addresses"]),
        })
    rows.sort(key=lambda r: (-(r.get("total") or 0), r.get("name") or ""))
    return rows


def delivered_basename_index(summary, archive_map, role="family"):
    """Map an email-attachment basename → the delivered item id it opens, or None
    when that basename is AMBIGUOUS (maps to >1 distinct delivered item). Used to
    resolve G-4 attachment chips CONSERVATIVELY: a basename that isn't present, or
    collides across delivered items, yields no link (file_id None) — never a wrong
    or broken one.

    Delivered items come from two sources sharing the archive's id space:
      - document_classifications[].filename → that document's `file` (the Documents
        view / lightbox opens a doc by its file id). Email-sourced docs (the .eml
        bodies themselves) are skipped — they aren't a browsable attachment target;
        for the family role, account_credentials docs are skipped too (never
        browsable), mirroring document_rows/vital_docs.
      - archive_map entries (working_path → canonical): basename(working_path) →
        working_path (the photo/media lightbox opens an image by its src key).

    A value of None marks a collided basename; an absent key is simply unmatched —
    both resolve to file_id None at lookup, which is exactly the fail-closed rule.

    A delivered item extracted from mail (tools/expandfiles.py's
    `_extract_attachments`) is deconflicted with a `msgNNNNN_` index prefix
    (e.g. "msg00101_DSC00399.JPG") — but the email's own attachments[] list
    still names it by its ORIGINAL filename ("DSC00399.JPG"), so a bare
    basename match never fires for the majority of attachments (usability
    report finding #7: 13.5% resolved). Also index the de-prefixed form,
    through the same collision-detection `add()` call — a real collision
    (two different messages' attachments sharing a stripped name) still fails
    closed exactly as before.
    """
    idx = {}
    msg_prefix = re.compile(r"^msg\d+_")

    def add(name, fid):
        if not name or not fid:
            return
        base = os.path.basename(name)
        _add_one(base, fid)
        stripped = msg_prefix.sub("", base, count=1)
        if stripped != base:
            _add_one(stripped, fid)

    def _add_one(base, fid):
        if base in idx:
            if idx[base] != fid:
                idx[base] = None  # two distinct delivered items share a name → fail closed
        else:
            idx[base] = fid

    for d in (summary or {}).get("document_classifications", []) or []:
        if (d.get("source") or "").lower() == "email":
            continue
        if role != "examiner" and d.get("category") == "account_credentials":
            continue
        f = d.get("file")
        add(d.get("filename") or os.path.basename(f or ""), f)
    for wp in (archive_map or {}).get("entries", {}) or {}:
        add(os.path.basename(wp), wp)
    return idx


def _resolve_attachments(entry, attachment_index):
    """Attachment chip records for one message's email_index `attachments[]`.

    Carries {filename, content_type, size_bytes, is_inline, file_id}. `file_id` is
    set ONLY when the basename maps to exactly one delivered item (via
    delivered_basename_index); a collided/absent basename → None (name-only chip).
    `is_inline` is preserved so the UI can suppress cid-embedded inline images
    (logos/signatures) from the visible chip list."""
    idx = attachment_index or {}
    out = []
    for a in entry.get("attachments") or []:
        fn = a.get("filename")
        fid = idx.get(os.path.basename(fn)) if fn else None
        out.append({
            "filename": fn,
            "content_type": a.get("content_type"),
            "size_bytes": a.get("size_bytes"),
            "is_inline": bool(a.get("is_inline")),
            "file_id": fid,
        })
    return out


def email_thread_messages(email_by_file, files, *, body_cap=8000):
    """Resolve one thread's messages (by .eml path) to displayable records.

    `email_by_file` maps an .eml path → its email_index.json record. Bodies are
    the message's own text (ocr_text), truncated — NOT passed through
    neutralize_summary (that scrub is for OUR generated summaries, not the
    owner's correspondence).
    """
    msgs = []
    for f in files or []:
        rec = (email_by_file or {}).get(f) or {}
        body = (rec.get("ocr_text") or "").strip()
        msgs.append({
            "file": f,
            "from": rec.get("email_from"),
            # Resolved sender name (email_triage, from the case's address
            # books). The raw header rides along beside it: the family reads
            # the name, the examiner can still see the address.
            "from_display": rec.get("from_display") or rec.get("email_from"),
            "to": rec.get("email_to"),
            "subject": (rec.get("email_subject") or "").replace("\n", " ").strip(),
            "date": rec.get("email_date_iso") or rec.get("email_date"),
            "body": body[:body_cap],
        })
    msgs.sort(key=lambda m: (m.get("date") or ""))
    return msgs


def email_thread_detail(threads_index, email_by_file, sig_by_file, thread_id, *,
                        body_cap=8000, thread_map=None, attachment_index=None):
    """Threaded conversation for one thread (#3) — reuses gen_email_threads'
    proven JWZ threading so the view matches the static explorer: clean plain-text
    bodies (rendered pre-wrap by the UI), reply nesting via `depth`, per-message
    significance. Returns None if the thread_id is unknown.

    `thread_map` ({thread_id: thread}) lets the caller pass a prebuilt index so the
    lookup is O(1) instead of a linear scan of the ~17k-thread list per detail GET
    (R-5); when absent the scan is used (backward compatible)."""
    if thread_map is not None:
        meta = thread_map.get(thread_id)
    else:
        threads = (threads_index or {}).get("threads", []) or []
        meta = next((t for t in threads if t.get("thread_id") == thread_id), None)
    if meta is None:
        return None
    entries = [email_by_file[f] for f in (meta.get("files") or []) if f in (email_by_file or {})]
    msgs = []
    if entries:
        from tools.gen_email_threads import build_threads  # pure; lazy to avoid import coupling
        depth = {}
        for bt in build_threads(entries):
            for m in bt["messages"]:
                e = m["entry"]
                f = e.get("file")
                rp = m.get("reply_to_file")
                d = depth[rp] + 1 if (rp is not None and rp in depth) else 0
                depth[f] = d
                msgs.append({
                    "file": f,
                    "from": e.get("email_from"),
                    "from_display": e.get("from_display") or e.get("email_from"),
                    "to": e.get("email_to"),
                    "subject": (e.get("email_subject") or "").replace("\n", " ").strip(),
                    "date": e.get("email_date_iso") or e.get("email_date"),
                    "body": (e.get("ocr_text") or "").strip()[:body_cap],
                    "significance": (sig_by_file or {}).get(f),
                    "depth": min(d, 6),
                    # G-4: attachments that rode on this message, each cross-linked
                    # to its delivered doc/photo when the basename resolves uniquely.
                    "attachments": _resolve_attachments(e, attachment_index),
                })
    return {
        "thread_id": thread_id,
        "subject": (meta.get("subject") or "(no subject)").replace("\n", " ").strip(),
        "messages": msgs,
    }


def message_rows(conversation_index, role="family", *, cap=None):
    """Conversation-grain rows for the Messages section, from message_triage's
    conversation_index.json (mirrors email_rows' thread grain: the list runs off
    the small index; per-message detail is a separate lazy per-conversation load).

    ROLE-SCOPED (wyeast.core.audience.can_see_conversation), and it has to be —
    this function is the one chokepoint the family's Messages list, the overview
    count, the family search and build_fts all route through. It used to take no
    role at all and exclude only `discard`, which meant:

      * an ESTATE-RESCUED conversation — one family-relevance triage had already
        thrown out — came back as verdict "keep" and, because keep sorts into the
        first band below, LED the family's Messages list; and
      * a PLATFORM conversation reached the family with bodies that were never
        screened, because message_triage only chunks keep-verdict conversations
        and chunks are the only thing sensitive_scan ever sees.

    The examiner still sees both (platform traffic is account-existence evidence).
    Discards reach nobody, as before.

    NOTE: no significance data yet — llm_synthesis's message classification
    sweep is a parallel lane; when it lands, join its per-conversation
    significance here and rank by it (like email_rows), keeping this
    size/recency order as the tiebreak.
    """
    rows = []
    excluded = 0
    for c in conversation_index or []:
        verdict = c.get("triage_verdict") or "keep"
        if not can_see_conversation(c, role):
            excluded += 1
            continue
        participants = c.get("participants") or []
        rows.append({
            "conversation_id": c.get("conversation_id"),
            "platform": c.get("platform"),
            "display_name": (c.get("display_name") or "").strip()
                            or ", ".join(participants) or "(conversation)",
            "participants": participants,
            "span": c.get("span") or [None, None],
            "message_count": c.get("message_count") or 0,
            "call_event_count": c.get("call_event_count") or 0,
            "verdict": verdict,
        })
    # Stable multi-key sort: size, then recency (span end), then keep-first.
    rows.sort(key=lambda r: -(r.get("message_count") or 0))
    rows.sort(key=lambda r: ((r.get("span") or [None, None])[-1] or ""), reverse=True)
    rows.sort(key=lambda r: 0 if r.get("verdict") == "keep" else 1)
    if excluded:
        log(f"messages[{role}]: excluded {excluded} conversation(s) this audience "
            f"may not see (discarded, or — for the family — platform traffic and "
            f"estate-rescued conversations)")
    if cap is not None and len(rows) > cap:
        log(f"messages: capping at {cap} of {len(rows)} conversations")
        rows = rows[:cap]
    return rows


# iMessage app content (link previews, stickers, Apple Pay, polls). The store
# records these as attachments, but no media file has ever existed for them —
# so an unresolved one is NOT missing material and must not be reported as if
# it were. Extension is the reliable marker; the stem is an opaque UUID.
_APP_PAYLOAD_EXTS = {".pluginpayloadattachment"}


def attachment_kind(name) -> str:
    """Classify a message attachment for display: what it IS, not whether we
    have it (availability is `src`). One of: app_payload, image, video,
    document, unknown.

    Extension sets come from wyeast.core.media (config-driven, file_types.json)
    rather than a local literal, so an extension added there reaches the
    archive too — same lazy-import pattern as the video helpers above.
    """
    ext = Path(str(name or "")).suffix.lower()
    if ext in _APP_PAYLOAD_EXTS:
        return "app_payload"
    try:
        from wyeast.core.media import (IMAGE_EXTENSIONS, VIDEO_EXTENSIONS,
                                       PDF_EXTENSIONS)
    except Exception:                       # keep the archive renderable
        return "unknown"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in PDF_EXTENSIONS or ext in {".doc", ".docx", ".txt", ".rtf",
                                        ".pages", ".numbers", ".key",
                                        ".xls", ".xlsx", ".ppt", ".pptx"}:
        return "document"
    return "unknown"


def conversation_detail(conv, *, index_record=None, attachment_resolver=None,
                        body_cap=4000, msg_cap=10000):
    """Chronological bubble-transcript detail for ONE conversation, from its lazy
    per-conversation JSON (output/metadata/messages/<conversation_id>.json).
    Mirrors email_thread_detail; returns None when `conv` is None (unknown
    conversation / message_triage not run).

    Each message carries `direction` (sent|received) for bubble styling.
    `attachment_resolver(source_path) -> servable src or None` resolves an
    attachment's source-side path to something /media can serve (the caller
    wires it through canonical_for + resolve_media_path, exactly like photos);
    an unresolvable attachment renders name-only — never a broken link.
    """
    if not conv:
        return None
    # The per-conversation file (messages/<id>.json) holds the transcript; the
    # title and the resolved participant names live on the conversation_index
    # record, which the caller already has in hand. Merge rather than re-read.
    meta = index_record or conv
    resolve = attachment_resolver or (lambda _p: None)
    # handle -> resolved name, from the conversation's own participant_contacts
    # (message_triage). The per-message `sender` is deliberately the RAW handle
    # — chunk keys hash the rendered text, so resolving names into the stored
    # transcript would move every key — but the bubble a reader sees should
    # carry the name, so it is resolved here at render time.
    by_handle = {}
    for pc in (meta.get("participant_contacts")
               or conv.get("participant_contacts") or []):
        name = (pc.get("display_name") or "").strip()
        handle = (pc.get("handle") or "").strip()
        if handle and name and name != handle:
            by_handle[handle] = name
    msgs = []
    for m in (conv.get("messages") or [])[:msg_cap]:
        atts = []
        for a in m.get("attachments") or []:
            try:
                src = resolve(a)
            except Exception:
                src = None
            atts.append({"name": os.path.basename(a or ""), "src": src,
                         "kind": attachment_kind(a)})
        sender = m.get("sender")
        msgs.append({
            "ts": m.get("ts"),
            "sender": sender,
            "sender_display": by_handle.get(sender, sender),
            "direction": m.get("direction"),
            # The owner's own words — truncated, but NOT neutralize_summary'd
            # (that scrub is for OUR generated summaries, same as email bodies).
            "text": (m.get("text") or "")[:body_cap],
            "attachments": atts,
        })
    msgs.sort(key=lambda m: m.get("ts") or "")
    calls = [{"ts": c.get("ts"), "call_type": c.get("call_type"),
              "duration_s": c.get("duration_s")}
             for c in (conv.get("call_events") or [])]
    calls.sort(key=lambda c: c.get("ts") or "")
    return {
        "conversation_id": conv.get("conversation_id"),
        "platform": conv.get("platform"),
        "participants": conv.get("participants") or meta.get("participants") or [],
        # The title the Messages LIST shows for this conversation — a resolved
        # contact, or a group title composed from its members. The detail view
        # used to build its own heading by joining participants, so the same
        # thread was "Alex Rendon, Brian Okafor, Casey Lindqvist + 1" in the list
        # and "Alex Rendon (+15035550178), Brian Okafor (+15035550179), ..."
        # one click later.
        "display_name": (meta.get("display_name")
                         or conv.get("display_name") or "").strip() or None,
        "display_name_source": (meta.get("display_name_source")
                                or conv.get("display_name_source")),
        "triage_verdict": conv.get("triage_verdict"),
        "messages": msgs,
        "call_events": calls,
    }


def actions_history(paths):
    """Parse output/metadata/family_actions.ndjson, newest first (for History).

    Entries are rebased on the way in for the same reason the indexes are: a row's
    `target` is an ITEM ID, and History links on it. Written de-rebased by
    append_action, so the log stays portable across a move — see
    wyeast/core/rebase.py. Line-at-a-time rather than through load_json because
    this is NDJSON, not a document.
    """
    path = paths.metadata_dir / "family_actions.ndjson"
    to_local = _rebase.active().to_local
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(to_local(json.loads(line)))
                except json.JSONDecodeError:
                    continue
    except (FileNotFoundError, OSError):
        return []
    rows.reverse()
    return rows
