"""Port of ``legacy-node/tests/run-state-terminal.test.ts`` (11 cases).

Byte-exact behavioral parity tests for the DAG-aware terminal-status detection
(``pipeline_orchestrator.run_state.compute_run_terminal_status`` /
``finalize_run_terminal_status``) plus the inline run-finalization performed by
``complete_phase`` / ``fail_phase``. Each Node ``it(...)`` maps 1:1 to a
``test_*`` method here, grouped into classes mirroring the Node ``describe``
blocks: ``computeRunTerminalStatus`` (8 cases), ``finalizeRunTerminalStatus``
(1 case), and ``completePhase terminal transition`` (2 cases).

The success terminal state is ``'completed'`` (NOT ``'done'``), matching the
``RunState.status`` union and ``validate_run_state_integrity``.

All paths are OS-agnostic: ``state_dir`` comes from the ``tmp_path`` fixture and
config is built inline via :func:`make_config` (mirroring the Node ``makeConfig``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
from pipeline_orchestrator.run_state import (
    complete_phase,
    compute_run_terminal_status,
    fail_phase,
    finalize_run_terminal_status,
    init_run,
    load_run_state,
    skip_phase,
    start_phase,
)


def make_config(
    phases: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]] | None = None,
) -> PipelineConfig:
    """Build a minimal :class:`PipelineConfig` for testing.

    Mirrors the Node ``makeConfig`` helper: each ``phases`` value is a partial
    phase dict (defaults filled by :class:`PhaseDefinition`), and each ``edges``
    entry is a dict with the wire key ``from`` (mapped to ``EdgeDefinition.from_``).
    """
    if edges is None:
        edges = []
    full_phases: dict[str, PhaseDefinition] = {}
    for name, partial in phases.items():
        full_phases[name] = PhaseDefinition(
            id=name,
            description="",
            inputs=partial.get("inputs", []),
            outputs=partial.get("outputs", []),
            tools=[],
            entry_point=partial.get("entry_point", False),
            optional=partial.get("optional", False),
            input_mode=partial.get("input_mode"),
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


# ── computeRunTerminalStatus ─────────────────────────────────────────


class TestComputeRunTerminalStatus:
    """Port of the ``describe('computeRunTerminalStatus')`` block."""

    def test_returns_completed_when_all_phases_are_completed(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        config = make_config(
            {
                "discovery": {"entry_point": True},
                "curation": {"entry_point": False},
            }
        )
        state = init_run("test", "1.0.0", ["discovery", "curation"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        state = complete_phase(state, "discovery", temp_dir)
        state = start_phase(state, "curation", temp_dir)
        state = complete_phase(state, "curation", temp_dir)

        assert compute_run_terminal_status(state, config) == "completed"

    def test_returns_completed_when_some_phases_pending_but_unreachable(
        self, tmp_path: Path
    ) -> None:
        # research_discovery requires a "research_brief" input that no completed
        # phase produces. Its DAG branch is untaken, so it can never run —
        # terminal status should still be 'completed'.
        temp_dir = str(tmp_path)
        config = make_config(
            {
                "discovery": {"entry_point": True},
                "research_discovery": {
                    "entry_point": False,
                    "inputs": ["research_brief"],
                },
            }
        )
        state = init_run(
            "test", "1.0.0", ["discovery", "research_discovery"], temp_dir
        )
        state = start_phase(state, "discovery", temp_dir)
        state = complete_phase(state, "discovery", temp_dir)

        # research_discovery is still pending with no satisfiable inputs
        assert state.phases["research_discovery"].status == "pending"
        assert compute_run_terminal_status(state, config) == "completed"

    def test_returns_running_when_a_phase_is_in_progress(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        config = make_config(
            {
                "discovery": {"entry_point": True},
                "curation": {"entry_point": False},
            }
        )
        state = init_run("test", "1.0.0", ["discovery", "curation"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)

        assert compute_run_terminal_status(state, config) == "running"

    def test_returns_running_when_more_phases_can_still_run(
        self, tmp_path: Path
    ) -> None:
        # Two entry-point phases, only one completed — the other is still eligible.
        temp_dir = str(tmp_path)
        config = make_config(
            {
                "discovery": {"entry_point": True},
                "curation": {"entry_point": True},
            }
        )
        state = init_run("test", "1.0.0", ["discovery", "curation"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        state = complete_phase(state, "discovery", temp_dir)

        assert compute_run_terminal_status(state, config) == "running"

    def test_returns_failed_when_any_phase_failed_even_if_others_completed(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        config = make_config(
            {
                "discovery": {"entry_point": True},
                "curation": {"entry_point": True},
            }
        )
        state = init_run("test", "1.0.0", ["discovery", "curation"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        state = complete_phase(state, "discovery", temp_dir)
        state = start_phase(state, "curation", temp_dir)
        state = fail_phase(state, "curation", "boom", temp_dir)

        assert compute_run_terminal_status(state, config) == "failed"

    def test_returns_running_for_freshly_initialized_run_with_entry_point(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        config = make_config(
            {
                "discovery": {"entry_point": True},
            }
        )
        state = init_run("test", "1.0.0", ["discovery"], temp_dir)
        assert compute_run_terminal_status(state, config) == "running"

    def test_returns_completed_when_every_phase_is_skipped(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        config = make_config(
            {
                "discovery": {"entry_point": True},
            }
        )
        state = init_run("test", "1.0.0", ["discovery"], temp_dir)
        state = skip_phase(state, "discovery", temp_dir)
        assert compute_run_terminal_status(state, config) == "completed"

    def test_returns_completed_when_terminal_phase_completes_with_pending_feedback_loop(
        self, tmp_path: Path
    ) -> None:
        # Simulates: implementation_scaffold (terminal) completed, but
        # research_discovery (entry_point=True, reachable via validation feedback
        # edge) is still pending.
        temp_dir = str(tmp_path)
        config = make_config(
            {
                "validation": {"entry_point": True},
                "implementation_scaffold": {
                    "entry_point": False,
                    "inputs": ["design-spec"],
                },
                "research_discovery": {"entry_point": True},
            },
            [
                # feedback edge makes research_discovery reachable
                {"from": "validation", "to": "research_discovery"},
                # no edges FROM implementation_scaffold — it is the terminal node
            ],
        )
        state = init_run(
            "test",
            "1.0.0",
            ["validation", "implementation_scaffold", "research_discovery"],
            temp_dir,
        )
        state = start_phase(state, "validation", temp_dir)
        state = complete_phase(state, "validation", temp_dir)
        state = start_phase(state, "implementation_scaffold", temp_dir)
        state = complete_phase(state, "implementation_scaffold", temp_dir)

        # research_discovery is pending and technically reachable via the feedback
        # edge, but implementation_scaffold has no outgoing edges — run must be
        # 'completed'.
        assert state.phases["research_discovery"].status == "pending"
        assert compute_run_terminal_status(state, config) == "completed"


# ── finalizeRunTerminalStatus ────────────────────────────────────────


class TestFinalizeRunTerminalStatus:
    """Port of the ``describe('finalizeRunTerminalStatus')`` block."""

    def test_sets_status_and_completed_at_and_persists_to_disk(
        self, tmp_path: Path
    ) -> None:
        # Build a multi-phase run where the inline allDone block doesn't fire,
        # so completed_at isn't populated before finalize runs.
        temp_dir = str(tmp_path)
        state = init_run("test", "1.0.0", ["discovery", "curation"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        state = complete_phase(state, "discovery", temp_dir)

        # curation is still pending; run status should be 'running' and
        # completed_at unset
        assert state.status == "running"
        assert state.completed_at is None

        finalized = finalize_run_terminal_status(state, "completed", temp_dir)

        assert finalized.status == "completed"
        assert finalized.completed_at, "completed_at should be populated"

        loaded = load_run_state(temp_dir)
        assert loaded is not None
        assert loaded.status == "completed"
        assert loaded.completed_at == finalized.completed_at


# ── completePhase terminal transition (direct + via handler) ────────


class TestCompletePhaseTerminalTransition:
    """Port of the ``describe('completePhase terminal transition ...')`` block."""

    def test_complete_phase_alone_populates_completed_at_when_all_finish(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        state = init_run("test", "1.0.0", ["discovery"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        state = complete_phase(state, "discovery", temp_dir)

        # The inline "all done" block inside complete_phase should flip both
        # status AND completed_at for the happy path.
        assert state.status == "completed"
        assert state.completed_at, (
            "run.completed_at should be set after the last phase completes"
        )

        loaded = load_run_state(temp_dir)
        assert loaded is not None
        assert loaded.status == "completed"
        assert loaded.completed_at == state.completed_at

    def test_fail_phase_populates_run_level_completed_at(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        state = init_run("test", "1.0.0", ["discovery"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        state = fail_phase(state, "discovery", "boom", temp_dir)

        assert state.status == "failed"
        assert state.completed_at, (
            "run.completed_at should be set after a phase fails"
        )
