"""Port of ``legacy-node/tests/phase1-metadata-errors.test.ts`` (23 cases).

Two halves:

1. **Tool schema annotations (9 cases)** over the registered FastMCP tool list
   (built from a fresh ``FastMCP("pipeline")`` to avoid singleton bleed). Field
   mapping (TS ``ToolSchema`` → :class:`mcp.types.Tool`):

   * TS ``s._meta?.max_result_chars`` → ``tool.meta`` (the FastMCP ``meta=``
     param surfaces on ``list_tools()`` as a plain ``dict`` — serialized
     ``_meta`` — or ``None``); read ``tool.meta["max_result_chars"]``.
   * TS ``s.annotations?.readOnlyHint`` / ``destructiveHint`` → ``tool.annotations``
     (a :class:`mcp.types.ToolAnnotations` or ``None``)
     ``.readOnlyHint`` / ``.destructiveHint``.

2. **PipelineError (14 cases)** over
   :class:`~pipeline_orchestrator.errors.PipelineError` /
   :class:`~pipeline_orchestrator.errors.ErrorClass`. Mapping:

   * TS ``err instanceof Error`` → ``isinstance(err, Exception)``;
     ``instanceof PipelineError`` → ``isinstance(err, PipelineError)``.
   * TS ``err.name === 'PipelineError'`` → ``type(err).__name__ == "PipelineError"``.
   * TS ``err.error_class`` → ``err.error_class`` (an :class:`ErrorClass` member,
     constructed with ``ErrorClass.<member>`` — the ctor param is typed, never a
     raw string).
   * TS ``err.toJSON()`` → ``err.to_dict()``. Python has no auto-``toJSON``, so
     the "JSON.stringify uses toJSON automatically" case is faithfully adapted to
     assert ``json.dumps(err.to_dict())`` parses back correctly (kept as its own
     case so the count stays 23).

Each Node ``it(...)`` maps 1:1 to a ``test_*`` method, grouped in classes
mirroring the Node ``describe`` blocks.
"""

from __future__ import annotations

import asyncio
import json

from mcp.server.fastmcp import FastMCP
from mcp.types import Tool

from pipeline_orchestrator.errors import ErrorClass, PipelineError
from pipeline_orchestrator.tools import register_tools


def _tool_schemas() -> list[Tool]:
    """Build the FastMCP ``toolSchemas`` equivalent from a fresh instance."""
    mcp = FastMCP("pipeline")
    register_tools(mcp)
    return asyncio.run(mcp.list_tools())


# ── Tool schema annotation tests (9 cases) ───────────────────────────


class TestToolSchemaAnnotations:
    """Port of the Node ``describe('tool schema annotations', ...)`` block."""

    def test_every_schema_has_meta_max_result_chars_defined(self) -> None:
        missing = [
            s.name
            for s in _tool_schemas()
            if s.meta is None or s.meta.get("max_result_chars") is None
        ]
        assert missing == [], (
            f"Schemas missing _meta.max_result_chars: {', '.join(missing)}"
        )

    def test_meta_max_result_chars_is_50000_or_10000(self) -> None:
        for schema in _tool_schemas():
            chars = (schema.meta or {}).get("max_result_chars")
            assert chars in (50000, 10000), (
                f"{schema.name}: expected max_result_chars to be 50000 or 10000, "
                f"got {chars}"
            )

    def test_read_only_annotated_schemas_use_max_result_chars_50000(self) -> None:
        read_only = [
            s
            for s in _tool_schemas()
            if s.annotations is not None and s.annotations.readOnlyHint is True
        ]
        wrong = [
            f"{s.name}={(s.meta or {}).get('max_result_chars')}"
            for s in read_only
            if (s.meta or {}).get("max_result_chars") != 50000
        ]
        assert wrong == [], (
            f"Read-only schemas with wrong max_result_chars: {', '.join(wrong)}"
        )

    def test_destructive_annotated_schemas_use_max_result_chars_10000(self) -> None:
        destructive = [
            s
            for s in _tool_schemas()
            if s.annotations is not None and s.annotations.destructiveHint is True
        ]
        wrong = [
            f"{s.name}={(s.meta or {}).get('max_result_chars')}"
            for s in destructive
            if (s.meta or {}).get("max_result_chars") != 10000
        ]
        assert wrong == [], (
            f"Destructive schemas with wrong max_result_chars: {', '.join(wrong)}"
        )

    def test_annotated_schemas_have_exactly_one_of_readonly_or_destructive(
        self,
    ) -> None:
        annotated = [s for s in _tool_schemas() if s.annotations is not None]
        for schema in annotated:
            assert schema.annotations is not None
            ro = schema.annotations.readOnlyHint is True
            dest = schema.annotations.destructiveHint is True
            assert (ro and not dest) or (not ro and dest), (
                f"{schema.name}: should have exactly one of readOnlyHint or "
                f"destructiveHint, got readOnly={ro}, destructive={dest}"
            )

    def test_at_least_38_of_43_schemas_have_annotations(self) -> None:
        annotated = [s for s in _tool_schemas() if s.annotations is not None]
        assert len(annotated) >= 38, (
            f"Expected at least 38 annotated schemas, got {len(annotated)}"
        )

    def test_total_schema_count_is_43(self) -> None:
        schemas = _tool_schemas()
        assert len(schemas) == 43, f"Expected 43 tool schemas, got {len(schemas)}"

    def test_all_schema_names_are_unique(self) -> None:
        names = [s.name for s in _tool_schemas()]
        unique = set(names)
        assert len(names) == len(unique), "Duplicate schema names found"

    def test_every_schema_has_non_empty_name_description_and_input_schema(
        self,
    ) -> None:
        for schema in _tool_schemas():
            assert len(schema.name) > 0, "Schema name must not be empty"
            assert schema.description is not None and len(schema.description) > 0, (
                f"{schema.name}: description must not be empty"
            )
            assert schema.inputSchema is not None, (
                f"{schema.name}: inputSchema must not be null"
            )
            assert schema.inputSchema["type"] == "object", (
                f'{schema.name}: inputSchema.type must be "object"'
            )


