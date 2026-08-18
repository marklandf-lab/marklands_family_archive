"""Unit tests for wyeast.core.filetypes — the file-type single source of truth.

The load-bearing guarantee is *no behavior change*: with no config file (or a
partial one), the resolved sets must equal the values that used to be
hardcoded in wyeast.core.media / s01 / s08 / s11 / convertraw / expandfiles.
"""

import json

import pytest

from wyeast.core import filetypes as ft
from wyeast.core import media


# ── Fallback-to-defaults (the air-gap / no-behavior-change contract) ──────────
def test_missing_config_falls_back_to_defaults(tmp_path):
    cfg = ft.load_file_types(config_dir_override=tmp_path)  # empty dir, no file
    assert cfg == ft._DEFAULTS
    img = ft.image_extensions(cfg)
    # Legacy raster + the HEIF family that reuses the existing HEIF path (verified
    # on Zone B: pillow_heif opens these for CLIP).
    assert {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff",
            ".heic", ".heif", ".hif", ".heics", ".heifs"} <= img
    # Vector / editor / exotic-raster / AVIF stills route to the surface 'graphic'
    # category, never collect->photos: the vision stages' PIL/pillow_heif path
    # can't open them (pillow_heif has no AVIF decoder on Zone B).
    assert not ({".svg", ".eps", ".ai", ".psd", ".xcf", ".jxl", ".ico",
                 ".avif", ".avifs"} & img)
    vid = ft.video_extensions(cfg)
    assert {".mov", ".mp4", ".m4v", ".mkv", ".webm", ".360", ".insv", ".crm"} <= vid
    assert ft.pdf_extensions(cfg) == frozenset({".pdf"})


def test_unreadable_config_degrades_to_defaults(tmp_path):
    (tmp_path / ft.CONFIG_FILENAME).write_text("{ this is not json")
    assert ft.load_file_types(config_dir_override=tmp_path) == ft._DEFAULTS


def test_shipped_config_matches_embedded_defaults():
    """config/file_types.json must mirror the embedded defaults exactly."""
    cfg = ft.load_file_types()  # resolves the real config/ dir
    assert ft.image_extensions(cfg) == ft.image_extensions(ft._DEFAULTS)
    assert ft.video_extensions(cfg) == ft.video_extensions(ft._DEFAULTS)
    assert ft.document_extensions(cfg) == ft.document_extensions(ft._DEFAULTS)
    assert ft.raw_extensions(cfg) == ft.raw_extensions(ft._DEFAULTS)
    assert ft.archive_handlers(cfg) == ft.archive_handlers(ft._DEFAULTS)
    for name in ft._DEFAULTS["engine_sets"]:
        if name == "_note":
            continue
        assert ft.engine_set(name, cfg) == ft.engine_set(name, ft._DEFAULTS)


# ── media.py re-export stays identical (drop-in compatibility) ────────────────
def test_media_reexport_matches_filetypes():
    assert media.IMAGE_EXTENSIONS == ft.IMAGE_EXTENSIONS
    assert media.VIDEO_EXTENSIONS == ft.VIDEO_EXTENSIONS
    assert media.PDF_EXTENSIONS == ft.PDF_EXTENSIONS


def test_sets_lowercase_dot_prefixed_disjoint():
    for s in (ft.IMAGE_EXTENSIONS, ft.VIDEO_EXTENSIONS, ft.PDF_EXTENSIONS):
        assert s and all(e == e.lower() and e.startswith(".") for e in s)
    assert not (ft.IMAGE_EXTENSIONS & ft.VIDEO_EXTENSIONS)
    assert not (ft.IMAGE_EXTENSIONS & ft.PDF_EXTENSIONS)
    assert not (ft.VIDEO_EXTENSIONS & ft.PDF_EXTENSIONS)


# ── Per-category override + fallback ──────────────────────────────────────────
def test_partial_override_falls_back_per_category(tmp_path):
    (tmp_path / ft.CONFIG_FILENAME).write_text(json.dumps({
        "schema_version": 1,
        "dispositions": {"image": {"extensions": [".jpg", ".avif"],
                                   "action": "collect", "bucket": "photos"}},
        "engine_sets": {},
    }))
    cfg = ft.load_file_types(config_dir_override=tmp_path)
    # overridden category reflects the file
    assert ft.image_extensions(cfg) == frozenset({".jpg", ".avif"})
    # untouched categories fall back to embedded defaults
    assert ft.video_extensions(cfg) == ft.video_extensions(ft._DEFAULTS)
    assert ft.engine_set("ocr_tesseract", cfg) == ft.engine_set("ocr_tesseract", ft._DEFAULTS)


