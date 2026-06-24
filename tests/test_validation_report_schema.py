"""Ported pytest cases for the validation-report schema + gate binding.

Port of ``legacy-node/tests/validation-report-schema.test.ts`` (5 cases),
ported 1:1. The cases assert that the existing ``validation-report.json`` schema
accepts a well-formed report, rejects one missing required fields, accepts the
optional fields (``spec_errors_found`` / ``validated_spec_path``) when present,
and that the ``quality-gates.toml`` ``validation`` gate references
``artifact_type="validation-report"`` with a ``field_present`` check on
``results``.

Path resolution mirrors ``test_validator.py``: the Node test reads
``../pipeline/schemas`` and ``../pipeline/quality-gates.toml`` relative to
``legacy-node/tests``; the Python assets live under
``src/pipeline_orchestrator/`` and are resolved OS-agnostically from
``REPO_ROOT`` (the ``Path(__file__)`` idiom), not via hardcoded separators.
"""

from __future__ import annotations

import os
from pathlib import Path

from pipeline_orchestrator.storage import get_artifact_dir, get_run_data_dir
from pipeline_orchestrator.toml_loader import load_quality_gates
from pipeline_orchestrator.validator import load_schemas, validate_artifact

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = REPO_ROOT / "src" / "pipeline_orchestrator" / "schemas"
QUALITY_GATES_TOML = (
    REPO_ROOT / "src" / "pipeline_orchestrator" / "pipeline" / "quality-gates.toml"
)


def test_loads_validation_report_schema() -> None:
    """``load_schemas`` exposes ``validation-report.json`` from the schemas dir."""
    schemas = load_schemas(SCHEMAS_DIR)
    assert "validation-report.json" in schemas


def test_validates_a_well_formed_validation_report() -> None:
    """A well-formed validation report validates with no errors."""
    schemas = load_schemas(SCHEMAS_DIR)
    artifact = {
        "report_id": "validation-report-spec-test",
        "spec_id": "spec-test",
        "phase": "validation",
        "validated_date": "2026-04-10",
        "results": [
            {
                "check_id": "VAL-1",
                "claim": "Sample claim",
                "status": "PASS",
                "evidence": "Sample evidence",
            },
        ],
        "overall_status": "PASS",
    }
    result = validate_artifact(schemas, "validation-report.json", artifact)
    assert result.valid is True, f"Unexpected errors: {', '.join(result.errors)}"


def test_rejects_a_validation_report_missing_required_fields() -> None:
    """A validation report missing required fields is invalid with errors."""
    schemas = load_schemas(SCHEMAS_DIR)
    artifact = {
        "report_id": "validation-report-bad",
        # missing: spec_id, phase, validated_date, results, overall_status
    }
    result = validate_artifact(schemas, "validation-report.json", artifact)
    assert result.valid is False
    assert len(result.errors) > 0


def test_validates_optional_fields_when_present() -> None:
    """The optional ``spec_errors_found`` / ``validated_spec_path`` fields pass."""
    schemas = load_schemas(SCHEMAS_DIR)
    artifact = {
        "report_id": "validation-report-spec-test-2",
        "spec_id": "spec-test-2",
        "phase": "validation",
        "validated_date": "2026-04-10T12:34:56Z",
        "results": [],
        "overall_status": "PASS",
        "spec_errors_found": [],
        "validated_spec_path": os.path.join(
            get_artifact_dir(
                get_run_data_dir(
                    "pipeline_mcp_data", "example-run", "2026-04-10T12:00:00Z"
                ),
                "specs",
            ),
            "test-spec.json",
        ),
    }
    result = validate_artifact(schemas, "validation-report.json", artifact)
    assert result.valid is True, f"Unexpected errors: {', '.join(result.errors)}"


def test_quality_gates_validation_gate_references_validation_report() -> None:
    """The ``validation`` gate's ``field_present`` check binds to the report."""
    gates = load_quality_gates(QUALITY_GATES_TOML)
    validation_gate = next((g for g in gates if g.phase == "validation"), None)
    assert validation_gate is not None, "expected a validation gate entry"
    field_present_check = next(
        (c for c in validation_gate.checks if c.check_type == "field_present"),
        None,
    )
    assert (
        field_present_check is not None
    ), "expected a field_present check in the validation gate"
    assert field_present_check.params["artifact_type"] == "validation-report"
    assert field_present_check.params["field"] == "results"
