"""Live MCP dispatch tests (S1 / T6.7) — invoke the REGISTERED tools end-to-end.

Every other suite calls the ``handle_*`` handlers DIRECTLY; none drives a tool
through the registered ``@mcp.tool`` body. That gap is exactly what let the
2026-06-24 "PORT COMPLETE" claim be false: the 43 tool bodies enumerated on
``tools/list`` but every body still ``raise``d ``NotImplementedError``, so a real
``tools/call`` failed. These tests permanently close that gap.

They build a fresh in-process :class:`FastMCP`, register the full 43, and call
each tool through ``mcp.call_tool`` (the same path a stdio client hits — FastMCP
passes our ``CallToolResult`` through verbatim because ``@tool_result`` suppresses
the output schema). Assertions cover all three wire formats (pretty JSON /
plain-text / single-line compact ``PipelineError`` JSON) and one end-to-end
state-mutation lifecycle, and assert that NO tool path returns
``NotImplementedError``.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from typing import Any, cast

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

from pipeline_orchestrator import server as srv
from pipeline_orchestrator.tools import register_tools


def _fresh_server() -> FastMCP:
    mcp: FastMCP = FastMCP("pipeline")
    register_tools(mcp)
    return mcp


# One server reused across tests — the tools are stateless; the only mutable
# state lives in the module-level ``srv.state`` singleton, reset by the fixture.
_MCP = _fresh_server()
_TOOLS = {t.name: t for t in asyncio.run(_MCP.list_tools())}
_ALL_NAMES = sorted(_TOOLS)


def _reset_state() -> None:
    s = srv.state
    s.active_run = None
    s.active_run_dir = ""
    s.active_debate = None
    s._project_root = None
    s._config = None
    s._schemas = None
    s._quality_gates = None
    s._hooks_config = None


@pytest.fixture(autouse=True)
def _isolated_state() -> Any:
    _reset_state()
    yield
    _reset_state()


def _text(result: CallToolResult) -> str:
    assert isinstance(result, CallToolResult)
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def _empty_for(prop: dict[str, Any]) -> Any:
    t = prop.get("type")
    if t == "string":
        return ""
    if t == "array":
        return []
    if t == "object":
        return {}
    if t == "boolean":
        return False
    if t in ("number", "integer"):
        return 0
    return ""


def _minimal_args(schema: dict[str, Any]) -> dict[str, Any]:
    """Build the minimal schema-valid args: each required field -> typed empty."""
    required = schema.get("required", [])
    props = schema.get("properties", {})
    return {name: _empty_for(props.get(name, {})) for name in required}


# The two debate tools have enum-constrained required fields; an empty string
# fails FastMCP's pre-handler schema validation, so supply valid values that
# pass the schema and then exercise the handler (no active debate).
_ARG_OVERRIDES: dict[str, dict[str, Any]] = {
    "pipeline_init_debate": {
        "input_type": "knowledge-overview",
        "input_id": "",
        "input_version": "",
        "artifact_content": {},
    },
    "pipeline_submit_argument": {
        "role": "advocate",
        "position": "",
        "confidence": 0,
    },
}


def _args_for(name: str) -> dict[str, Any]:
    if name in _ARG_OVERRIDES:
        return dict(_ARG_OVERRIDES[name])
    return _minimal_args(_TOOLS[name].inputSchema)


def _call(name: str, args: dict[str, Any]) -> CallToolResult:
    # FastMCP.call_tool is typed ``Sequence[ContentBlock] | dict`` but returns
    # our verbatim ``CallToolResult`` (``convert_result`` passes it through when
    # ``output_schema is None``, which ``@tool_result`` guarantees).
    return cast("CallToolResult", asyncio.run(_MCP.call_tool(name, args)))


# ── 1. The anti-regression gate: every registered tool dispatches LIVE ──


def test_all_43_tools_registered() -> None:
    assert len(_ALL_NAMES) == 43


@pytest.mark.parametrize("name", _ALL_NAMES)
def test_every_registered_tool_dispatches_without_not_implemented(
    name: str,
) -> None:
    """A real tools/call reaches the handler — never NotImplementedError.

    This is the permanent guard for T6.7: before wiring, every body raised
    ``NotImplementedError('wired by the global register_tools seam (T6.5)')``.
    """
    result = _call(name, _args_for(name))
    assert isinstance(result, CallToolResult)
    text = _text(result)
    assert "NotImplementedError" not in text, name
    assert "register_tools seam" not in text, name
    # A formed envelope: isError is a real bool the wire carries.
    assert isinstance(result.isError, bool), name


@pytest.mark.parametrize("name", _ALL_NAMES)
def test_every_tool_outputschema_suppressed(name: str) -> None:
    """`@tool_result` must yield ``outputSchema is None`` for every tool (R-H1)."""
    assert _TOOLS[name].outputSchema is None, name


# ── 2. Byte-exact wire format — one representative case per wire kind ──

# Plain-text error envelope (``"Error: <msg>"`` — raised ValueError / require_*).
_PLAIN_ERROR = {
    "pipeline_init_run": "Error: run_id is required",
    "pipeline_next_phases": "Error: No active run. Call pipeline_init_run first.",
    "pipeline_start_phase": "Error: No active run. Call pipeline_init_run first.",
    "pipeline_cc_load_collection": "Error: file_path is required",
    "pipeline_synth_load_collection": "Error: file_path is required",
    "pipeline_save_debate": (
        "Error: No active debate. Call pipeline_init_debate first."
    ),
    "pipeline_submit_argument": (
        "Error: No active debate. Call pipeline_init_debate first."
    ),
    "pipeline_feature_request": "Error: target_directory is required",
    "pipeline_analyze_codebase": "Error: codebase_path is required",
}

# Plain-text success envelope (empty-result handlers).
_PLAIN_SUCCESS = {
    "pipeline_cc_list_overviews": "No saved overviews found.",
    "pipeline_synth_list_specs": "No saved specs found.",
    "pipeline_cc_query": "No items matched the query.",
    "pipeline_synth_query": "No matching items found.",
}

# Single-line compact PipelineError JSON envelope (§4.2 error path).
_PIPELINE_ERROR_JSON = {
    "pipeline_validate_artifact": {
        "error_class": "validation_error",
        "message": "schema is required",
        "recovery_action": (
            "Provide the schema parameter with the name of a JSON Schema file."
        ),
        "retryable": False,
        "details": {},
    },
    "pipeline_list_artifacts": {
        "error_class": "validation_error",
        "message": "storage_key is required",
        "recovery_action": "Provide the storage_key parameter.",
        "retryable": False,
        "details": {},
    },
    "pipeline_load_artifact": {
        "error_class": "validation_error",
        "message": "storage_key and file_name are required",
        "recovery_action": "Provide both storage_key and file_name parameters.",
        "retryable": False,
        "details": {},
    },
}


@pytest.mark.parametrize("name,expected", sorted(_PLAIN_ERROR.items()))
def test_plain_text_error_envelopes_byte_exact(name: str, expected: str) -> None:
    result = _call(name, _args_for(name))
    assert result.isError is True, name
    assert _text(result) == expected, name


@pytest.mark.parametrize("name,expected", sorted(_PLAIN_SUCCESS.items()))
def test_plain_text_success_envelopes_byte_exact(
    name: str, expected: str
) -> None:
    result = _call(name, _args_for(name))
    assert result.isError is False, name
    assert _text(result) == expected, name


@pytest.mark.parametrize("name,expected", sorted(_PIPELINE_ERROR_JSON.items()))
def test_pipeline_error_json_is_single_line_compact_and_exact(
    name: str, expected: dict[str, Any]
) -> None:
    result = _call(name, _args_for(name))
    assert result.isError is True, name
    text = _text(result)
    # Single-line, compact separators (JS ``JSON.stringify`` parity, §4.2).
    assert "\n" not in text, name
    assert ", " not in text, name
    assert '": ' not in text, name
    assert json.loads(text) == expected, name


# ── 3. The two ``*_load_collection`` no-annotation tools (spec callout) ──


def test_load_collection_tools_dispatch_through_envelope() -> None:
    for name in ("pipeline_cc_load_collection", "pipeline_synth_load_collection"):
        _reset_state()
        result = _call(name, {"file_path": ""})
        assert result.isError is True
        assert _text(result) == "Error: file_path is required"
        # These two are the only read tools with NO annotations (spec §5.4/§5.5).
        assert _TOOLS[name].annotations is None, name


# ── 4. Pretty-JSON success envelope: get_config + run_status ──


def test_get_config_pretty_json_wire_format() -> None:
    result = _call("pipeline_get_config", {})
    assert result.isError is False
    text = _text(result)
    assert "\n" in text  # indent=2 pretty JSON
    obj = json.loads(text)
    assert list(obj.keys()) == [
        "project_root",
        "pipeline_dir",
        "pipeline",
        "phases",
        "edges",
        "schemas",
        "storage",
    ]
    assert obj["project_root"] == "(not set — call pipeline_init_run first)"
    assert isinstance(obj["phases"], list) and len(obj["phases"]) > 0


def test_run_status_no_run_envelope() -> None:
    result = _call("pipeline_run_status", {})
    assert result.isError is False
    obj = json.loads(_text(result))
    assert obj["status"] == "no_run"


# ── 5. End-to-end state-mutation lifecycle through tools/call ──


def test_lifecycle_mutation_end_to_end_via_call_tool() -> None:
    """Drive init_run -> next_phases -> start_phase through the REGISTERED tools.

    Proves a real state mutation flows through the dispatch path and mutates the
    module-level ``ServerState`` singleton (not just a handler-local object).
    """
    with tempfile.TemporaryDirectory() as project_root:
        r_init = _call(
            "pipeline_init_run", {"run_id": "e2e", "project_root": project_root}
        )
        assert r_init.isError is False
        init_obj = json.loads(_text(r_init))
        assert init_obj["status"] == "initialized"
        assert srv.state.active_run is not None
        assert srv.state.active_run.run_id == "e2e"

        r_next = _call("pipeline_next_phases", {})
        assert r_next.isError is False
        phases = json.loads(_text(r_next))["available_phases"]
        assert len(phases) > 0
        first = phases[0]["name"]

        r_start = _call("pipeline_start_phase", {"phase": first})
        assert r_start.isError is False
        assert srv.state.active_run.phases[first].status == "in_progress"
        assert srv.state.active_run.status == "running"
