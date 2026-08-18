"""
wyeast.core.filetypes — the single source of truth for file-type handling.

Every "what kind of file is this, and what should the pipeline do with it"
decision is keyed off the data here: which extensions are images / videos /
documents / RAW / archives / email; which downstream engine can read which
format; and what to do with a file at intake (collect into a bucket, read it
in place, surface it for the family, convert it, expand it, or ignore it).

The data lives in ``config/file_types.json`` (resolved via
``wyeast.core.config.config_dir()``), but every value has an embedded default
here that exactly matches the historically-hardcoded sets. A missing or
unreadable config file therefore changes **nothing** — this keeps the
air-gapped Zone-B workstation safe and makes the externalization a pure
refactor. The config OVERRIDES defaults per-category; categories absent from
the file fall back to the embedded default for that category.

Stdlib-pure (json / pathlib / os only) so it imports under every step venv,
like the rest of ``wyeast.core``. ``wyeast.core.media`` re-exports the
resolved IMAGE / VIDEO / PDF frozensets from here, so existing
``from wyeast.core.media import IMAGE_EXTENSIONS`` call sites are unchanged.
"""

import fnmatch
import json
import logging
import os
import sys
from pathlib import Path

from wyeast.core.config import config_dir

_log = logging.getLogger("wyeast.filetypes")

CONFIG_FILENAME = "file_types.json"

