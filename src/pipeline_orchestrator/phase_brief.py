"""Self-contained subagent brief generation (port of ``phase-brief.ts``).

Pure builder: given a phase name + run state + config + quality gates, returns a
:class:`PhaseBrief` dict carrying everything a subagent needs to execute the
phase without any further context from the main conversation.

Undefined-drop parity (§8 trap #2): conditional keys (``model_tier`` /
``resolved_model`` / ``run_data_dir`` / ``quality_gate``) are OMITTED from the
returned dict — never set to ``None`` — mirroring the TS ``...(cond ? {k:v} : {})``
spreads and the ``quality_gate`` ``undefined`` when no gate matches.
"""

from __future__ import annotations

from typing import Any

from pipeline_orchestrator.models import (
    PhaseDefinition,
    PipelineConfig,
    QualityGate,
    RunState,
)


def generate_phase_brief(
    phase_name: str,
    run_state: RunState,
    config: PipelineConfig,
    quality_gates: list[QualityGate],
    *,
    resolved_model: str | None = None,
    run_data_dir: str | None = None,
) -> dict[str, Any]:
    """Generate a self-contained subagent brief for executing ``phase_name``.

    The brief contains everything needed to execute the phase without additional
    context from the main conversation. Raises ``ValueError`` (the Node code
    throws a plain ``Error``) byte-exactly when the phase is unknown.
    """
    phase_def = config.phases.get(phase_name)
    if not phase_def:
        raise ValueError(
            f'Phase "{phase_name}" not found in pipeline configuration'
        )

    # Prefer the persisted per-phase input_artifacts (written by handleStartPhase
    # via resolveInputArtifacts in run-state.ts). When that list is empty, fall
    # back to the legacy DAG/available-artifacts derivation so (a) pre-Fix-3.4
    # run-state files still produce sensible briefs and (b) entry_point phases
    # without upstream outputs still include any type-matched initial artifacts.
    phase_state = run_state.phases.get(phase_name)
    persisted_paths = phase_state.input_artifacts if phase_state else []
    if len(persisted_paths) > 0:
        input_artifacts = _hydrate_input_artifact_paths(persisted_paths, run_state)
    else:
        input_artifacts = _resolve_input_artifacts_from_available(
            phase_name, phase_def, run_state, config
        )

    # Determine expected output types and schema files.
    expected_types = phase_def.outputs
    schema_files = [f"{t}.json" for t in expected_types]

    # Find quality gate for this phase.
    gate = next((g for g in quality_gates if g.phase == phase_name), None)
    quality_gate_info: dict[str, Any] | None = None
    if gate is not None:
        quality_gate_info = {
            "on_failure": gate.on_failure,
            "checks": [
                {"check_type": c.check_type, "description": c.description}
                for c in gate.checks
            ],
        }

    # Build storage paths from config.
    storage_paths: dict[str, str] = {}
    storage = config.storage
    if storage is not None:
        for key, path in storage.paths.items():
            storage_paths[key] = path

    # Generate instruction.
    instruction = _build_instruction(
        phase_name,
        phase_def,
        input_artifacts,
        expected_types,
        gate,
        resolved_model=resolved_model,
        run_data_dir=run_data_dir,
    )

    brief: dict[str, Any] = {
        "phase_name": phase_name,
        "description": phase_def.description,
    }
    if phase_def.model_tier:
        brief["model_tier"] = phase_def.model_tier
    if resolved_model:
        brief["resolved_model"] = resolved_model
    if run_data_dir:
        brief["run_data_dir"] = run_data_dir
    brief["input_artifacts"] = input_artifacts
    brief["output_requirements"] = {
        "expected_types": expected_types,
        "schema_files": schema_files,
    }
    if quality_gate_info is not None:
        brief["quality_gate"] = quality_gate_info
    brief["storage_paths"] = storage_paths
    brief["tools"] = phase_def.tools
    brief["instruction"] = instruction
    return brief


def _hydrate_input_artifact_paths(
    paths: list[str],
    run_state: RunState,
) -> list[dict[str, str]]:
    """Hydrate stored artifact paths into the ``{type, path, phase}`` shape.

    Looks each path up in ``run_state.available_artifacts``. First entry wins if
    duplicates exist (preserves the earliest source phase for a given path).
    Paths that cannot be matched (defensive edge case) are still included with
    ``type: 'unknown'``, ``phase: 'unknown'`` so callers never silently lose data.
    """
    by_path: dict[str, dict[str, str]] = {}
    for ref in run_state.available_artifacts:
        if ref.path not in by_path:
            by_path[ref.path] = {"type": ref.type, "phase": ref.phase}

    result: list[dict[str, str]] = []
    for path in paths:
        hit = by_path.get(path)
        if hit is not None:
            result.append({"type": hit["type"], "path": path, "phase": hit["phase"]})
        else:
            result.append({"type": "unknown", "path": path, "phase": "unknown"})
    return result


def _resolve_input_artifacts_from_available(
    phase_name: str,
    phase_def: PhaseDefinition,
    run_state: RunState,
    config: PipelineConfig,
) -> list[dict[str, str]]:
    """Legacy fallback: resolve input artifacts from DAG deps + available artifacts.

    Used when ``run_state.phases[phase_name].input_artifacts`` is empty (e.g.
    pre-Fix-3.4 run-state files, or entry_point phases that consume type-matched
    initial artifacts with no upstream DAG edge).
    """
    input_types = set(phase_def.inputs)

    # Find upstream phases via DAG edges.
    upstream_phases = [e.from_ for e in config.edges if e.to == phase_name]

    inputs: list[dict[str, str]] = []
    for ref in run_state.available_artifacts:
        # Include if artifact type matches phase inputs.
        if ref.type in input_types:
            inputs.append({"type": ref.type, "path": ref.path, "phase": ref.phase})
            continue
        # Include if artifact is from an upstream phase.
        if ref.phase in upstream_phases:
            inputs.append({"type": ref.type, "path": ref.path, "phase": ref.phase})

    return inputs


def _build_instruction(
    phase_name: str,
    phase_def: PhaseDefinition,
    input_artifacts: list[dict[str, str]],
    expected_types: list[str],
    gate: QualityGate | None,
    *,
    resolved_model: str | None = None,
    run_data_dir: str | None = None,
) -> str:
    """Build a structured instruction string for the subagent (byte-exact)."""
    lines: list[str] = [
        f'Execute phase "{phase_name}": {phase_def.description}',
        "",
    ]

    if resolved_model:
        lines.append(f"Model: {resolved_model}")
    if run_data_dir:
        lines.append(f"Run data directory: {run_data_dir}")
    if resolved_model or run_data_dir:
        lines.append("")

    if len(input_artifacts) > 0:
        lines.append("Input artifacts:")
        for art in input_artifacts:
            lines.append(f"  - {art['type']}: {art['path']}")
        lines.append("")

    if len(expected_types) > 0:
        lines.append(f"Expected outputs: {', '.join(expected_types)}")
        lines.append(
            "Store outputs using pipeline_store_artifact with the correct "
            "artifact_type."
        )
        lines.append("")

    if gate is not None:
        lines.append(f"Quality gate ({gate.on_failure}):")
        for check in gate.checks:
            lines.append(f"  - {check.description}")
        lines.append("")

    lines.append(
        "When done, call pipeline_complete_phase to mark this phase as complete."
    )

    return "\n".join(lines)