# ── Engine sets (consumed by stages in video_frames) ────────────────────────────────
def test_engine_sets_match_legacy_values():
    assert ft.engine_set("ocr_tesseract") == frozenset({".pdf", ".tiff", ".tif", ".bmp", ".png"})
    assert ft.engine_set("ocr_paddle") == frozenset({".jpg", ".jpeg", ".png", ".tiff", ".tif"})
    assert ft.engine_set("scene_exclude") == frozenset({".webp"})
    assert ft.engine_set("heif_decode") == frozenset({
        ".heic", ".heif", ".hif", ".heics", ".heifs"})
    assert ft.engine_set("exif_untaggable") == frozenset({".bmp"})
    # s06 invariant still holds when computed via the config
    assert ft.IMAGE_EXTENSIONS - ft.engine_set("scene_exclude") == media.IMAGE_EXTENSIONS - {".webp"}


# ── disposition_for routing ───────────────────────────────────────────────────
@pytest.mark.parametrize("name,expected", [
    ("vacation.JPG", ("image", "collect", "photos")),
    ("clip.MOV", ("video", "collect", "videos")),
    ("will.pdf", ("document", "collect", "documents")),
    ("DSC1234.CR2", ("raw", "convert_raw", None)),
    ("backup.zip", ("archive", "expand", None)),
    ("photos.tar.gz", ("archive", "expand", None)),
    ("letter.eml", ("email", "read_in_place", None)),
    ("4321.emlx", ("email", "read_in_place", None)),          # Apple Mail single message
    ("4321.partial.emlx", ("email", "read_in_place", None)),  # ...held incompletely
    ("mystery.cnm", ("unknown", "flag", None)),               # deliberately NOT claimed
    ("message_001.txt", ("email_sniff", "sniff_email", None)),
    ("birthdays.ics", ("calendar", "surface", "other/calendars")),
    ("reminders.vcs", ("calendar", "surface", "other/calendars")),
    ("dad.vcf", ("contact", "surface", "other/contacts")),
    ("voicemail.m4a", ("audio", "surface", "other/audio")),
    ("song.flac", ("audio", "surface", "other/audio")),
    ("clip.360", ("video", "collect", "videos")),
    ("canon.crm", ("video", "collect", "videos")),          # Canon Raw Movie = video
    ("logo.svg", ("graphic", "surface", "other/graphics")),
    ("layers.psd", ("graphic", "surface", "other/graphics")),
    ("photo.jxl", ("graphic", "surface", "other/graphics")),  # surfaced, not vision-processed
    ("phone.avif", ("graphic", "surface", "other/graphics")),  # no pillow_heif AVIF decoder -> surfaced
    ("messages.sqlite", ("database", "surface", "other/databases")),
    ("contacts.abcddb", ("database", "surface", "other/databases")),
    ("AddressBook.sqlitedb", ("database", "surface", "other/databases")),
    ("evidence.E01", ("disk_image", "surface", "other/disk_images")),
    ("laptop.vmdk", ("disk_image", "surface", "other/disk_images")),
    ("recent.lnk", ("shortcut", "surface", "other/shortcuts")),
    ("GX010001.lrv", ("media_sidecar", "ignore", None)),    # proxy video sidecar
    ("GX010001.thm", ("media_sidecar", "ignore", None)),    # clip thumbnail
    ("thumbs.db", ("ignore", "ignore", None)),              # name beats .db->database
    ("font.ttf", ("ignore", "ignore", None)),
    ("stage.log", ("ignore", "ignore", None)),
    ("raw_metadata.json", ("ignore", "ignore", None)),   # exact-name ignore
    ("mms_parts_map.json", ("ignore", "ignore", None)),  # expand_mms linkage map
    ("AlbumInfo.json", ("ignore", "ignore", None)),      # restored gallery sidecar
    ("mystery.xyz", ("unknown", "flag", None)),
    # AppleDouble resource-fork stubs: basename pattern beats every extension
    ("._Front Door - 2023.mp4", ("appledouble", "ignore", None)),
    ("._IMG_0001.JPG", ("appledouble", "ignore", None)),
    ("._backup.zip", ("appledouble", "ignore", None)),
    ("._DSC1234.CR2", ("appledouble", "ignore", None)),
    ("._letter.eml", ("appledouble", "ignore", None)),
    # message exports: matched by basename PATTERN, never by extension
    ("sms-20240101.xml", ("message_export", "sniff_message", None)),
    ("calls-20240101.xml", ("message_export", "sniff_message", None)),
    ("message_1.json", ("message_export", "sniff_message", None)),   # FB/IG thread
    ("messages.json", ("message_export", "sniff_message", None)),    # Google Chat
    ("result.json", ("message_export", "sniff_message", None)),      # Telegram
    ("Hangouts.json", ("message_export", "sniff_message", None)),
    ("WhatsApp Chat with Mom.txt", ("message_export", "sniff_message", None)),
    ("+15551234567 - Text - 2021-03-01T12_00_00Z.html",
     ("message_export", "sniff_message", None)),                     # Takeout Voice
    ("Jane Doe - Voicemail - 2020-05-01.html",
     ("message_export", "sniff_message", None)),
    ("SMS-20240101.XML", ("message_export", "sniff_message", None)),  # case-insensitive
    ("notes.txt", ("email_sniff", "sniff_email", None)),   # generic .txt untouched
    ("random.json", ("unknown", "flag", None)),            # generic .json NOT claimed
    ("settings.xml", ("unknown", "flag", None)),           # generic .xml NOT claimed
    ("index.html", ("unknown", "flag", None)),             # generic .html NOT claimed
    # encrypted message stores
    ("msgstore.db.crypt14", ("encrypted_message_store", "surface", "other/messages/encrypted")),
    ("msgstore.db.crypt15", ("encrypted_message_store", "surface", "other/messages/encrypted")),
    ("signal-2024-01-01-02-03-04.backup",
     ("encrypted_message_store", "surface", "other/messages/encrypted")),
    # SQLite WAL/SHM/journal sidecars for every naming variant ride with the db
    ("ChatStorage.sqlite-wal", ("database", "surface", "other/databases")),
    ("ChatStorage.sqlite-shm", ("database", "surface", "other/databases")),
    ("sms.db-journal", ("database", "surface", "other/databases")),
    ("chat.sqlite3-wal", ("database", "surface", "other/databases")),
    ("notes.sqlite-journal", ("database", "surface", "other/databases")),
    ("AddressBook.sqlitedb-wal", ("database", "surface", "other/databases")),
])
def test_disposition_for(name, expected):
    assert ft.disposition_for(name) == expected


