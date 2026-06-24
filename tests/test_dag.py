"""Port of ``legacy-node/tests/dag.test.ts`` (14 cases).

Byte-exact behavioral parity tests for the DAG resolver
(``pipeline_orchestrator.dag``). Each Node ``it(...)`` maps 1:1 to a ``test_*``
method here, grouped into classes mirroring the Node ``describe`` blocks:
``resolveNextPhases`` (12 cases) and ``getPhaseInputSatisfaction`` (2 cases).

``resolve_input_artifacts`` is ported into ``dag.py`` (roadmap T1.4) but is NOT
exercised by these 14 cases — the Node ``dag.test.ts`` does not touch it. Its
dedicated tests land later in T6.3 (``input-artifact-resolution``, 6 cases),
exactly as ``remove_phase_artifacts`` was ported-then-tested-later in T1.2.

These cases are pure config logic (no filesystem): every config is constructed
inline via :func:`make_config`, mirroring the Node test's ``makeConfig`` shape.
"""

from __future__ import annotations

from typing import Any

from pipeline_orchestrator.dag import (
    get_phase_input_satisfaction,
    resolve_next_phases,
)
from pipeline_orchestrator.models import (
    DebateConfig,
    DebateOutputConfig,
    EdgeDefinition,
    KBDefaults,
    PhaseDefinition,
    PipelineConfig,
    PipelineMeta,
    StorageConfig,
)


