import type { PipelineConfig, PhaseDefinition } from './types.ts'

export interface InputSatisfaction {
  satisfied: string[]
  missing: string[]
  can_run: boolean
}

export function getPhaseInputSatisfaction(
  phase: PhaseDefinition,
  availableArtifacts: string[],
): InputSatisfaction {
  const satisfied = phase.inputs.filter(i => availableArtifacts.includes(i))
  const missing = phase.inputs.filter(i => !availableArtifacts.includes(i))

  let can_run: boolean
  switch (phase.input_mode) {
    case 'any':
      can_run = satisfied.length > 0
      break
    case 'one_of':
    case 'one_of_primary':
      can_run = satisfied.length > 0
      break
    case 'required_optional':
      can_run = phase.inputs.length > 0 && availableArtifacts.includes(phase.inputs[0])
      break
    default:
      can_run = phase.inputs.length === 0 || missing.length === 0
      break
  }

  return { satisfied, missing, can_run }
}

export function resolveNextPhases(
  config: PipelineConfig,
  completedPhases: string[],
  availableArtifacts: string[],
  skipPhases: string[] = [],
): string[] {
  const completed = new Set(completedPhases)
  const skipped = new Set(skipPhases)
  const candidates: string[] = []

  for (const [name, phase] of Object.entries(config.phases)) {
    if (completed.has(name) || skipped.has(name)) continue

    // Entry-point phases accept external input — bypass input satisfaction check
    if (!phase.entry_point) {
      const { can_run } = getPhaseInputSatisfaction(phase, availableArtifacts)
      if (!can_run) continue
    }

    const incomingEdges = config.edges.filter(e => e.to === name)
    if (incomingEdges.length > 0) {
      const requiredEdges = incomingEdges.filter(e => !e.optional)
      const hasCompletedOrSkippedPredecessor = requiredEdges.length === 0 ||
        requiredEdges.some(e => completed.has(e.from) || skipped.has(e.from))

      if (!hasCompletedOrSkippedPredecessor && !phase.entry_point) continue
    }

    candidates.push(name)
  }

  return candidates
}

export function getDownstreamPhases(
  config: PipelineConfig,
  phaseName: string,
): string[] {
  return config.edges
    .filter(e => e.from === phaseName)
    .map(e => e.to)
}

export function getUpstreamPhases(
  config: PipelineConfig,
  phaseName: string,
): string[] {
  return config.edges
    .filter(e => e.to === phaseName)
    .map(e => e.from)
}