# ── Embedded defaults — must mirror the previously-hardcoded sets exactly ──────
# (media.py IMAGE/VIDEO/PDF, s01 DOCUMENT_EXTENSIONS, convertraw.sh RAW_EXTS,
#  expandfiles ARCHIVE_EXTENSIONS, s10 sniff set, s08/s11/s06/s05 engine sets).
_DEFAULTS = {
    "schema_version": 1,
    "dispositions": {
        "appledouble": {
            # macOS AppleDouble resource-fork stubs (`._<name>` sidecars written
            # to non-native filesystems / inside zips). A few KB of Finder
            # metadata, zero content — a `._Foo.mp4` stub is NOT a video. Must
            # be first in _CATEGORY_ORDER so the pattern beats every other
            # category's patterns and extensions. Left in place, never collected.
            "name_patterns": ["._*"],
            "action": "ignore",
        },
        "image": {
            # Raster stills the vision stages treat as family photos. HEIF-family
            # variants reuse the existing HEIF path (heif_decode / needs_jpeg_for_llava).
            # AVIF is NOT here — pillow_heif (Zone B) has no AVIF decoder, so CLIP
            # cannot open it; it lives in "graphic" instead.
            "extensions": [
                ".jpg", ".jpeg", ".jpe", ".jfif",
                ".png", ".gif", ".bmp", ".webp",
                ".tiff", ".tif",
                ".heic", ".heif", ".hif", ".heics", ".heifs",
                ".insp",
            ],
            "action": "collect", "bucket": "photos",
        },
        "graphic": {
            # Vector / editor-project / HDR / exotic-raster / icon stills — surfaced,
            # NOT collected into photos (the vision stages' PIL/pillow_heif path
            # can't open them; AVIF included — no pillow_heif AVIF decoder on Zone B).
            "extensions": [
                ".svg", ".svgz", ".eps", ".ai",
                ".psd", ".psb", ".xcf",
                ".exr", ".hdr", ".dds",
                ".jxl", ".jp2", ".j2k", ".jpf", ".jpx",
                ".avif", ".avifs",
                ".tga", ".pcx", ".ico", ".cur", ".icns",
            ],
            "action": "surface", "bucket": "other/graphics",
        },
        "video": {
            # Container formats. .crm is Canon Raw Movie (video, not still RAW).
            "extensions": [
                ".mov", ".qt", ".mp4", ".m4v", ".avi", ".mkv", ".webm",
                ".wmv", ".asf", ".flv", ".f4v",
                ".mpg", ".mpeg", ".mpe", ".m1v", ".m2v", ".vob",
                ".3gp", ".3gpp", ".3g2",
                ".mts", ".m2ts", ".m2t", ".ts",
                ".mxf", ".ogv", ".rm", ".rmvb",
                ".insv", ".360", ".crm",
                ".hevc", ".h264", ".264", ".265",
            ],
            "action": "collect", "bucket": "videos",
        },
        "document": {
            # .txt / .eml intentionally excluded (OCR-sidecar false positives;
            # email is read in place by stage 10) — see "email"/"ignore" below.
            "extensions": [
                ".pdf",
                ".doc", ".docx", ".docm", ".dot", ".dotx", ".dotm", ".wbk",
                ".xls", ".xlsx", ".xlsm", ".xlsb", ".xlt", ".xltx", ".xltm", ".xlm",
                ".ppt", ".pptx", ".pptm", ".pps", ".ppsx", ".ppsm",
                ".rtf",
                ".odt", ".ott", ".ods", ".odp", ".sxw",
                ".csv", ".tsv",
                ".pages", ".numbers", ".key",
                ".wpd", ".wp", ".wp5", ".wp6", ".wpt", ".wps", ".wri",
                ".epub", ".mobi", ".azw", ".azw3", ".fb2", ".ibooks",
            ],
            "action": "collect", "bucket": "documents",
        },
        "raw": {
            # Camera RAW — converted to a JPEG proxy by convertraw; original
            # retained. Ambiguous .raw omitted; .crm is video.
            "extensions": [
                ".cr2", ".cr3", ".crw",
                ".nef", ".nrw",
                ".arw", ".arq", ".srf", ".sr2",
                ".raf", ".orf", ".ori", ".rw2", ".rwl",
                ".pef", ".ptx", ".srw", ".x3f",
                ".3fr", ".fff", ".iiq", ".cap", ".mos",
                ".dng", ".gpr",
                ".mrw", ".dcr", ".kdc", ".k25", ".dcs", ".drf",
                ".erf", ".mef", ".bay", ".mdc", ".rwz",
                ".ari", ".braw", ".r3d",
            ],
            "action": "convert_raw",
        },
        "archive": {
            # Container formats expanded by expandfiles. Value = handler key.
            # Limited to formats with a real handler in expandfiles.HANDLERS.
            "extensions": {
                ".zip": "zip", ".rar": "rar", ".7z": "7z",
                ".tar": "tar", ".tar.gz": "tar", ".tgz": "tar",
                ".tar.bz2": "tar", ".tar.xz": "tar", ".tbz2": "tar", ".txz": "tar",
                ".gz": "gz", ".bz2": "bz2", ".xz": "xz",
                ".pst": "pst", ".ost": "pst", ".mbox": "mbox", ".mbx": "mbox",
                ".msg": "msg", ".dmg": "dmg", ".iso": "iso",
            },
            "action": "expand",
        },
        "email": {
            # Leaf email files — read in place by stage 10, never moved.
            # Apple Mail .emlx shares this disposition; wyeast.core.mailformats
            # strips its byte-count/plist frame. .cnm stays unknown/flag — no
            # sample or spec was ever sourced for it.
            "extensions": [".eml", ".emlx"],
            "action": "read_in_place",
        },
        "email_sniff": {
            # Non-.eml candidates content-sniffed for RFC 2822 by stage 10 /
            # expandfiles (mirrors s10 DEFAULT_SNIFF_EXTENSIONS).
            "extensions": [".txt", ".mail"],
            "action": "sniff_email",
        },
        "message_export": {
            # Message-corpus exports (SMS/WhatsApp/IM) recognized by BASENAME
            # PATTERN only — .xml/.json/.html/.txt are far too common in case
            # data to claim by extension (messaging-ingestion spec §4.1).
            # `sniff_message` is a claim to inspect, not a promise the file is
            # handled: files stay in place (collect_dedup takes no action) and
            # reconciliation keeps them examiner-visible until the Phase-1
            # message_triage stage content-sniffs and parses them.
            "extensions": [],
            "name_patterns": [
                "sms-*.xml", "calls-*.xml",           # SMS Backup & Restore
                "message_*.json", "messages.json",    # Facebook/Instagram, Google Chat
                "result.json",                        # Telegram Desktop
                "Hangouts.json",                      # Google Takeout Hangouts
                "WhatsApp Chat*.txt",                 # WhatsApp chat export
                "* - Text - *.html",                  # Google Takeout Voice (texts)
                "* - Voicemail - *.html",             # Google Takeout Voice (voicemail)
            ],
            "action": "sniff_message",
        },
        "calendar": {
            "extensions": [".ics", ".vcs"],
            "action": "surface", "bucket": "other/calendars",
        },
        "contact": {
            "extensions": [".vcf"],
            "action": "surface", "bucket": "other/contacts",
        },
        "audio": {
            "extensions": [
                ".mp3", ".aac", ".m4a", ".m4b", ".m4p",
                ".wav", ".aiff", ".aif", ".aifc",
                ".flac", ".ogg", ".oga", ".opus", ".wma",
                ".amr", ".awb", ".3ga", ".caf",
                ".wv", ".ape", ".mpc", ".dsf", ".dff",
                ".aa", ".aax", ".ra", ".ram", ".au", ".snd",
                ".mid", ".midi",
            ],
            "action": "surface", "bucket": "other/audio",
        },
        "database": {
            # May contain user data — surfaced for examiner inspection, never
            # ignored. WAL/SHM/journal sidecars ride along for every SQLite
            # naming variant: a phone-pulled store routinely carries its newest
            # messages only in the WAL (messaging-ingestion spec §3.3), so a
            # stranded sidecar is real data loss.
            "extensions": [
                ".db", ".sqlite", ".sqlite3", ".sqlitedb",
                ".db-wal", ".db-shm", ".db-journal",
                ".sqlite-wal", ".sqlite-shm", ".sqlite-journal",
                ".sqlite3-wal", ".sqlite3-shm", ".sqlite3-journal",
                ".sqlitedb-wal", ".sqlitedb-shm", ".sqlitedb-journal",
                ".plist", ".abcddb", ".abcdmr",
            ],
            "action": "surface", "bucket": "other/databases",
        },
        "encrypted_message_store": {
            # Encrypted message stores (WhatsApp Android crypt12/14/15, Signal
            # backups) — unreadable without an examiner-supplied key (Phase 3);
            # surfaced so they are never silently invisible, and reconciliation
            # raises an examiner-attention item while any are present.
            "extensions": [".crypt12", ".crypt14", ".crypt15"],
            "name_patterns": ["signal-*.backup"],
            "action": "surface", "bucket": "other/messages/encrypted",
        },
        "disk_image": {
            # Forensic disk/VM images — surfaced for examiner mount-and-recurse,
            # not auto-expanded. Legacy single-file AFF v1 (.aff) is intentionally
            # excluded: it collides with the ubiquitous Hunspell affix dictionary
            # and is superseded by .aff4; .aff is ignored as app-resource non-content.
            "extensions": [
                ".e01", ".ex01", ".l01", ".lx01", ".s01",
                ".afd", ".afm", ".aff4",
                ".vmdk", ".vhd", ".vhdx", ".vdi", ".qcow2", ".dd",
                ".sparseimage",
            ],
            "action": "surface", "bucket": "other/disk_images",
        },
        "shortcut": {
            # Forensically meaningful (targets + MAC times) — surfaced, not discarded.
            "extensions": [".lnk", ".url", ".webloc"],
            "action": "surface", "bucket": "other/shortcuts",
        },
        "media_sidecar": {
            # Action-cam / 360 proxies + clip thumbnails — duplicate-of-master,
            # left in place (ignored), never collected.
            "extensions": [".lrv", ".lrf", ".thm"],
            "action": "ignore",
        },
        "ignore": {
            # Known non-content files: never collected, never flagged. 'names' also
            # covers intake-tool summaries and gallery sidecars read in place by the
            # gallery catalog (AlbumInfo.json / Subscribed Albums.json).
            "extensions": [
                ".exe", ".dll", ".com", ".msi", ".sys", ".drv",
                ".so", ".dylib", ".o", ".a", ".ko", ".elf", ".out",
                ".deb", ".rpm", ".apk", ".appimage", ".pkg", ".jar",
                ".class", ".pyc", ".pyo", ".obj", ".lib", ".pdb",
                ".tmp", ".temp", ".swp", ".swo", ".swn",
                ".part", ".partial", ".crdownload", ".crswap", ".download",
                ".cache", ".pid", ".lock", ".aria2",
                ".ttf", ".otf", ".ttc", ".woff", ".woff2", ".eot",
                ".fon", ".fnt", ".dfont", ".pfb", ".pfm",
                ".aff",  # Hunspell affix dictionary (app resource, not AFF disk image)
                ".log", ".ini", ".cfg", ".conf", ".reg", ".inf", ".manifest",
                ".ds_store",
            ],
            "names": [
                "expandfiles_summary.json", "raw_metadata.json",
                "mms_parts_map.json",
                "AlbumInfo.json", "Subscribed Albums.json",
                "thumbs.db", "ehthumbs.db", "ehthumbs_vista.db", "desktop.ini",
                ".ds_store", ".localized",
                "hiberfil.sys", "pagefile.sys", "swapfile.sys",
            ],
            "action": "ignore",
        },
    },
    "engine_sets": {
        # Data only — the handling/branching logic stays in the stages.
        "pdf": [".pdf"],
        # Audio formats the transcribe stage (faster-whisper / PyAV-ffmpeg) can
        # decode for speech-to-text. A speech-oriented subset of the `audio`
        # disposition: note-sequence (.mid/.midi) and DRM/exotic lossless
        # formats are excluded.
        "transcription": [
            ".mp3", ".aac", ".m4a", ".m4b", ".m4p",
            ".wav", ".aiff", ".aif", ".aifc",
            ".flac", ".ogg", ".oga", ".opus", ".wma",
            ".amr", ".awb", ".3ga", ".caf", ".au", ".snd",
        ],
        "ocr_tesseract": [".pdf", ".tiff", ".tif", ".bmp", ".png"],
        "ocr_paddle": [".jpg", ".jpeg", ".png", ".tiff", ".tif"],
        "scene_exclude": [".webp"],            # s06: LLaVA tokenizer crash on webp
        "needs_jpeg_for_llava": [             # s06: convert first
            ".webp", ".heic", ".heif", ".hif", ".heics", ".heifs",
        ],
        "scene_tiff_skip": [".tiff", ".tif"],  # s06: Ollama/LLaVA crash on tiff
        "heif_decode": [                       # s11: decode via PIL not OpenCV
            ".heic", ".heif", ".hif", ".heics", ".heifs",
        ],
        "exif_untaggable": [".bmp"],           # s05/s06: exiftool can't tag bmp
    },
    # Top-level OS/system subtrees pruned from intake collection when the source
    # is a full system volume. Anchored to depth-1 dirs under original_files/ and
    # gated by a root_marker; pruned files are kept + reported, never deleted.
    # Keep in sync with config/file_types.json.
    "skip_dirs": {
        "names": [
            "WINDOWS", "WinNT", "Program Files", "Program Files (x86)",
            "ProgramData", "Windows.old", "$WINDOWS.~BT", "$WINDOWS.~WS",
            "$Recycle.Bin", "RECYCLER", "System Volume Information",
            "MSOCache", "PerfLogs", "Recovery",
        ],
        "root_markers": ["boot.ini", "ntldr", "NTDETECT.COM", "pagefile.sys", "bootmgr"],
    },
    # Directory-shaped containers: a bundle DIRECTORY whose real payload is an
    # extensionless file inside it. Apple Mail stores a mailbox as a directory
    # ``INBOX.mbox/`` holding a file literally named ``mbox`` (alongside
    # ``table_of_contents``). Neither half is reachable by the extension table:
    # every intake caller filters to ``is_file()`` so the bundle directory is
    # never classified, and ``"mbox".endswith(".mbox")`` is False for the inner
    # file. Without this rule the entire mailbox falls to unknown/flag and
    # contributes ZERO messages — silent data loss, not a duplication problem.
    # Keyed by bundle-directory suffix; the value names the inner payload
    # file(s) and the expandfiles handler that reads them.
    # Keep in sync with config/file_types.json.
    "bundle_containers": {
        ".mbox": {"inner_names": ["mbox"], "handler": "mbox"},
    },
}

