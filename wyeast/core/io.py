"""
wyeast.core.io — atomic JSON I/O for inter-step index files.

All JSON index output under output/metadata/ must go through
atomic_write_json(): steps may run concurrently with readers (search.py,
chat_case.py, gen_case_report.py), and a crash mid-write must leave either
the previous complete file or nothing partial — never a truncated document.

Contracts (restructuring-spec.md §2.2): each index file has a JSON Schema in
wyeast/schemas/. read_index(..., schema="metadata_index") validates against
it. jsonschema is an *optional* dependency — this module must stay stdlib-pure
so it imports under every step venv (enforced by tests/unit/test_invariants.py),
so jsonschema is imported lazily and a missing install is a no-op (the Zone B
path) — but a loud one: the skip is warned about once per process, and
jsonschema_available() lets a caller refuse to report a pass it did not
actually verify. When present, a contract violation raises under
WYEAST_CONTRACT_STRICT=1 (CI: fail-fast) and only logs a warning otherwise
(production: fail-safe).
"""

import json
import logging
import os
import sys
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"

_log = logging.getLogger(__name__)

# Set once the first time validate_index() is asked to validate something under
# an interpreter with no jsonschema, so the "validation is off" warning is loud
# but not repeated per index file.
_jsonschema_warned = False


def atomic_write_json(path, data, indent: int = 2) -> None:
    """
    Write `data` as JSON to `path` atomically.

    Serialises to a sibling .tmp file, fsyncs it to disk, then os.replace()
    onto the destination — an atomic rename on POSIX. A crash mid-write
    leaves either the previous complete file or nothing partial; readers
    never observe a truncated document.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=indent)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_index(path, default=None, schema=None):
    """
    Read a JSON index file written by an upstream step.

    Returns `default` when the file does not exist and a default was given;
    raises FileNotFoundError otherwise. Parse errors always raise — a
    corrupt index must halt the pipeline, not silently degrade.

    When `schema` (an index name like "metadata_index") is given, the
    parsed document is validated against wyeast/schemas/<schema>.schema.json
    via validate_index() — see this module's docstring for the strict/soft
    semantics.
    """
    path = Path(path)
    if not path.exists():
        if default is not None:
            return default
        raise FileNotFoundError(
            f"Index file not found: {path} — has the upstream step run?")
    with open(path) as f:
        doc = json.load(f)
    if schema is not None:
        validate_index(doc, schema, source=path)
    return doc


def load_schema(name: str) -> dict:
    """Load the JSON Schema for an index by name (e.g. "metadata_index").
    Stdlib-only; raises FileNotFoundError if no such schema is committed."""
    schema_path = SCHEMA_DIR / f"{name}.schema.json"
    if not schema_path.exists():
        raise FileNotFoundError(f"No schema committed for index {name!r}: {schema_path}")
    with open(schema_path) as f:
        return json.load(f)


def jsonschema_available() -> bool:
    """True if schema validation can actually run under this interpreter.

    Callers that report a validation *result* must consult this first: a
    no-op validate_index() is indistinguishable from a clean pass, so
    reporting success without checking is a vacuous green (see
    tools/validate_outputs.py, which refuses to do exactly that).
    """
    try:
        import jsonschema  # noqa: F401
        return True
    except ImportError:
        return False


def validate_index(doc, schema, source=None, strict=None) -> None:
    """
    Validate `doc` against the named index schema (or a schema dict).

    No-op when jsonschema is not installed (the air-gapped Zone B path —
    core must import without it), but a *loud* one: the first skipped
    validation in a process logs a warning naming the interpreter, so a
    disabled guardrail is never silent. When jsonschema is installed, a
    contract violation raises ValueError if `strict` (defaulting to the
    WYEAST_CONTRACT_STRICT=1 environment flag) and otherwise logs a warning
    and returns. `source` is only used to make messages point at the
    offending file.
    """
    try:
        import jsonschema
    except ImportError:
        global _jsonschema_warned
        if not _jsonschema_warned:
            _jsonschema_warned = True
            _log.warning(
                "jsonschema is not installed under %s — schema validation is "
                "DISABLED for this process; every index it checks passes "
                "unvalidated. Install jsonschema into this venv to restore the "
                "contract guarantee. (logged once per process)",
                sys.executable)
        return
    if strict is None:
        strict = os.environ.get("WYEAST_CONTRACT_STRICT") == "1"
    schema_doc = load_schema(schema) if isinstance(schema, str) else schema
    validator = jsonschema.Draft202012Validator(schema_doc)
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.path))
    if not errors:
        return
    where = f" ({source})" if source else ""
    detail = "; ".join(f"at {list(e.path)}: {e.message}" for e in errors[:5])
    msg = f"index{where} violates schema {schema!r}: {detail}"
    if strict:
        raise ValueError(msg)
    _log.warning(msg)
