"""Port of ``legacy-node/tests/debate-handlers.test.ts`` (5 cases) + additive
integrity tests.

The 5 ported cases exercise ``handle_save_debate`` through a stubbed
:class:`SaveDebateContext` (the DI seam): the Feature-C ``persist_debate`` vs
``store_artifact`` routing, the no-active-debate guard, the missing-field guards,
and the validation-failure (plain-text ``is_error``) branch — with byte-exact
assertions on the response envelope.

The additive integrity tests mirror the cc/synth precedent (§5.3):

* ``test_register_debate_tools_meta_and_annotations`` — builds a fresh
  ``FastMCP("pipeline")``, registers the debate tools, and asserts via
  ``asyncio.run(mcp.list_tools())`` that exactly the four ``pipeline_*`` debate
  tools register, each with ``_meta.max_result_chars == 10000`` and
  ``ToolAnnotations(destructiveHint=True)`` (none is a ``*_load_collection``, so
  the no-annotations exception does not apply).
* ``test_save_debate_response_keeps_raw_non_ascii`` — the save-success JSON keeps
  raw UTF-8 (no ``\\uXXXX`` escapes), matching Node's raw-UTF-8 ``JSON.stringify``.
* ``test_module_level_active_debate_default_getters`` — the default
  module-level ``ACTIVE_DEBATE`` getter/setter round-trip.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from pipeline_orchestrator.debate import init_debate, submit_argument
from pipeline_orchestrator.models import DebateState, DebateSynthesis, RunState
from pipeline_orchestrator.tools.debate_tools import (
    DebateContext,
    SaveDebateContext,
    get_active_debate,
    handle_init_debate,
    handle_save_debate,
    register_debate_tools,
    set_active_debate,
)
from pipeline_orchestrator.validator import ValidationResult

# ── Helpers (mirror the TS makeSaveReadyDebate / makeStubCtx) ──────────


def _make_save_ready_debate() -> DebateState:
    """A DebateState ready to save (round-1 complete + synthesis recorded)."""
    state = init_debate(
        input_type="design-spec",
        input_id="spec-save-test",
        input_version="1.0.0",
        artifact_content={"spec_id": "spec-save-test", "title": "Save-ready spec"},
    )
    from pipeline_orchestrator.models import AgentArgument

    state = submit_argument(
        state,
        AgentArgument(role="advocate", position="Solid approach.", confidence=0.9),
    )
    state = submit_argument(
        state, AgentArgument(role="critic", position="Has some gaps.", confidence=0.7)
    )
    state = submit_argument(
        state,
        AgentArgument(role="risk_specialist", position="Drawdown OK.", confidence=0.8),
    )
    state = submit_argument(
        state,
        AgentArgument(
            role="domain_specialist",
            position="Domain constraints noted.",
            confidence=0.75,
        ),
    )
    state.synthesis = DebateSynthesis(
        changes_accepted=["Add slippage model"],
        changes_rejected=[],
        open_questions=[],
    )
    state.output_version = "1.1.0"
    return state


def _make_stub_ctx(
    *,
    initial_debate: DebateState | None,
    active_run: RunState | None = None,
    validation_result: ValidationResult | None = None,
    include_persist_debate: bool = False,
    stored_path: str = "/tmp/legacy/debates/dt-fallback.json",
    persist_debate_path: str = "/tmp/runs/abc/debates/dt-persisted.json",
) -> tuple[SaveDebateContext, dict[str, object]]:
    """Mirror the TS ``makeStubCtx`` — a SaveDebateContext + a captured call log."""
    log: dict[str, object] = {
        "store_artifact_calls": [],
        "persist_debate_calls": [],
        "add_artifact_calls": [],
        "active_debate": initial_debate,
        "active_run": active_run,
    }
    validation = validation_result or ValidationResult(valid=True, errors=[])

    def get_active_debate_fn() -> DebateState | None:
        return log["active_debate"]  # type: ignore[return-value]

    def set_active_debate_fn(s: DebateState | None) -> None:
        log["active_debate"] = s

    def validate_artifact_fn(
        _schemas: object, _file: str, _artifact: object
    ) -> ValidationResult:
        return validation

    def store_artifact_fn(
        config: object,
        key: str,
        name: str,
        artifact: object,
        force: object = None,
    ) -> str:
        calls = log["store_artifact_calls"]
        assert isinstance(calls, list)
        calls.append(
            {
                "config": config,
                "key": key,
                "name": name,
                "artifact": artifact,
                "force": force,
            }
        )
        return stored_path

    def get_storage_config_fn(base_dir: str) -> object:
        return {"base_dir": base_dir, "paths": {"debates": "debates"}}

    def get_active_run_fn() -> RunState | None:
        return log["active_run"]  # type: ignore[return-value]

    def add_artifact_fn(state: RunState, ref: object, state_dir: str) -> RunState:
        calls = log["add_artifact_calls"]
        assert isinstance(calls, list)
        calls.append({"ref": ref, "state_dir": state_dir})
        return state

    def set_active_run_fn(s: RunState) -> None:
        log["active_run"] = s

    persist_debate_fn = None
    if include_persist_debate:

        def persist_debate_impl(
            transcript: object, file_name: str, force: object = None
        ) -> str:
            calls = log["persist_debate_calls"]
            assert isinstance(calls, list)
            calls.append(
                {"transcript": transcript, "file_name": file_name, "force": force}
            )
            return persist_debate_path

        persist_debate_fn = persist_debate_impl

    ctx = SaveDebateContext(
        get_active_debate=get_active_debate_fn,
        set_active_debate=set_active_debate_fn,
        validate_artifact=validate_artifact_fn,
        get_schemas=lambda: {},
        store_artifact=store_artifact_fn,
        get_storage_config=get_storage_config_fn,
        base_dir_from_run_dir=lambda run_dir: f"{run_dir}/..",
        get_project_root=lambda: "/tmp/project",
        get_config_storage_base_dir=lambda: "pipeline_mcp_data",
        get_active_run=get_active_run_fn,
        get_active_run_dir=lambda: "/tmp/project/pipeline_mcp_data/runs/test-run",
        add_artifact=add_artifact_fn,
        set_active_run=set_active_run_fn,
        persist_debate=persist_debate_fn,
    )
    return ctx, log


# ── handle_save_debate — persist_debate routing (Feature C) ────────────


def test_save_debate_prefers_persist_debate_and_skips_store_artifact() -> None:
    debate = _make_save_ready_debate()
    ctx, log = _make_stub_ctx(
        initial_debate=debate,
        include_persist_debate=True,
        persist_debate_path="/tmp/runs/xyz/debates/dt-from-persist.json",
    )

    result = handle_save_debate(
        {"file_name": "dt-from-persist.json", "phase": "knowledge-overview-debate"},
        ctx,
    )

    persist_calls = log["persist_debate_calls"]
    store_calls = log["store_artifact_calls"]
    assert isinstance(persist_calls, list) and len(persist_calls) == 1
    assert isinstance(store_calls, list) and len(store_calls) == 0
    assert persist_calls[0]["file_name"] == "dt-from-persist.json"

    passed_transcript = persist_calls[0]["transcript"]
    assert isinstance(passed_transcript, dict)
    assert passed_transcript["transcript_id"] == debate.transcript_id
    assert passed_transcript["input_type"] == "design-spec"
    assert passed_transcript["input_id"] == "spec-save-test"

    response = json.loads(result.json)
    assert response["stored_path"] == "/tmp/runs/xyz/debates/dt-from-persist.json"
    assert response["status"] == "saved"
    assert response["transcript_id"] == debate.transcript_id

    assert log["active_debate"] is None


def test_save_debate_falls_back_to_store_artifact() -> None:
    debate = _make_save_ready_debate()
    ctx, log = _make_stub_ctx(
        initial_debate=debate,
        include_persist_debate=False,
        stored_path="/tmp/legacy/debates/dt-from-store.json",
    )

    result = handle_save_debate(
        {"file_name": "dt-from-store.json", "phase": "design-debate"},
        ctx,
    )

    store_calls = log["store_artifact_calls"]
    persist_calls = log["persist_debate_calls"]
    assert isinstance(store_calls, list) and len(store_calls) == 1
    assert isinstance(persist_calls, list) and len(persist_calls) == 0
    call = store_calls[0]
    assert call["key"] == "debates"
    assert call["name"] == "dt-from-store.json"
    # Handler passes None for force (preserving throw-on-collision semantics).
    assert call["force"] is None

    response = json.loads(result.json)
    assert response["stored_path"] == "/tmp/legacy/debates/dt-from-store.json"
    assert response["status"] == "saved"


def test_save_debate_throws_when_no_active_debate() -> None:
    ctx, _log = _make_stub_ctx(initial_debate=None, include_persist_debate=True)
    with pytest.raises(ValueError, match="(?i)No active debate"):
        handle_save_debate({"file_name": "dt-xyz.json", "phase": "p"}, ctx)


def test_save_debate_throws_when_file_name_or_phase_missing() -> None:
    debate = _make_save_ready_debate()
    ctx_no_file, _l1 = _make_stub_ctx(
        initial_debate=debate, include_persist_debate=True
    )
    with pytest.raises(ValueError, match="file_name and phase are required"):
        handle_save_debate({"phase": "p"}, ctx_no_file)

    debate2 = _make_save_ready_debate()
    ctx_no_phase, _l2 = _make_stub_ctx(
        initial_debate=debate2, include_persist_debate=True
    )
    with pytest.raises(ValueError, match="file_name and phase are required"):
        handle_save_debate({"file_name": "dt-y.json"}, ctx_no_phase)


def test_save_debate_returns_is_error_when_validation_fails() -> None:
    debate = _make_save_ready_debate()
    ctx, log = _make_stub_ctx(
        initial_debate=debate,
        include_persist_debate=True,
        validation_result=ValidationResult(
            valid=False, errors=['missing required field "transcript_id"']
        ),
    )

    result = handle_save_debate({"file_name": "dt-bad.json", "phase": "p"}, ctx)

    assert result.is_error is True
    assert "Debate transcript validation FAILED" in result.json
    assert "missing required field" in result.json
    # Must NOT have attempted to persist on validation failure.
    persist_calls = log["persist_debate_calls"]
    store_calls = log["store_artifact_calls"]
    assert isinstance(persist_calls, list) and len(persist_calls) == 0
    assert isinstance(store_calls, list) and len(store_calls) == 0


# ── register_debate_tools — registration integrity ────────────────────


def test_register_debate_tools_meta_and_annotations() -> None:
    """Integrity: exactly 4 debate tools, each ``_meta=10000`` + destructiveHint.

    Builds a fresh FastMCP, registers the debate tools, and inspects the
    ``mcp.types.Tool`` objects ``list_tools()`` exposes. All four are identical
    in shape — none is a ``*_load_collection``, so the no-annotations exception
    does not apply. ``register_debate_tools`` is NOT wired into the global
    ``register_tools`` seam: this test constructs its own FastMCP instance, so
    the global handshake stays at 0.
    """
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("pipeline")
    register_debate_tools(mcp)

    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}

    expected_names = {
        "pipeline_init_debate",
        "pipeline_submit_argument",
        "pipeline_synthesize_debate",
        "pipeline_save_debate",
    }
    assert set(by_name) == expected_names
    assert len(tools) == 4

    for name in expected_names:
        assert by_name[name].meta is not None, f"{name} must carry _meta"
        assert by_name[name].meta == {"max_result_chars": 10000}, name
        ann = by_name[name].annotations
        assert ann is not None, f"{name} must carry annotations"
        assert ann.destructiveHint is True, name
        assert ann.readOnlyHint is None, name


# ── Additive integrity ────────────────────────────────────────────────


def test_init_debate_response_keeps_raw_non_ascii() -> None:
    """The init-debate JSON keeps raw UTF-8 (no ``\\uXXXX`` escapes).

    The response echoes ``input_id`` verbatim, so a non-ASCII id (em-dash +
    accented char) must survive raw — matching Node's raw-UTF-8 ``JSON.stringify``.
    """
    store: dict[str, DebateState | None] = {"value": None}
    ctx = DebateContext(
        get_active_debate=lambda: store["value"],
        set_active_debate=lambda s: store.__setitem__("value", s),
    )
    result = handle_init_debate(
        {
            "input_type": "design-spec",
            "input_id": "spec-é—dash",
            "input_version": "1.0.0",
            "artifact_content": {"spec_id": "spec-é—dash"},
        },
        ctx,
    )
    assert "spec-é—dash" in result.json
    assert "\\u" not in result.json


def test_module_level_active_debate_default_getters() -> None:
    """The default module-level ``ACTIVE_DEBATE`` getter/setter round-trips."""
    try:
        assert get_active_debate() is None
        state = init_debate(
            input_type="design-spec",
            input_id="spec-mod",
            input_version="1.0.0",
            artifact_content={},
        )
        set_active_debate(state)
        assert get_active_debate() is state
    finally:
        set_active_debate(None)