# Disposition categories consulted, in priority order, by disposition_for().
# (name_patterns across ALL categories are checked before any extension match,
# so message_export's position here only orders it among other pattern sets.)
_CATEGORY_ORDER = (
    "appledouble",
    "image", "graphic", "video", "document", "raw", "archive",
    "email", "email_sniff", "message_export", "calendar", "contact", "audio",
    "database", "encrypted_message_store", "disk_image", "shortcut",
    "media_sidecar", "ignore",
)


# ── Loading ───────────────────────────────────────────────────────────────────
def load_file_types(config_dir_override=None) -> dict:
    """
    Return the merged file-types config: the embedded defaults with any
    per-category overrides from ``config/file_types.json`` applied. A missing
    or unreadable file yields the embedded defaults unchanged (no behavior
    change), matching the graceful-degrade contract of load_pipeline_config().
    """
    base = Path(config_dir_override) if config_dir_override else config_dir()
    path = base / CONFIG_FILENAME
    try:
        with open(path) as f:
            user = json.load(f)
    except FileNotFoundError:
        return _DEFAULTS
    except Exception as e:  # malformed JSON / unreadable — degrade, don't halt
        _log.warning("Could not read %s: %s — using built-in file-type defaults", path, e)
        return _DEFAULTS
    return _merge(user)