def make_config(
    phases: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
) -> PipelineConfig:
    """Build a minimal :class:`PipelineConfig` for testing.

    Mirrors the Node ``makeConfig`` helper: each ``phases`` value is a partial
    phase dict (defaults filled by :class:`PhaseDefinition`), and each ``edges``
    entry is a dict with the wire key ``from`` (mapped to ``EdgeDefinition.from_``).
    """
    full_phases: dict[str, PhaseDefinition] = {}
    for name, partial in phases.items():
        full_phases[name] = PhaseDefinition(
            id=partial.get("id", name),
            description=partial.get("description", ""),
            inputs=partial.get("inputs", []),
            outputs=partial.get("outputs", []),
            tools=partial.get("tools", []),
            entry_point=partial.get("entry_point", False),
            input_mode=partial.get("input_mode"),
            output_mode=partial.get("output_mode"),
            optional=partial.get("optional"),
            reusable=partial.get("reusable"),
            model_tier=partial.get("model_tier"),
            model=partial.get("model"),
        )
    edge_defs = [
        EdgeDefinition(
            from_=e["from"],
            to=e["to"],
            note=e.get("note"),
            optional=e.get("optional"),
            input_as=e.get("input_as"),
            when_input=e.get("when_input"),
        )
        for e in edges
    ]
    return PipelineConfig(
        pipeline=PipelineMeta(id="test", version="1.0.0", description=""),
        phases=full_phases,
        edges=edge_defs,
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


# ── resolveNextPhases ────────────────────────────────────────────────


class TestResolveNextPhases:
    """Port of the ``describe('resolveNextPhases')`` block."""

    def test_returns_entry_points_when_no_phases_completed(self) -> None:
        config = make_config(
            {
                "discovery": {
                    "inputs": ["research-manifest"],
                    "outputs": ["raw-collection"],
                    "entry_point": True,
                },
                "curation": {
                    "inputs": ["raw-collection"],
                    "outputs": ["curated-collection"],
                    "entry_point": True,
                },
            },
            [{"from": "discovery", "to": "curation"}],
        )

        next_phases = resolve_next_phases(config, [], ["research-manifest"])
        assert "discovery" in next_phases

    def test_returns_downstream_phase_after_predecessor_completes(self) -> None:
        config = make_config(
            {
                "discovery": {
                    "inputs": ["research-manifest"],
                    "outputs": ["raw-collection"],
                    "entry_point": True,
                },
                "curation": {
                    "inputs": ["raw-collection"],
                    "outputs": ["curated-collection"],
                    "entry_point": True,
                },
            },
            [{"from": "discovery", "to": "curation"}],
        )

        next_phases = resolve_next_phases(
            config, ["discovery"], ["research-manifest", "raw-collection"]
        )
        assert "curation" in next_phases
        assert "discovery" not in next_phases

    def test_skips_non_entry_point_phases_without_required_inputs(self) -> None:
        config = make_config(
            {
                "discovery": {
                    "inputs": ["research-manifest"],
                    "outputs": ["raw-collection"],
                    "entry_point": True,
                },
                "synthesis": {
                    "inputs": ["curated-collection", "knowledge-overview"],
                    "input_mode": "any",
                    "outputs": ["design-spec"],
                    "entry_point": False,
                },
            },
            [{"from": "discovery", "to": "synthesis"}],
        )

        next_phases = resolve_next_phases(config, [], ["research-manifest"])
        assert "discovery" in next_phases
        assert (
            "synthesis" not in next_phases
        ), "non-entry-point phase without inputs should be skipped"

    def test_entry_point_phases_bypass_input_satisfaction(self) -> None:
        config = make_config(
            {
                "discovery": {
                    "inputs": ["research-manifest"],
                    "outputs": ["raw-collection"],
                    "entry_point": True,
                },
                "synthesis": {
                    "inputs": ["curated-collection", "knowledge-overview"],
                    "input_mode": "any",
                    "outputs": ["design-spec"],
                    "entry_point": True,
                },
            },
            [],
        )

        next_phases = resolve_next_phases(config, [], [])
        assert (
            "discovery" in next_phases
        ), "entry-point phase available even with no artifacts"
        assert (
            "synthesis" in next_phases
        ), "entry-point phase available even with no matching inputs"

    def test_handles_any_input_mode(self) -> None:
        config = make_config(
            {
                "synthesis": {
                    "inputs": ["curated-collection", "knowledge-overview"],
                    "input_mode": "any",
                    "outputs": ["design-spec"],
                    "entry_point": True,
                },
            },
            [],
        )

        next_phases = resolve_next_phases(config, [], ["curated-collection"])
        assert "synthesis" in next_phases

    def test_handles_optional_edges_does_not_block_downstream(self) -> None:
        config = make_config(
            {
                "codebase": {
                    "inputs": ["codebase-path"],
                    "outputs": ["codebase-requirements"],
                    "entry_point": True,
                    "optional": True,
                },
                "synthesis": {
                    "inputs": ["curated-collection", "codebase-requirements"],
                    "input_mode": "any",
                    "outputs": ["design-spec"],
                    "entry_point": True,
                },
            },
            [{"from": "codebase", "to": "synthesis", "optional": True}],
        )

        next_phases = resolve_next_phases(config, [], ["curated-collection"])
        assert "synthesis" in next_phases

    def test_does_not_return_already_completed_phases(self) -> None:
        config = make_config(
            {
                "discovery": {
                    "inputs": ["research-manifest"],
                    "outputs": ["raw-collection"],
                    "entry_point": True,
                },
            },
            [],
        )

        next_phases = resolve_next_phases(
            config, ["discovery"], ["research-manifest", "raw-collection"]
        )
        assert "discovery" not in next_phases

    def test_respects_skip_list(self) -> None:
        config = make_config(
            {
                "discovery": {
                    "inputs": ["research-manifest"],
                    "outputs": ["raw-collection"],
                    "entry_point": True,
                },
                "curation": {
                    "inputs": ["raw-collection"],
                    "outputs": ["curated-collection"],
                    "entry_point": True,
                },
            },
            [{"from": "discovery", "to": "curation"}],
        )

        next_phases = resolve_next_phases(
            config, ["discovery"], ["raw-collection"], ["curation"]
        )
        assert "curation" not in next_phases

    def test_entry_point_with_unregistered_input_types_appears(self) -> None:
        config = make_config(
            {
                "ingestion": {
                    "inputs": ["file-paths"],
                    "outputs": ["raw-collection"],
                    "entry_point": True,
                },
                "curation": {
                    "inputs": ["raw-collection"],
                    "outputs": ["curated-collection"],
                    "entry_point": True,
                },
                "synthesis": {
                    "inputs": ["curated-collection", "knowledge-overview"],
                    "input_mode": "any",
                    "outputs": ["design-spec"],
                    "entry_point": True,
                },
            },
            [
                {"from": "ingestion", "to": "curation"},
                {"from": "curation", "to": "synthesis"},
            ],
        )

        # No artifacts available at all — entry-point phases must still appear
        next_phases = resolve_next_phases(config, [], [])
        assert (
            "ingestion" in next_phases
        ), 'entry-point phase with unregistered input "file-paths" must appear'
        assert (
            "curation" in next_phases
        ), 'entry-point phase with unregistered input "raw-collection" must appear'
        assert (
            "synthesis" in next_phases
        ), "entry-point phase with unregistered inputs must appear"

    def test_non_entry_point_with_unregistered_inputs_does_not_appear(self) -> None:
        config = make_config(
            {
                "discovery": {
                    "inputs": ["research-manifest"],
                    "outputs": ["raw-collection"],
                    "entry_point": True,
                },
                "debate": {
                    "inputs": ["knowledge-overview", "design-spec"],
                    "input_mode": "one_of_primary",
                    "outputs": ["debate-transcript"],
                    "entry_point": False,
                },
            },
            [{"from": "discovery", "to": "debate"}],
        )

        next_phases = resolve_next_phases(config, [], [])
        assert "discovery" in next_phases, "entry-point phase should appear"
        assert (
            "debate" not in next_phases
        ), "non-entry-point phase with missing inputs must NOT appear"

    def test_entry_point_with_dag_edges_no_predecessors_still_appears(self) -> None:
        config = make_config(
            {
                "codebase": {
                    "inputs": ["codebase-path"],
                    "outputs": ["codebase-requirements"],
                    "entry_point": True,
                    "optional": True,
                },
                "validation": {
                    "inputs": ["design-spec", "codebase-requirements"],
                    "input_mode": "required_optional",
                    "outputs": ["design-spec"],
                    "entry_point": True,
                },
            },
            [
                {"from": "codebase", "to": "validation", "optional": True},
            ],
        )

        # No artifacts, no completed phases — entry-point with incoming optional
        # edge must still appear
        next_phases = resolve_next_phases(config, [], [])
        assert (
            "codebase" in next_phases
        ), "entry-point phase must appear with no artifacts"
        assert (
            "validation" in next_phases
        ), "entry-point phase with only optional incoming edges must appear"

    def test_skipped_phases_propagate_edges_to_downstream(self) -> None:
        config = make_config(
            {
                "phase_a": {
                    "inputs": ["input-a"],
                    "outputs": ["output-a"],
                    "entry_point": True,
                },
                "phase_b": {"inputs": ["output-a"], "outputs": ["output-b"]},
                "phase_c": {"inputs": ["output-b"], "outputs": ["output-c"]},
            },
            [
                {"from": "phase_a", "to": "phase_b"},
                {"from": "phase_b", "to": "phase_c"},
            ],
        )

        # phase_a completed, phase_b skipped (done externally), output-b available
        next_phases = resolve_next_phases(
            config,
            ["phase_a"],
            ["input-a", "output-a", "output-b"],
            ["phase_b"],
        )
        assert (
            "phase_a" not in next_phases
        ), "phase_a should not appear (already completed)"
        assert "phase_b" not in next_phases, "phase_b should not appear (skipped)"
        assert (
            "phase_c" in next_phases
        ), "phase_c should be reachable via skipped phase_b edge"


# ── getPhaseInputSatisfaction ────────────────────────────────────────


class TestGetPhaseInputSatisfaction:
    """Port of the ``describe('getPhaseInputSatisfaction')`` block."""

    def test_returns_satisfied_inputs_for_a_phase(self) -> None:
        config = make_config(
            {
                "synthesis": {
                    "inputs": ["curated-collection", "knowledge-overview"],
                    "input_mode": "any",
                    "outputs": ["design-spec"],
                    "entry_point": True,
                },
            },
            [],
        )

        result = get_phase_input_satisfaction(
            config.phases["synthesis"], ["curated-collection"]
        )
        assert result.satisfied == ["curated-collection"]
        assert result.missing == ["knowledge-overview"]
        assert result.can_run is True

    def test_returns_can_run_false_when_no_inputs_and_mode_not_any(self) -> None:
        config = make_config(
            {
                "curation": {
                    "inputs": ["raw-collection"],
                    "outputs": ["curated-collection"],
                    "entry_point": True,
                },
            },
            [],
        )

        result = get_phase_input_satisfaction(config.phases["curation"], [])
        assert result.can_run is False
        assert result.missing == ["raw-collection"]
