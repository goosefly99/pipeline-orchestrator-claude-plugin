"""Port of ``legacy-node/tests/quality-gates.test.ts`` (41 cases).

Byte-exact behavioral parity tests for
:mod:`pipeline_orchestrator.quality_gates`. Each Node ``it(...)`` maps 1:1 to a
``test_*`` method here, grouped into classes mirroring the Node ``describe``
blocks:

* ``checkFieldPresent`` ×10
* ``checkMinItems`` ×5
* ``checkCrossRefValid`` ×5
* ``runCheck`` ×3
* ``loadArtifactForGate`` ×3
* ``runGateChecks`` ×4
* ``loadQualityGates`` ×3 (the loader lives in ``toml_loader``)
* ``removePhaseArtifacts`` ×4 (the rollback helper lives in ``run_state``)
* ``bundled quality-gates.toml`` ×1
* ``gate_evaluated events (Fix 3.6)`` ×3

The 3 ``gate_evaluated`` cases drive ``handleCompletePhase`` from
``lifecycle-handlers.ts`` — the lifecycle-handler layer is a **T6.2** deliverable
(roadmap Fix 3.6 lives in ``lifecycle-handlers.ts`` / ``artifact-handlers.ts``,
NOT ``quality-gates.ts``). They are represented 1:1 here but skipped, deferred to
T6.2 where ``handle_complete_phase`` and its ``gate_evaluated`` event emission
land. The pure gate engine they exercise is fully covered by the isolation cases
above.

Fixtures mirror the Node ``beforeEach`` ``mkdtempSync`` via the ``tmp_path``
fixture; artifact files are written with :func:`json.dump`. All paths are
OS-agnostic (``os.path.join`` over ``tmp_path``) — never hardcoded slashes.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from pipeline_orchestrator.dag import InputSatisfaction
from pipeline_orchestrator.errors import ErrorClass, PipelineError
from pipeline_orchestrator.hooks import PreInitHookResult
from pipeline_orchestrator.models import (
    ArtifactRef,
    DebateConfig,
    DebateOutputConfig,
    KBDefaults,
    PhaseDefinition,
    PipelineConfig,
    PipelineMeta,
    QualityCheck,
    QualityGate,
    RunState,
    StorageConfig,
)
from pipeline_orchestrator.quality_gates import (
    check_cross_ref_valid,
    check_field_present,
    check_min_items,
    load_artifact_for_gate,
    run_check,
    run_gate_checks,
)
from pipeline_orchestrator.run_state import (
    RecommendedAction,
    RecoveryResult,
    add_artifact,
    complete_phase,
    init_run,
    load_run_state,
    remove_phase_artifacts,
    start_phase,
)
from pipeline_orchestrator.toml_loader import load_quality_gates
from pipeline_orchestrator.tools.lifecycle_tools import (
    LifecycleContext,
    handle_complete_phase,
)

# ── Fixtures / helpers ────────────────────────────────────────────────


@pytest.fixture
def temp_dir(tmp_path: Path) -> str:
    """A fresh temp directory (mirrors the Node ``mkdtempSync`` beforeEach)."""
    return str(tmp_path)


def _now_iso() -> str:
    """ISO 8601 UTC timestamp (mirrors the Node ``new Date().toISOString()``)."""
    return datetime.now(UTC).isoformat()


def _write_json(path: str, obj: Any) -> None:
    """Write ``obj`` as JSON to ``path`` (mirrors the Node ``writeFileSync``)."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


# ── checkFieldPresent ────────────────────────────────────────────────


