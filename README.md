# mac_family_archive

The Wyeast **Family Archive** — the local web app for exploring and curating a
finished estate case — packaged to run on macOS instead of the Zone B examiner
workstation.

This is **Phase 0** of
[`docs/specs/family-archive-macos-standalone.md`](https://github.com/WyeastCorp/Wyeast/blob/main/docs/specs/family-archive-macos-standalone.md):
the tool runs on a Mac, from a terminal, exactly the way it runs on the Linux
side. There is deliberately no `.app` bundle, no icon, no double-click launch
and no code signing yet — those are Phases 1–2.

The archive server itself is **unmodified**. Same verbs, same audit trail, same
move ledger and chain of custody as Zone B. See [UPSTREAM.md](UPSTREAM.md) for
exactly what was copied and the four small local modifications.

## Requirements

- macOS with Python **3.10 or newer** (`wyeast/core/delivery.py` uses PEP 604
  `str | None` annotations). Homebrew or a python.org installer both work.
- A network connection **once**, to install two pip packages. After that the
  archive runs entirely offline — no Ollama, no GPU, no cloud.
- A finished case tree (see [Getting a case onto the Mac](#getting-a-case-onto-the-mac)).

Total third-party footprint is two packages: `pillow` and `pillow_heif`. Do
**not** install Wyeast's `requirements/venv-phase1.txt` here — it carries the
~15 GB torch/CUDA stack the archive never imports, and several of those wheels
have no macOS build at all.

## Install

**[INSTALL.md](INSTALL.md) is the step-by-step guide** — installing Python on
macOS, getting this private repo onto the Mac, offline installs, and a
troubleshooting table. The short version, if you already have Python 3.10+:

```bash
git clone https://github.com/WyeastCorp/mac_family_archive.git
cd mac_family_archive
./setup.sh          # or ./setup.sh --dev to also get pytest
```

`setup.sh` creates a private `.venv`, installs the pinned dependencies, and
checks the two things that vary between Python builds on macOS:

- **SQLite FTS5** — needed for the Search view. Apple's system `python3` has
  historically shipped without it. If the check warns, use a python.org or
  Homebrew Python.
- **HEIC/HEIF decode** — needed for iPhone photos. Without `pillow_heif` those
  thumbnails fail silently (blank, not an error), which is exactly the media a
  family archive is built from.

## Run

```bash
./family_archive.sh CASE_ID --cases-root /path/to/cases
```

Then open the URL it prints (`http://127.0.0.1:7766/`). Stop with Ctrl-C.

```bash
./family_archive.sh CASE_ID --role family     # family view, honours the export gate
./family_archive.sh CASE_ID --port 7777
./family_archive.sh CASE_ID --force           # serve even if the case looks incomplete
```

Exit codes match Zone B: `0` served · `1` bad args / missing case ·
`2` case not complete · `3` family view blocked.

### Where it looks for cases

`family_archive.sh` resolves the cases root in this order, and only supplies one
if you didn't:

1. `--cases-root /path` on the command line
2. `$WYEAST_CASES_ROOT`
3. a `cases/` directory next to this checkout
4. `~/WyeastCases`

The default has to come from the launcher because
`config/pipeline_config.json` is a byte-identical copy of Zone B's and still
names `/data/cases`, which doesn't exist on a Mac.

`$WYEAST_PYTHON` overrides the interpreter if you don't want the `.venv`.

### Getting a case onto the Mac

A family Mac is not Zone B and must never reach `/cases` directly. Case data
arrives as a **curation bundle** on removable media — the same OS-independent
format the Linux appliance spec defines
([`family-archive-standalone-appliance.md`](https://github.com/WyeastCorp/Wyeast/blob/main/docs/specs/family-archive-standalone-appliance.md)).
Point `--cases-root` at the bundle's cases directory.

> **The sanitized-metadata half of that bundler does not exist yet.** It is
> named as outstanding v1 work in the appliance spec and it blocks shipping a
> bundle to *any* off-Zone-B target, Mac included. Until it lands, this repo is
> exercised against a manually materialized case tree, which is precisely what
> Phase 0 is for.

## Verifying

```bash
./setup.sh --dev
./run_tests.sh
```

693 tests, carried over from Wyeast's suite: the archive server, the view/data
builders, the FTS index, Export, the audience/role gate, the move ledger, the
chain of custody, and the release-gate enforcement. Nothing touches a real case.

This checkout was verified on Linux against a real case corpus before being
published: every page shell, every `/api/*` view, `/assets/*`, JPEG **and HEIC**
thumbnail rendering, and FTS5 search all served correctly under a venv
containing *only* `pillow` and `pillow_heif`. The equivalent run on actual Mac
hardware is the remaining Phase 0 step.

## Known macOS caveats

- **APFS is case-insensitive by default.** The pipeline that produced the case
  ran on a case-sensitive Linux filesystem. Two delivered files differing only
  in case will collide on copy to a Mac. Worth checking when a bundle is built.
- **APFS normalizes filenames.** macOS returns NFD-decomposed Unicode from
  directory listings where Linux stores NFC. Filenames with accents that are
  matched between a directory scan and an index JSON could mismatch. Not
  observed yet — flagged because it is the classic Linux→Mac path bug.
- **Gatekeeper does not apply** to this iteration: there is no `.app`, and
  scripts run from a terminal aren't gated. It becomes a real problem in
  Phase 2.
- **Symlinks and `fcntl.flock` work natively** on macOS — both are POSIX, and
  neither needs admin rights the way they do on Windows. This is the whole
  reason a Mac port is packaging work rather than a rewrite.

## What this is not

- Not an installer, not a `.app`, not signed or notarized (Phases 1–2).
- Not the pipeline. `wyeast/stages/`, the orchestration scripts and the stage
  registry are not here; a Mac only ever consumes a case the pipeline already
  finished.
- Not a second source of truth. Fixes to the archive itself belong upstream in
  WyeastCorp/Wyeast, then flow here via `./sync_from_wyeast.sh`.
