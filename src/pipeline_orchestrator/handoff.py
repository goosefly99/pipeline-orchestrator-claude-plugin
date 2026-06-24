"""Phase-boundary session handoff generation (port of ``handoff.ts``).

Pure compact builder: a small (<5K-token, ~20K-char) paths-only payload for
starting a fresh agent session at a phase boundary. It contains only references
(never artifact content): run identity, phase statuses with artifact paths, DAG
edges, next available phases, quality-gate summaries, and an adaptive suggested
opening instruction.

Undefined-drop parity (§8 trap #2): per-phase ``model_tier`` / ``input_artifacts``
keys are OMITTED when absent/empty (mirroring the TS ``...(cond ? {k:v} : {})``
spreads), never set to ``None``.
"""

from __future__ import annotations

from typing import Any

from pipeline_orchestrator.models import PipelineConfig, QualityGate, RunState


def generate_handoff(
    run_state: RunState,
    config: PipelineConfig,
    quality_gates: list[QualityGate],
    next_phases: list[str],
) -> dict[str, Any]:
    """Generate a compact handoff payload for a new agent session.

    Total target: <5K tokens (~20K chars). Contains only references — never
    artifact content.
    """
    # Build compact phase summaries.
    phases: list[dict[str, Any]] = []
    for name, p in run_state.phases.items():
        artifact_paths = [
            a.path for a in run_state.available_artifacts if a.phase == name
        ]
        phase_def = config.phases.get(name)
        persisted_inputs = p.input_artifacts
        entry: dict[str, Any] = {
            "name": name,
            "status": p.status,
            "artifact_paths": artifact_paths,
        }
        if phase_def is not None and phase_def.model_tier:
            entry["model_tier"] = phase_def.model_tier
        if len(persisted_inputs) > 0:
            entry["input_artifacts"] = persisted_inputs
        phases.append(entry)

    completed_phases = [
        name for name, p in run_state.phases.items() if p.status == "completed"
    ]

    in_progress_phases = [
        name for name, p in run_state.phases.items() if p.status == "in_progress"
    ]

    # Compact DAG edges.
    dag_edges = [{"from": e.from_, "to": e.to} for e in config.edges]

    # Quality gate summaries (compact).
    gates_summary = [
        {
            "phase": g.phase,
            "on_failure": g.on_failure,
            "check_count": len(g.checks),
        }
        for g in quality_gates
    ]

    # Generate suggested instruction.
    suggested_instruction: str
    if len(in_progress_phases) > 0:
        suggested_instruction = (
            f'Continue working on phase "{in_progress_phases[0]}". '
            "Call pipeline_run_status to verify current state."
        )
    elif len(next_phases) > 0:
        suggested_instruction = (
            f'Start phase "{next_phases[0]}". Call pipeline_run_status first to '
            "confirm state, then pipeline_start_phase."
        )
    elif run_state.status == "completed":
        suggested_instruction = (
            "All phases complete. Call pipeline_validate_run for final validation."
        )
    else:
        suggested_instruction = (
            "Call pipeline_run_status to assess current state and determine next "
            "steps."
        )

    return {
        "run_id": run_state.run_id,
        "pipeline": {"id": config.pipeline.id, "version": config.pipeline.version},
        "phases": phases,
        "dag_edges": dag_edges,
        "completed_phases": completed_phases,
        "in_progress_phases": in_progress_phases,
        "next_available_phases": next_phases,
        "quality_gates": gates_summary,
        "suggested_instruction": suggested_instruction,
    }