class TestCheckFieldPresent:
    def test_passes_when_field_exists_and_is_non_empty_string(self) -> None:
        result = check_field_present({"name": "test"}, {"field": "name"})
        assert result.passed is True
        assert result.check_type == "field_present"

    def test_passes_when_field_exists_and_is_non_empty_array(self) -> None:
        result = check_field_present({"items": [1, 2, 3]}, {"field": "items"})
        assert result.passed is True

    def test_fails_when_field_is_missing(self) -> None:
        result = check_field_present({"name": "test"}, {"field": "missing"})
        assert result.passed is False
        assert "missing" in result.message

    def test_fails_when_field_is_null(self) -> None:
        result = check_field_present({"name": None}, {"field": "name"})
        assert result.passed is False

    def test_fails_when_field_is_empty_string(self) -> None:
        result = check_field_present({"name": "  "}, {"field": "name"})
        assert result.passed is False
        assert "empty" in result.message

    def test_fails_when_field_is_empty_array(self) -> None:
        result = check_field_present({"items": []}, {"field": "items"})
        assert result.passed is False
        assert "empty array" in result.message

    def test_fails_when_field_param_is_missing(self) -> None:
        result = check_field_present({"name": "test"}, {})
        assert result.passed is False
        assert "misconfigured" in result.message

    def test_resolves_dot_notation_paths_passes_when_nested_field_present(
        self,
    ) -> None:
        artifact = {"architecture": {"components": ["A", "B"]}}
        result = check_field_present(artifact, {"field": "architecture.components"})
        assert result.passed is True

    def test_resolves_dot_notation_paths_fails_when_nested_field_missing(
        self,
    ) -> None:
        artifact = {"architecture": {"title": "foo"}}  # no 'components'
        result = check_field_present(artifact, {"field": "architecture.components"})
        assert result.passed is False
        assert "architecture.components" in result.message

    def test_design_spec_with_only_architecture_components_passes_the_gate(
        self,
    ) -> None:
        artifact = {
            "spec_id": "test",
            "title": "T",
            "architecture": {
                "components": [
                    {"name": "C1", "description": "d", "responsibilities": []}
                ]
            },
        }
        result = check_field_present(artifact, {"field": "architecture.components"})
        assert result.passed is True


# ── checkMinItems ────────────────────────────────────────────────────


class TestCheckMinItems:
    def test_passes_when_array_has_at_least_min_items(self) -> None:
        result = check_min_items({"items": [1, 2, 3]}, {"field": "items", "min": 2})
        assert result.passed is True
        assert "3 items" in result.message

    def test_passes_when_array_has_exactly_min_items(self) -> None:
        result = check_min_items({"items": [1, 2]}, {"field": "items", "min": 2})
        assert result.passed is True

    def test_fails_when_array_has_fewer_than_min_items(self) -> None:
        result = check_min_items({"items": [1]}, {"field": "items", "min": 3})
        assert result.passed is False
        assert "1 items" in result.message
        assert "minimum: 3" in result.message

    def test_fails_when_field_is_not_an_array(self) -> None:
        result = check_min_items({"items": "not-array"}, {"field": "items", "min": 1})
        assert result.passed is False
        assert "not an array" in result.message

    def test_fails_when_params_are_missing(self) -> None:
        result = check_min_items({"items": [1]}, {})
        assert result.passed is False
        assert "misconfigured" in result.message


# ── checkCrossRefValid ───────────────────────────────────────────────


class TestCheckCrossRefValid:
    def test_passes_when_all_cross_references_are_valid_array_target(self) -> None:
        artifact = {"refs": ["a", "b"]}
        target_artifact = {"ids": ["a", "b", "c"]}
        all_artifacts = {"target-type": target_artifact}

        result = check_cross_ref_valid(
            artifact,
            {
                "source_field": "refs",
                "target_artifact_type": "target-type",
                "target_field": "ids",
            },
            all_artifacts,
        )
        assert result.passed is True

    def test_passes_when_all_cross_references_are_valid_object_target(self) -> None:
        artifact = {"refs": ["key1", "key2"]}
        target_artifact = {"items": {"key1": "v1", "key2": "v2", "key3": "v3"}}
        all_artifacts = {"target-type": target_artifact}

        result = check_cross_ref_valid(
            artifact,
            {
                "source_field": "refs",
                "target_artifact_type": "target-type",
                "target_field": "items",
            },
            all_artifacts,
        )
        assert result.passed is True

    def test_fails_when_cross_references_are_invalid(self) -> None:
        artifact = {"refs": ["a", "missing"]}
        target_artifact = {"ids": ["a", "b"]}
        all_artifacts = {"target-type": target_artifact}

        result = check_cross_ref_valid(
            artifact,
            {
                "source_field": "refs",
                "target_artifact_type": "target-type",
                "target_field": "ids",
            },
            all_artifacts,
        )
        assert result.passed is False
        assert "1 value(s)" in result.message

    def test_fails_when_target_artifact_is_not_found(self) -> None:
        artifact = {"refs": ["a"]}
        all_artifacts: dict[str, dict[str, Any]] = {}

        result = check_cross_ref_valid(
            artifact,
            {
                "source_field": "refs",
                "target_artifact_type": "missing-type",
                "target_field": "ids",
            },
            all_artifacts,
        )
        assert result.passed is False
        assert "not found" in result.message

    def test_fails_when_params_are_missing(self) -> None:
        result = check_cross_ref_valid({}, {}, {})
        assert result.passed is False
        assert "misconfigured" in result.message


