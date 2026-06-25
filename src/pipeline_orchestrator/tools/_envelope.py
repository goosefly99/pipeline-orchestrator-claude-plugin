"""Tool result envelope (spec §4.2) — the single most error-prone wire contract.

This module is the Python analogue of the Node ``text(content, isError)`` helper
plus the dispatch ``try/catch`` in ``server.ts`` (lines ~573–584). It exposes:

- :func:`tool_result` — a decorator wrapping a handler that returns a
  :class:`~pipeline_orchestrator.models.HandlerResponse` (``json: str`` +
  ``is_error: bool``). The decorator maps that to an MCP ``CallToolResult`` whose
  single text block carries ``HandlerResponse.json`` **verbatim** (never
  auto-JSON-wrapped) and whose ``isError`` carries ``HandlerResponse.is_error``.
  Raised errors are caught and rendered byte-exactly to the Node contract.
- :func:`response_envelope` / :class:`ResponseEnvelope` — the
  ``{status, data, next_step?, warnings?}`` helper serialized with
  ``json.dumps(env, indent=2)`` used by mutation/workflow tools.

Why ``CallToolResult``: FastMCP passes a returned ``CallToolResult`` through
verbatim (``func_metadata.convert_result`` short-circuits on it, and the lowlevel
server's ``call_tool`` handler returns ``ServerResult(results)`` unchanged when
the result is a ``CallToolResult``). This is the only FastMCP return shape that
lets a tool surface BOTH arbitrary verbatim text AND ``isError`` without the
framework's ad-hoc ``_convert_to_content`` re-serialization touching the text.

Byte-exactness (§4.2):
- A raised :class:`PipelineError` → text =
  ``json.dumps(err.to_dict(), separators=(",", ":"), ensure_ascii=False)``
  (single-line, no spaces — matching JS ``JSON.stringify``), ``is_error=True``.
- Any other raised ``Exception`` → text = ``"Error: " + str(err)``,
  ``is_error=True``.

Output-schema landmine (R-H1 — binding, do not reintroduce): FastMCP infers
an ``outputSchema`` for a tool from its callable's RETURN annotation. Because
``functools.wraps`` copies ``__wrapped__`` onto the wrapper, FastMCP's
``inspect.signature(func, eval_str=True)`` follows it to the INNER handler's
annotation (``-> str`` / ``-> HandlerResponse``) and builds an output model.
At ``call_tool`` time ``convert_result`` then validates the ``CallToolResult``'s
``structuredContent`` (always ``None`` for a verbatim text result) against that
model → ``ValidationError`` → ``ToolError`` → the tool call FAILS. ``tool_result``
DEFEATS this unconditionally via :func:`_suppress_output_schema`, so a BARE
``@mcp.tool()`` registration of a wrapped handler ALWAYS yields
``outputSchema is None`` (verified live in ``test_envelope.py``). Wirers
(T6.2–T6.5) therefore need NOT pass ``structured_output=False`` — and MUST NOT
rely on that as the protection. Do not remove the suppression.

MCP stdio safety (§4.5/§15): nothing in this module writes to stdout.
"""

from __future__ import annotations

import functools
import inspect
import json
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, ParamSpec, cast, overload

from mcp.types import CallToolResult, TextContent

from pipeline_orchestrator.errors import PipelineError
from pipeline_orchestrator.models import HandlerResponse

# Single-line JSON separators (no spaces) — JS ``JSON.stringify`` emits no
# spaces, but Python ``json.dumps`` defaults to ``", "`` / ``": "``. Pinning
# these is the byte-exactness fix for the PipelineError error path (§4.2 trap #1).
_COMPACT_SEPARATORS = (",", ":")


def _text_result(text: str, is_error: bool) -> CallToolResult:
    """Build the MCP ``CallToolResult`` carrying ``text`` verbatim + ``is_error``.

    Mirrors the Node ``text(content, isError)`` helper:
    ``{ content: [{ type: 'text', text: content }], isError }``. The text block
    is passed through unmodified — the envelope NEVER auto-JSON-wraps it.
    """
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        isError=is_error,
    )


def _render_handler_response(response: HandlerResponse) -> CallToolResult:
    """Map a returned :class:`HandlerResponse` to a ``CallToolResult`` verbatim."""
    return _text_result(response.json, response.is_error)