def _merge(user: dict) -> dict:
    """Overlay a user config over defaults at category / engine-set granularity."""
    merged = {
        "schema_version": user.get("schema_version", _DEFAULTS["schema_version"]),
        "dispositions": dict(_DEFAULTS["dispositions"]),
        "engine_sets": dict(_DEFAULTS["engine_sets"]),
        # skip_dirs is replaced wholesale (not category-merged): a user that
        # supplies its own block fully controls the prune list.
        "skip_dirs": dict(user.get("skip_dirs") or _DEFAULTS["skip_dirs"]),
        # bundle_containers is likewise replaced wholesale.
        "bundle_containers": dict(
            user.get("bundle_containers") or _DEFAULTS["bundle_containers"]),
    }
    for k, v in (user.get("dispositions") or {}).items():
        merged["dispositions"][k] = v
    for k, v in (user.get("engine_sets") or {}).items():
        merged["engine_sets"][k] = v
    return merged


def _exts(category_entry) -> list:
    """Extensions of a disposition entry — keys when the value is a dict (archive)."""
    exts = category_entry.get("extensions", [])
    return list(exts.keys()) if isinstance(exts, dict) else list(exts)


def _norm(exts) -> frozenset:
    return frozenset(e.lower() for e in exts)


# ── Resolved accessors (optional cfg for testability) ─────────────────────────
def _disp(cfg, name) -> dict:
    return (cfg or load_file_types())["dispositions"].get(name, {})


