#!/usr/bin/env python3
"""regen_video_posters.py — rebuild the missing video poster stills on a Mac.

WHY THIS EXISTS. The archive does not decode video at display time. The pipeline
extracted a still out of each video and wrote it to the WORKING tree
(extracted/photos/<hash>_<name>_f000001.jpg); video_frame_map.json records which
still belongs to which video, and the Videos grid just serves that file. A
delivery carries output/ and leaves the working trees behind, so on a delivered
case every one of those stills is absent — /thumb 404s and the browser draws its
broken-image box. Rebasing cannot help: it points a path at the right place, and
there is no file at that place. Relocation cannot help either — that works by
finding a delivered COPY under output/, and the stills have none.

So we regenerate them from the delivered videos, which ARE here.

WHY qlmanage. It is macOS's Quick Look thumbnailer, backed by AVFoundation, at
/usr/bin/qlmanage. It ships with the OS, so this adds nothing to the archive's
deliberately tiny two-package footprint (pillow, pillow_heif) and needs no
ffmpeg. Its one limitation is that it grabs a frame near the START of the movie
and cannot seek — which is exactly right here, because every poster frame in the
map is frame_index 1 at frame_offset_seconds 0. The later frames (30s, 60s, …)
that some videos also carry are NOT regenerated: they are used for a person's
video appearances beyond the first, and reproducing them faithfully needs a
seeking decoder. `--report` counts them so the gap stays visible.

SAFETY. This writes into a case, so:
  * it is a DRY RUN unless you pass --apply;
  * it NEVER overwrites an existing file — a still that is already there is
    skipped, so the real workstation stills always win if they are re-delivered
    later;
  * it writes only to the paths video_frame_map.json ALREADY names, so no
    pipeline index is edited and no new id enters the archive's id space. Nothing
    in the server changes; the posters simply start resolving.

Usage:
  ./regen_video_posters.py CASE_ID --cases-root /path/to/cases            # dry run
  ./regen_video_posters.py CASE_ID --cases-root /path/to/cases --apply
  ./regen_video_posters.py CASE_ID --cases-root ... --apply --limit 5     # trial
"""
import argparse
import concurrent.futures
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from wyeast.core.delivery import canonical_for  # noqa: E402
from wyeast.core.paths import CasePaths  # noqa: E402
from wyeast.core.rebase import PathRebaser, detect_recorded_root  # noqa: E402

QLMANAGE = "/usr/bin/qlmanage"
# A Quick Look fallback (generic movie icon, or a decode it gave up on) comes back
# tiny. A real frame never does. Cheap guard against writing an icon into the
# gallery and calling it a poster.
MIN_EDGE_PX = 64
DEFAULT_BOX = 1024
DEFAULT_JOBS = 4


# ── planning (pure; no subprocess, no writes) ───────────────────────────────────

def poster_frames(frame_map):
    """{source_video -> poster frame path}, picked the way tools/_archive_data.
    video_rows picks it: the FIRST frame each source video contributes, in file
    order. Mirrored rather than reimplemented on frame_index, so the set we
    regenerate is exactly the set the grid asks for even if a future case orders
    its map differently."""
    poster = {}
    for frame, info in (frame_map or {}).items():
        source = (info or {}).get("source_video")
        if source and source not in poster:
            poster[source] = frame
    return poster


def build_plan(frame_map, archive_entries):
    """Decide what to do with every poster, without touching anything.

    Returns (todo, skipped, unresolved) where todo is [(video, dest)] —
    `video` is the DELIVERED file (the archive canonical, not the working path
    the map names, which a delivery does not carry).
    """
    todo, skipped, unresolved = [], [], []
    for source, dest in poster_frames(frame_map).items():
        if os.path.exists(dest):
            skipped.append(dest)          # never overwrite: a real still wins
            continue
        canonical = canonical_for(source, archive_entries)
        if canonical is None or not os.path.exists(str(canonical)):
            # Undelivered, quarantined, or moved out. The video has no tile in the
            # grid either, so a poster for it would serve nobody.
            unresolved.append(source)
            continue
        todo.append((str(canonical), dest))
    return todo, skipped, unresolved


def orphan_frame_count(frame_map):
    """Frames that are NOT a poster — the 30s/60s/… stills used for a person's
    later video appearances. Out of scope for qlmanage (no seek); counted so the
    remaining gap is reported rather than quietly implied to be fixed."""
    return len(frame_map or {}) - len(poster_frames(frame_map))


# ── grabbing ────────────────────────────────────────────────────────────────────