def _render_pipeline_error(err: PipelineError) -> CallToolResult:
    """Render a raised :class:`PipelineError` to the single-line error envelope.

    Byte-exact to the Node ``return text(JSON.stringify(err.toJSON()), true)``
    branch: compact separators (no spaces), ``ensure_ascii=False`` so non-ASCII
    is left raw exactly as JS does.
    """
    text = json.dumps(
        err.to_dict(),
        separators=_COMPACT_SEPARATORS,
        ensure_ascii=False,
    )
    return _text_result(text, True)


def _render_exception(err: Exception) -> CallToolResult:
    """Render any non-``PipelineError`` exception to the ``Error: <msg>`` envelope.

    Byte-exact to the Node ``const msg = err instanceof Error ? err.message :
    String(err); return text(`Error: ${msg}`, true)`` branch. Python ``str(err)``
    is the analogue of an ``Error.message`` / ``String(err)``.
    """
    return _text_result(f"Error: {err}", True)


def _suppress_output_schema(
    wrapper: Callable[..., Any], inner: Callable[..., Any]
) -> None:
    """Neutralize FastMCP's return-type → ``outputSchema`` inference (R-H1).

    ``tool_result`` always returns a :class:`CallToolResult` (verbatim text +
    ``isError``), whose ``structuredContent`` is ``None``. If FastMCP infers an
    ``outputSchema`` for the wrapped tool, ``convert_result`` validates that
    ``None`` against the model at ``call_tool`` time and the call fails (see the
    module docstring). FastMCP derives the schema from the callable's RETURN
    annotation, resolved by ``inspect.signature(func, eval_str=True)`` — which
    follows ``__wrapped__`` (set by ``functools.wraps``) to ``inner``'s
    annotation.

    This makes the wrapper present as having NO inferable return type, so EVERY
    branch of FastMCP's ``func_metadata`` declines to build an output model and
    a bare ``@mcp.tool()`` registration yields ``outputSchema is None`` — for
    inner handlers annotated ``-> str``, ``-> HandlerResponse``, or unannotated,
    sync or async. Belt-and-suspenders (each independently sufficient against a
    different resolution path; all applied so no future FastMCP code path can
    resurface ``inner``'s annotation):

    1. Pin an explicit ``__signature__`` with an empty return annotation. This is
       the load-bearing fix: ``inspect.signature`` prefers ``__signature__`` over
       ``__wrapped__``, so the inner ``-> str`` hint is never resolved.
    2. Drop ``__wrapped__`` (set by ``functools.wraps``) so nothing — including a
       future ``inspect.signature`` call without an explicit ``__signature__`` —
       follows the wrapper back to ``inner``.
    3. Clear ``__annotations__["return"]`` so ``typing.get_type_hints`` cannot
       surface ``inner``'s return hint either.

    ``__name__`` / ``__doc__`` (which FastMCP uses for the tool name + description)
    are preserved — ``functools.wraps`` copied them and none of the above touches
    them.
    """
    # Resolve PEP 563 string annotations (the module uses
    # ``from __future__ import annotations``, so the inner handler's param
    # annotations arrive as strings). ``eval_str=True`` evaluates them
    # against ``inner.__globals__`` — without this, pinning ``__signature__``
    # below freezes the *string* annotations, and FastMCP (which honours an
    # explicit ``__signature__`` and does NOT re-evaluate it) then hands a
    # bare forward ref (e.g. a module-local ``Literal`` alias) to pydantic,
    # which cannot resolve it from its own namespace and raises. Resolving
    # here makes the wrapped tool's ``inputSchema`` build byte-identically to
    # a bare (unwrapped) registration. Fall back to the unresolved signature
    # if any annotation is not runtime-evaluable (e.g. a TYPE_CHECKING-only
    # forward ref); builtins still resolve. The return annotation is blanked
    # regardless, so the outputSchema suppression (R-H1) is unaffected.
    try:
        inner_sig = inspect.signature(inner, eval_str=True)
    except (NameError, TypeError, AttributeError):
        inner_sig = inspect.signature(inner)
    wrapper.__signature__ = inner_sig.replace(  # type: ignore[attr-defined]
        return_annotation=inspect.Signature.empty
    )
    if hasattr(wrapper, "__wrapped__"):
        del wrapper.__wrapped__
    wrapper.__annotations__.pop("return", None)


_P = ParamSpec("_P")

SyncHandler = Callable[_P, HandlerResponse]
AsyncHandler = Callable[_P, Awaitable[HandlerResponse]]


@overload
def tool_result(
    fn: Callable[_P, Awaitable[HandlerResponse]],
) -> Callable[_P, Coroutine[Any, Any, CallToolResult]]: ...


