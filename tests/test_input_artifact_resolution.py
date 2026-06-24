"""Port of ``legacy-node/tests/input-artifact-resolution.test.ts`` (6 cases).

Byte-exact behavioral parity tests for upstream input-artifact resolution:

* ``resolveInputArtifacts (run-state helper)`` x3 — the pure DAG helper
  :func:`pipeline_orchestrator.dag.resolve_input_artifacts` (already ported in T1.4).
* ``handleStartPhase — input_artifacts wiring`` x3 — the
  :func:`pipeline_orchestrator.tools.lifecycle_tools.handle_start_phase` attachment
  (already ported in T6.2) that records resolved input artifacts on the phase and
  surfaces them in the response + persisted run state.

Both target symbols already exist; this task only ports the TS tests against them.
The Node ``makeConfig`` / ``makeCtx`` object literals are reproduced as Python
factories; ``makeCtx`` builds a :class:`LifecycleContext` over the real run-state
helpers (mirroring the Node test's wiring). Fixtures use ``tmp_path``; all paths
are OS-agnostic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pipeline_orchestrator.dag import InputSatisfaction, resolve_input_artifacts
from pipeline_orchestrator.hooks import PreInitHookResult
from pipeline_orchestrator.models import (
    ArtifactRef,
    DebateConfig,
    DebateOutputConfig,
    EdgeDefinition,
    KBDefaults,
    PhaseDefinition,
    PipelineConfig,
    PipelineMeta,
    RunState,
    StorageConfig,
)
from pipeline_orchestrator.quality_gates import GateResult
from pipeline_orchestrator.run_state import (
    RecommendedAction,
    RecoveryResult,
    add_artifact,
    complete_phase,
    init_run,
    load_run_state,
    start_phase,
)
from pipeline_orchestrator.tools.lifecycle_tools import (
    LifecycleContext,
    handle_start_phase,
)

# ── Minimal config factory (mirrors the Node ``makeConfig``) ──────────


def make_config(
    phases: dict[str, dict[str, Any]],
    edges: list[EdgeDefinition] | None = None,
) -> PipelineConfig:
    if edges is None:
        edges = []
    full_phases: dict[str, PhaseDefinition] = {}
    for name, partial in phases.items():
        full_phases[name] = PhaseDefinition(
            id=name,
            description=str(partial.get("description", "")),
            inputs=list(partial.get("inputs", [])),
            outputs=list(partial.get("outputs", [])),
            tools=list(partial.get("tools", [])),
            entry_point=bool(partial.get("entry_point", False)),
            optional=bool(partial.get("optional", False)),
            input_mode=partial.get("input_mode"),
        )
    return PipelineConfig(
        pipeline=PipelineMeta(id="test", version="1.0.0", description=""),
        phases=full_phases,
        edges=edges,
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
            available_in=[], max_queries_per_phase=20, query_mode="proactive"
        ),
        schemas={},
        storage=StorageConfig(base_dir="test", paths={}),
    )


# ── Mock context factory backed by real run-state helpers ─────────────


def make_ctx(
    initial_state: RunState,
    state_dir: str,
    config: PipelineConfig,
) -> tuple[LifecycleContext, list[RunState]]:
    """Build a :class:`LifecycleContext` over real run-state helpers.

    Returns ``(ctx, active_run_cell)`` so callers can read the mutated active run
    (the Python analogue of the Node ``getState`` closure).
    """
    active_run: list[RunState] = [initial_state]
    active_run_dir: list[str] = [state_dir]

    def _set_active_run(s: RunState) -> None:
        active_run[0] = s

    def _add_artifact(s: RunState, ref: ArtifactRef, d: str) -> RunState:
        updated = add_artifact(s, ref, d)
        active_run[0] = updated
        return updated

    def _start_phase(s: RunState, phase: str, d: str) -> RunState:
        updated = start_phase(s, phase, d)
        active_run[0] = updated
        return updated

    def _complete_phase(s: RunState, phase: str, d: str) -> RunState:
        updated = complete_phase(s, phase, d)
        active_run[0] = updated
        return updated

    def _get_phase_input_satisfaction(
        phase: PhaseDefinition, _types: list[str]
    ) -> InputSatisfaction:
        return InputSatisfaction(satisfied=[], missing=phase.inputs, can_run=True)

    ctx = LifecycleContext(
        get_active_run=lambda: active_run[0],
        set_active_run=_set_active_run,
        get_active_run_dir=lambda: active_run_dir[0],
        set_active_run_dir=lambda d: active_run_dir.__setitem__(0, d),
        get_config=lambda: config,
        set_project_root=lambda _root: None,
        get_storage_config=lambda *_a, **_k: StorageConfig(
            base_dir=state_dir, paths={}
        ),
        init_run=lambda *_a, **_k: initial_state,
        skip_phase=lambda s, _p, _d: s,
        add_artifact=_add_artifact,
        start_phase=_start_phase,
        complete_phase=_complete_phase,
        fail_phase=lambda s, *_a: s,
        retry_phase=lambda s, *_a: s,
        resolve_next_phases=lambda *_a: [],
        get_phase_input_satisfaction=_get_phase_input_satisfaction,
        load_run_state=lambda _d: None,
        recover_run=lambda _run_dir: RecoveryResult(
            state=active_run[0], recovered_phases=[], warnings=[]
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
    return ctx, active_run


def _artifact_ref(path: str, phase: str, type_: str = "doc") -> ArtifactRef:
    return ArtifactRef(
        type=type_,
        path=path,
        phase=phase,
        created_at="2026-01-01T00:00:00.000Z",
        version=1,
    )


# ── resolveInputArtifacts (pure helper) ───────────────────────────────


class TestResolveInputArtifacts:
    def test_returns_empty_for_entry_point_phase_with_no_incoming_edges(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        config = make_config({"phase_a": {"entry_point": True}})
        state = init_run("t-empty", "1.0.0", ["phase_a"], temp_dir)
        assert resolve_input_artifacts(state, "phase_a", config) == []

    def test_walks_incoming_edges_and_collects_output_artifacts(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        config = make_config(
            {
                "phase_a": {"entry_point": True, "outputs": ["doc"]},
                "phase_b": {"entry_point": False, "inputs": ["doc"]},
            },
            [EdgeDefinition(from_="phase_a", to="phase_b")],
        )
        state = init_run("t-walk", "1.0.0", ["phase_a", "phase_b"], temp_dir)
        state = start_phase(state, "phase_a", temp_dir)
        state = add_artifact(
            state, _artifact_ref("/tmp/a1.json", "phase_a"), temp_dir
        )
        state = add_artifact(
            state, _artifact_ref("/tmp/a2.json", "phase_a"), temp_dir
        )
        state = complete_phase(state, "phase_a", temp_dir)

        assert resolve_input_artifacts(state, "phase_b", config) == [
            "/tmp/a1.json",
            "/tmp/a2.json",
        ]

    def test_deduplicates_paths_across_upstream_preserving_first_seen_order(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        config = make_config(
            {
                "up_a": {"entry_point": True, "outputs": ["doc"]},
                "up_b": {"entry_point": True, "outputs": ["doc"]},
                "phase_b": {"entry_point": False, "inputs": ["doc"]},
            },
            [
                EdgeDefinition(from_="up_a", to="phase_b"),
                EdgeDefinition(from_="up_b", to="phase_b"),
            ],
        )
        state = init_run(
            "t-dedup", "1.0.0", ["up_a", "up_b", "phase_b"], temp_dir
        )
        state = start_phase(state, "up_a", temp_dir)
        state = add_artifact(
            state, _artifact_ref("/tmp/shared.json", "up_a"), temp_dir
        )
        state = add_artifact(
            state, _artifact_ref("/tmp/only-a.json", "up_a"), temp_dir
        )
        state = complete_phase(state, "up_a", temp_dir)

        state = start_phase(state, "up_b", temp_dir)
        state = add_artifact(
            state, _artifact_ref("/tmp/shared.json", "up_b"), temp_dir
        )
        state = add_artifact(
            state, _artifact_ref("/tmp/only-b.json", "up_b"), temp_dir
        )
        state = complete_phase(state, "up_b", temp_dir)

        resolved = resolve_input_artifacts(state, "phase_b", config)
        assert resolved == ["/tmp/shared.json", "/tmp/only-a.json", "/tmp/only-b.json"]
        assert resolved.count("/tmp/shared.json") == 1


# ── handleStartPhase — input_artifacts wiring ─────────────────────────


class TestHandleStartPhaseInputArtifactsWiring:
    def test_populates_input_artifacts_from_upstream_and_persists(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        config = make_config(
            {
                "phase_a": {"entry_point": True, "outputs": ["doc"]},
                "phase_b": {"entry_point": False, "inputs": ["doc"]},
            },
            [EdgeDefinition(from_="phase_a", to="phase_b")],
        )
        state = init_run("t-happy", "1.0.0", ["phase_a", "phase_b"], temp_dir)
        state = start_phase(state, "phase_a", temp_dir)
        state = add_artifact(
            state, _artifact_ref("/tmp/art1.json", "phase_a"), temp_dir
        )
        state = add_artifact(
            state, _artifact_ref("/tmp/art2.json", "phase_a"), temp_dir
        )
        state = complete_phase(state, "phase_a", temp_dir)

        ctx, active_run = make_ctx(state, temp_dir, config)

        result = handle_start_phase({"phase": "phase_b"}, ctx)
        parsed = json.loads(result.json)

        assert parsed["phase"] == "phase_b"
        assert parsed["status"] == "in_progress"
        assert parsed["input_artifacts"] == ["/tmp/art1.json", "/tmp/art2.json"]

        mem = active_run[0]
        assert mem.phases["phase_b"].input_artifacts == [
            "/tmp/art1.json",
            "/tmp/art2.json",
        ]

        disk = load_run_state(temp_dir)
        assert disk is not None
        assert disk.phases["phase_b"].input_artifacts == [
            "/tmp/art1.json",
            "/tmp/art2.json",
        ]

    def test_entry_point_phase_with_no_upstream_yields_empty_input_artifacts(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        config = make_config(
            {"phase_a": {"entry_point": True, "outputs": ["doc"]}}
        )
        state = init_run("t-entry", "1.0.0", ["phase_a"], temp_dir)
        ctx, active_run = make_ctx(state, temp_dir, config)

        result = handle_start_phase({"phase": "phase_a"}, ctx)
        parsed = json.loads(result.json)

        assert parsed["input_artifacts"] == []
        assert active_run[0].phases["phase_a"].input_artifacts == []

        disk = load_run_state(temp_dir)
        assert disk is not None
        assert disk.phases["phase_a"].input_artifacts == []

    def test_deduplicates_paths_when_two_upstream_produced_same_path(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        config = make_config(
            {
                "up_a": {"entry_point": True, "outputs": ["doc"]},
                "up_b": {"entry_point": True, "outputs": ["doc"]},
                "phase_b": {"entry_point": False, "inputs": ["doc"]},
            },
            [
                EdgeDefinition(from_="up_a", to="phase_b"),
                EdgeDefinition(from_="up_b", to="phase_b"),
            ],
        )
        state = init_run(
            "t-dedup-int", "1.0.0", ["up_a", "up_b", "phase_b"], temp_dir
        )
        state = start_phase(state, "up_a", temp_dir)
        state = add_artifact(
            state, _artifact_ref("/tmp/dup.json", "up_a"), temp_dir
        )
        state = add_artifact(
            state, _artifact_ref("/tmp/only-a.json", "up_a"), temp_dir
        )
        state = complete_phase(state, "up_a", temp_dir)

        state = start_phase(state, "up_b", temp_dir)
        state = add_artifact(
            state, _artifact_ref("/tmp/dup.json", "up_b"), temp_dir
        )
        state = add_artifact(
            state, _artifact_ref("/tmp/only-b.json", "up_b"), temp_dir
        )
        state = complete_phase(state, "up_b", temp_dir)

        ctx, active_run = make_ctx(state, temp_dir, config)

        result = handle_start_phase({"phase": "phase_b"}, ctx)
        parsed = json.loads(result.json)

        assert parsed["input_artifacts"] == [
            "/tmp/dup.json",
            "/tmp/only-a.json",
            "/tmp/only-b.json",
        ]
        assert parsed["input_artifacts"].count("/tmp/dup.json") == 1

        disk = load_run_state(temp_dir)
        assert disk is not None
        assert disk.phases["phase_b"].input_artifacts == [
            "/tmp/dup.json",
            "/tmp/only-a.json",
            "/tmp/only-b.json",
        ]
        assert active_run[0].phases["phase_b"].input_artifacts == [
            "/tmp/dup.json",
            "/tmp/only-a.json",
            "/tmp/only-b.json",
        ]