# ── runCheck dispatcher ──────────────────────────────────────────────


class TestRunCheck:
    def test_dispatches_to_field_present_check(self) -> None:
        check = QualityCheck(
            check_type="field_present", description="test", params={"field": "name"}
        )
        result = run_check(check, {"name": "value"}, {})
        assert result.passed is True
        assert result.check_type == "field_present"

    def test_dispatches_to_min_items_check(self) -> None:
        check = QualityCheck(
            check_type="min_items",
            description="test",
            params={"field": "items", "min": 1},
        )
        result = run_check(check, {"items": [1, 2]}, {})
        assert result.passed is True
        assert result.check_type == "min_items"

    def test_returns_failure_for_unknown_check_type(self) -> None:
        check = QualityCheck(
            check_type="unknown_check",  # type: ignore[arg-type]
            description="test",
            params={},
        )
        result = run_check(check, {}, {})
        assert result.passed is False
        assert "Unknown check type" in result.message


# ── loadArtifactForGate ──────────────────────────────────────────────


class TestLoadArtifactForGate:
    def test_loads_a_valid_json_artifact_from_disk(self, temp_dir: str) -> None:
        path = os.path.join(temp_dir, "artifact.json")
        _write_json(path, {"name": "test", "items": [1, 2]})

        result = load_artifact_for_gate(path)
        assert result is not None
        assert result["name"] == "test"

    def test_returns_null_for_nonexistent_file(self, temp_dir: str) -> None:
        result = load_artifact_for_gate(os.path.join(temp_dir, "nonexistent.json"))
        assert result is None

    def test_returns_null_for_invalid_json(self, temp_dir: str) -> None:
        path = os.path.join(temp_dir, "bad.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ broken json")

        result = load_artifact_for_gate(path)
        assert result is None


# ── runGateChecks orchestrator ───────────────────────────────────────


