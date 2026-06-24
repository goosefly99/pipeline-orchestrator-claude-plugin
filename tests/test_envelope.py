"""Unit tests for the tool result envelope (spec §4.2).

NEW additive tests (no Node oracle test file exists for the envelope wrapper —
the contract lives in the dispatch ``try/catch`` of ``server.ts`` lines ~573–584
plus the ``text()`` helper). These tests exercise the ``@tool_result`` decorator
directly (no tools are registered in T6.1) across all three contract paths:

1. **Return path** — ``HandlerResponse.json`` is carried VERBATIM (never
   auto-JSON-wrapped); ``is_error`` flows through (both ``False`` and ``True``).
2. **Raised ``PipelineError``** — rendered as single-line ``json.dumps(...,
   separators=(",", ":"), ensure_ascii=False)`` of ``to_dict()``, ``isError=True``.
3. **Raised other ``Exception``** — rendered as ``"Error: " + str(err)``,
   ``isError=True``.

Plus the ``ResponseEnvelope`` helper (``{status, data, next_step?, warnings?}``
serialized ``json.dumps(env, indent=2)``) with undefined-drop parity for the
optional keys.
"""

from __future__ import annotations

import asyncio
import json

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

from pipeline_orchestrator.errors import ErrorClass, PipelineError
from pipeline_orchestrator.models import HandlerResponse
from pipeline_orchestrator.tools._envelope import (
    ResponseEnvelope,
    response_envelope,
    tool_result,
)


def _text_of(result: CallToolResult) -> str:
    """Extract the single text block's text from a CallToolResult."""
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, TextContent)
    assert block.type == "text"
    return block.text


# ── Return path: verbatim json + is_error passthrough ────────────────


def test_return_path_carries_json_verbatim_no_wrap() -> None:
    """A returned HandlerResponse.json is mapped to text content VERBATIM.

    The decorator must NOT auto-JSON-wrap a plain text block — a human-readable
    line-join handler payload must survive byte-identically.
    """
    plain = "Validation passed.\n  - 3 sources present\n  - items >= 1"

    @tool_result
    def handler() -> HandlerResponse:
        return HandlerResponse(json=plain, is_error=False)

    result = handler()
    assert isinstance(result, CallToolResult)
    assert _text_of(result) == plain
    assert result.isError is False


def test_return_path_passes_json_string_unparsed() -> None:
    """A JSON string payload is carried as-is (still a string, not re-encoded)."""
    payload = json.dumps({"status": "ok", "data": {"n": 1}}, indent=2)

    @tool_result
    def handler() -> HandlerResponse:
        return HandlerResponse(json=payload, is_error=False)

    assert _text_of(handler()) == payload


def test_return_path_is_error_true_flows_through() -> None:
    """A returned HandlerResponse(is_error=True) sets isError without rewriting text.

    This is the second error-signalling convention (return, not raise):
    validation-failure text blocks, kb_search no-index, etc.
    """
    msg = "No index built for collection. Run pipeline_kb_build_index first."

    @tool_result
    def handler() -> HandlerResponse:
        return HandlerResponse(json=msg, is_error=True)

    result = handler()
    assert _text_of(result) == msg
    assert result.isError is True


# ── Raised PipelineError: single-line, byte-exact JSON.stringify parity ─


