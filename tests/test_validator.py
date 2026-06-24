"""Ported pytest cases for the JSON-Schema validator (port of ``validator.test.ts``).

The five Node cases are ported 1:1. They assert valid/invalid + ``errors``
emptiness/non-emptiness + a ``/not found/i`` throw on an unknown schema — they do
**not** pin exact error strings, so this suite does not assert on the error text.

The Node test reads ``../pipeline/schemas`` (relative to ``legacy-node/tests``);
the Python schemas live at ``src/pipeline_orchestrator/schemas/``, resolved here
OS-agnostically from ``REPO_ROOT`` (the ``Path(__file__)`` idiom used by
``test_schema_assets.py``), not via hardcoded separators.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline_orchestrator.validator import load_schemas, validate_artifact

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = REPO_ROOT / "src" / "pipeline_orchestrator" / "schemas"


def test_loads_all_twelve_schema_files() -> None:
    """``loadSchemas`` loads all 12 schemas; the 6 named ones are present."""
    schemas = load_schemas(SCHEMAS_DIR)
    assert len(schemas) == 12
    assert schemas["pipeline-run.json"]
    assert schemas["raw-collection.json"]
    assert schemas["design-spec.json"]
    assert schemas["scaffold-document.json"]
    assert schemas["scaffold-manifest.json"]
    assert schemas["validation-report.json"]


def test_validates_a_valid_raw_collection() -> None:
    """A well-formed raw collection validates with zero errors."""
    schemas = load_schemas(SCHEMAS_DIR)
    artifact = {
        "collection_id": "test--12345678",
        "created_date": "2026-04-03T14:30:00Z",
        "status": "raw",
        "source_runs": [],
        "items": [],
    }

    result = validate_artifact(schemas, "raw-collection.json", artifact)
    assert result.valid is True
    assert len(result.errors) == 0


def test_rejects_a_raw_collection_missing_required_fields() -> None:
    """A raw collection missing required fields is invalid with non-empty errors."""
    schemas = load_schemas(SCHEMAS_DIR)
    artifact = {
        "collection_id": "test--12345678",
        # missing: created_date, status, items
    }

    result = validate_artifact(schemas, "raw-collection.json", artifact)
    assert result.valid is False
    assert len(result.errors) > 0


def test_validates_a_valid_debate_transcript() -> None:
    """A well-formed debate transcript validates."""
    schemas = load_schemas(SCHEMAS_DIR)
    artifact = {
        "transcript_id": "dt-12345678",
        "input_type": "design-spec",
        "input_id": "spec-123",
        "created_date": "2026-04-03T16:00:00Z",
        "rounds": [],
        "synthesis": {
            "changes_accepted": [],
            "changes_rejected": [],
        },
    }

    result = validate_artifact(schemas, "debate-transcript.json", artifact)
    assert result.valid is True


def test_throws_on_unknown_schema() -> None:
    """An unknown ``schema_file`` raises with a ``/not found/i`` message."""
    schemas = load_schemas(SCHEMAS_DIR)

    with pytest.raises(ValueError, match="(?i)not found"):
        validate_artifact(schemas, "nonexistent.json", {})