class TestRunGateChecks:
    def test_returns_passed_true_when_all_checks_pass(self, temp_dir: str) -> None:
        artifact_path = os.path.join(temp_dir, "artifact.json")
        _write_json(artifact_path, {"sources": ["s1"], "items": [1, 2, 3]})

        gate = QualityGate(
            phase="curation",
            on_failure="block",
            checks=[
                QualityCheck(
                    check_type="field_present",
                    description="sources present",
                    params={"field": "sources"},
                ),
                QualityCheck(
                    check_type="min_items",
                    description="min items",
                    params={"field": "items", "min": 1},
                ),
            ],
        )

        refs = [
            ArtifactRef(
                type="curated-collection",
                path=artifact_path,
                phase="curation",
                created_at=_now_iso(),
            ),
        ]

        result = run_gate_checks(gate, refs)
        assert result.passed is True
        assert result.phase == "curation"
        assert len(result.results) == 2
        assert all(r.passed for r in result.results)

    def test_returns_passed_false_when_any_check_fails(self, temp_dir: str) -> None:
        artifact_path = os.path.join(temp_dir, "artifact.json")
        _write_json(artifact_path, {"items": []})

        gate = QualityGate(
            phase="curation",
            on_failure="block",
            checks=[
                QualityCheck(
                    check_type="field_present",
                    description="sources present",
                    params={"field": "sources"},
                ),
                QualityCheck(
                    check_type="min_items",
                    description="min items",
                    params={"field": "items", "min": 1},
                ),
            ],
        )

        refs = [
            ArtifactRef(
                type="curated-collection",
                path=artifact_path,
                phase="curation",
                created_at=_now_iso(),
            ),
        ]

        result = run_gate_checks(gate, refs)
        assert result.passed is False
        failed = [r for r in result.results if not r.passed]
        assert len(failed) >= 1

    def test_fails_check_when_no_artifacts_available(self) -> None:
        gate = QualityGate(
            phase="curation",
            on_failure="block",
            checks=[
                QualityCheck(
                    check_type="field_present",
                    description="test",
                    params={"field": "name"},
                ),
            ],
        )

        result = run_gate_checks(gate, [])
        assert result.passed is False
        assert "No artifacts" in result.results[0].message

    def test_uses_artifact_type_param_to_select_correct_artifact(
        self, temp_dir: str
    ) -> None:
        art1_path = os.path.join(temp_dir, "art1.json")
        art2_path = os.path.join(temp_dir, "art2.json")
        _write_json(art1_path, {"sources": ["s1"]})
        _write_json(art2_path, {"components": ["c1"]})

        gate = QualityGate(
            phase="design_synthesis",
            on_failure="block",
            checks=[
                QualityCheck(
                    check_type="field_present",
                    description="components check",
                    params={"field": "components", "artifact_type": "design-spec"},
                ),
            ],
        )

        refs = [
            ArtifactRef(
                type="curated-collection",
                path=art1_path,
                phase="design_synthesis",
                created_at=_now_iso(),
            ),
            ArtifactRef(
                type="design-spec",
                path=art2_path,
                phase="design_synthesis",
                created_at=_now_iso(),
            ),
        ]

        result = run_gate_checks(gate, refs)
        assert result.passed is True


# ── loadQualityGates (TOML parsing) ──────────────────────────────────


class TestLoadQualityGates:
    def test_returns_empty_array_for_nonexistent_file(self, temp_dir: str) -> None:
        gates = load_quality_gates(os.path.join(temp_dir, "nonexistent.toml"))
        assert gates == []

    def test_parses_a_valid_quality_gates_toml(self, temp_dir: str) -> None:
        toml = """
[[gates]]
phase = "curation"
on_failure = "block"

[[gates.checks]]
check_type = "field_present"
description = "Sources must be present"
[gates.checks.params]
field = "sources"
artifact_type = "curated-collection"

[[gates.checks]]
check_type = "min_items"
description = "At least 1 item"
[gates.checks.params]
field = "items"
min = 1

[[gates]]
phase = "validation"
on_failure = "warn"

[[gates.checks]]
check_type = "field_present"
description = "Results required"
[gates.checks.params]
field = "results"
"""
        toml_path = os.path.join(temp_dir, "quality-gates.toml")
        with open(toml_path, "w", encoding="utf-8") as fh:
            fh.write(toml)

        gates = load_quality_gates(toml_path)
        assert len(gates) == 2

        assert gates[0].phase == "curation"
        assert gates[0].on_failure == "block"
        assert len(gates[0].checks) == 2
        assert gates[0].checks[0].check_type == "field_present"
        assert gates[0].checks[0].params["field"] == "sources"
        assert gates[0].checks[1].check_type == "min_items"

        assert gates[1].phase == "validation"
        assert gates[1].on_failure == "warn"
        assert len(gates[1].checks) == 1

    def test_defaults_on_failure_to_warn_when_not_specified(
        self, temp_dir: str
    ) -> None:
        toml = """
[[gates]]
phase = "test"

[[gates.checks]]
check_type = "field_present"
description = "test"
[gates.checks.params]
field = "name"
"""
        toml_path = os.path.join(temp_dir, "gates.toml")
        with open(toml_path, "w", encoding="utf-8") as fh:
            fh.write(toml)

        gates = load_quality_gates(toml_path)
        assert gates[0].on_failure == "warn"


# ── removePhaseArtifacts (rollback) ──────────────────────────────────