def test_raised_pipeline_error_renders_single_line_compact() -> None:
    """A raised PipelineError → compact single-line json of to_dict(), isError=True.

    Byte-exact to the Node ``text(JSON.stringify(err.toJSON()), true)`` branch:
    NO spaces after ``,`` / ``:`` (separators=(",", ":")).
    """

    @tool_result
    def handler() -> HandlerResponse:
        raise PipelineError(
            "bad field",
            ErrorClass.validation_error,
            recovery_action="fix the field",
            retryable=False,
            details={"field": "sources"},
        )

    result = handler()
    assert result.isError is True
    text = _text_of(result)
    # No spaces (compact separators).
    assert ", " not in text
    assert '": ' not in text
    # Round-trips to the exact to_dict() shape.
    parsed = json.loads(text)
    assert parsed == {
        "error_class": "validation_error",
        "message": "bad field",
        "recovery_action": "fix the field",
        "retryable": False,
        "details": {"field": "sources"},
    }
    # And is byte-identical to the manual compact dumps (the JSON.stringify analogue).
    expected = json.dumps(
        {
            "error_class": "validation_error",
            "message": "bad field",
            "recovery_action": "fix the field",
            "retryable": False,
            "details": {"field": "sources"},
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
    assert text == expected


def test_raised_pipeline_error_preserves_non_ascii_raw() -> None:
    """ensure_ascii=False — non-ASCII is left raw exactly as JS JSON.stringify does."""

    @tool_result
    def handler() -> HandlerResponse:
        raise PipelineError("naïve café — €", ErrorClass.unknown_error)

    text = _text_of(handler())
    # The raw non-ASCII chars survive (NOT \uXXXX-escaped).
    assert "naïve café — €" in text
    assert "\\u" not in text


# ── Raised generic Exception: "Error: <msg>" ─────────────────────────


def test_raised_generic_exception_renders_error_prefix() -> None:
    """A raised non-PipelineError → text = 'Error: ' + str(err), isError=True."""

    @tool_result
    def handler() -> HandlerResponse:
        raise ValueError("something broke")

    result = handler()
    assert result.isError is True
    assert _text_of(result) == "Error: something broke"


def test_raised_runtime_error_uses_str_message() -> None:
    """str(err) is the Error.message / String(err) analogue."""

    @tool_result
    def handler() -> HandlerResponse:
        raise RuntimeError("No active run. Call pipeline_init_run first.")

    assert (
        _text_of(handler())
        == "Error: No active run. Call pipeline_init_run first."
    )


# ── async handler support ────────────────────────────────────────────


def test_async_handler_return_path() -> None:
    """The decorator supports async tool bodies (FastMCP awaits coroutine tools)."""

    @tool_result
    async def handler() -> HandlerResponse:
        return HandlerResponse(json="async ok", is_error=False)

    result = asyncio.run(handler())
    assert isinstance(result, CallToolResult)
    assert _text_of(result) == "async ok"
    assert result.isError is False


def test_async_handler_raised_pipeline_error() -> None:
    """A raised PipelineError in an async body is caught + rendered identically."""

    @tool_result
    async def handler() -> HandlerResponse:
        raise PipelineError("async bad", ErrorClass.gate_blocked)

    result = asyncio.run(handler())
    assert result.isError is True
    parsed = json.loads(_text_of(result))
    assert parsed["error_class"] == "gate_blocked"
    assert parsed["message"] == "async bad"


def test_async_handler_raised_generic_exception() -> None:
    """A raised generic exception in an async body → 'Error: <msg>'."""

    @tool_result
    async def handler() -> HandlerResponse:
        raise KeyError("missing")

    result = asyncio.run(handler())
    assert result.isError is True
    # str(KeyError('missing')) == "'missing'" (Python quotes the key).
    assert _text_of(result) == "Error: 'missing'"


# ── ResponseEnvelope helper ──────────────────────────────────────────


def test_response_envelope_minimal_omits_optional_keys() -> None:
    """{status, data} only — next_step / warnings omitted when absent (drop parity)."""
    out = response_envelope("ok", {"run_id": "r1"})
    parsed = json.loads(out)
    assert parsed == {"status": "ok", "data": {"run_id": "r1"}}
    assert "next_step" not in parsed
    assert "warnings" not in parsed


def test_response_envelope_includes_optional_keys_when_present() -> None:
    """next_step / warnings appear only when truthy."""
    out = response_envelope(
        "ok",
        {"phase": "curation"},
        next_step="call pipeline_complete_phase",
        warnings=["gate degraded"],
    )
    parsed = json.loads(out)
    assert parsed["next_step"] == "call pipeline_complete_phase"
    assert parsed["warnings"] == ["gate degraded"]


def test_response_envelope_is_two_space_pretty() -> None:
    """Serialized with json.dumps(env, indent=2) — 2-space pretty (§4.2)."""
    out = response_envelope("error", {"code": "x"})
    # indent=2 produces a newline + 2-space indent for the first nested key.
    assert out.startswith('{\n  "status": "error"')


def test_response_envelope_dataclass_drops_empty_warnings() -> None:
    """An empty warnings list is treated as absent (drop parity)."""
    env = ResponseEnvelope(status="ok", data={"a": 1}, warnings=[])
    assert env.to_dict() == {"status": "ok", "data": {"a": 1}}


# ── LIVE FastMCP round-trip (R-H1 regression pin) ────────────────────
#
# These register a ``@tool_result``-wrapped handler on a REAL ``FastMCP`` via a
# BARE ``@mcp.tool()`` (NO ``structured_output`` kwarg) and drive it through
# ``mcp.call_tool`` / ``mcp.list_tools``. They prove the decorator self-protects
# against FastMCP's return-type → ``outputSchema`` inference (the R-H1 landmine):
# without ``_suppress_output_schema`` an inner ``-> str`` / ``-> HandlerResponse``
# annotation makes FastMCP build an output model, then ``convert_result``
# validates the (always-``None``) ``structuredContent`` of our ``CallToolResult``
# at call time → ``ValidationError`` → ``ToolError`` → the tool call FAILS.
#
# Async convention: this suite has no pytest-anyio/pytest-asyncio plugin, so
# async paths are driven via ``asyncio.run(...)`` (same as ``test_kb_tools.py``).
#
# mcp 1.28.0 API notes pinned here:
#   * ``mcp.list_tools()`` → ``list[mcp.types.Tool]``; the schema attr is
#     ``.outputSchema`` (camelCase). The fix guarantees it is ``None``.
#   * ``mcp.call_tool(name, args)`` returns the ``CallToolResult`` VERBATIM
#     (not a tuple, not a bare content list) when ``output_schema is None`` and
#     the tool body returned a ``CallToolResult`` — ``convert_result``
#     short-circuits on ``CallToolResult``. So we assert against
#     ``result.content`` / ``result.isError`` directly.


def _output_schema_of(mcp: FastMCP, name: str) -> object:
    """Return the registered tool's ``outputSchema`` via the live tool list."""
    tools = asyncio.run(mcp.list_tools())
    tool = next(t for t in tools if t.name == name)
    return tool.outputSchema


def _call(mcp: FastMCP, name: str) -> CallToolResult:
    """Drive ``mcp.call_tool`` and assert it returns a verbatim ``CallToolResult``."""
    result = asyncio.run(mcp.call_tool(name, {}))
    assert isinstance(result, CallToolResult), (
        f"call_tool must return CallToolResult verbatim, got {type(result)!r}"
    )
    return result


def _live_text_of(result: CallToolResult) -> str:
    """Extract the single text block from a live ``call_tool`` ``CallToolResult``."""
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, TextContent)
    assert block.type == "text"
    return block.text


def test_live_bare_registration_no_output_schema_str_inner() -> None:
    """Inner ``-> str`` (worst-case), bare @mcp.tool() → ``outputSchema is None``.

    The decorator self-protects without any ``structured_output`` kwarg.
    """
    mcp = FastMCP("test")

    @mcp.tool()
    @tool_result  # type: ignore[arg-type]  # worst-case: inner declared ``-> str``
    def str_annotated() -> str:
        return HandlerResponse(json="ok", is_error=False)  # type: ignore[return-value]

    assert _output_schema_of(mcp, "str_annotated") is None


def test_live_bare_registration_no_output_schema_handlerresponse_inner() -> None:
    """Inner ``-> HandlerResponse`` (spec style) also → ``outputSchema is None``."""
    mcp = FastMCP("test")

    @mcp.tool()
    @tool_result
    def hr_annotated() -> HandlerResponse:
        return HandlerResponse(json="ok", is_error=False)

    assert _output_schema_of(mcp, "hr_annotated") is None


def test_live_roundtrip_success_handler_response() -> None:
    """(a) success HandlerResponse → verbatim text, isError False, schema None."""
    mcp = FastMCP("test")
    plain = "Validation passed.\n  - 3 sources present"

    @mcp.tool()
    @tool_result
    def ok() -> HandlerResponse:
        return HandlerResponse(json=plain, is_error=False)

    assert _output_schema_of(mcp, "ok") is None
    result = _call(mcp, "ok")
    assert _live_text_of(result) == plain  # verbatim, NOT auto-JSON-wrapped
    assert result.isError is False


def test_live_roundtrip_returned_error_passthrough() -> None:
    """(b) returned HandlerResponse(is_error=True) → text verbatim, isError True."""
    mcp = FastMCP("test")
    msg = "No index built for collection. Run pipeline_kb_build_index first."

    @mcp.tool()
    @tool_result
    def returns_error() -> HandlerResponse:
        return HandlerResponse(json=msg, is_error=True)

    assert _output_schema_of(mcp, "returns_error") is None
    result = _call(mcp, "returns_error")
    assert _live_text_of(result) == msg
    assert result.isError is True


def test_live_roundtrip_raised_pipeline_error() -> None:
    """(c) raised PipelineError → compact single-line JSON, no spaces, isError True."""
    mcp = FastMCP("test")

    @mcp.tool()
    @tool_result
    def raises_pe() -> HandlerResponse:
        raise PipelineError(
            "bad field",
            ErrorClass.validation_error,
            recovery_action="fix the field",
            retryable=False,
            details={"field": "sources"},
        )

    assert _output_schema_of(mcp, "raises_pe") is None
    result = _call(mcp, "raises_pe")
    assert result.isError is True
    text = _live_text_of(result)
    assert ", " not in text  # compact separators (no spaces)
    assert '": ' not in text
    expected = json.dumps(
        {
            "error_class": "validation_error",
            "message": "bad field",
            "recovery_action": "fix the field",
            "retryable": False,
            "details": {"field": "sources"},
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
    assert text == expected


def test_live_roundtrip_raised_generic_exception() -> None:
    """(d) raised generic Exception → 'Error: <msg>', isError True."""
    mcp = FastMCP("test")

    @mcp.tool()
    @tool_result
    def raises_generic() -> HandlerResponse:
        raise ValueError("something broke")

    assert _output_schema_of(mcp, "raises_generic") is None
    result = _call(mcp, "raises_generic")
    assert result.isError is True
    assert _live_text_of(result) == "Error: something broke"


def test_live_roundtrip_async_success_handler_response() -> None:
    """Async (a): success HandlerResponse → verbatim text + isError False."""
    mcp = FastMCP("test")

    @mcp.tool()
    @tool_result
    async def async_ok() -> HandlerResponse:
        return HandlerResponse(json="async ok", is_error=False)

    assert _output_schema_of(mcp, "async_ok") is None
    result = _call(mcp, "async_ok")
    assert _live_text_of(result) == "async ok"
    assert result.isError is False


def test_live_roundtrip_async_returned_error_passthrough() -> None:
    """Async (b): returned HandlerResponse(is_error=True) passthrough."""
    mcp = FastMCP("test")

    @mcp.tool()
    @tool_result
    async def async_returns_error() -> HandlerResponse:
        return HandlerResponse(json="async no-index", is_error=True)

    result = _call(mcp, "async_returns_error")
    assert _live_text_of(result) == "async no-index"
    assert result.isError is True


def test_live_roundtrip_async_raised_pipeline_error() -> None:
    """Async (c): raised PipelineError → compact single-line JSON, isError True."""
    mcp = FastMCP("test")

    @mcp.tool()
    @tool_result
    async def async_raises_pe() -> HandlerResponse:
        raise PipelineError("async bad", ErrorClass.gate_blocked)

    assert _output_schema_of(mcp, "async_raises_pe") is None
    result = _call(mcp, "async_raises_pe")
    assert result.isError is True
    text = _live_text_of(result)
    assert ", " not in text
    parsed = json.loads(text)
    assert parsed["error_class"] == "gate_blocked"
    assert parsed["message"] == "async bad"


def test_live_roundtrip_async_raised_generic_exception() -> None:
    """Async (d): raised generic Exception → 'Error: <msg>', isError True."""
    mcp = FastMCP("test")

    @mcp.tool()
    @tool_result
    async def async_raises_generic() -> HandlerResponse:
        raise KeyError("missing")

    result = _call(mcp, "async_raises_generic")
    assert result.isError is True
    # str(KeyError('missing')) == "'missing'" (Python quotes the key).
    assert _live_text_of(result) == "Error: 'missing'"