def image_extensions(cfg=None) -> frozenset:
    return _norm(_exts(_disp(cfg, "image")))


def video_extensions(cfg=None) -> frozenset:
    return _norm(_exts(_disp(cfg, "video")))


def pdf_extensions(cfg=None) -> frozenset:
    return engine_set("pdf", cfg)


def document_extensions(cfg=None) -> frozenset:
    return _norm(_exts(_disp(cfg, "document")))


def raw_extensions(cfg=None) -> frozenset:
    return _norm(_exts(_disp(cfg, "raw")))


def archive_handlers(cfg=None) -> dict:
    """Map of archive extension -> handler key (e.g. '.zip' -> 'zip')."""
    exts = _disp(cfg, "archive").get("extensions", {})
    return {k.lower(): v for k, v in exts.items()} if isinstance(exts, dict) else {}


def sniff_extensions(cfg=None) -> frozenset:
    return _norm(_exts(_disp(cfg, "email_sniff")))


def engine_set(name, cfg=None) -> frozenset:
    """A named downstream-engine extension set (e.g. 'ocr_tesseract')."""
    return _norm((cfg or load_file_types())["engine_sets"].get(name, []))


def bundle_containers(cfg=None) -> dict:
    """The directory-bundle container policy, keyed by bundle-dir suffix.

    Underscore-prefixed keys (``_note``) are comment slots and are skipped.
    """
    entry = (cfg or load_file_types()).get("bundle_containers") or {}
    return {k: v for k, v in entry.items() if not k.startswith("_")}