class TestRemovePhaseArtifacts:
    def test_removes_all_artifacts_for_a_specific_phase(self, temp_dir: str) -> None:
        state = init_run(
            "rollback-test", "1.0.0", ["discovery", "curation"], temp_dir
        )
        state = add_artifact(
            state,
            ArtifactRef(
                type="raw-collection",
                path="raw/test1.json",
                phase="discovery",
                created_at=_now_iso(),
            ),
            temp_dir,
        )
        state = add_artifact(
            state,
            ArtifactRef(
                type="curated-collection",
                path="curated/test1.json",
                phase="curation",
                created_at=_now_iso(),
            ),
            temp_dir,
        )

        assert len(state.available_artifacts) == 2

        removed = remove_phase_artifacts(state, "curation", temp_dir)

        assert removed == ["curated/test1.json"]
        assert len(state.available_artifacts) == 1
        assert state.available_artifacts[0].phase == "discovery"
        assert state.phases["curation"].output_artifacts == []

    def test_preserves_artifacts_from_other_phases(self, temp_dir: str) -> None:
        state = init_run(
            "rollback-preserve", "1.0.0", ["discovery", "curation"], temp_dir
        )
        state = add_artifact(
            state,
            ArtifactRef(
                type="raw-collection",
                path="raw/test1.json",
                phase="discovery",
                created_at=_now_iso(),
            ),
            temp_dir,
        )
        state = add_artifact(
            state,
            ArtifactRef(
                type="raw-collection",
                path="raw/test2.json",
                phase="discovery",
                created_at=_now_iso(),
            ),
            temp_dir,
        )

        removed = remove_phase_artifacts(state, "curation", temp_dir)

        assert removed == []
        assert len(state.available_artifacts) == 2

    def test_persists_changes_to_disk_after_removal(self, temp_dir: str) -> None:
        state = init_run("rollback-persist", "1.0.0", ["discovery"], temp_dir)
        state = add_artifact(
            state,
            ArtifactRef(
                type="raw-collection",
                path="raw/test1.json",
                phase="discovery",
                created_at=_now_iso(),
            ),
            temp_dir,
        )

        remove_phase_artifacts(state, "discovery", temp_dir)

        loaded = load_run_state(temp_dir)
        assert loaded is not None
        assert len(loaded.available_artifacts) == 0

    def test_handles_phase_with_no_artifacts_gracefully(self, temp_dir: str) -> None:
        state = init_run("rollback-empty", "1.0.0", ["discovery"], temp_dir)

        removed = remove_phase_artifacts(state, "discovery", temp_dir)

        assert removed == []
        assert len(state.available_artifacts) == 0


# ── Bundled quality-gates.toml loading ───────────────────────────────


class TestBundledQualityGatesToml:
    def test_loads_the_bundled_quality_gates_toml_without_errors(self) -> None:
        gates_path = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "pipeline_orchestrator"
            / "pipeline"
            / "quality-gates.toml"
        )
        gates = load_quality_gates(gates_path)
        assert len(gates) > 0, "Expected at least one gate definition"

        # Each gate should have phase and checks.
        for gate in gates:
            assert gate.phase, "Gate must have a phase"
            assert len(gate.checks) > 0, (
                f'Gate for phase "{gate.phase}" must have at least one check'
            )
            assert gate.on_failure in ("warn", "block"), (
                'Gate on_failure must be "warn" or "block"'
            )


# ── gate_evaluated events (Fix 3.6) ──────────────────────────────────
#
# These 3 cases drive ``handle_complete_phase`` from ``lifecycle_tools.py`` —
# the gate-enforcement wiring (one ``gate_evaluated`` event per check, atomic
# ``remove_phase_artifacts`` rollback before raising ``gate_blocked``,
# warn-collects-warnings). Unskipped at T6.2 when ``handle_complete_phase`` and
# its event emission landed; ported 1:1 against the TS oracle.


