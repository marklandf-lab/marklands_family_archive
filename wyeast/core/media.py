"""
wyeast.core.media — canonical media-type extension sets.

The single home for the photo / video / PDF extension buckets the pipeline
keys off. These are now sourced from ``wyeast.core.filetypes`` (backed by
``config/file_types.json``, with embedded defaults identical to the values
that used to live here), so "which extensions count as an image / video /
PDF" is decided in exactly one place and is operator-configurable. This module
remains the stable import surface: ``from wyeast.core.media import
IMAGE_EXTENSIONS`` and membership tests (`ext in IMAGE_EXTENSIONS`) are
unchanged.

Stdlib-pure (frozensets only) — imports under every step venv, like the rest
of wyeast.core.

Note: gen_review.py deliberately keeps its OWN, smaller image set. It renders
matches as browser <img> tags and browsers cannot display HEIC/HEIF, so that
set is a presentation concern, not the ingest vocabulary defined here.
"""

from wyeast.core.filetypes import (
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    PDF_EXTENSIONS,
)

__all__ = ["IMAGE_EXTENSIONS", "VIDEO_EXTENSIONS", "PDF_EXTENSIONS"]