def test_name_patterns_beat_extensions_but_not_exact_ignores():
    # Pattern pass runs after exact-name ignores and before the extension pass:
    # WhatsApp txt is carved out of email_sniff's .txt, but a pattern could
    # never shadow an exact-name ignore.
    assert ft.disposition_for("WhatsApp Chat with Dad.txt")[0] == "message_export"
    assert ft.disposition_for("whatsapp chat 3.TXT")[0] == "message_export"
    assert ft.disposition_for("thumbs.db") == ("ignore", "ignore", None)


def test_appledouble_beats_other_patterns_and_extensions():
    # appledouble is first in _CATEGORY_ORDER, so a `._` stub of a file whose
    # basename would otherwise match another category's pattern (message
    # exports) or extension (video/image) is still ignored.
    assert ft.disposition_for("._WhatsApp Chat with Dad.txt")[0] == "appledouble"
    assert ft.disposition_for("._sms-20240101.xml")[0] == "appledouble"
    assert ft._CATEGORY_ORDER[0] == "appledouble"
    # Ordinary dotfiles are NOT AppleDouble — `.ds_store` still exact-ignores,
    # and a bare `.hidden.jpg` is still an image.
    assert ft.disposition_for(".ds_store") == ("ignore", "ignore", None)
    assert ft.disposition_for(".hidden.jpg")[0] == "image"


def test_surface_buckets():
    sb = ft.surface_buckets()
    assert sb[".ics"] == "other/calendars"
    assert sb[".vcf"] == "other/contacts"
    assert sb[".mp3"] == "other/audio"
    assert sb[".svg"] == "other/graphics"
    assert sb[".db"] == "other/databases"
    assert sb[".sqlite-wal"] == "other/databases"
    assert sb[".crypt14"] == "other/messages/encrypted"
    assert sb[".e01"] == "other/disk_images"
    assert sb[".lnk"] == "other/shortcuts"