def _make_gate_test_config() -> PipelineConfig:
    """Minimal single-phase (curation) PipelineConfig (Node ``makeGateTestConfig``)."""
    phases = {
        "curation": PhaseDefinition(
            id=0,
            description="",
            inputs=[],
            outputs=["curated-collection"],
            tools=[],
            entry_point=True,
            optional=False,
        ),
    }
    return PipelineConfig(
        pipeline=PipelineMeta(id="gate-evt-test", version="1.0.0", description=""),
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
            available_in=[], max_queries_per_phase=20, query_mode="proactive"
        ),
        schemas={},
        storage=StorageConfig(base_dir="test", paths={}),
    )


def _make_gate_test_ctx(
    initial_state: RunState,
    state_dir: str,
    gates: list[QualityGate],
) -> LifecycleContext:
    """Real-backed LifecycleContext wrapping run-state helpers + a gate list.

    ``run_gate_checks`` remains the real pure implementation (Node
    ``makeGateTestCtx``). ``active_run`` / ``active_run_dir`` are held in
    single-element lists so the callables can rebind them.
    """
    active_run: list[RunState | None] = [initial_state]
    active_run_dir: list[str] = [state_dir]

    def _set_active_run(s: RunState) -> None:
        active_run[0] = s

    def _complete_phase(s: RunState, phase: str, d: str) -> RunState:
        updated = complete_phase(s, phase, d)
        active_run[0] = updated
        return updated

    def _phase_input_sat(
        phase: PhaseDefinition, _types: list[str]
    ) -> InputSatisfaction:
        return InputSatisfaction(satisfied=[], missing=phase.inputs, can_run=True)

    return LifecycleContext(
        get_active_run=lambda: active_run[0],
        set_active_run=_set_active_run,
        get_active_run_dir=lambda: active_run_dir[0],
        set_active_run_dir=lambda d: active_run_dir.__setitem__(0, d),
        get_config=lambda: _make_gate_test_config(),
        set_project_root=lambda _root: None,
        get_storage_config=lambda *_a, **_k: StorageConfig(
            base_dir=state_dir, paths={}
        ),
        init_run=lambda *_a, **_k: initial_state,
        skip_phase=lambda s, _p, _d: s,
        add_artifact=lambda s, ref, d: add_artifact(s, ref, d),
        start_phase=lambda s, phase, d: start_phase(s, phase, d),
        complete_phase=_complete_phase,
        fail_phase=lambda s, _p, _e, _d: s,
        retry_phase=lambda s, _p, _d: s,
        resolve_next_phases=lambda *_a: [],
        get_phase_input_satisfaction=_phase_input_sat,
        load_run_state=lambda _d: None,
        recover_run=lambda _d: RecoveryResult(
            state=initial_state, recovered_phases=[], warnings=[]
        ),
        remove_phase_artifacts=lambda s, phase, d: remove_phase_artifacts(
            s, phase, d
        ),
        get_quality_gates=lambda: gates,
        run_gate_checks=lambda gate, refs: run_gate_checks(gate, refs),
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


def _read_events(run_dir: str) -> list[dict[str, Any]]:
    """Parse events.jsonl into a list of dicts (Node ``readEvents``)."""
    events_path = os.path.join(run_dir, "events.jsonl")
    if not os.path.exists(events_path):
        return []
    with open(events_path, encoding="utf-8") as fh:
        content = fh.read()
    events: list[dict[str, Any]] = []
    for line in content.split("\n"):
        trimmed = line.strip()
        if not trimmed:
            continue
        try:
            events.append(json.loads(trimmed))
        except (json.JSONDecodeError, ValueError):
            continue
    return events


class TestGateEvaluatedEventsFix36:
    def test_writes_one_gate_evaluated_event_per_check_for_passing_gate(
        self, temp_dir: str
    ) -> None:
        # Arrange: artifact on disk that satisfies both checks.
        artifact_path = os.path.join(temp_dir, "curated.json")
        _write_json(artifact_path, {"sources": ["s1", "s2"], "items": [1, 2, 3]})

        state = init_run("gate-evt-pass", "1.0.0", ["curation"], temp_dir)
        state = start_phase(state, "curation", temp_dir)
        state = add_artifact(
            state,
            ArtifactRef(
                type="curated-collection",
                path=artifact_path,
                phase="curation",
                created_at=_now_iso(),
            ),
            temp_dir,
        )

        gate = QualityGate(
            phase="curation",
            on_failure="block",
            checks=[
                QualityCheck(
                    check_type="field_present",
                    description="sources present",
                    params={
                        "field": "sources",
                        "artifact_type": "curated-collection",
                    },
                ),
                QualityCheck(
                    check_type="min_items",
                    description="min items",
                    params={
                        "field": "items",
                        "min": 1,
                        "artifact_type": "curated-collection",
                    },
                ),
            ],
        )

        ctx = _make_gate_test_ctx(state, temp_dir, [gate])

        handle_complete_phase({"phase": "curation"}, ctx)

        events = _read_events(temp_dir)
        gate_events = [
            e
            for e in events
            if e.get("event") == "gate_evaluated" and e.get("phase") == "curation"
        ]
        assert len(gate_events) == 2

        for evt in gate_events:
            details = evt["details"]
            assert details["gate_name"] == "curation"
            assert details["result"] == "pass"
            assert details["artifact_type"] == "curated-collection"

        check_types = sorted(e["details"]["check_type"] for e in gate_events)
        assert check_types == ["field_present", "min_items"]

    def test_writes_gate_evaluated_events_fail_before_blocking_gate_throws(
        self, temp_dir: str
    ) -> None:
        # Artifact missing the required field so field_present fails.
        artifact_path = os.path.join(temp_dir, "curated-bad.json")
        _write_json(artifact_path, {"items": [1]})

        state = init_run("gate-evt-fail", "1.0.0", ["curation"], temp_dir)
        state = start_phase(state, "curation", temp_dir)
        state = add_artifact(
            state,
            ArtifactRef(
                type="curated-collection",
                path=artifact_path,
                phase="curation",
                created_at=_now_iso(),
            ),
            temp_dir,
        )

        gate = QualityGate(
            phase="curation",
            on_failure="block",
            checks=[
                QualityCheck(
                    check_type="field_present",
                    description="sources present",
                    params={
                        "field": "sources",
                        "artifact_type": "curated-collection",
                    },
                ),
            ],
        )

        ctx = _make_gate_test_ctx(state, temp_dir, [gate])

        with pytest.raises(PipelineError) as exc_info:
            handle_complete_phase({"phase": "curation"}, ctx)
        assert exc_info.value.error_class == ErrorClass.gate_blocked

        events = _read_events(temp_dir)
        gate_events = [
            e
            for e in events
            if e.get("event") == "gate_evaluated" and e.get("phase") == "curation"
        ]
        assert len(gate_events) == 1
        details = gate_events[0]["details"]
        assert details["gate_name"] == "curation"
        assert details["check_type"] == "field_present"
        assert details["result"] == "fail"
        assert details["artifact_type"] == "curated-collection"

    def test_writes_gate_evaluated_events_warn_for_warn_mode_failure(
        self, temp_dir: str
    ) -> None:
        artifact_path = os.path.join(temp_dir, "curated-warn.json")
        _write_json(artifact_path, {"items": []})

        state = init_run("gate-evt-warn", "1.0.0", ["curation"], temp_dir)
        state = start_phase(state, "curation", temp_dir)
        state = add_artifact(
            state,
            ArtifactRef(
                type="curated-collection",
                path=artifact_path,
                phase="curation",
                created_at=_now_iso(),
            ),
            temp_dir,
        )

        gate = QualityGate(
            phase="curation",
            on_failure="warn",
            checks=[
                QualityCheck(
                    check_type="min_items",
                    description="min items",
                    params={
                        "field": "items",
                        "min": 1,
                        "artifact_type": "curated-collection",
                    },
                ),
            ],
        )

        ctx = _make_gate_test_ctx(state, temp_dir, [gate])

        # warn-mode: must NOT throw; should return an envelope + still emit event.
        handle_complete_phase({"phase": "curation"}, ctx)

        events = _read_events(temp_dir)
        gate_events = [
            e
            for e in events
            if e.get("event") == "gate_evaluated" and e.get("phase") == "curation"
        ]
        assert len(gate_events) == 1
        details = gate_events[0]["details"]
        assert details["result"] == "warn"
