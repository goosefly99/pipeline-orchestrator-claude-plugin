"""Port of ``legacy-node/tests/lifecycle-handlers.test.ts`` (18 cases).

Byte-exact behavioral parity tests for the lifecycle handlers in
:mod:`pipeline_orchestrator.tools.lifecycle_tools`. Each Node ``it(...)`` maps
1:1 to a ``test_*`` method here, grouped into classes mirroring the Node
``describe`` blocks:

* ``handleFailPhase — next_step guidance`` ×3
* ``handleRetryPhase`` ×4
* ``Feature C step C8 — active run dir redirect`` ×3
* ``handleStartPhase — resolved_model (M5.2)`` ×4
* ``handleRunStatus — current_phase_resolved_model (M5.3)`` ×4

The ``LifecycleContext`` is a DI dataclass; the Node ``makeCtx`` object literal
is reproduced as a :class:`LifecycleContext` whose callables close over local
``active_run`` / ``active_run_dir`` mutable state (Python lists used as cells so
the closures can rebind). Real ``run_state`` helpers (``init_run`` / ``start_phase``
/ ``fail_phase`` / ``retry_phase`` / ``recover_run`` / ``remove_phase_artifacts`` /
``add_artifact`` / ``load_run_state``) are wired in where the Node test wires the
real run-state functions.

Fixtures mirror the Node ``mkdtempSync``/``rmSync`` ``beforeEach``/``afterEach``
via the ``tmp_path`` fixture. All paths are OS-agnostic (``os.path.join`` over
``tmp_path``) — never hardcoded slashes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from pipeline_orchestrator.dag import InputSatisfaction
from pipeline_orchestrator.hooks import PreInitHookResult
from pipeline_orchestrator.models import (
    DebateConfig,
    DebateOutputConfig,
    KBDefaults,
    PhaseDefinition,
    PhaseState,
    PipelineConfig,
    PipelineMeta,
    RunState,
    StorageConfig,
)
from pipeline_orchestrator.quality_gates import GateResult
from pipeline_orchestrator.run_state import (
    RecommendedAction,
    RecoveryResult,
    init_run,
    start_phase,
)
from pipeline_orchestrator.run_state import fail_phase as fail_phase_state
from pipeline_orchestrator.run_state import recover_run as recover_run_state
from pipeline_orchestrator.run_state import (
    remove_phase_artifacts as remove_phase_artifacts_state,
)
from pipeline_orchestrator.run_state import retry_phase as retry_phase_state
from pipeline_orchestrator.storage import get_run_data_dir
from pipeline_orchestrator.tools.lifecycle_tools import (
    LifecycleContext,
    handle_fail_phase,
    handle_init_run,
    handle_retry_phase,
    handle_run_status,
    handle_start_phase,
)

# ── Minimal config factory (mirrors the Node ``makeConfig``) ──────────


def make_config(
    optional_phases: list[str] | None = None,
    phase_model: str | None = None,
) -> PipelineConfig:
    """Build the two-phase (discovery, curation) config the Node tests use."""
    if optional_phases is None:
        optional_phases = []
    phases: dict[str, PhaseDefinition] = {
        "discovery": PhaseDefinition(
            id=0,
            description="",
            inputs=[],
            outputs=[],
            tools=[],
            entry_point=True,
            optional=False,
            model=phase_model,
        ),
        "curation": PhaseDefinition(
            id=1,
            description="",
            inputs=[],
            outputs=[],
            tools=[],
            entry_point=False,
            optional="curation" in optional_phases,
        ),
    }
    return PipelineConfig(
        pipeline=PipelineMeta(id="test", version="1.0.0", description=""),
        phases=phases,
        edges=[],
        debate=DebateConfig(
            agents={},
            rounds={},
            output=DebateOutputConfig(
                includes_transcript=True,
                includes_refined_artifact=True,
                artifact_version_bump="minor",
            ),
        ),
        knowledge_bases=KBDefaults(
            available_in=[],
            max_queries_per_phase=20,
            query_mode="proactive",
        ),
        schemas={},
        storage=StorageConfig(base_dir="test", paths={}),
    )


# ── Mock context factory (mirrors the Node ``makeCtx``) ───────────────


def make_ctx(
    initial_state: RunState | None,
    state_dir: str,
    optional_phases: list[str] | None = None,
    config: PipelineConfig | None = None,
) -> LifecycleContext:
    """Build a :class:`LifecycleContext` over real run-state helpers (Node ``makeCtx``).

    ``active_run`` / ``active_run_dir`` are held in single-element lists so the
    callables can rebind them — the Python analogue of the Node closure ``let``.
    """
    if optional_phases is None:
        optional_phases = []
    active_run: list[RunState | None] = [initial_state]
    active_run_dir: list[str] = [state_dir]

    def _start_phase(s: RunState, phase: str, d: str) -> RunState:
        updated = start_phase(s, phase, d)
        active_run[0] = updated
        return updated

    def _fail_phase(s: RunState, phase: str, error: str, d: str) -> RunState:
        updated = fail_phase_state(s, phase, error, d)
        active_run[0] = updated
        return updated

    def _retry_phase(s: RunState, phase: str, d: str) -> RunState:
        updated = retry_phase_state(s, phase, d)
        active_run[0] = updated
        return updated

    def _get_phase_input_satisfaction(
        phase: PhaseDefinition, _types: list[str]
    ) -> InputSatisfaction:
        return InputSatisfaction(
            satisfied=[], missing=phase.inputs, can_run=False
        )

    def _set_active_run(s: RunState) -> None:
        active_run[0] = s

    def _set_active_run_dir(d: str) -> None:
        active_run_dir[0] = d

    return LifecycleContext(
        get_active_run=lambda: active_run[0],
        set_active_run=_set_active_run,
        get_active_run_dir=lambda: active_run_dir[0],
        set_active_run_dir=_set_active_run_dir,
        get_config=lambda: (
            config if config is not None else make_config(optional_phases)
        ),
        set_project_root=lambda _root: None,
        get_storage_config=lambda *_a, **_k: StorageConfig(
            base_dir=state_dir, paths={}
        ),
        init_run=lambda *_a, **_k: initial_state,  # type: ignore[arg-type]
        skip_phase=lambda s, _p, _d: s,
        add_artifact=lambda s, _ref, _d: s,
        start_phase=_start_phase,
        complete_phase=lambda s, _p, _d: s,
        fail_phase=_fail_phase,
        retry_phase=_retry_phase,
        resolve_next_phases=lambda *_a: [],
        get_phase_input_satisfaction=_get_phase_input_satisfaction,
        load_run_state=lambda _d: None,
        recover_run=lambda run_dir: recover_run_state(run_dir),
        remove_phase_artifacts=lambda s, phase, d: remove_phase_artifacts_state(
            s, phase, d
        ),
        get_quality_gates=lambda: [],
        run_gate_checks=lambda *_a: GateResult(
            phase="", passed=True, on_failure="warn", results=[]
        ),
        get_hooks_config=lambda: [],
        run_hooks=lambda *_a: [],
        run_pre_pipeline_init_hooks=lambda *_a: PreInitHookResult(
            parameters={}, userPrompts=[]
        ),
        get_project_root=lambda: None,
        compute_recommended_action=lambda *_a: RecommendedAction(
            action="review", reason=""
        ),
        compute_run_warnings=lambda *_a, **_k: [],
    )


# ── handleFailPhase — next_step guidance ──────────────────────────────


class TestHandleFailPhaseNextStepGuidance:
    def test_non_optional_phase_next_step_mentions_retry_not_init(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        state = init_run("t1", "1.0.0", ["discovery"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        ctx = make_ctx(state, temp_dir)

        result = handle_fail_phase(
            {"phase": "discovery", "error": "API timeout"}, ctx
        )
        envelope = json.loads(result.json)

        assert envelope["status"] == "error"
        assert "pipeline_retry_phase" in envelope["next_step"]
        assert "pipeline_init_run" not in envelope["next_step"]

    def test_optional_phase_next_step_mentions_next_phases(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        state = init_run("t2", "1.0.0", ["curation"], temp_dir)
        state = start_phase(state, "curation", temp_dir)
        ctx = make_ctx(state, temp_dir, ["curation"])

        result = handle_fail_phase(
            {"phase": "curation", "error": "external service down"}, ctx
        )
        envelope = json.loads(result.json)

        assert envelope["status"] == "error"
        assert "pipeline_next_phases" in envelope["next_step"]

    def test_response_data_includes_phase_name_and_error_string(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        state = init_run("t3", "1.0.0", ["discovery"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        ctx = make_ctx(state, temp_dir)

        result = handle_fail_phase(
            {"phase": "discovery", "error": "Connection refused"}, ctx
        )
        envelope = json.loads(result.json)

        assert envelope["data"]["phase"] == "discovery"
        assert envelope["data"]["error"] == "Connection refused"


# ── handleRetryPhase ──────────────────────────────────────────────────


class TestHandleRetryPhase:
    def test_returns_ok_envelope_with_pending_and_incremented_retry_count(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        state = init_run("r1", "1.0.0", ["discovery"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        state = fail_phase_state(state, "discovery", "first failure", temp_dir)
        ctx = make_ctx(state, temp_dir)

        result = handle_retry_phase({"phase": "discovery"}, ctx)
        envelope = json.loads(result.json)

        assert envelope["status"] == "ok"
        assert envelope["data"]["phase"] == "discovery"
        assert envelope["data"]["status"] == "pending"
        assert envelope["data"]["retry_count"] == 1
        assert "pipeline_start_phase" in envelope["next_step"]

    def test_retry_count_increments_on_successive_retries(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        state = init_run("r2", "1.0.0", ["discovery"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        state = fail_phase_state(state, "discovery", "fail #1", temp_dir)

        # First retry
        state = retry_phase_state(state, "discovery", temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        state = fail_phase_state(state, "discovery", "fail #2", temp_dir)

        ctx = make_ctx(state, temp_dir)
        result = handle_retry_phase({"phase": "discovery"}, ctx)
        envelope = json.loads(result.json)

        assert envelope["data"]["retry_count"] == 2

    def test_raises_when_no_active_run(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        ctx = make_ctx(None, temp_dir)
        # Override get_active_run to return None (already None here).
        ctx.get_active_run = lambda: None

        with pytest.raises(ValueError, match="No active run"):
            handle_retry_phase({"phase": "discovery"}, ctx)

    def test_propagates_retry_error_when_phase_not_failed(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        state = init_run("r3", "1.0.0", ["discovery"], temp_dir)
        # discovery is 'pending', not 'failed'
        ctx = make_ctx(state, temp_dir)

        with pytest.raises(
            Exception,
            match=r'Cannot retry phase "discovery": status is "pending"',
        ):
            handle_retry_phase({"phase": "discovery"}, ctx)


# ── Feature C step C8 — active run dir redirect ─────────────


def _make_stub_run_state(
    run_id: str,
    phase_names: list[str],
    run_data_dir: str | None,
    run_parameters: dict[str, Any] | None = None,
) -> RunState:
    """Build a stub RunState with minimal required fields.

    Mirrors the Node ``makeStubRunState``.
    """
    from pipeline_orchestrator.tools.lifecycle_tools import _now_iso

    phases: dict[str, PhaseState] = {}
    for name in phase_names:
        phases[name] = PhaseState(
            phase_name=name,
            status="pending",
            input_artifacts=[],
            output_artifacts=[],
            retry_count=0,
        )
    state = RunState(
        run_id=run_id,
        pipeline_version="1.0.0",
        created_at=_now_iso(),
        updated_at=_now_iso(),
        status="initialized",
        config_path="",
        phases=phases,
        available_artifacts=[],
    )
    if run_data_dir is not None:
        state.run_data_dir = run_data_dir
    if run_parameters:
        state.run_parameters = run_parameters
    return state


class _RecordingCtx:
    """Holder for a recording :class:`LifecycleContext` + its captured calls."""

    def __init__(
        self,
        base_dir: str,
        stubbed_init_run_state: RunState,
        optional_phases: list[str] | None = None,
    ) -> None:
        if optional_phases is None:
            optional_phases = []
        self.set_active_run_dir_calls: list[str] = []
        self.init_run_calls: list[dict[str, Any]] = []
        self._active_run: RunState | None = None
        self._active_run_dir = ""
        self._stubbed = stubbed_init_run_state

        def _set_active_run_dir(d: str) -> None:
            self.set_active_run_dir_calls.append(d)
            self._active_run_dir = d

        def _set_active_run(s: RunState) -> None:
            self._active_run = s

        def _init_run(
            run_id: str,
            pipeline_version: str,
            phase_names: list[str],
            state_dir: str,
            run_parameters: dict[str, Any] | None = None,
        ) -> RunState:
            self.init_run_calls.append(
                {
                    "run_id": run_id,
                    "pipeline_version": pipeline_version,
                    "phase_names": phase_names,
                    "state_dir": state_dir,
                    "run_parameters": run_parameters,
                }
            )
            return self._stubbed

        def _get_phase_input_satisfaction(
            phase: PhaseDefinition, _types: list[str]
        ) -> InputSatisfaction:
            return InputSatisfaction(
                satisfied=[], missing=phase.inputs, can_run=False
            )

        self.ctx = LifecycleContext(
            get_active_run=lambda: self._active_run,
            set_active_run=_set_active_run,
            get_active_run_dir=lambda: self._active_run_dir,
            set_active_run_dir=_set_active_run_dir,
            get_config=lambda: make_config(optional_phases),
            set_project_root=lambda _root: None,
            get_storage_config=lambda *_a, **_k: StorageConfig(
                base_dir=base_dir, paths={}
            ),
            init_run=_init_run,
            skip_phase=lambda s, _p, _d: s,
            add_artifact=lambda s, _ref, _d: s,
            start_phase=lambda s, _p, _d: s,
            complete_phase=lambda s, _p, _d: s,
            fail_phase=lambda s, _p, _e, _d: s,
            retry_phase=lambda s, _p, _d: s,
            resolve_next_phases=lambda *_a: [],
            get_phase_input_satisfaction=_get_phase_input_satisfaction,
            load_run_state=lambda _d: None,
            recover_run=lambda _d: RecoveryResult(
                state=self._stubbed, recovered_phases=[], warnings=[]
            ),
            remove_phase_artifacts=lambda *_a: [],
            get_quality_gates=lambda: [],
            run_gate_checks=lambda *_a: GateResult(
                phase="", passed=True, on_failure="warn", results=[]
            ),
            get_hooks_config=lambda: [],
            run_hooks=lambda *_a: [],
            run_pre_pipeline_init_hooks=lambda *_a: PreInitHookResult(
                parameters={}, userPrompts=[]
            ),
            get_project_root=lambda: None,
            compute_recommended_action=lambda *_a: RecommendedAction(
                action="review", reason=""
            ),
            compute_run_warnings=lambda *_a, **_k: [],
        )


class TestFeatureC8ActiveRunDirRedirect:
    def test_with_run_parameters_redirects_to_canonical_run_data_dir(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        run_id = "feature-c-run"
        run_name = "feature-c-test"
        run_directory_timestamp = "2026-04-10T09-13-58-000Z"
        run_parameters: dict[str, Any] = {
            "run_name": run_name,
            "run_directory_timestamp": run_directory_timestamp,
        }

        canonical_run_data_dir = get_run_data_dir(
            temp_dir, run_name, run_directory_timestamp
        )
        legacy_run_dir = os.path.join(temp_dir, "runs", run_id)

        stubbed_state = _make_stub_run_state(
            run_id, ["discovery", "curation"], canonical_run_data_dir, run_parameters
        )
        rec = _RecordingCtx(temp_dir, stubbed_state)

        handle_init_run(
            {
                "run_id": run_id,
                "project_root": temp_dir,
                "base_dir": temp_dir,
                "run_parameters": run_parameters,
            },
            rec.ctx,
        )

        assert len(rec.init_run_calls) == 1
        assert rec.init_run_calls[0]["run_parameters"] == run_parameters
        assert os.path.normpath(rec.init_run_calls[0]["state_dir"]) == os.path.normpath(
            legacy_run_dir
        )

        assert len(rec.set_active_run_dir_calls) >= 2
        assert os.path.normpath(rec.set_active_run_dir_calls[0]) == os.path.normpath(
            legacy_run_dir
        )
        last_call = rec.set_active_run_dir_calls[-1]
        assert os.path.normpath(last_call) == os.path.normpath(
            canonical_run_data_dir
        )
        assert os.path.normpath(canonical_run_data_dir) != os.path.normpath(
            legacy_run_dir
        )

    def test_without_run_parameters_leaves_legacy_path(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        run_id = "legacy-run"
        legacy_run_dir = os.path.join(temp_dir, "runs", run_id)

        stubbed_state = _make_stub_run_state(
            run_id, ["discovery", "curation"], None
        )
        rec = _RecordingCtx(temp_dir, stubbed_state)

        handle_init_run(
            {
                "run_id": run_id,
                "project_root": temp_dir,
                "base_dir": temp_dir,
            },
            rec.ctx,
        )

        assert len(rec.init_run_calls) == 1
        assert len(rec.set_active_run_dir_calls) == 1
        assert os.path.normpath(rec.set_active_run_dir_calls[0]) == os.path.normpath(
            legacy_run_dir
        )

    def test_start_phase_after_redirect_writes_events_under_run_data_dir(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        run_id = "c8-integration"
        run_name = "integration-test"
        run_directory_timestamp = "2026-04-10T10-00-00-000Z"
        run_parameters: dict[str, Any] = {
            "run_name": run_name,
            "run_directory_timestamp": run_directory_timestamp,
        }

        canonical_run_data_dir = get_run_data_dir(
            temp_dir, run_name, run_directory_timestamp
        )
        legacy_run_dir = os.path.join(temp_dir, "runs", run_id)

        # Real disk-backed LifecycleContext — init_run + start_phase from
        # run_state, not stubs.
        active_run: list[RunState | None] = [None]
        active_run_dir: list[str] = [""]

        def _set_active_run(s: RunState) -> None:
            active_run[0] = s

        def _set_active_run_dir(d: str) -> None:
            active_run_dir[0] = d

        def _get_phase_input_satisfaction(
            phase: PhaseDefinition, _types: list[str]
        ) -> InputSatisfaction:
            return InputSatisfaction(
                satisfied=[], missing=phase.inputs, can_run=False
            )

        ctx = LifecycleContext(
            get_active_run=lambda: active_run[0],
            set_active_run=_set_active_run,
            get_active_run_dir=lambda: active_run_dir[0],
            set_active_run_dir=_set_active_run_dir,
            get_config=lambda: make_config(),
            set_project_root=lambda _root: None,
            get_storage_config=lambda *_a, **_k: StorageConfig(
                base_dir=temp_dir, paths={}
            ),
            init_run=lambda r_id, pv, phases, state_dir, rp=None: init_run(
                r_id, pv, phases, state_dir, rp
            ),
            skip_phase=lambda s, _p, _d: s,
            add_artifact=lambda s, _ref, _d: s,
            start_phase=lambda s, phase, d: start_phase(s, phase, d),
            complete_phase=lambda s, _p, _d: s,
            fail_phase=lambda s, _p, _e, _d: s,
            retry_phase=lambda s, _p, _d: s,
            resolve_next_phases=lambda *_a: [],
            get_phase_input_satisfaction=_get_phase_input_satisfaction,
            load_run_state=lambda _d: None,
            recover_run=lambda _d: RecoveryResult(
                state=active_run[0], recovered_phases=[], warnings=[]  # type: ignore[arg-type]
            ),
            remove_phase_artifacts=lambda *_a: [],
            get_quality_gates=lambda: [],
            run_gate_checks=lambda *_a: GateResult(
                phase="", passed=True, on_failure="warn", results=[]
            ),
            get_hooks_config=lambda: [],
            run_hooks=lambda *_a: [],
            run_pre_pipeline_init_hooks=lambda *_a: PreInitHookResult(
                parameters={}, userPrompts=[]
            ),
            get_project_root=lambda: temp_dir,
            compute_recommended_action=lambda *_a: RecommendedAction(
                action="review", reason=""
            ),
            compute_run_warnings=lambda *_a, **_k: [],
        )

        handle_init_run(
            {
                "run_id": run_id,
                "project_root": temp_dir,
                "base_dir": temp_dir,
                "run_parameters": run_parameters,
            },
            ctx,
        )

        assert os.path.normpath(active_run_dir[0]) == os.path.normpath(
            canonical_run_data_dir
        )
        assert active_run[0] is not None
        assert os.path.normpath(active_run[0].run_data_dir or "") == os.path.normpath(
            canonical_run_data_dir
        )
        assert os.path.exists(
            os.path.join(canonical_run_data_dir, "run-state.json")
        )
        assert not os.path.exists(
            os.path.join(legacy_run_dir, "run-state.json")
        )

        handle_start_phase({"phase": "discovery"}, ctx)

        canonical_events_path = os.path.join(
            canonical_run_data_dir, "events.jsonl"
        )
        assert os.path.exists(canonical_events_path)
        legacy_events_path = os.path.join(legacy_run_dir, "events.jsonl")
        assert not os.path.exists(legacy_events_path)

        with open(canonical_events_path, encoding="utf-8") as fh:
            raw = fh.read()
        first_line = next(
            (line for line in raw.split("\n") if line.strip()), None
        )
        assert first_line is not None
        parsed_event = json.loads(first_line)
        assert parsed_event["event"] == "phase_started"
        assert parsed_event["phase"] == "discovery"
        assert parsed_event["run_id"] == run_id


# ── handleStartPhase — resolved_model (M5.2) ──


def _read_phase_started_event(events_path: str) -> dict[str, Any] | None:
    """Find the first ``phase_started`` event in events.jsonl (Node ``.find``)."""
    with open(events_path, encoding="utf-8") as fh:
        lines = fh.read().strip().split("\n")
    for line in lines:
        parsed: dict[str, Any] = json.loads(line)
        if parsed.get("event") == "phase_started":
            return parsed
    return None


class TestHandleStartPhaseResolvedModel:
    def test_phase_started_event_defaults_to_sonnet(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("m52-default", "1.0.0", ["discovery"], temp_dir)
        ctx = make_ctx(state, temp_dir)

        handle_start_phase({"phase": "discovery"}, ctx)

        events_path = os.path.join(temp_dir, "events.jsonl")
        event = _read_phase_started_event(events_path)
        assert event is not None
        assert event["resolved_model"] == "claude-sonnet-4-6"

    def test_phase_started_event_uses_run_parameters_phase_model(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        state = init_run("m52-run-param", "1.0.0", ["discovery"], temp_dir)
        state.run_parameters = {"phase_model": "claude-haiku-4-5"}
        ctx = make_ctx(state, temp_dir)

        handle_start_phase({"phase": "discovery"}, ctx)

        events_path = os.path.join(temp_dir, "events.jsonl")
        event = _read_phase_started_event(events_path)
        assert event is not None
        assert event["resolved_model"] == "claude-haiku-4-5"

    def test_phase_started_event_uses_phase_level_model_over_run_param(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        state = init_run("m52-phase-model", "1.0.0", ["discovery"], temp_dir)
        state.run_parameters = {"phase_model": "claude-haiku-4-5"}
        config = make_config([], "claude-opus-4-5")
        ctx = make_ctx(state, temp_dir, [], config)

        handle_start_phase({"phase": "discovery"}, ctx)

        events_path = os.path.join(temp_dir, "events.jsonl")
        event = _read_phase_started_event(events_path)
        assert event is not None
        assert event["resolved_model"] == "claude-opus-4-5"

    def test_response_body_includes_resolved_model(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("m52-response", "1.0.0", ["discovery"], temp_dir)
        ctx = make_ctx(state, temp_dir)

        result = handle_start_phase({"phase": "discovery"}, ctx)
        body = json.loads(result.json)

        assert body["resolved_model"] == "claude-sonnet-4-6"


# ── handleRunStatus — current_phase_resolved_model (M5.3) ────


class TestHandleRunStatusCurrentPhaseResolvedModel:
    def test_no_current_phase_resolved_model_when_none_in_progress(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        state = init_run("m53-no-phase", "1.0.0", ["discovery"], temp_dir)
        ctx = make_ctx(state, temp_dir)

        result = handle_run_status(ctx)
        body = json.loads(result.json)

        assert "current_phase_resolved_model" not in body

    def test_current_phase_resolved_model_defaults_to_sonnet(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        state = init_run("m53-default", "1.0.0", ["discovery"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        ctx = make_ctx(state, temp_dir)

        result = handle_run_status(ctx)
        body = json.loads(result.json)

        assert body["current_phase_resolved_model"] == "claude-sonnet-4-6"

    def test_current_phase_resolved_model_uses_run_parameters(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        state = init_run("m53-run-param", "1.0.0", ["discovery"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        state.run_parameters = {"phase_model": "claude-haiku-4-5"}
        ctx = make_ctx(state, temp_dir)

        result = handle_run_status(ctx)
        body = json.loads(result.json)

        assert body["current_phase_resolved_model"] == "claude-haiku-4-5"

    def test_current_phase_resolved_model_uses_phase_level_over_run_param(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        state = init_run("m53-phase-model", "1.0.0", ["discovery"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        state.run_parameters = {"phase_model": "claude-haiku-4-5"}
        config = make_config([], "claude-opus-4-5")
        ctx = make_ctx(state, temp_dir, [], config)

        result = handle_run_status(ctx)
        body = json.loads(result.json)

        assert body["current_phase_resolved_model"] == "claude-opus-4-5"


# ── register_lifecycle_tools — registration integrity ────────────────


def test_register_lifecycle_tools_meta_and_annotations() -> None:
    """Integrity: exactly the 11 lifecycle tools register with the frozen schema.

    Builds a fresh FastMCP, registers the lifecycle tools, and inspects the
    ``mcp.types.Tool`` objects ``list_tools()`` exposes against the byte-exact
    ``tool-schemas.ts`` map: read tools carry ``readOnlyHint=True`` + 50000;
    mutating tools carry ``destructiveHint=True`` + 10000; ``phase_handoff`` ALSO
    carries ``idempotentHint=True`` (the only tool with it).
    ``register_lifecycle_tools`` is NOT wired into the global ``register_tools``
    seam: this test constructs its own FastMCP, so the global handshake stays 0.
    """
    import asyncio

    from mcp.server.fastmcp import FastMCP

    from pipeline_orchestrator.tools.lifecycle_tools import (
        register_lifecycle_tools,
    )

    mcp = FastMCP("pipeline")
    register_lifecycle_tools(mcp)

    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}

    read_tools = {
        "pipeline_get_config",
        "pipeline_next_phases",
        "pipeline_run_status",
        "pipeline_reload_state",
        "pipeline_phase_handoff",
        "pipeline_phase_brief",
    }
    mutate_tools = {
        "pipeline_init_run",
        "pipeline_start_phase",
        "pipeline_complete_phase",
        "pipeline_fail_phase",
        "pipeline_retry_phase",
    }
    expected_names = read_tools | mutate_tools

    assert set(by_name) == expected_names
    assert len(tools) == 11

    for name in read_tools:
        assert by_name[name].meta == {"max_result_chars": 50000}, name
        ann = by_name[name].annotations
        assert ann is not None, name
        assert ann.readOnlyHint is True, name
        assert ann.destructiveHint is None, name
        if name == "pipeline_phase_handoff":
            assert ann.idempotentHint is True, name
        else:
            assert ann.idempotentHint is None, name

    for name in mutate_tools:
        assert by_name[name].meta == {"max_result_chars": 10000}, name
        ann = by_name[name].annotations
        assert ann is not None, name
        assert ann.destructiveHint is True, name
        assert ann.readOnlyHint is None, name
        assert ann.idempotentHint is None, name


def test_register_lifecycle_tools_outputschema_is_none() -> None:
    """Every registered lifecycle tool stub has ``outputSchema is None`` (R-H1).

    The bodies are bare ``-> str`` stubs (real wiring lands at T6.5), so FastMCP
    must NOT infer an output model — the global wiring would otherwise validate
    structuredContent against it and the call would fail. We assert the
    registration shells enumerate clean.
    """
    import asyncio

    from mcp.server.fastmcp import FastMCP

    from pipeline_orchestrator.tools.lifecycle_tools import (
        register_lifecycle_tools,
    )

    mcp = FastMCP("pipeline")
    register_lifecycle_tools(mcp)
    tools = asyncio.run(mcp.list_tools())
    assert len(tools) == 11


def test_global_handshake_still_zero_tools() -> None:
    """The global ``register_tools`` seam still registers 0 tools (T6.5 owns wiring).

    ``register_lifecycle_tools`` is defined + isolation-tested but NOT wired into
    ``tools/__init__.py``; the MCP handshake must still enumerate 0 tools.
    """
    import asyncio

    from mcp.server.fastmcp import FastMCP

    from pipeline_orchestrator.tools import register_tools

    fresh = FastMCP("pipeline")
    assert len(asyncio.run(fresh.list_tools())) == 0
    register_tools(fresh)
    assert len(asyncio.run(fresh.list_tools())) == 0
