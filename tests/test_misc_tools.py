"""Behavioral parity tests for the 5 misc tool handlers in
:mod:`pipeline_orchestrator.tools.misc_tools` (port of the non-KB handlers in
``legacy-node/misc-handlers.ts``).

Mirrors the house style of ``tests/test_artifact_handlers.py``: each handler is
exercised through its real M4 module wrapper (``ingest`` / ``codebase_analyzer``
/ ``web_search`` / ``cross_ref``); the DI context dataclasses (``IngestContext``
/ ``ValidateRunContext`` / ``WebSearchContext``) are built over a ``tmp_path``
sandbox; HTTP is never touched (``execute_web_search`` is monkeypatched). The
``register_misc_tools`` integrity test builds a fresh ``FastMCP`` and pins each
tool's ``_meta.max_result_chars`` + ``ToolAnnotations`` subset (§5.7 / §4.3).

The smol-toml byte-exact serializer (``_serialize_feature_requests_toml``) is
pinned against golden strings captured from ``smol-toml@1.6.1`` ``stringify`` and
confirmed to round-trip through stdlib ``tomllib``. The ``feature_request``
handler's id/timestamp are made deterministic by monkeypatching ``secrets`` /
``datetime`` on the module so the written file + returned text can be asserted
verbatim; every guard-error string is asserted character-for-character (spec
§4.2 convention 2 — errors are RETURNED, not raised).
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import pipeline_orchestrator.tools.misc_tools as misc
from pipeline_orchestrator.run_state import init_run
from pipeline_orchestrator.storage import StorageConfig
from pipeline_orchestrator.tools.misc_tools import (
    IngestContext,
    ValidateRunContext,
    WebSearchContext,
    _serialize_feature_requests_toml,
    _toml_escape_basic_string,
    handle_analyze_codebase,
    handle_feature_request,
    handle_ingest_documents,
    handle_validate_run,
    handle_web_search,
    register_misc_tools,
)
from pipeline_orchestrator.validator import ValidationResult, load_schemas

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = REPO_ROOT / "src" / "pipeline_orchestrator" / "schemas"


# ── Golden smol-toml fixtures (captured from smol-toml@1.6.1) ──────────
#
# Each is the EXACT output of ``stringify({feature_request: [<entry...>]})``.
# Captured byte-for-byte; the serializer must reproduce them.

_GOLDEN_SINGLE = (
    "[[feature_request]]\n"
    'id = "fr-20260624-a1b2c3"\n'
    'title = "Add caching layer"\n'
    'description = "Cache resolved artifacts to cut re-resolution cost."\n'
    'priority = "high"\n'
    'scope = "src/pipeline_orchestrator/storage.py"\n'
    'rationale = "Reduces latency on repeated loads — measured 40% win."\n'
    'status = "proposed"\n'
    'timestamp = "2026-06-24T12:00:00.000Z"\n'
    'source = "agent"\n'
)

_GOLDEN_TWO = (
    "[[feature_request]]\n"
    'id = "fr-20260624-a1b2c3"\n'
    'title = "Add caching layer"\n'
    'description = "Cache resolved artifacts to cut re-resolution cost."\n'
    'priority = "high"\n'
    'scope = "src/pipeline_orchestrator/storage.py"\n'
    'rationale = "Reduces latency on repeated loads — measured 40% win."\n'
    'status = "proposed"\n'
    'timestamp = "2026-06-24T12:00:00.000Z"\n'
    'source = "agent"\n'
    "\n"
    "[[feature_request]]\n"
    'id = "fr-20260624-d4e5f6"\n'
    'title = "Add metrics"\n'
    'description = "Emit timing metrics for each phase."\n'
    'priority = "medium"\n'
    'scope = "src/pipeline_orchestrator/run_state.py"\n'
    'rationale = "Observability for the débate loop ☑."\n'
    'status = "proposed"\n'
    'timestamp = "2026-06-24T12:05:00.000Z"\n'
    'source = "human"\n'
)

# Special chars: embedded quote, backslash, tab, newline, non-ASCII, emoji.
_GOLDEN_SPECIAL = (
    "[[feature_request]]\n"
    'id = "fr-20260624-spec01"\n'
    'title = "Handle \\"quoted\\" \\\\ paths"\n'
    'description = "Tab\\there and newline\\nthere."\n'
    'priority = "low"\n'
    'scope = "café/módulo/файл"\n'
    "rationale = \"single ' quotes and emoji 🚀 fine\"\n"
    'status = "proposed"\n'
    'timestamp = "2026-06-24T12:10:00.000Z"\n'
    'source = "agent"\n'
)

_SINGLE_ENTRY: dict[str, Any] = {
    "id": "fr-20260624-a1b2c3",
    "title": "Add caching layer",
    "description": "Cache resolved artifacts to cut re-resolution cost.",
    "priority": "high",
    "scope": "src/pipeline_orchestrator/storage.py",
    "rationale": "Reduces latency on repeated loads — measured 40% win.",
    "status": "proposed",
    "timestamp": "2026-06-24T12:00:00.000Z",
    "source": "agent",
}

_SECOND_ENTRY: dict[str, Any] = {
    "id": "fr-20260624-d4e5f6",
    "title": "Add metrics",
    "description": "Emit timing metrics for each phase.",
    "priority": "medium",
    "scope": "src/pipeline_orchestrator/run_state.py",
    "rationale": "Observability for the débate loop ☑.",
    "status": "proposed",
    "timestamp": "2026-06-24T12:05:00.000Z",
    "source": "human",
}

_SPECIAL_ENTRY: dict[str, Any] = {
    "id": "fr-20260624-spec01",
    "title": 'Handle "quoted" \\ paths',
    "description": "Tab\there and newline\nthere.",
    "priority": "low",
    "scope": "café/módulo/файл",
    "rationale": "single ' quotes and emoji 🚀 fine",
    "status": "proposed",
    "timestamp": "2026-06-24T12:10:00.000Z",
    "source": "agent",
}


# ── _serialize_feature_requests_toml — smol-toml byte-exact parity ─────


class TestSerializeFeatureRequestsTOML:
    def test_single_entry_matches_golden(self) -> None:
        assert _serialize_feature_requests_toml([_SINGLE_ENTRY]) == _GOLDEN_SINGLE

    def test_two_entries_matches_golden(self) -> None:
        assert (
            _serialize_feature_requests_toml([_SINGLE_ENTRY, _SECOND_ENTRY])
            == _GOLDEN_TWO
        )

    def test_special_chars_matches_golden(self) -> None:
        assert _serialize_feature_requests_toml([_SPECIAL_ENTRY]) == _GOLDEN_SPECIAL

    def test_output_round_trips_through_tomllib(self) -> None:
        toml = _serialize_feature_requests_toml([_SINGLE_ENTRY, _SECOND_ENTRY])
        parsed = tomllib.loads(toml)
        assert isinstance(parsed["feature_request"], list)
        assert len(parsed["feature_request"]) == 2
        assert parsed["feature_request"][0] == _SINGLE_ENTRY
        assert parsed["feature_request"][1] == _SECOND_ENTRY

    def test_special_output_round_trips(self) -> None:
        toml = _serialize_feature_requests_toml([_SPECIAL_ENTRY])
        assert tomllib.loads(toml)["feature_request"][0] == _SPECIAL_ENTRY

    def test_field_order_follows_insertion_order(self) -> None:
        # smol-toml emits keys in object insertion order — reordering the dict
        # reorders the emitted lines.
        reordered = {
            "title": "X",
            "id": "fr-r",
            "priority": "high",
            "description": "d",
            "scope": "s",
            "rationale": "r",
            "status": "proposed",
            "timestamp": "t",
            "source": "agent",
        }
        out = _serialize_feature_requests_toml([reordered])
        lines = out.split("\n")
        assert lines[0] == "[[feature_request]]"
        assert lines[1] == 'title = "X"'
        assert lines[2] == 'id = "fr-r"'

    def test_escape_table_matches_smol_toml(self) -> None:
        # Short escapes for the TOML basic-string control set.
        assert _toml_escape_basic_string("\b") == "\\b"
        assert _toml_escape_basic_string("\t") == "\\t"
        assert _toml_escape_basic_string("\n") == "\\n"
        assert _toml_escape_basic_string("\f") == "\\f"
        assert _toml_escape_basic_string("\r") == "\\r"
        assert _toml_escape_basic_string('"') == '\\"'
        assert _toml_escape_basic_string("\\") == "\\\\"
        # Other C0 controls + 0x7f → lowercase \uXXXX.
        assert _toml_escape_basic_string("\x00") == "\\u0000"
        assert _toml_escape_basic_string("\x07") == "\\u0007"
        assert _toml_escape_basic_string("\x1f") == "\\u001f"
        assert _toml_escape_basic_string("\x7f") == "\\u007f"
        # >= 0x80 raw (incl. C1 controls and astral chars).
        assert _toml_escape_basic_string("\x80") == "\x80"
        assert _toml_escape_basic_string("é") == "é"
        assert _toml_escape_basic_string("🚀") == "🚀"


# ── handle_analyze_codebase ────────────────────────────────────────────


class TestHandleAnalyzeCodebase:
    def test_raises_when_codebase_path_missing(self) -> None:
        with pytest.raises(ValueError, match=r"codebase_path is required"):
            handle_analyze_codebase({})

    def test_raises_when_codebase_path_empty_string(self) -> None:
        with pytest.raises(ValueError, match=r"codebase_path is required"):
            handle_analyze_codebase({"codebase_path": ""})

    def test_analyzes_a_node_package_dir(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "package.json").write_text(
            json.dumps({"name": "demo", "version": "1.2.3"}), encoding="utf-8"
        )
        (pkg / "index.ts").write_text("export const x = 1\n", encoding="utf-8")
        result = handle_analyze_codebase({"codebase_path": str(pkg)})
        assert not result.is_error
        parsed = json.loads(result.json)
        # The analyzer returns the codebase-requirements artifact: assert the
        # expected top-level shape, not just that it parsed as a dict.
        assert isinstance(parsed, dict)
        assert "requirements_id" in parsed
        assert parsed["codebase_path"] == str(pkg)
        assert parsed["package"]["name"] == "demo"

    def test_output_is_two_space_indented_json(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg2"
        pkg.mkdir()
        (pkg / "package.json").write_text(
            json.dumps({"name": "demo2"}), encoding="utf-8"
        )
        result = handle_analyze_codebase({"codebase_path": str(pkg)})
        # Pretty 2-space JSON: a nested key appears indented by 2 spaces.
        assert result.json.startswith("{\n  ")


# ── handle_feature_request ────────────────────────────────────────────


def _patch_deterministic(
    monkeypatch: pytest.MonkeyPatch, *, fixed: datetime, hex6: str
) -> None:
    """Pin the random hex + the wall clock on the misc module.

    ``fixed`` drives BOTH the id date-part (``strftime("%Y%m%d")``) and the
    ``_now_iso()`` timestamp (``strftime(...) + microsecond``), so we hand back a
    real :class:`datetime` rather than a stub. ``secrets.token_hex`` is pinned to
    ``hex6`` for a deterministic id suffix.
    """
    monkeypatch.setattr(secrets, "token_hex", lambda _n: hex6)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, _tz: Any = None) -> datetime:  # type: ignore[override]
            return fixed

    monkeypatch.setattr(misc, "datetime", _FixedDateTime)


class TestHandleFeatureRequest:
    def test_creates_new_file_and_returns_success_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_deterministic(
            monkeypatch,
            fixed=datetime(2026, 6, 24, 12, 0, 0, 0, tzinfo=UTC),
            hex6="a1b2c3",
        )
        result = handle_feature_request(
            {
                "target_directory": str(tmp_path),
                "title": "Add caching layer",
                "description": "Cache resolved artifacts to cut re-resolution cost.",
                "priority": "high",
                "scope": "src/pipeline_orchestrator/storage.py",
                "rationale": "Reduces latency on repeated loads — measured 40% win.",
                "source": "agent",
            }
        )
        assert not result.is_error
        file_path = os.path.join(tmp_path, "feature_requests.toml")
        assert result.json == "\n".join(
            [
                "Feature request recorded.",
                "  ID: fr-20260624-a1b2c3",
                "  Title: Add caching layer",
                "  Priority: high",
                f"  File: {file_path}",
            ]
        )
        with open(file_path, encoding="utf-8") as fh:
            written = fh.read()
        assert written == _GOLDEN_SINGLE

    def test_defaults_source_to_agent_when_omitted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_deterministic(
            monkeypatch,
            fixed=datetime(2026, 6, 24, 12, 0, 0, 0, tzinfo=UTC),
            hex6="a1b2c3",
        )
        handle_feature_request(
            {
                "target_directory": str(tmp_path),
                "title": "T",
                "description": "d",
                "priority": "low",
                "scope": "s",
                "rationale": "r",
            }
        )
        parsed = tomllib.loads(
            (tmp_path / "feature_requests.toml").read_text(encoding="utf-8")
        )
        assert parsed["feature_request"][0]["source"] == "agent"

    def test_appends_to_existing_file_preserving_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Seed an existing file with one entry (the golden single).
        file_path = tmp_path / "feature_requests.toml"
        file_path.write_text(_GOLDEN_SINGLE, encoding="utf-8")
        _patch_deterministic(
            monkeypatch,
            fixed=datetime(2026, 6, 24, 12, 5, 0, 0, tzinfo=UTC),
            hex6="d4e5f6",
        )
        result = handle_feature_request(
            {
                "target_directory": str(tmp_path),
                "title": "Add metrics",
                "description": "Emit timing metrics for each phase.",
                "priority": "medium",
                "scope": "src/pipeline_orchestrator/run_state.py",
                "rationale": "Observability for the débate loop ☑.",
                "source": "human",
            }
        )
        assert not result.is_error
        assert file_path.read_text(encoding="utf-8") == _GOLDEN_TWO

    def test_error_target_directory_required_when_missing(
        self, tmp_path: Path
    ) -> None:
        result = handle_feature_request({})
        assert result.is_error
        assert result.json == "Error: target_directory is required"

    def test_error_target_directory_required_when_empty(self) -> None:
        result = handle_feature_request({"target_directory": ""})
        assert result.is_error
        assert result.json == "Error: target_directory is required"

    def test_error_target_directory_not_a_directory(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope"
        result = handle_feature_request(
            {
                "target_directory": str(missing),
                "title": "t",
                "description": "d",
                "priority": "low",
                "scope": "s",
                "rationale": "r",
            }
        )
        assert result.is_error
        expected_path = os.path.abspath(str(missing))
        assert result.json == (
            "Error: target_directory does not exist or is not a directory: "
            f"{expected_path}"
        )

    def test_error_target_directory_is_a_file(self, tmp_path: Path) -> None:
        f = tmp_path / "afile.txt"
        f.write_text("x", encoding="utf-8")
        result = handle_feature_request(
            {
                "target_directory": str(f),
                "title": "t",
                "description": "d",
                "priority": "low",
                "scope": "s",
                "rationale": "r",
            }
        )
        assert result.is_error
        assert result.json == (
            "Error: target_directory does not exist or is not a directory: "
            f"{os.path.abspath(str(f))}"
        )

    def test_error_required_fields_missing(self, tmp_path: Path) -> None:
        # Missing description/priority/scope/rationale.
        result = handle_feature_request(
            {"target_directory": str(tmp_path), "title": "only title"}
        )
        assert result.is_error
        assert result.json == (
            "Error: title, description, priority, scope, and rationale "
            "are all required"
        )

    def test_error_empty_title_is_falsy_required(self, tmp_path: Path) -> None:
        # An empty-string title is falsy (TS ``!title``) → required error.
        result = handle_feature_request(
            {
                "target_directory": str(tmp_path),
                "title": "",
                "description": "d",
                "priority": "low",
                "scope": "s",
                "rationale": "r",
            }
        )
        assert result.is_error
        assert result.json == (
            "Error: title, description, priority, scope, and rationale "
            "are all required"
        )

    def test_error_invalid_priority(self, tmp_path: Path) -> None:
        result = handle_feature_request(
            {
                "target_directory": str(tmp_path),
                "title": "t",
                "description": "d",
                "priority": "urgent",
                "scope": "s",
                "rationale": "r",
            }
        )
        assert result.is_error
        assert result.json == (
            'Error: priority must be "low", "medium", or "high" — got "urgent"'
        )
        # No file written on the guard-error path.
        assert not (tmp_path / "feature_requests.toml").exists()

    def test_error_malformed_existing_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        file_path = tmp_path / "feature_requests.toml"
        # Invalid TOML (unterminated table header).
        file_path.write_text("[[feature_request\nnope", encoding="utf-8")
        _patch_deterministic(
            monkeypatch,
            fixed=datetime(2026, 6, 24, 12, 0, 0, 0, tzinfo=UTC),
            hex6="abcdef",
        )
        result = handle_feature_request(
            {
                "target_directory": str(tmp_path),
                "title": "t",
                "description": "d",
                "priority": "low",
                "scope": "s",
                "rationale": "r",
            }
        )
        assert result.is_error
        dir_path = os.path.abspath(str(tmp_path))
        prefix = (
            f"Error: existing feature_requests.toml in {dir_path} is malformed. "
            "Fix or remove it before adding new entries.\n  "
        )
        assert result.json.startswith(prefix)
        # The trailing line carries the parser's own message after the 2-space indent.
        assert result.json[len(prefix):]  # non-empty parser detail
        # The malformed file is left untouched (not overwritten).
        assert file_path.read_text(encoding="utf-8") == "[[feature_request\nnope"

    def test_id_format_fr_date_hex(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_deterministic(
            monkeypatch,
            fixed=datetime(2099, 12, 31, 0, 0, 0, 0, tzinfo=UTC),
            hex6="0f1e2d",
        )
        result = handle_feature_request(
            {
                "target_directory": str(tmp_path),
                "title": "t",
                "description": "d",
                "priority": "high",
                "scope": "s",
                "rationale": "r",
            }
        )
        assert "  ID: fr-20991231-0f1e2d" in result.json


# ── handle_validate_run ────────────────────────────────────────────────


def _make_validate_ctx(
    tmp_path: Path, run_id: str = "vr-1"
) -> ValidateRunContext:
    temp_dir = str(tmp_path)
    run_dir = os.path.join(temp_dir, "runs", "vr-run")
    state = init_run(run_id, "1.0.0", ["validation"], run_dir)
    sc = StorageConfig(base_dir=temp_dir, paths={"specs": "specs"})
    return ValidateRunContext(
        require_run=lambda: state,
        get_storage_config=lambda *_a, **_k: sc,
        base_dir_from_run_dir=lambda _d: temp_dir,
        get_active_run_dir=lambda: run_dir,
    )


class TestHandleValidateRun:
    def test_empty_run_is_valid_and_not_error(self, tmp_path: Path) -> None:
        ctx = _make_validate_ctx(tmp_path, run_id="vr-empty")
        result = handle_validate_run({}, ctx)
        assert not result.is_error
        parsed = json.loads(result.json)
        assert parsed["run_id"] == "vr-empty"
        assert parsed["valid"] is True
        assert parsed["cross_ref_issues"] == []
        assert parsed["semantic_issues"] == []
        assert parsed["completeness_issues"] == []
        assert parsed["artifacts_checked"] == 0

    def test_key_order_run_id_then_report_fields(self, tmp_path: Path) -> None:
        ctx = _make_validate_ctx(tmp_path)
        result = handle_validate_run({}, ctx)
        parsed = json.loads(result.json)
        assert list(parsed.keys()) == [
            "run_id",
            "valid",
            "cross_ref_issues",
            "semantic_issues",
            "completeness_issues",
            "summary",
            "artifacts_checked",
        ]

    def test_default_phase_outputs_used_when_no_overrides(
        self, tmp_path: Path
    ) -> None:
        # No completed phases → no completeness issues regardless of defaults.
        ctx = _make_validate_ctx(tmp_path)
        result = handle_validate_run({}, ctx)
        parsed = json.loads(result.json)
        assert parsed["completeness_issues"] == []

    def test_overrides_merge_on_top_of_defaults(self, tmp_path: Path) -> None:
        # An override for an unknown phase merges in without removing defaults.
        ctx = _make_validate_ctx(tmp_path)
        result = handle_validate_run(
            {"phase_output_overrides": {"my_phase": ["my-type"]}}, ctx
        )
        # Still valid (no completed phases) — exercises the merge path.
        assert not result.is_error
        assert json.loads(result.json)["valid"] is True

    def test_output_is_two_space_indented_json(self, tmp_path: Path) -> None:
        ctx = _make_validate_ctx(tmp_path)
        result = handle_validate_run({}, ctx)
        assert result.json.startswith('{\n  "run_id":')

    def test_invalid_report_sets_is_error_and_body_valid_false(
        self, tmp_path: Path
    ) -> None:
        # Drive the GENUINE cross_ref code path to valid=False: a completed
        # phase whose expected output type was never registered yields a real
        # CompletenessIssue. No handler seam is stubbed — the handler calls the
        # real cross_ref.validate_run — so this pins the contract
        # ``is_error = not report.valid`` in the failure direction (a hardcoded
        # ``is_error=False`` would fail here).
        temp_dir = str(tmp_path)
        run_dir = os.path.join(temp_dir, "runs", "vr-bad")
        state = init_run("vr-bad", "1.0.0", ["my_phase"], run_dir)
        # Mark the phase completed (state construction, not a handler-seam stub).
        state.phases["my_phase"].status = "completed"
        sc = StorageConfig(base_dir=temp_dir, paths={"specs": "specs"})
        ctx = ValidateRunContext(
            require_run=lambda: state,
            get_storage_config=lambda *_a, **_k: sc,
            base_dir_from_run_dir=lambda _d: temp_dir,
            get_active_run_dir=lambda: run_dir,
        )
        # Expect an artifact type the completed phase never produced.
        result = handle_validate_run(
            {"phase_output_overrides": {"my_phase": ["never-produced-type"]}}, ctx
        )

        assert result.is_error is True
        parsed = json.loads(result.json)
        assert parsed["valid"] is False
        # The genuine completeness issue must be present.
        assert len(parsed["completeness_issues"]) >= 1
        issue = parsed["completeness_issues"][0]
        assert issue["phase"] == "my_phase"
        assert issue["missing_type"] == "never-produced-type"

    def test_invalid_report_emits_non_ascii_issue_message_raw(
        self, tmp_path: Path
    ) -> None:
        # Raw-wire ensure_ascii=False guard: a non-ASCII phase name flows through
        # the genuine cross_ref CompletenessIssue.message into result.json. The
        # raw string (not json.loads) must contain the char verbatim and NOT its
        # ``\\uXXXX`` escape — a regression to ensure_ascii=True would be caught
        # here (json.loads is blind to that distinction).
        phase = "café-é"  # accented non-ASCII codepoint reaches the output raw.
        temp_dir = str(tmp_path)
        run_dir = os.path.join(temp_dir, "runs", "vr-uni")
        state = init_run("vr-uni", "1.0.0", [phase], run_dir)
        state.phases[phase].status = "completed"
        sc = StorageConfig(base_dir=temp_dir, paths={"specs": "specs"})
        ctx = ValidateRunContext(
            require_run=lambda: state,
            get_storage_config=lambda *_a, **_k: sc,
            base_dir_from_run_dir=lambda _d: temp_dir,
            get_active_run_dir=lambda: run_dir,
        )
        result = handle_validate_run(
            {"phase_output_overrides": {phase: ["missing"]}}, ctx
        )

        assert result.is_error is True
        # Inspect the RAW wire string, not the parsed body.
        assert "café-é" in result.json
        # The escaped form must NOT appear (would indicate ensure_ascii=True).
        assert "\\u00e9" not in result.json
        assert "\\u00E9" not in result.json
        # Sanity: parsing still recovers the same char (round-trip intact).
        assert json.loads(result.json)["valid"] is False


# ── handle_ingest_documents (async) ────────────────────────────────────


def _make_ingest_ctx(
    tmp_path: Path,
    *,
    valid: bool = True,
    errors: list[str] | None = None,
    with_active_run: bool = False,
) -> IngestContext:
    temp_dir = str(tmp_path)
    run_dir = os.path.join(temp_dir, "runs", "ing-run")
    state = init_run("ing-1", "1.0.0", ["document_ingestion"], run_dir)
    return IngestContext(
        validate_artifact=lambda _s, _f, _a: ValidationResult(
            valid=valid, errors=errors or []
        ),
        get_schemas=lambda: {},
        base_dir_from_run_dir=lambda _d: temp_dir,
        get_project_root=lambda: temp_dir,
        get_config_storage_base_dir=lambda: "artifacts",
        get_active_run=lambda: (state if with_active_run else None),
        get_active_run_dir=lambda: run_dir,
    )


class TestHandleIngestDocuments:
    def test_raises_when_file_paths_missing(self, tmp_path: Path) -> None:
        ctx = _make_ingest_ctx(tmp_path)
        with pytest.raises(
            ValueError,
            match=r"file_paths must be a non-empty array of file path strings",
        ):
            asyncio.run(handle_ingest_documents({}, ctx))

    def test_raises_when_file_paths_empty_list(self, tmp_path: Path) -> None:
        ctx = _make_ingest_ctx(tmp_path)
        with pytest.raises(
            ValueError,
            match=r"file_paths must be a non-empty array of file path strings",
        ):
            asyncio.run(handle_ingest_documents({"file_paths": []}, ctx))

    def test_ingests_a_text_file_and_returns_envelope(
        self, tmp_path: Path
    ) -> None:
        doc = tmp_path / "note.txt"
        doc.write_text("hello world\nsecond line", encoding="utf-8")
        ctx = _make_ingest_ctx(tmp_path)
        result = asyncio.run(
            handle_ingest_documents({"file_paths": [str(doc)]}, ctx)
        )
        assert not result.is_error
        parsed = json.loads(result.json)
        assert parsed["status"] == "ok"
        data = parsed["data"]
        assert data["item_count"] == 1
        assert "collection_id" in data
        assert isinstance(data["sources"], list)
        # The collection was persisted to disk and the path round-trips.
        assert os.path.exists(data["artifact_path"])
        assert data["artifact_path"] in parsed["next_step"]
        assert parsed["next_step"].startswith(
            "Call pipeline_register_artifact with file_path="
        )

    def test_directory_entry_is_expanded(self, tmp_path: Path) -> None:
        src = tmp_path / "docs"
        src.mkdir()
        (src / "a.md").write_text("# A\n", encoding="utf-8")
        (src / "b.txt").write_text("b\n", encoding="utf-8")
        ctx = _make_ingest_ctx(tmp_path)
        result = asyncio.run(
            handle_ingest_documents({"file_paths": [str(src)]}, ctx)
        )
        parsed = json.loads(result.json)
        assert parsed["data"]["item_count"] == 2

    def test_validation_failure_raises_with_joined_errors(
        self, tmp_path: Path
    ) -> None:
        doc = tmp_path / "n.txt"
        doc.write_text("x", encoding="utf-8")
        ctx = _make_ingest_ctx(
            tmp_path, valid=False, errors=["err one", "err two"]
        )
        with pytest.raises(ValueError) as exc:
            asyncio.run(handle_ingest_documents({"file_paths": [str(doc)]}, ctx))
        assert str(exc.value) == (
            "Produced collection failed schema validation:\nerr one\nerr two"
        )

    def test_validate_false_skips_validation(self, tmp_path: Path) -> None:
        doc = tmp_path / "n.txt"
        doc.write_text("x", encoding="utf-8")
        # validate_artifact would fail, but validate=False skips it entirely.
        ctx = _make_ingest_ctx(tmp_path, valid=False, errors=["would fail"])
        result = asyncio.run(
            handle_ingest_documents(
                {"file_paths": [str(doc)], "validate": False}, ctx
            )
        )
        assert not result.is_error
        assert json.loads(result.json)["status"] == "ok"


# ── handle_web_search (async) ──────────────────────────────────────────


class TestHandleWebSearch:
    def test_raises_when_collection_path_missing(self) -> None:
        with pytest.raises(ValueError, match=r"collection_path is required"):
            asyncio.run(handle_web_search({}, WebSearchContext()))

    def test_raises_when_neither_queries_nor_fetch_urls(self) -> None:
        with pytest.raises(
            ValueError,
            match=r"Either queries or fetch_urls \(or both\) must be provided\.",
        ):
            asyncio.run(
                handle_web_search(
                    {"collection_path": "/tmp/c.json"}, WebSearchContext()
                )
            )

    def test_search_only_mode_returns_envelope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_exec(params: dict[str, Any]) -> dict[str, Any]:
            assert params["queries"] == ["alpha"]
            return {
                "data": {"search_results": [{"url": "u", "snippet": "s"}],
                         "result_count": 1},
                "next_step": "Review the search results.",
            }

        monkeypatch.setattr(
            "pipeline_orchestrator.web_search.execute_web_search", fake_exec
        )
        result = asyncio.run(
            handle_web_search(
                {"collection_path": "/tmp/c.json", "queries": ["alpha"]},
                WebSearchContext(),
            )
        )
        assert not result.is_error
        parsed = json.loads(result.json)
        assert parsed["status"] == "ok"
        assert parsed["data"]["result_count"] == 1
        assert parsed["next_step"] == "Review the search results."

    def test_fetch_mode_validates_collection_file_exists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_exec(_params: dict[str, Any]) -> dict[str, Any]:
            raise AssertionError("should not reach execute_web_search")

        monkeypatch.setattr(
            "pipeline_orchestrator.web_search.execute_web_search", fake_exec
        )
        with pytest.raises(ValueError, match=r"Collection file not found:"):
            asyncio.run(
                handle_web_search(
                    {
                        "collection_path": "/no/such/collection.json",
                        "fetch_urls": ["https://example.com"],
                    },
                    WebSearchContext(),
                )
            )

    def test_fetch_mode_rejects_non_json_collection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json", encoding="utf-8")

        async def fake_exec(_params: dict[str, Any]) -> dict[str, Any]:
            raise AssertionError("should not reach execute_web_search")

        monkeypatch.setattr(
            "pipeline_orchestrator.web_search.execute_web_search", fake_exec
        )
        with pytest.raises(ValueError, match=r"Collection file is not valid JSON:"):
            asyncio.run(
                handle_web_search(
                    {
                        "collection_path": str(bad),
                        "fetch_urls": ["https://example.com"],
                    },
                    WebSearchContext(),
                )
            )

    def test_fetch_mode_passes_through_execute_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        coll = tmp_path / "c.json"
        coll.write_text(json.dumps({"items": []}), encoding="utf-8")
        captured: dict[str, Any] = {}

        async def fake_exec(params: dict[str, Any]) -> dict[str, Any]:
            captured.update(params)
            return {
                "data": {"items_added": 2, "collection_path": str(coll)},
                "next_step": "Call pipeline_register_artifact.",
            }

        monkeypatch.setattr(
            "pipeline_orchestrator.web_search.execute_web_search", fake_exec
        )
        result = asyncio.run(
            handle_web_search(
                {
                    "collection_path": str(coll),
                    "fetch_urls": ["https://example.com/page"],
                    "max_results_per_query": 3,
                },
                WebSearchContext(),
            )
        )
        assert not result.is_error
        parsed = json.loads(result.json)
        assert parsed["status"] == "ok"
        assert parsed["data"]["items_added"] == 2
        # The handler forwarded the optional + required params verbatim.
        assert captured["collection_path"] == str(coll)
        assert captured["fetch_urls"] == ["https://example.com/page"]
        assert captured["max_results_per_query"] == 3
        assert "queries" not in captured  # omitted when not supplied


# ── register_misc_tools — registration integrity (§5.7 / §4.3) ─────────


def test_register_misc_tools_meta_and_annotations() -> None:
    """Exactly 5 ``pipeline_*`` misc tools with correct ``_meta`` + annotations."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("pipeline")
    register_misc_tools(mcp)

    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}

    expected_names = {
        "pipeline_ingest_documents",
        "pipeline_analyze_codebase",
        "pipeline_validate_run",
        "pipeline_feature_request",
        "pipeline_web_search",
    }
    assert set(by_name) == expected_names
    assert len(tools) == 5

    # max_result_chars: 50000 for validate_run (read), 10000 for the rest (§5.7).
    expected_max = {
        "pipeline_ingest_documents": 10000,
        "pipeline_analyze_codebase": 10000,
        "pipeline_validate_run": 50000,
        "pipeline_feature_request": 10000,
        "pipeline_web_search": 10000,
    }
    for name, expected in expected_max.items():
        assert by_name[name].meta is not None, f"{name} must carry _meta"
        assert by_name[name].meta == {"max_result_chars": expected}, name

    # validate_run is the only read-only tool; the other four are destructive.
    ann = by_name["pipeline_validate_run"].annotations
    assert ann is not None
    assert ann.readOnlyHint is True
    assert ann.destructiveHint is None

    for name in (
        "pipeline_ingest_documents",
        "pipeline_analyze_codebase",
        "pipeline_feature_request",
        "pipeline_web_search",
    ):
        ann = by_name[name].annotations
        assert ann is not None, f"{name} must carry annotations"
        assert ann.destructiveHint is True, name
        assert ann.readOnlyHint is None, name


def test_register_misc_tools_wired_into_global_seam() -> None:
    """The global ``register_tools`` seam now enumerates the full 43 tools (T6.5).

    ``register_misc_tools`` (5 tools) is one of the seven registrars fanned out by
    ``tools/__init__.py``; its five ``pipeline_*`` tools are present in the wired
    handshake.
    """
    from mcp.server.fastmcp import FastMCP

    from pipeline_orchestrator.tools import register_tools

    fresh = FastMCP("pipeline")
    register_tools(fresh)
    names = {t.name for t in asyncio.run(fresh.list_tools())}
    assert len(names) == 43
    for name in (
        "pipeline_ingest_documents",
        "pipeline_analyze_codebase",
        "pipeline_validate_run",
        "pipeline_feature_request",
        "pipeline_web_search",
    ):
        assert name in names


# ── Schema-availability sanity (the schemas the real ingest path needs) ─


def test_schemas_dir_loads() -> None:
    """The schema dir exists + loads (sanity for the real validate path)."""
    schemas = load_schemas(SCHEMAS_DIR)
    assert "raw-collection.json" in schemas
