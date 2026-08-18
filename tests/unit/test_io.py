import builtins
import contextlib
import json
import logging
import sys

import pytest

from wyeast.core.io import atomic_write_json, read_index


def test_atomic_write_creates_parents_and_round_trips(tmp_path):
    target = tmp_path / "output" / "metadata" / "scene_index.json"
    data = {"a": [1, 2], "b": {"c": "x"}}
    atomic_write_json(target, data)
    assert json.loads(target.read_text()) == data


def test_atomic_write_replaces_existing(tmp_path):
    target = tmp_path / "idx.json"
    atomic_write_json(target, {"v": 1})
    atomic_write_json(target, {"v": 2})
    assert json.loads(target.read_text()) == {"v": 2}


def test_atomic_write_leaves_no_tmp_file(tmp_path):
    atomic_write_json(tmp_path / "idx.json", [])
    assert [p.name for p in tmp_path.iterdir()] == ["idx.json"]


def test_embedlib_uses_canonical_atomic_write():
    embed = pytest.importorskip("wyeast.embed")  # needs `requests`
    assert embed.atomic_write_json is atomic_write_json


def test_read_index_missing_raises_with_hint(tmp_path):
    with pytest.raises(FileNotFoundError, match="upstream step"):
        read_index(tmp_path / "metadata_index.json")


def test_read_index_default_and_corrupt(tmp_path):
    assert read_index(tmp_path / "absent.json", default=[]) == []
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(json.JSONDecodeError):
        read_index(bad)


# ── Disabled-validator visibility (BACKLOG P0: a guardrail that turns itself off) ──
#
# validate_index() no-ops without jsonschema so wyeast.core stays stdlib-pure and
# importable under every venv. The failure mode is that the no-op is silent, and a
# silent no-op is indistinguishable from a clean pass. These pin the two defences:
# the skip is warned about, and it is programmatically detectable.

@contextlib.contextmanager
def _no_jsonschema(monkeypatch):
    """Make `import jsonschema` raise ImportError, as it does under any venv
    except venv-phase1, and reset the once-per-process warning latch."""
    import wyeast.core.io as core_io

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "jsonschema":
            raise ImportError("simulated: jsonschema absent in this venv")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(core_io, "_jsonschema_warned", False)
    yield core_io


def test_jsonschema_available_reports_false_when_absent(monkeypatch):
    with _no_jsonschema(monkeypatch) as core_io:
        assert core_io.jsonschema_available() is False


def test_jsonschema_available_reports_true_when_present():
    pytest.importorskip("jsonschema")
    from wyeast.core.io import jsonschema_available
    assert jsonschema_available() is True


def test_validate_index_warns_that_validation_did_not_run(monkeypatch, caplog):
    """The load-bearing property: skipping validation is never silent."""
    with _no_jsonschema(monkeypatch) as core_io:
        with caplog.at_level(logging.WARNING, logger=core_io.__name__):
            core_io.validate_index({"anything": 1}, "metadata_index", strict=True)

    assert len(caplog.records) == 1, "expected exactly one warning"
    msg = caplog.records[0].getMessage()
    assert "jsonschema" in msg
    assert "DISABLED" in msg, "warning must name that validation did not run"
    assert sys.executable in msg, "warning must name the offending interpreter"


def test_validate_index_does_not_raise_when_disabled(monkeypatch):
    """Fail-safe is deliberate: a missing jsonschema must not halt the air-gapped
    path, even under strict. Loudness is the remedy, not an exception."""
    with _no_jsonschema(monkeypatch) as core_io:
        core_io.validate_index({"bad": True}, "metadata_index", strict=True)


def test_disabled_warning_is_logged_once_per_process(monkeypatch, caplog):
    """One warning per process, not one per index file — loud, not spam."""
    with _no_jsonschema(monkeypatch) as core_io:
        with caplog.at_level(logging.WARNING, logger=core_io.__name__):
            for _ in range(5):
                core_io.validate_index({}, "metadata_index")

    assert len(caplog.records) == 1
