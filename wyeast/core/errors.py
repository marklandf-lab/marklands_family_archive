"""
wyeast.core.errors — typed errors for the exception taxonomy (review M5).

Stdlib-pure (enforced by ``test_core_package_is_stdlib_pure``). Provides the
class-C primitive of the M5 taxonomy: a REQUIRED index/map that is present but
unreadable/corrupt must **fail fast** — never be silently coerced to an empty
default, which fails open (the ``load_archive_map`` regression fixed in #186 let
a corrupt delivery map read as "no mappings", leaving flagged media in the
delivery tree).

Use ``load_required_index`` for any inter-stage contract file a stage cannot
correctly run without. A reader that can legitimately tolerate ABSENCE passes
``missing_ok=True`` (a *corrupt present* file still raises); one that cannot
leaves it False (absence also raises).
"""

import json
from pathlib import Path


class RequiredDataError(RuntimeError):
    """A required index/map is present-but-corrupt, or missing when not allowed.

    Class C of the exception taxonomy (docs/archive/specs/review-exception-taxonomy.md):
    required inter-stage contract data must fail fast, never be swallowed into an
    empty default. Subclasses ``RuntimeError`` so existing broad ``except
    Exception`` handlers (e.g. the s11 export-gate brander) still catch it.
    """


def load_required_index(path, *, missing_ok: bool = False):
    """Load a required JSON index/map, or raise :class:`RequiredDataError`.

    - **Absent** file: returns ``None`` when ``missing_ok`` else raises.
    - **Present but unreadable/unparseable**: ALWAYS raises ``RequiredDataError``
      (chained from the underlying ``OSError`` / ``json.JSONDecodeError``) —
      never returns a default. This is the class-C rule that prevents fail-open.

    Returns the parsed JSON object on success.
    """
    p = Path(path)
    try:
        text = p.read_text()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise RequiredDataError(f"required index missing: {p}") from None
    except OSError as e:
        raise RequiredDataError(f"required index unreadable: {p}: {e}") from e
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        raise RequiredDataError(f"required index corrupt: {p}: {e}") from e