def test_raw_globs_cli(capsys):
    assert ft._main(["--raw-globs"]) == 0
    out = capsys.readouterr().out
    assert "-iname" in out and "*.cr2" in out
    assert not out.strip().startswith("-o")  # leading -o stripped


# ── Schema conformance + embedded-default sync ────────────────────────────────
def test_shipped_config_validates_against_schema():
    """config/file_types.json conforms to config/file_types.schema.json."""
    jsonschema = pytest.importorskip("jsonschema")
    from wyeast.core.config import config_dir
    cfg = json.loads((config_dir() / "file_types.json").read_text())
    schema = json.loads((config_dir() / "file_types.schema.json").read_text())
    jsonschema.validate(cfg, schema)


def test_every_file_category_is_in_category_order():
    """A disposition category absent from _CATEGORY_ORDER is silently skipped by
    disposition_for(), so the shipped file must not declare one that isn't routed."""
    cfg = ft.load_file_types()
    declared = set(cfg["dispositions"]) - {"_note", "_comment"}
    assert declared <= set(ft._CATEGORY_ORDER), (
        f"categories not in _CATEGORY_ORDER: {declared - set(ft._CATEGORY_ORDER)}")


def test_embedded_defaults_match_shipped_dispositions():
    """Air-gap invariant: every disposition's extensions in the shipped file equal
    the embedded defaults, so a missing/unreadable file changes nothing."""
    cfg = ft.load_file_types()
    for cat in ft._CATEGORY_ORDER:
        assert ft._exts(cfg["dispositions"].get(cat, {})) == \
               ft._exts(ft._DEFAULTS["dispositions"].get(cat, {})), f"mismatch in {cat}"


def test_embedded_defaults_match_shipped_name_patterns():
    """Same air-gap invariant for name_patterns: a missing config file must not
    change which basenames are recognized as message exports etc."""
    cfg = ft.load_file_types()
    for cat in ft._CATEGORY_ORDER:
        assert cfg["dispositions"].get(cat, {}).get("name_patterns", []) == \
               ft._DEFAULTS["dispositions"].get(cat, {}).get("name_patterns", []), \
               f"name_patterns mismatch in {cat}"


def test_embedded_skip_dirs_match_shipped():
    """skip_dirs must also match embedded defaults for air-gap parity."""
    cfg = ft.load_file_types()
    assert cfg["skip_dirs"]["names"] == ft._DEFAULTS["skip_dirs"]["names"]
    assert cfg["skip_dirs"]["root_markers"] == ft._DEFAULTS["skip_dirs"]["root_markers"]


def test_with_case_overrides_replaces_names_keeps_default_markers():
    base = ft.load_file_types()
    # case overrides only names -> default root_markers inherited
    merged = ft.with_case_overrides(base, {"skip_dirs": {"names": ["OnlyThis"]}})
    sp = ft.skip_dirs(merged)
    assert sp["names"] == frozenset({"onlythis"})
    assert "ntldr" in sp["root_markers"]              # default markers preserved
    # absent override -> unchanged object
    assert ft.with_case_overrides(base, {}) is base
    assert ft.with_case_overrides(base, None) is base


def test_with_case_overrides_can_disable():
    base = ft.load_file_types()
    merged = ft.with_case_overrides(base, {"skip_dirs": {"names": []}})
    assert ft.skip_dirs(merged)["names"] == frozenset()


def test_dir_pruner_gate_and_anchoring(tmp_path):
    (tmp_path / "WINDOWS" / "x").mkdir(parents=True)
    (tmp_path / "Devon" / "windows").mkdir(parents=True)
    # no marker -> inert
    prune = ft.dir_pruner(tmp_path)
    assert prune(tmp_path / "WINDOWS" / "x" / "a.dll") is None
    # add marker -> gate fires, depth-1 WINDOWS pruned, nested 'windows' not
    (tmp_path / "ntldr").write_text("x")
    prune = ft.dir_pruner(tmp_path)
    assert prune(tmp_path / "WINDOWS" / "x" / "a.dll") == "WINDOWS"
    assert prune(tmp_path / "Devon" / "windows" / "trip.jpg") is None
    # disabled policy -> always None even with a marker
    off = ft.with_case_overrides(ft.load_file_types(), {"skip_dirs": {"names": []}})
    assert ft.dir_pruner(tmp_path, off)(tmp_path / "WINDOWS" / "x" / "a.dll") is None