@overload
def tool_result(
    fn: Callable[_P, HandlerResponse],
) -> Callable[_P, CallToolResult]: ...


def tool_result(
    fn: Callable[_P, HandlerResponse] | Callable[_P, Awaitable[HandlerResponse]],
) -> Callable[_P, CallToolResult] | Callable[_P, Coroutine[Any, Any, CallToolResult]]:
    """Wrap a handler returning :class:`HandlerResponse` into an MCP tool body.

    The wrapped callable returns a :class:`CallToolResult` whose text content is
    the handler's ``HandlerResponse.json`` **verbatim** and whose ``isError``
    carries ``HandlerResponse.is_error``. Two error-signalling conventions both
    port (§4.2 / R1 §0):

    1. **Return** ``HandlerResponse(text, is_error=True)`` directly — passed
       through unchanged (validation-failure text blocks, kb_search no-index,
       cc_get_overview not-found, feature_request guard errors).
    2. **Raise** ``PipelineError`` / ``Exception`` — caught and rendered to the
       byte-exact error envelope here.

    Supports both sync and async handlers (FastMCP awaits coroutine tools).
    The decorator itself writes nothing to stdout.
    """
    if _is_coroutine(fn):
        async_fn = cast("Callable[_P, Awaitable[HandlerResponse]]", fn)

        @functools.wraps(async_fn)
        async def _async_wrapper(
            *args: _P.args, **kwargs: _P.kwargs
        ) -> CallToolResult:
            try:
                response = await async_fn(*args, **kwargs)
            except PipelineError as err:
                return _render_pipeline_error(err)
            except Exception as err:  # noqa: BLE001 — contract: catch-all → envelope
                return _render_exception(err)
            return _render_handler_response(response)

        _suppress_output_schema(_async_wrapper, async_fn)
        return _async_wrapper

    sync_fn = cast("Callable[_P, HandlerResponse]", fn)

    @functools.wraps(sync_fn)
    def _sync_wrapper(*args: _P.args, **kwargs: _P.kwargs) -> CallToolResult:
        try:
            response = sync_fn(*args, **kwargs)
        except PipelineError as err:
            return _render_pipeline_error(err)
        except Exception as err:  # noqa: BLE001 — contract: catch-all → envelope
            return _render_exception(err)
        return _render_handler_response(response)

    _suppress_output_schema(_sync_wrapper, sync_fn)
    return _sync_wrapper


def _is_coroutine(fn: Callable[..., Any]) -> bool:
    """Return ``True`` if ``fn`` is an ``async def`` (unwrapping ``functools``)."""
    unwrapped = inspect.unwrap(fn)
    return inspect.iscoroutinefunction(unwrapped)


# ── ResponseEnvelope (§4.2) ──────────────────────────────────────────
#
# The structured success/error envelope used by mutation/workflow tools:
#   { status: 'ok' | 'error', data: {...}, next_step?: str, warnings?: [str] }
# serialized with ``json.dumps(env, indent=2)`` (2-space, pretty). Conditional
# keys (``next_step`` / ``warnings``) are OMITTED when absent, mirroring the TS
# ``...(cond ? {k:v} : {})`` spreads (undefined-drop parity).


@dataclass
class ResponseEnvelope:
    """Structured tool envelope (``{status, data, next_step?, warnings?}``).

    ``next_step`` / ``warnings`` are the optional keys: they are omitted from the
    serialized object when ``None`` / empty (undefined-drop parity with TS).
    """

    status: str
    data: dict[str, Any] = field(default_factory=dict)
    next_step: str | None = None
    warnings: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-able dict, omitting absent optional keys."""
        env: dict[str, Any] = {"status": self.status, "data": self.data}
        if self.next_step:
            env["next_step"] = self.next_step
        if self.warnings:
            env["warnings"] = self.warnings
        return env

    def to_json(self) -> str:
        """Serialize as ``json.dumps(env, indent=2)`` (2-space pretty, §4.2)."""
        return json.dumps(self.to_dict(), indent=2)


def response_envelope(
    status: str,
    data: dict[str, Any] | None = None,
    *,
    next_step: str | None = None,
    warnings: list[str] | None = None,
) -> str:
    """Build + serialize a :class:`ResponseEnvelope` to its 2-space JSON string.

    Convenience for handlers that return a structured envelope as the
    ``HandlerResponse.json`` payload.
    """
    return ResponseEnvelope(
        status=status,
        data=data if data is not None else {},
        next_step=next_step,
        warnings=warnings,
    ).to_json()
