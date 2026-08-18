"""
wyeast.core.paths — canonical case-directory layout.

One place that knows where everything lives inside /cases/CASE_ID/. The
naming here is load-bearing: log files, the metadata dir, and per-stage
log names follow the conventions documented in the README, and other tools
(run_pipeline.sh log sweep, gen_case_report.py, chat_case.py) rely on them.
"""

import re
from dataclasses import dataclass
from pathlib import Path


def sanitize_person_name(name: str) -> str:
    """Reduce a real name to a filesystem-safe token: keep [A-Za-z0-9._-],
    collapse every other run to a single underscore, trim edge underscores.
    'Jane Harding' -> 'Jane_Harding'."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").strip())
    return safe.strip("_")


def display_person_folder(person_id: str, cluster_identities=None) -> str:
    """Folder/label name for a person cluster.

    Returns the bare structural id ('Person_03') when the cluster has no
    enrolled identity, or 'Person_03_Jane_Harding' when it does. Keeping the
    Person_NN prefix preserves cluster traceability (chain-of-custody) and the
    downstream person_clusters[person_id] contract; the name is additive.

    cluster_identities is the {person_id: {"name": ...}} map from
    face_clustering.json (may be None/empty for un-enrolled cases).
    """
    if not cluster_identities:
        return person_id
    ident = cluster_identities.get(person_id)
    name = ident.get("name") if isinstance(ident, dict) else None
    safe = sanitize_person_name(name) if name else ""
    return f"{person_id}_{safe}" if safe else person_id


@dataclass(frozen=True)
class CasePaths:
    case_id: str
    case_dir: Path

    @classmethod
    def from_case_id(cls, case_id: str, cases_root="/cases") -> "CasePaths":
        return cls(case_id=case_id, case_dir=Path(cases_root) / case_id)

    @classmethod
    def from_case_dir(cls, case_dir) -> "CasePaths":
        case_dir = Path(case_dir)
        return cls(case_id=case_dir.name, case_dir=case_dir)

    @property
    def original_files_dir(self) -> Path:
        return self.case_dir / "original_files"

    @property
    def extracted_dir(self) -> Path:
        return self.case_dir / "extracted"

    @property
    def output_dir(self) -> Path:
        return self.case_dir / "output"

    @property
    def metadata_dir(self) -> Path:
        return self.case_dir / "output" / "metadata"

    @property
    def suspense_dir(self) -> Path:
        return self.case_dir / "output" / "suspense"

    @property
    def archive_dir(self) -> Path:
        """Canonical single-copy delivery archive (original gallery structure).
        Every de-duped photo/video has exactly one physical home here; the
        other output views (all_photos_by_scene, by_person, by_event) are
        relative symlinks into this tree."""
        return self.case_dir / "output" / "archive"

    @property
    def duplicates_dir(self) -> Path:
        """Duplicate holding, OUTSIDE output/ (not part of client delivery).
        Subfolders exact/, perceptual/, videos/ (created by stage 01)."""
        return self.case_dir / "duplicates"

    @property
    def quarantine_dir(self) -> Path:
        """Sensitivity-match holding, OUTSIDE output/ (not part of client
        delivery). One subfolder per first-matched filter (created by step 11)."""
        return self.case_dir / "quarantine"

    @property
    def logs_dir(self) -> Path:
        return self.case_dir / f"logs_{self.case_id}"

    @property
    def custody_log(self) -> Path:
        return self.logs_dir / "chain_of_custody.log"

    @property
    def case_config_path(self) -> Path:
        return self.case_dir / "case_config.json"

    @property
    def complete_marker(self) -> Path:
        return self.case_dir / "PIPELINE_COMPLETE"

    def stage_log(self, stage: str) -> Path:
        """Per-stage log file, e.g. stage_log('scene_classify')."""
        return self.logs_dir / f"{stage}_{self.case_id}.log"

    def index(self, filename: str) -> Path:
        """A JSON index file under output/metadata/."""
        return self.metadata_dir / filename

    def ensure_dirs(self) -> None:
        """Create the directories every step needs before logging/writing."""
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_dir.mkdir(parents=True, exist_ok=True)
