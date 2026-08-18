"""
Unit tests for wyeast.core.errors — the class-C primitive of the M5 exception
taxonomy (required index/map present-but-corrupt must fail fast, never swallow).
"""

import json

import pytest

from wyeast.core.errors import RequiredDataError, load_required_index


def test_loads_valid_json(tmp_path):
    p = tmp_path / "ocr_index.json"
    p.write_text(json.dumps({"a": 1}))
    assert load_required_index(p) == {"a": 1}


def test_corrupt_present_file_always_raises(tmp_path):
    p = tmp_path / "ocr_index.json"
    p.write_text("{ not valid json ")
    with pytest.raises(RequiredDataError):
        load_required_index(p)                      # default missing_ok=False
    with pytest.raises(RequiredDataError):
        load_required_index(p, missing_ok=True)     # corrupt raises even so


def test_missing_raises_by_default(tmp_path):
    with pytest.raises(RequiredDataError):
        load_required_index(tmp_path / "absent.json")


def test_missing_ok_returns_none(tmp_path):
    assert load_required_index(tmp_path / "absent.json", missing_ok=True) is None


def test_required_data_error_is_runtimeerror():
    # Must stay a RuntimeError so existing broad `except Exception` handlers
    # (e.g. the s11 export-gate brander catching load_archive_map) still catch it.
    assert issubclass(RequiredDataError, RuntimeError)


def test_corrupt_chains_underlying_cause(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("nope")
    try:
        load_required_index(p)
    except RequiredDataError as e:
        assert isinstance(e.__cause__, (json.JSONDecodeError, ValueError))
    else:
        pytest.fail("expected RequiredDataError")
