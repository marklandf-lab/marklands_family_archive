"""
wyeast.core.config — layered configuration loading with legacy fallbacks.

Two layers (see README "Configuration"):
  pipeline_config.json  system-wide, lives in the config directory (config_dir())
  case_config.json      per-case, lives in /cases/CASE_ID/

Config sections are named after the stage (`scene_classify`, `ocr`, ...).
Older examiner-supplied configs may still use the pre-13-step section names
(`phase1`, `phase2b`, `exifenrich`, ...); those fallbacks are owned HERE, as
data, so they exist in exactly one place. Preserve them — older configs
must keep working.
"""

import json
import logging
import os
from pathlib import Path

DEFAULT_SCRIPTS_DIR = Path("/opt/estate-pipeline/app")
DEFAULT_CASES_ROOT = Path("/cases")

_log = logging.getLogger("wyeast.config")


def config_dir() -> Path:
    """
    Return the directory holding the system-wide config files
    (pipeline_config.json, case_config.json template).

    By default this is the repo's `config/` directory, computed from this
    module's location (config.py lives at wyeast/core/config.py, so
    parents[2] is the repo root). Overridable via the
    ESTATE_PIPELINE_CONFIG_DIR environment variable so the location is no
    longer tied to the scripts root / --scripts-dir.
    """
    override = os.environ.get("ESTATE_PIPELINE_CONFIG_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "config"

# Stage section name -> legacy section names accepted as fallbacks, in
# precedence order. The current name always wins when both are present.
LEGACY_SECTION_NAMES = {
    "collect_dedup":     ("phase1",),
    "video_frames":      ("videoframes",),
    "exif_enrich":       ("exifenrich",),
    "geo_cluster":       ("precluster",),
    "face_cluster":      ("phase2a",),
    "scene_classify":    ("phase2b",),
    "video_deliver":     ("phase2c",),
    "ocr":               ("phase3",),
    "transcribe":        (),
    "audio_events":      (),
    "embed":             (),
    "email_triage":      (),
    "message_triage":    (),
    "estate_materiality": (),
    "sensitive_scan":    ("sensitive_detection",),
    "delivery_quarantine": (),
    "vital_doc_confirm": (),
    "llm_synthesis":     ("phase4",),
    "email_threads":     (),   # new stage — no pre-rename section name exists
    "reconciliation":    (),
}

# Prompt template key -> legacy keys accepted as fallbacks.
LEGACY_PROMPT_KEYS = {
    "llava_adjudicate":      ("phase2b_llava_adjudicate",),
    "clip_caption_template": ("phase2b_clip_template",),
    "email_classify":        ("phase4_email",),
    "document_classify":     ("phase4_document",),
    "cluster_narrative":     ("phase4_cluster",),
    "sensitive_visual":      (),
    "sensitive_text":        (),
}

# Sensitivity-filter sub-key (inside sensitive_scan.sensitivity_filters) ->
# legacy filter names accepted as fallbacks, in precedence order. Mirrors
# LEGACY_SECTION_NAMES; the current name always wins when both are present.
LEGACY_FILTER_NAMES = {
    "explicit_sexual_imagery": ("explicit_sexual_content",),
}


def load_json(path) -> dict:
    """Strict JSON load. Raises on missing file or parse error."""
    with open(path) as f:
        return json.load(f)


def load_pipeline_config(scripts_dir=None) -> dict:
    """
    Load pipeline_config.json.

    When `scripts_dir` is given the file is read from there (the legacy
    locator path, still used by callers that pass --scripts-dir); otherwise
    the file is located via config_dir() — the system-wide config directory
    — which is no longer tied to the scripts root.

    Mirrors the step scripts' historical behaviour: a missing file yields {}
    (steps fall back to built-in defaults); an unreadable file logs a
    warning and yields {} rather than halting the pipeline.
    """
    base = Path(scripts_dir) if scripts_dir else config_dir()
    path = base / "pipeline_config.json"
    try:
        return load_json(path)
    except FileNotFoundError:
        return {}
    except Exception as e:
        _log.warning("Could not read %s: %s", path, e)
        return {}


def load_case_config(case_dir) -> dict:
    """
    Load case_config.json for a case. Raises FileNotFoundError when absent:
    the watcher / run_pipeline.sh always provisions it, so a missing file
    means the case directory is malformed and the step must halt.
    """
    return load_json(Path(case_dir) / "case_config.json")


def stage_section(cfg: dict, stage_name: str) -> dict:
    """
    Return the config section for a stage, honouring legacy section names.

    The current name wins; otherwise the first legacy name present is used
    (with a deprecation WARNING so the clock is visible in step logs).
    Missing entirely -> {}.
    """
    if stage_name in cfg:
        return cfg[stage_name]
    for legacy in LEGACY_SECTION_NAMES.get(stage_name, ()):
        if legacy in cfg:
            _log.warning(
                "Config uses deprecated legacy section name %r for stage %r — "
                "still honoured, but support will be removed in a future "
                "release; rename the section to %r.", legacy, stage_name, stage_name)
            return cfg[legacy]
    return {}


def merged_stage_section(pipeline_cfg: dict, case_cfg: dict, stage_name: str) -> dict:
    """
    Return a stage's config section with per-case overrides applied.

    Starts from the pipeline_config section and overlays the case_config
    section (legacy section names honoured on both sides via stage_section()).
    Case keys win; nested dicts merge recursively, so a case can tune a single
    sub-key (e.g. one filter's threshold) without redefining the whole block.
    A missing case section leaves the pipeline section unchanged.
    """
    def _merge(base: dict, override: dict) -> dict:
        out = dict(base)
        for k, v in override.items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k] = _merge(out[k], v)
            else:
                out[k] = v
        return out
    return _merge(stage_section(pipeline_cfg, stage_name),
                  stage_section(case_cfg or {}, stage_name))


def get_prompt(pipeline_cfg: dict, key: str, default: str = None):
    """
    Return an LLM prompt template from pipeline_config's `prompts` section,
    honouring legacy key names. Falls back to `default` (the step's built-in
    template) when neither the current nor a legacy key is present.
    """
    prompts = pipeline_cfg.get("prompts", {})
    if key in prompts:
        return prompts[key]
    for legacy in LEGACY_PROMPT_KEYS.get(key, ()):
        if prompts.get(legacy):
            _log.warning(
                "Config uses deprecated legacy prompt key %r for %r — still "
                "honoured, but support will be removed in a future release; "
                "rename it to %r.", legacy, key, key)
            return prompts[legacy]
    return default


def filter_section(sensitivity_filters: dict, name: str) -> dict:
    """
    Return a sensitivity-filter sub-block, honouring legacy filter names.

    Current name wins; otherwise the first legacy name present is used (with a
    deprecation WARNING so the clock is visible in step logs). Missing
    entirely -> {}. Mirrors stage_section()/get_prompt().
    """
    if name in sensitivity_filters:
        return sensitivity_filters[name]
    for legacy in LEGACY_FILTER_NAMES.get(name, ()):
        if legacy in sensitivity_filters:
            _log.warning(
                "case_config uses deprecated sensitivity-filter name %r — still "
                "honoured, but support will be removed in a future release; "
                "rename it to %r.", legacy, name)
            return sensitivity_filters[legacy]
    return {}


def cases_root(pipeline_cfg: dict) -> Path:
    """The cases root directory from pipeline config `paths`, default /cases."""
    return Path(pipeline_cfg.get("paths", {}).get("cases", str(DEFAULT_CASES_ROOT)))