def bundle_container_handler(path, cfg=None):
    """Expand-handler key for a container file inside a bundle directory, else None.

    Apple Mail stores a mailbox as a *directory* ``INBOX.mbox/`` whose payload
    is a file literally named ``mbox``. That file has no extension, and the
    directory itself is never classified because every intake caller filters to
    ``is_file()`` — so the extension table alone sends the whole mailbox to
    unknown/flag. Matching on (parent-directory suffix, inner filename) is what
    makes the payload reachable.

    The parent-suffix gate is deliberate: a bare file named ``mbox`` outside a
    ``*.mbox`` directory is not assumed to be a mail container.
    """
    p = Path(path)
    name = p.name.lower()
    parent = p.parent.name.lower()
    for suffix, spec in bundle_containers(cfg).items():
        if not parent.endswith(suffix.lower()):
            continue
        if name in {n.lower() for n in (spec.get("inner_names") or ())}:
            return spec.get("handler")
    return None


def skip_dirs(cfg=None) -> dict:
    """The intake directory-prune policy.

    Returns ``{"names": frozenset(lowercased dir names),
    "root_markers": frozenset(lowercased filenames)}``. ``names`` are top-level
    subtrees pruned from stage-01 collection; ``root_markers`` gate the prune so
    it only fires on a recognizable system volume. Empty ``names`` disables it.
    """
    entry = (cfg or load_file_types()).get("skip_dirs") or {}
    return {
        "names": frozenset(n.lower() for n in entry.get("names", [])),
        "root_markers": frozenset(m.lower() for m in entry.get("root_markers", [])),
    }


def dir_pruner(src_dir, cfg=None):
    """Build the skip_dirs predicate for one source tree.

    Returns ``excluded_subtree(path) -> str | None``: the matched top-level OS
    subtree name when ``path`` is pruned by the policy, else ``None``. The
    system-volume gate (``root_markers`` present at ``src_dir`` root) and the
    list of skip names are evaluated once here; the returned closure only does a
    cheap depth-1 anchor check, so it is safe to call per file. When the policy
    is disabled (empty names) or the gate does not fire, the predicate always
    returns ``None``. Shared by stage 01 (collection) and stage 14
    (reconciliation accounting) so both agree on exactly what was excluded.
    """
    sp = skip_dirs(cfg)
    names = sp["names"]
    src = Path(src_dir)
    if not names or not src.exists():
        return lambda p: None
    top = {e.name.lower() for e in src.iterdir()}
    if sp["root_markers"] and not (sp["root_markers"] & top):
        return lambda p: None

    def excluded_subtree(path):
        rel = Path(path).relative_to(src).parts
        if len(rel) > 1 and rel[0].lower() in names:
            return rel[0]
        return None

    return excluded_subtree