# ── PipelineError tests (10 cases) ───────────────────────────────────


class TestPipelineErrorClass:
    """Port of the Node ``describe('PipelineError class', ...)`` block."""

    def test_is_an_instance_of_error(self) -> None:
        err = PipelineError("test", ErrorClass.validation_error)
        assert isinstance(err, Exception), "PipelineError should be instanceof Error"
        assert isinstance(err, PipelineError), (
            "PipelineError should be instanceof PipelineError"
        )

    def test_has_name_set_to_pipeline_error(self) -> None:
        err = PipelineError("test", ErrorClass.validation_error)
        assert type(err).__name__ == "PipelineError"

    def test_stores_error_class_correctly(self) -> None:
        err = PipelineError("bad input", ErrorClass.validation_error)
        assert err.error_class == ErrorClass.validation_error

    def test_defaults_recovery_action_to_empty_string(self) -> None:
        err = PipelineError("test", ErrorClass.unknown_error)
        assert err.recovery_action == ""

    def test_defaults_retryable_to_false(self) -> None:
        err = PipelineError("test", ErrorClass.unknown_error)
        assert err.retryable is False

    def test_defaults_details_to_empty_object(self) -> None:
        err = PipelineError("test", ErrorClass.unknown_error)
        assert err.details == {}

    def test_accepts_all_optional_fields(self) -> None:
        err = PipelineError(
            "timeout",
            ErrorClass.file_io_error,
            recovery_action="Retry the operation.",
            retryable=True,
            details={"path": "/tmp/artifact.json", "attempt": 3},
        )
        assert err.recovery_action == "Retry the operation."
        assert err.retryable is True
        assert err.details == {"path": "/tmp/artifact.json", "attempt": 3}

    def test_preserves_cause_when_provided(self) -> None:
        cause = Exception("original")
        err = PipelineError("wrapped", ErrorClass.unknown_error, cause=cause)
        assert err.cause is cause

    def test_does_not_set_cause_when_omitted(self) -> None:
        err = PipelineError("test", ErrorClass.unknown_error)
        assert err.cause is None


# ── PipelineError.to_dict() serialization (4 cases) ──────────────────


class TestPipelineErrorSerialization:
    """Port of the Node ``describe('PipelineError.toJSON() serialization', ...)``."""

    def test_returns_all_required_fields(self) -> None:
        err = PipelineError(
            "Phase not found",
            ErrorClass.state_transition_error,
            recovery_action="Check phase name.",
            retryable=False,
            details={"phase": "discovery"},
        )
        result = err.to_dict()
        assert result["error_class"] == "state_transition_error"
        assert result["message"] == "Phase not found"
        assert result["recovery_action"] == "Check phase name."
        assert result["retryable"] is False
        assert result["details"] == {"phase": "discovery"}

    def test_includes_defaults_when_no_options_provided(self) -> None:
        err = PipelineError("simple", ErrorClass.artifact_not_found)
        result = err.to_dict()
        assert result["error_class"] == "artifact_not_found"
        assert result["message"] == "simple"
        assert result["recovery_action"] == ""
        assert result["retryable"] is False
        assert result["details"] == {}

    def test_produces_valid_json_via_round_trip(self) -> None:
        err = PipelineError(
            "test",
            ErrorClass.gate_blocked,
            recovery_action="Fix quality issues.",
            retryable=True,
            details={"checks_failed": ["min_items", "cross_ref"]},
        )
        serialized = json.dumps(err.to_dict())
        parsed = json.loads(serialized)
        assert parsed["error_class"] == "gate_blocked"
        assert parsed["message"] == "test"
        assert parsed["retryable"] is True
        assert parsed["details"] == {"checks_failed": ["min_items", "cross_ref"]}

    def test_json_dumps_of_to_dict_round_trips(self) -> None:
        # Faithful adaptation of the Node "JSON.stringify(err) uses toJSON
        # automatically" case: Python has no auto-toJSON, so we assert that
        # ``json.dumps(err.to_dict())`` parses back to the same wire object.
        err = PipelineError("auto", ErrorClass.schema_mismatch)
        serialized = json.dumps(err.to_dict())
        parsed = json.loads(serialized)
        assert parsed["error_class"] == "schema_mismatch"
        assert parsed["message"] == "auto"


# ── PipelineError error classes coverage (1 case) ────────────────────


class TestPipelineErrorClassesCoverage:
    """Port of the Node ``describe('PipelineError error classes coverage', ...)``."""

    def test_all_9_error_classes_can_be_instantiated_and_serialized(self) -> None:
        all_error_classes = [
            ErrorClass.validation_error,
            ErrorClass.state_transition_error,
            ErrorClass.file_io_error,
            ErrorClass.schema_mismatch,
            ErrorClass.artifact_not_found,
            ErrorClass.phase_not_ready,
            ErrorClass.configuration_error,
            ErrorClass.gate_blocked,
            ErrorClass.unknown_error,
        ]
        # Pin the member count at 9 exactly (the Node contract uses an explicit
        # 9-element array): a dropped or added ErrorClass member must fail here,
        # not pass silently.
        assert len(all_error_classes) == 9
        assert set(all_error_classes) == set(ErrorClass)
        for cls in all_error_classes:
            err = PipelineError(f"test {cls}", cls)
            assert err.error_class == cls
            result = err.to_dict()
            assert result["error_class"] == cls.value
