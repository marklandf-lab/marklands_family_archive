# Installing the Family Archive on macOS

Step-by-step setup for a Mac (Darwin). Allow about **15 minutes**, most of it
downloading Python.

This installs the **Phase 0** build: the archive runs from a Terminal window,
the same way it runs on the Linux examiner workstation. There is no `.app`, no
icon and no double-click launch yet — see [README.md](README.md) for what that
means.

**You do not need Xcode, a compiler, or Homebrew's `libheif`.** Both
dependencies ship as prebuilt binaries for every supported macOS and chip, so
nothing is built from source during install.

---

## Before you start

| You need | Notes |
|---|---|
| A Mac running **macOS 11 (Big Sur) or later** on Apple Silicon, or **macOS 10.15 (Catalina) or later** on Intel | These floors come from the dependency wheels, see [Appendix A](#appendix-a--what-actually-gets-installed) |
| **Python 3.10 or newer** | Step 2 installs it. Apple's preinstalled Python is too old — see the warning there |
| An internet connection, **once** | Only to download Python and two Python packages. After that the archive runs entirely offline — no cloud, no Ollama, no GPU |
| Access to the `WyeastCorp/mac_family_archive` repository | It is private; Step 3 covers the ways in |
| About **200 MB** of disk | ~120 MB Python, ~60 MB packages, ~3 MB this repo. Case data is separate and much larger |

Everything below happens in **Terminal**. To open it: press `⌘ Space`, type
`Terminal`, press Return. Commands are typed at the prompt and run with Return.

---

## Step 1 — Check what Mac you have

Two facts matter later: your macOS version and your chip.

```bash
sw_vers -productVersion    # e.g. 15.3
uname -m                   # arm64 = Apple Silicon · x86_64 = Intel
```

Compare `sw_vers` against the floors in the table above. If your Mac is older
than that, stop here — the prebuilt packages will not install, and this
iteration has no fallback for it.

---

## Step 2 — Install Python 3.10 or newer

First check whether you already have a usable one:

```bash
python3 --version
```

> ### ⚠️ The version Apple ships is too old
>
> If that printed **3.9.6** — or if it opened a dialog offering to install
> "command line developer tools" — you are looking at Apple's bundled Python,
> which has been 3.9.6 for several macOS releases. It **will not work**, and
> it fails early and clearly rather than subtly: `pip` refuses both packages
> outright, because each declares `requires_python >= 3.10`.
>
> Even if it had installed, the archive would fail on import: several modules
> (`wyeast/core/delivery.py`, `filetypes.py`, `moves.py`) use `str | None`
> type syntax that only exists from Python 3.10 onward.
>
> Installing a newer Python **does not remove or replace** Apple's. The two sit
> side by side, and macOS keeps using its own for system tasks.

If `python3 --version` printed **3.10.0 or higher**, skip ahead to
[Step 3](#step-3--get-the-repository-onto-the-mac).

Otherwise pick one of the two routes below. Either is fine — **Route A** if you
just want it working, **Route B** if you already live in a terminal.

### Route A — the python.org installer (recommended)

A normal Mac app installer. No command line, no Xcode.

1. Go to **<https://www.python.org/downloads/macos/>**
2. Download the latest **macOS 64-bit universal2 installer** under the newest
   stable release (any version 3.10+ works; 3.12 or 3.13 are good picks).
   *Universal2* means one download covers both Apple Silicon and Intel.
3. Open the downloaded `.pkg` and click through the installer.
4. At the end it opens a Finder window. **Double-click `Install Certificates.command`.**
   Skip this and downloads later fail with SSL errors.
5. Back in Terminal, confirm — open a **new** Terminal window first, so it picks
   up the changed `PATH`:

```bash
python3 --version
```

You should now see the version you installed.

### Route B — Homebrew

If you already have Homebrew:

```bash
brew install python@3.12
python3 --version
```

If you do **not** have Homebrew, installing it pulls in the Xcode Command Line
Tools (several GB) as a prerequisite. That is a much heavier path than Route A
for the same result — prefer Route A unless you want Homebrew anyway.

If `python3 --version` still shows the old version after `brew install`, your
`PATH` prefers Apple's. Either open a new Terminal, or use the versioned name
everywhere below:

```bash
python3.12 --version
```

...and tell the setup script which one to use:

```bash
export WYEAST_PYTHON="$(command -v python3.12)"
```

---

## Step 3 — Get the repository onto the Mac

The repository is **private**, so a plain `git clone` of the URL will ask for
credentials. Pick whichever route matches how you already authenticate.

### Option 1 — GitHub CLI (simplest if you have it)

```bash
brew install gh          # skip if already installed
gh auth login            # follow the browser prompts, once
gh repo clone WyeastCorp/mac_family_archive
cd mac_family_archive
```

### Option 2 — SSH key

If your Mac already has an SSH key registered with GitHub:

```bash
git clone git@github.com:WyeastCorp/mac_family_archive.git
cd mac_family_archive
```

### Option 3 — Download a ZIP

No git required. In a browser, open the repository, click **Code → Download
ZIP**, then unzip it (double-click in Finder) and move the folder wherever you
want it.

> **If you downloaded the ZIP through a browser**, macOS marks the contents
> "quarantined". Shell scripts run through `bash` are not blocked by this, but
> if you hit a permissions or "cannot be opened" error, clear the flag:
> ```bash
> xattr -dr com.apple.quarantine ~/Downloads/mac_family_archive-main
> ```

Whichever route you took, `cd` into the folder before continuing. Confirm you
are in the right place:

```bash
ls setup.sh family_archive.sh
```

Both filenames should print. If you get "No such file or directory", you are in
the wrong directory.

---

## Step 4 — Run the setup script

```bash
./setup.sh --dev
```

Use `--dev` if you want to run the test suite in Step 5 (recommended for a
first install — it is the fastest way to know the install is sound). Plain
`./setup.sh` installs the runtime only.

If you get `permission denied`, the execute bit was lost in transit:

```bash
chmod +x *.sh
./setup.sh --dev
```

### What it does

1. Finds a Python 3.10+ interpreter (honouring `$WYEAST_PYTHON` if you set one)
   and refuses with a clear message if it can't.
2. Creates a **private virtual environment** in `.venv/` inside this folder.
   Nothing is installed system-wide, and nothing touches your existing Python.
3. Installs `pillow` and `pillow_heif` at the exact versions the examiner
   workstation runs.
4. Checks the two things that genuinely vary between macOS Python builds.

### What you should see at the end

```
==> checking this interpreter's SQLite has FTS5 (needed for full-text search)
    FTS5: OK

==> checking HEIC/HEIF decode (iPhone photos)
    HEIC: OK (pillow_heif 1.3.0)

Setup complete. Next:  ./family_archive.sh CASE_ID --cases-root /path/to/cases
```

**Both checks must say OK.** They are not cosmetic:

- **FTS5** is the SQLite full-text extension. Without it the Search view does
  not work. Not every Python build links a SQLite that has it compiled in.
- **HEIC** is the format iPhones use for photos by default. Without it those
  thumbnails fail *silently* — blank tiles, no error — which is exactly the
  media a family archive is mostly made of.

If either warns, see [Troubleshooting](#troubleshooting).

---

## Step 5 — Verify the install

```bash
./run_tests.sh
```

Expected, in a few seconds:

```
693 passed, 3 skipped
```

The 3 skips are expected — they cover an optional schema-validation library
that this build deliberately does not install.

This exercises the server, the view builders, the search index, Export, the
role/audience gate, the move ledger, the chain of custody and the release gate.
It creates its own temporary fixtures and never touches real case data.

Anything other than `693 passed` means stop and investigate before pointing the
tool at a real case.

---

## Step 6 — Put a case where the archive can find it

The archive does not create case data — it reads a case the pipeline already
finished, delivered as a **curation bundle** on removable media.

Copy or mount the bundle, then note the path to its **cases directory** — the
folder that *contains* the case folder, not the case folder itself:

```
/Volumes/WyeastUSB/cases/          ← this is the --cases-root
└── CASE_001/                      ← this is the CASE_ID
    ├── case_config.json
    └── output/
        ├── archive/
        └── metadata/
```

Sanity-check it before launching:

```bash
ls /Volumes/WyeastUSB/cases/CASE_001/output/metadata/case_summary.json
```

If that file is missing the archive will refuse to start, and it is right to.

> **Note on availability.** The tool that builds a sanitized curation bundle
> does not exist yet — it is outstanding work on the Wyeast side and it gates
> handing a bundle to anyone off the examiner workstation. Until it lands,
> Phase 0 is exercised against a case tree assembled by hand. See
> [README.md](README.md#getting-a-case-onto-the-mac).

---

## Step 7 — Launch it

```bash
./family_archive.sh CASE_001 --cases-root /Volumes/WyeastUSB/cases
```

It prints:

```
Family Archive:  http://127.0.0.1:7766/
Case:            CASE_001   (role: examiner)
Stop with:       Ctrl+C
```

Open that URL in Safari or Chrome. **Leave the Terminal window open** — closing
it stops the server. Press `Control-C` in that window when you are done.

Common variations:

```bash
./family_archive.sh CASE_001 --cases-root /path/to/cases --role family
./family_archive.sh CASE_001 --cases-root /path/to/cases --port 7777
```

### Saving yourself the `--cases-root` every time

Set it once per Terminal session:

```bash
export WYEAST_CASES_ROOT=/Volumes/WyeastUSB/cases
./family_archive.sh CASE_001
```

Or make it permanent by appending that `export` line to `~/.zshrc`. The launcher
looks for a cases root in this order: `--cases-root` on the command line, then
`$WYEAST_CASES_ROOT`, then a `cases/` folder next to this one, then
`~/WyeastCases`.

> If macOS asks whether to allow incoming network connections, **Deny is the
> correct answer.** The server listens only on `127.0.0.1` — this Mac talking to
> itself — and denying does not affect it.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `python3: command not found`, or a dialog offering "command line developer tools" | No usable Python. Do [Step 2, Route A](#route-a--the-pythonorg-installer-recommended). |
| `ERROR: Package 'pillow' requires a different Python` | Your `python3` is older than 3.10. Do Step 2, then delete `.venv` and re-run `./setup.sh --dev`. |
| `setup.sh: Python 3.10 or newer is required` | Same as above — the script caught it for you. |
| `permission denied: ./setup.sh` | `chmod +x *.sh`, then retry. |
| SSL / certificate errors while downloading packages | You skipped `Install Certificates.command` in Step 2, Route A. Run it, then re-run `./setup.sh`. |
| `WARNING: this Python's SQLite has no FTS5` | That Python build links a SQLite without the search extension. Install Python from python.org (Route A) — those builds include it — then `rm -rf .venv` and re-run setup. |
| `WARNING: HEIC thumbnails will silently fail` | `pillow_heif` did not install. Re-run `./setup.sh` and read the pip output; the usual cause is an unsupported macOS version (see Step 1). |
| Tests report anything other than `693 passed` | Do not proceed to a real case. Capture the full output and raise it. |
| `no metadata dir at ...` | Wrong `--cases-root`, or you pointed at the case folder instead of the folder containing it. Re-check the layout in [Step 6](#step-6--put-a-case-where-the-archive-can-find-it). |
| `port 7766 is already in use` and a warning about a second instance | An archive server is probably already running in another Terminal window. Use that one, or relaunch with `--port 7777`. |
| Browser shows "can't connect" | The server stopped, or the Terminal window was closed. Relaunch. |
| Photos show as blank tiles | HEIC decode is missing — re-check the Step 4 output. |
| `cases root '...' does not exist` | The launcher had nothing to fall back on. Pass `--cases-root` explicitly. |

---

## Appendix A — What actually gets installed

Only two third-party packages, pinned to the versions the examiner workstation
runs:

| Package | Version | Why it is needed |
|---|---|---|
| `pillow` | 12.2.0 | Thumbnail rendering and EXIF-orientation correction |
| `pillow_heif` | 1.3.0 | Decoding HEIC/HEIF — the default iPhone photo format |

Both publish prebuilt wheels for CPython **3.10 through 3.14**, for **arm64
(Apple Silicon) and x86_64 (Intel)** alike. That is why no compiler is needed.
The wheel platform tags are also where the macOS floors in
[Before you start](#before-you-start) come from: `macosx_11_0_arm64` and
`macosx_10_15_x86_64`.

Everything else the archive uses is in Python's standard library — the web
server, the SQLite search index, the file locking, JSON handling.

Explicitly **not** installed: the examiner workstation's machine-learning stack
(PyTorch, CUDA, scikit-learn and friends, roughly 15 GB). The archive never
imports any of it, and much of it has no macOS build at all. Do not install
Wyeast's `requirements/venv-phase1.txt` here.

## Appendix B — Installing without the setup script

If you prefer to do it by hand, or `setup.sh` will not run:

```bash
cd /path/to/mac_family_archive
python3 -m venv .venv
./.venv/bin/python3 -m pip install --upgrade pip
./.venv/bin/python3 -m pip install -r requirements.txt      # or requirements-dev.txt
```

Then run the same two checks the script performs:

```bash
./.venv/bin/python3 -c "import sqlite3; sqlite3.connect(':memory:').execute('CREATE VIRTUAL TABLE t USING fts5(x)'); print('FTS5 OK')"
./.venv/bin/python3 -c "import pillow_heif; pillow_heif.register_heif_opener(); print('HEIC OK')"
```

`family_archive.sh` finds `.venv/bin/python3` on its own. To use a different
interpreter instead, set `WYEAST_PYTHON` to its full path.

## Appendix C — Installing on a Mac with no internet

The install itself needs the network once. To avoid that, download the two
wheels on a connected Mac **of the same chip and Python version**, carry them
over, and install from the folder:

```bash
# on the connected Mac
python3 -m pip download -r requirements.txt -d wheelhouse

# on the offline Mac, with wheelhouse/ copied across
python3 -m venv .venv
./.venv/bin/python3 -m pip install --no-index --find-links=wheelhouse -r requirements.txt
```

The chip and Python version must match, because the wheels are architecture- and
version-specific.

## Appendix D — Updating and uninstalling

**Update** — pull the latest code and rebuild the environment:

```bash
git pull
rm -rf .venv
./setup.sh --dev
./run_tests.sh
```

**Uninstall** — delete the folder:

```bash
rm -rf /path/to/mac_family_archive
```

That removes the archive and its private `.venv` completely. It does **not**
touch your case data, which lives elsewhere, and it does not touch the Python
you installed in Step 2 (remove that through its own uninstaller if you want it
gone).
