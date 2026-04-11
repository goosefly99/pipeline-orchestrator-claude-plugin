// handoff.ts — Phase-boundary session handoff generation

import type { RunState, PipelineConfig, QualityGate } from './types.ts'

/** Compact handoff payload for starting a fresh agent session. */
export interface HandoffPayload {
  run_id: string
  pipeline: { id: string; version: string }
  phases: Array<{
    name: string
    status: string
    artifact_paths: string[]
    model_tier?: string
    input_artifacts?: string[]
  }>
  dag_edges: Array<{ from: string; to: string }>
  completed_phases: string[]
  in_progress_phases: string[]
  next_available_phases: string[]
  quality_gates: Array<{
    phase: string
    on_failure: string
    check_count: number
  }>
  suggested_instruction: string
}

/**
 * Generate a compact handoff payload for starting a new agent session
 * at a phase boundary. Total target: <5K tokens (~20K chars).
 *
 * The handoff contains only references (never artifact content):
 * - Run identity and pipeline config summary
 * - Phase statuses with artifact paths
 * - DAG edges for dependency understanding
 * - Next available phases
 * - Quality gate criteria summaries
 * - Suggested opening instruction
 */
export function generateHandoff(
  runState: RunState,
  config: PipelineConfig,
  qualityGates: QualityGate[],
  nextPhases: string[],
): HandoffPayload {
  // Build compact phase summaries
  const phases = Object.entries(runState.phases).map(([name, p]) => {
    const artifactPaths = runState.available_artifacts
      .filter(a => a.phase === name)
      .map(a => a.path)
    const phaseDef = config.phases[name]
    const persistedInputs = p.input_artifacts ?? []
    return {
      name,
      status: p.status,
      artifact_paths: artifactPaths,
      ...(phaseDef?.model_tier ? { model_tier: phaseDef.model_tier } : {}),
      ...(persistedInputs.length > 0 ? { input_artifacts: persistedInputs } : {}),
    }
  })

  const completedPhases = Object.entries(runState.phases)
    .filter(([, p]) => p.status === 'completed')
    .map(([name]) => name)

  const inProgressPhases = Object.entries(runState.phases)
    .filter(([, p]) => p.status === 'in_progress')
    .map(([name]) => name)

  // Compact DAG edges
  const dagEdges = config.edges.map(e => ({ from: e.from, to: e.to }))

  // Quality gate summaries (compact)
  const gatesSummary = qualityGates.map(g => ({
    phase: g.phase,
    on_failure: g.on_failure,
    check_count: g.checks.length,
  }))

  // Generate suggested instruction
  let suggestedInstruction: string
  if (inProgressPhases.length > 0) {
    suggestedInstruction = `Continue working on phase "${inProgressPhases[0]}". Call pipeline_run_status to verify current state.`
  } else if (nextPhases.length > 0) {
    suggestedInstruction = `Start phase "${nextPhases[0]}". Call pipeline_run_status first to confirm state, then pipeline_start_phase.`
  } else if (runState.status === 'completed') {
    suggestedInstruction = 'All phases complete. Call pipeline_validate_run for final validation.'
  } else {
    suggestedInstruction = 'Call pipeline_run_status to assess current state and determine next steps.'
  }

  return {
    run_id: runState.run_id,
    pipeline: { id: config.pipeline.id, version: config.pipeline.version },
    phases,
    dag_edges: dagEdges,
    completed_phases: completedPhases,
    in_progress_phases: inProgressPhases,
    next_available_phases: nextPhases,
    quality_gates: gatesSummary,
    suggested_instruction: suggestedInstruction,
  }
}