def with_case_overrides(ft_cfg: dict, case_cfg: dict | None) -> dict:
    """Overlay per-case intake overrides from case_config.json onto a file-types
    cfg. Currently supports a top-level ``skip_dirs`` block in case_config.json,
    merged per key over the default (so a case may override just ``names`` —
    e.g. ``[]`` to disable — while inheriting the default ``root_markers``, or
    set ``root_markers: []`` to make the prune unconditional). Absent or empty
    override leaves ``ft_cfg`` unchanged.
    """
    override = (case_cfg or {}).get("skip_dirs")
    if not override:
        return ft_cfg
    merged = dict(ft_cfg)
    base = dict(ft_cfg.get("skip_dirs") or {})
    base.update(override)
    merged["skip_dirs"] = base
    return merged


def surface_buckets(cfg=None) -> dict:
    """Map of extension -> output bucket for 'surface' categories (calendar/contact/audio)."""
    out = {}
    cfg = cfg or load_file_types()
    for name, entry in cfg["dispositions"].items():
        if entry.get("action") == "surface":
            for e in _exts(entry):
                out[e.lower()] = entry.get("bucket")
    return out


def disposition_for(path, cfg=None):
    """
    Classify a file by name/extension. Returns ``(category, action, bucket)``.
    ``bucket`` is None when the action does not move the file. Unrecognised
    files return ``("unknown", "flag", None)`` so the reconciliation stage can
    surface them for examiner review rather than dropping them silently.

    Match order: exact-name ignores, then per-category ``name_patterns``
    (case-insensitive fnmatch on the basename), then extension suffixes.
    Patterns beat extensions so that e.g. ``WhatsApp Chat*.txt`` is a
    message export while generic ``.txt`` stays an email-sniff candidate.
    """
    cfg = cfg or load_file_types()
    p = Path(path)
    name = p.name.lower()
    dispositions = cfg["dispositions"]

    # Exact-name ignores (e.g. intake metadata jsons) take precedence.
    ignore = dispositions.get("ignore", {})
    if name in {n.lower() for n in ignore.get("names", [])}:
        return ("ignore", "ignore", None)

    # Directory-bundle payloads (Apple Mail ``INBOX.mbox/mbox``) are matched on
    # (parent suffix, inner name) because they carry no extension of their own.
    # Checked before name_patterns/extensions, which cannot express the parent
    # constraint and would otherwise leave the file at unknown/flag.
    if bundle_container_handler(p, cfg) is not None:
        archive = dispositions.get("archive", {})
        return ("archive", archive.get("action", "expand"), archive.get("bucket"))

    for category in _CATEGORY_ORDER:
        entry = dispositions.get(category)
        if not entry:
            continue
        for pattern in entry.get("name_patterns", ()):
            if fnmatch.fnmatchcase(name, pattern.lower()):
                return (category, entry.get("action"), entry.get("bucket"))

    for category in _CATEGORY_ORDER:
        entry = dispositions.get(category)
        if not entry:
            continue
        for ext in _exts(entry):
            if name.endswith(ext.lower()):
                return (category, entry.get("action"), entry.get("bucket"))
    return ("unknown", "flag", None)


# ── Module-level resolved sets (the media.py re-export surface) ────────────────
_CFG = load_file_types()
IMAGE_EXTENSIONS = image_extensions(_CFG)
VIDEO_EXTENSIONS = video_extensions(_CFG)
PDF_EXTENSIONS = pdf_extensions(_CFG)


# ── CLI: emit shell-friendly fragments for convertraw.sh / expandfiles ─────────
def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(description="Emit file-type sets for shell consumers.")
    ap.add_argument("--raw-globs", action="store_true",
                    help="Print find -iname expressions for RAW extensions.")
    ap.add_argument("--raw-exts", action="store_true",
                    help="Print bare RAW extensions (no dot), space-separated.")
    args = ap.parse_args(argv)
    cfg = load_file_types()
    if args.raw_globs:
        exprs = []
        for ext in sorted(raw_extensions(cfg)):
            exprs += ["-o", "-iname", f"*{ext}"]
        print(" ".join(exprs[1:]))  # drop leading -o
    elif args.raw_exts:
        print(" ".join(sorted(e.lstrip(".") for e in raw_extensions(cfg))))
    else:
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
