// phase-brief.ts — Self-contained subagent brief generation for phase execution

import type { RunState, PipelineConfig, QualityGate, PhaseDefinition } from './types.ts'

/** Structured phase brief for subagent delegation. */
export interface PhaseBrief {
  phase_name: string
  description: string
  model_tier?: string
  input_artifacts: Array<{
    type: string
    path: string
    phase: string
  }>
  output_requirements: {
    expected_types: string[]
    schema_files: string[]
  }
  quality_gate?: {
    on_failure: string
    checks: Array<{
      check_type: string
      description: string
    }>
  }
  storage_paths: Record<string, string>
  tools: string[]
  instruction: string
}

/**
 * Generate a self-contained subagent brief for executing a given phase.
 * The brief contains everything needed to execute the phase without
 * additional context from the main conversation.
 *
 * @param phaseName - Name of the phase to generate a brief for
 * @param runState - Current run state
 * @param config - Pipeline configuration
 * @param qualityGates - Quality gate configurations
 * @returns PhaseBrief ready to be passed as a subagent prompt
 */
export function generatePhaseBrief(
  phaseName: string,
  runState: RunState,
  config: PipelineConfig,
  qualityGates: QualityGate[],
): PhaseBrief {
  const phaseDef = config.phases[phaseName]
  if (!phaseDef) {
    throw new Error(`Phase "${phaseName}" not found in pipeline configuration`)
  }

  // Prefer the persisted per-phase input_artifacts (written by
  // handleStartPhase via resolveInputArtifacts in run-state.ts). When that
  // list is empty, fall back to the legacy DAG/available-artifacts derivation
  // so (a) pre-Fix-3.4 run-state files still produce sensible briefs and
  // (b) entry_point phases without upstream outputs still include any
  // type-matched initial artifacts the way they did before.
  const persistedPaths = runState.phases[phaseName]?.input_artifacts ?? []
  const inputArtifacts = persistedPaths.length > 0
    ? hydrateInputArtifactPaths(persistedPaths, runState)
    : resolveInputArtifactsFromAvailable(phaseName, phaseDef, runState, config)

  // Determine expected output types and schema files
  const expectedTypes = phaseDef.outputs
  const schemaFiles = expectedTypes.map(t => `${t}.json`)

  // Find quality gate for this phase
  const gate = qualityGates.find(g => g.phase === phaseName)
  const qualityGateInfo = gate ? {
    on_failure: gate.on_failure,
    checks: gate.checks.map(c => ({
      check_type: c.check_type,
      description: c.description,
    })),
  } : undefined

  // Build storage paths from config
  const storagePaths: Record<string, string> = {}
  for (const [key, path] of Object.entries(config.storage.paths)) {
    storagePaths[key] = path
  }

  // Generate instruction
  const instruction = buildInstruction(phaseName, phaseDef, inputArtifacts, expectedTypes, gate)

  return {
    phase_name: phaseName,
    description: phaseDef.description,
    ...(phaseDef.model_tier ? { model_tier: phaseDef.model_tier } : {}),
    input_artifacts: inputArtifacts,
    output_requirements: {
      expected_types: expectedTypes,
      schema_files: schemaFiles,
    },
    quality_gate: qualityGateInfo,
    storage_paths: storagePaths,
    tools: phaseDef.tools,
    instruction,
  }
}

/**
 * Hydrate a list of artifact paths (as stored in `runState.phases[x].input_artifacts`)
 * into the richer `{ type, path, phase }` shape by looking each path up in
 * `runState.available_artifacts`. Paths that cannot be matched (defensive
 * edge case — shouldn't happen in practice) are still included with
 * `type: 'unknown'`, `phase: 'unknown'` so callers never silently lose data.
 */
function hydrateInputArtifactPaths(
  paths: string[],
  runState: RunState,
): Array<{ type: string; path: string; phase: string }> {
  const byPath = new Map<string, { type: string; phase: string }>()
  for (const ref of runState.available_artifacts) {
    // First entry wins if duplicates exist — preserves the earliest source
    // phase for a given path.
    if (!byPath.has(ref.path)) {
      byPath.set(ref.path, { type: ref.type, phase: ref.phase })
    }
  }

  return paths.map(path => {
    const hit = byPath.get(path)
    if (hit) {
      return { type: hit.type, path, phase: hit.phase }
    }
    return { type: 'unknown', path, phase: 'unknown' }
  })
}

/**
 * Legacy fallback: resolve input artifacts for a phase from DAG dependencies
 * and available artifacts. Used when `runState.phases[phaseName].input_artifacts`
 * is empty (e.g. pre-Fix-3.4 run-state files, or entry_point phases that
 * consume type-matched initial artifacts with no upstream DAG edge).
 */
function resolveInputArtifactsFromAvailable(
  phaseName: string,
  phaseDef: PhaseDefinition,
  runState: RunState,
  config: PipelineConfig,
): Array<{ type: string; path: string; phase: string }> {
  const inputTypes = new Set(phaseDef.inputs)

  // Find upstream phases via DAG edges
  const upstreamPhases = config.edges
    .filter(e => e.to === phaseName)
    .map(e => e.from)

  const inputs: Array<{ type: string; path: string; phase: string }> = []

  for (const ref of runState.available_artifacts) {
    // Include if artifact type matches phase inputs
    if (inputTypes.has(ref.type)) {
      inputs.push({ type: ref.type, path: ref.path, phase: ref.phase })
      continue
    }
    // Include if artifact is from an upstream phase
    if (upstreamPhases.includes(ref.phase)) {
      inputs.push({ type: ref.type, path: ref.path, phase: ref.phase })
    }
  }

  return inputs
}

/**
 * Build a structured instruction for the subagent.
 */
function buildInstruction(
  phaseName: string,
  phaseDef: PhaseDefinition,
  inputArtifacts: Array<{ type: string; path: string }>,
  expectedTypes: string[],
  gate?: QualityGate,
): string {
  const lines: string[] = [
    `Execute phase "${phaseName}": ${phaseDef.description}`,
    '',
  ]

  if (inputArtifacts.length > 0) {
    lines.push('Input artifacts:')
    for (const art of inputArtifacts) {
      lines.push(`  - ${art.type}: ${art.path}`)
    }
    lines.push('')
  }

  if (expectedTypes.length > 0) {
    lines.push(`Expected outputs: ${expectedTypes.join(', ')}`)
    lines.push('Store outputs using pipeline_store_artifact with the correct artifact_type.')
    lines.push('')
  }

  if (gate) {
    lines.push(`Quality gate (${gate.on_failure}):`)
    for (const check of gate.checks) {
      lines.push(`  - ${check.description}`)
    }
    lines.push('')
  }

  lines.push('When done, call pipeline_complete_phase to mark this phase as complete.')

  return lines.join('\n')
}