def test_skip_dirs_helper_normalizes_case():
    sp = ft.skip_dirs()
    assert "windows" in sp["names"] and "program files" in sp["names"]
    assert "ntldr" in sp["root_markers"] and "pagefile.sys" in sp["root_markers"]
    # all entries lowercased
    assert all(n == n.lower() for n in sp["names"])
    assert all(m == m.lower() for m in sp["root_markers"])


def test_skip_dirs_user_block_replaces_defaults(tmp_path):
    (tmp_path / "file_types.json").write_text(json.dumps({
        "schema_version": 1, "dispositions": {}, "engine_sets": {},
        "skip_dirs": {"names": ["Junk"], "root_markers": []},
    }))
    cfg = ft.load_file_types(config_dir_override=tmp_path)
    assert ft.skip_dirs(cfg)["names"] == frozenset({"junk"})


# ── Directory-bundle containers (Apple Mail INBOX.mbox/mbox) ─────────────────
def test_bundle_container_payload_is_an_expandable_archive():
    """The extensionless payload inside a *.mbox directory must be reachable.

    Regression: Apple Mail stores a mailbox as a DIRECTORY 'INBOX.mbox/' whose
    contents are a file literally named 'mbox' plus 'table_of_contents'. The
    directory is never classified (intake filters to is_file()) and
    "mbox".endswith(".mbox") is False, so before this rule a whole mailbox —
    1.76 GB of real mail on the corpus that surfaced it — fell to unknown/flag
    and contributed zero messages.
    """
    assert ft.bundle_container_handler("/c/original_files/INBOX.mbox/mbox") == "mbox"
    assert ft.disposition_for("/c/original_files/INBOX.mbox/mbox") == (
        "archive", "expand", None)
    # nested under an account directory, and the .partial variant
    assert ft.bundle_container_handler(
        "/c/of/V10/acct-uuid/INBOX.mbox/mbox") == "mbox"
    assert ft.bundle_container_handler(
        "/c/of/INBOX.partial.mbox/mbox") == "mbox"


def test_bundle_container_requires_the_parent_suffix():
    """A bare file named 'mbox' outside a *.mbox directory is NOT a container.

    The parent-suffix gate is the whole reason this is a bundle rule rather
    than a name_pattern: name_patterns match the basename only and would
    swallow any unrelated file called 'mbox'.
    """
    assert ft.bundle_container_handler("/c/original_files/notes/mbox") is None
    assert ft.disposition_for("/c/original_files/notes/mbox") == (
        "unknown", "flag", None)


def test_bundle_rule_does_not_disturb_classic_mbox_files():
    """A real *.mbox FILE still classifies by extension, as before."""
    assert ft.bundle_container_handler("/c/original_files/archive.mbox") is None
    assert ft.disposition_for("/c/original_files/archive.mbox") == (
        "archive", "expand", None)


def test_bundle_containers_defaults_mirror_the_shipped_config():
    """_DEFAULTS must mirror config/file_types.json (air-gap contract)."""
    shipped = ft.load_file_types()
    assert ft.bundle_containers(shipped) == ft.bundle_containers(ft._DEFAULTS)
    assert ft.bundle_containers(shipped)[".mbox"]["handler"] == "mbox"


def test_bundle_containers_user_block_replaces_defaults(tmp_path):
    (tmp_path / "file_types.json").write_text(json.dumps({
        "schema_version": 1, "dispositions": {}, "engine_sets": {},
        "bundle_containers": {".foo": {"inner_names": ["payload"],
                                       "handler": "zip"}},
    }))
    cfg = ft.load_file_types(config_dir_override=tmp_path)
    assert ft.bundle_container_handler("/x/thing.foo/payload", cfg) == "zip"
    assert ft.bundle_container_handler("/x/INBOX.mbox/mbox", cfg) is None


def test_bundle_containers_ignores_comment_keys():
    """Underscore keys are comment slots, not bundle suffixes."""
    assert not any(k.startswith("_") for k in ft.bundle_containers())