def grab_poster(video, box=DEFAULT_BOX, timeout=60):
    """One frame from `video` as JPEG bytes, or (None, reason).

    qlmanage names its output after the input basename, and case filenames here
    contain spaces, '#' and other characters worth not predicting — so it writes
    into a private temp dir and we take whatever landed there.
    """
    try:
        from PIL import Image
    except ImportError:
        return None, "pillow not installed (run ./setup.sh)"
    with tempfile.TemporaryDirectory(prefix="wyeast-poster-") as td:
        try:
            subprocess.run([QLMANAGE, "-t", "-s", str(box), "-o", td, str(video)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return None, "qlmanage timed out"
        except OSError as exc:
            return None, f"qlmanage unavailable: {exc}"
        produced = sorted(p for p in Path(td).iterdir() if p.is_file())
        if not produced:
            return None, "no thumbnail produced"
        try:
            with Image.open(produced[0]) as im:
                im = im.convert("RGB")     # Quick Look hands back RGBA PNG
                width, height = im.size
                if max(width, height) < MIN_EDGE_PX:
                    return None, f"looks like a fallback icon ({width}x{height})"
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=85)
                return buf.getvalue(), f"{width}x{height}"
        except Exception as exc:                      # noqa: BLE001 — report, skip
            return None, f"unreadable thumbnail: {exc}"


def write_atomically(dest, data):
    """tmp + rename, so an interrupted run never leaves a truncated JPEG behind
    for the server to serve as a corrupt tile."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.name}.tmp{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, dest)


# ── driver ──────────────────────────────────────────────────────────────────────

def load_case(case_id, cases_root, recorded_root=None):
    """(paths, frame_map, archive_entries) with paths already rebased to here."""
    paths = CasePaths.from_case_id(case_id, cases_root)
    if not paths.metadata_dir.exists():
        raise SystemExit(f"no metadata dir at {paths.metadata_dir}")
    map_path = paths.metadata_dir / "archive_map.json"
    raw = {}
    if map_path.exists():
        with open(map_path, encoding="utf-8") as fh:
            raw = json.load(fh) or {}
    if recorded_root is None:
        recorded_root = detect_recorded_root(raw, paths.case_id)
    rebaser = PathRebaser.for_case(paths.case_dir, recorded_root)
    if rebaser.active:
        print(f"[posters] {rebaser.describe()}")
    archive_map = rebaser.load_json_file(map_path) if map_path.exists() else {}
    frame_map = rebaser.load_json_file(paths.metadata_dir / "video_frame_map.json")
    return paths, frame_map or {}, (archive_map.get("entries") or {})


def run(todo, *, apply, jobs=DEFAULT_JOBS, box=DEFAULT_BOX, grabber=grab_poster):
    """Generate every planned poster. `grabber` is injected so the planning and
    reporting path is testable without invoking Quick Look."""
    written, failed = [], []
    if not apply:
        return written, failed

    def one(job):
        video, dest = job
        data, note = grabber(video, box)
        if data is None:
            return dest, None, note
        write_atomically(dest, data)
        return dest, len(data), note

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for i, (dest, size, note) in enumerate(pool.map(one, todo), 1):
            if size is None:
                failed.append((dest, note))
            else:
                written.append(dest)
            if i % 50 == 0 or i == len(todo):
                print(f"[posters] {i}/{len(todo)} …")
    return written, failed


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("case_id")
    ap.add_argument("--cases-root", required=True)
    ap.add_argument("--recorded-root", default=None,
                    help="Case root the indexes were written against, when "
                         "detection from archive_map.json fails.")
    ap.add_argument("--apply", action="store_true",
                    help="Actually write the stills (default is a dry run).")
    ap.add_argument("--limit", type=int, default=None,
                    help="Only do the first N — use for a trial run.")
    ap.add_argument("--jobs", type=int, default=DEFAULT_JOBS)
    ap.add_argument("--box", type=int, default=DEFAULT_BOX,
                    help=f"Longest edge in px (default {DEFAULT_BOX}).")
    args = ap.parse_args(argv)

    if not os.path.exists(QLMANAGE):
        raise SystemExit(f"{QLMANAGE} not found — this tool is macOS-only.")

    _paths, frame_map, entries = load_case(
        args.case_id, args.cases_root, args.recorded_root)
    todo, skipped, unresolved = build_plan(frame_map, entries)
    orphans = orphan_frame_count(frame_map)

    print(f"[posters] {len(frame_map)} frames in the map · "
          f"{len(todo) + len(skipped) + len(unresolved)} posters")
    print(f"[posters]   to generate      {len(todo)}")
    print(f"[posters]   already present  {len(skipped)}  (never overwritten)")
    print(f"[posters]   no delivered source {len(unresolved)}")
    if orphans:
        print(f"[posters]   NOT covered: {orphans} later-offset frames "
              f"(person video appearances beyond the first; qlmanage cannot seek)")

    if args.limit is not None:
        todo = todo[:args.limit]
        print(f"[posters] --limit {args.limit}: doing {len(todo)}")
    if not args.apply:
        print("[posters] DRY RUN — nothing written. Re-run with --apply.")
        return 0

    written, failed = run(todo, apply=True, jobs=args.jobs, box=args.box)
    print(f"[posters] wrote {len(written)}, failed {len(failed)}")
    for dest, note in failed[:10]:
        print(f"[posters]   FAILED {os.path.basename(dest)}: {note}")
    if len(failed) > 10:
        print(f"[posters]   … and {len(failed) - 10} more")
    print("[posters] restart the archive server to pick them up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
