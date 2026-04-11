import { writeFileSync, readFileSync, mkdirSync, existsSync, renameSync, copyFileSync, unlinkSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { PipelineError } from './types.ts'
import type { RunState, PhaseState, ArtifactRef, PipelineConfig, PipelineRunParameters } from './types.ts'
import { resolveNextPhases } from './dag.ts'
import { getRunDataDir } from './storage.ts'

const STATE_FILE = 'run-state.json'

/**
 * Atomic rename with EPERM fallback for Windows cross-volume edge case.
 * Tries renameSync first (atomic on same volume); on EPERM, falls back to
 * copyFileSync + unlinkSync which is non-atomic but handles cross-volume moves.
 * Re-throws any non-EPERM error.
 */
export function atomicRename(tmpPath: string, finalPath: string): void {
  try {
    renameSync(tmpPath, finalPath)
  } catch (err: unknown) {
    if (err instanceof Error && (err as NodeJS.ErrnoException).code === 'EPERM') {
      copyFileSync(tmpPath, finalPath)
      unlinkSync(tmpPath)
    } else {
      throw err
    }
  }
}

function persist(state: RunState, stateDir: string): void {
  if (!existsSync(stateDir)) mkdirSync(stateDir, { recursive: true })
  state.updated_at = new Date().toISOString()
  const finalPath = join(stateDir, STATE_FILE)
  const tmpPath = finalPath + '.tmp'
  writeFileSync(tmpPath, JSON.stringify(state, null, 2), 'utf-8')
  atomicRename(tmpPath, finalPath)
}

export function initRun(
  runId: string,
  pipelineVersion: string,
  phaseNames: string[],
  stateDir: string,
  runParameters?: PipelineRunParameters,
): RunState {
  const phases: Record<string, PhaseState> = {}
  for (const name of phaseNames) {
    phases[name] = {
      phase_name: name,
      status: 'pending',
      input_artifacts: [],
      output_artifacts: [],
      retry_count: 0,
    }
  }

  const state: RunState = {
    run_id: runId,
    pipeline_version: pipelineVersion,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    status: 'initialized',
    phases,
    available_artifacts: [],
    config_path: stateDir,
  }

  // Persist run_parameters only when the caller provided a non-empty object.
  // Empty {} is omitted so legacy runs and runs without parameters stay clean.
  if (runParameters && Object.keys(runParameters).length > 0) {
    state.run_parameters = runParameters
  }

  // Feature C: compute per-run data directory from run_name + timestamp.
  // Requires the caller invariant `stateDir === {baseDir}/runs/{runId}` so
  // we can recover baseDir via two dirname() calls. Falls back to legacy
  // stateDir layout (with a warning) when parameterization is incomplete.
  if (runParameters?.run_name && runParameters?.run_directory_timestamp) {
    const baseDir = dirname(dirname(stateDir))
    state.run_data_dir = getRunDataDir(baseDir, runParameters.run_name, runParameters.run_directory_timestamp)
  } else {
    console.warn(
      `[pipeline] initRun(${runId}): run_data_dir parameterization inactive (run_name and/or run_directory_timestamp absent). Falling back to legacy ${stateDir}.`,
    )
  }

  persist(state, stateDir)
  return state
}

export function startPhase(state: RunState, phaseName: string, stateDir: string): RunState {
  const phase = state.phases[phaseName]
  if (!phase) {
    throw new PipelineError(`Phase "${phaseName}" not found in run state`, 'state_transition_error', {
      recovery_action: 'Verify phase name exists in pipeline configuration.',
      details: { phase: phaseName },
    })
  }
  if (phase.status !== 'pending') {
    throw new PipelineError(
      `Cannot start phase "${phaseName}": status is "${phase.status}" (expected "pending")`,
      'state_transition_error',
      {
        recovery_action: phase.status === 'failed'
          ? 'Call pipeline_retry_phase to reset to pending, then start.'
          : `Phase is "${phase.status}". Only pending phases can be started.`,
        details: { phase: phaseName, current_status: phase.status, expected_status: 'pending' },
      },
    )
  }

  phase.status = 'in_progress'
  phase.started_at = new Date().toISOString()
  state.status = 'running'

  persist(state, stateDir)
  return state
}

export function completePhase(state: RunState, phaseName: string, stateDir: string): RunState {
  const phase = state.phases[phaseName]
  if (!phase) {
    throw new PipelineError(`Phase "${phaseName}" not found in run state`, 'state_transition_error', {
      recovery_action: 'Verify phase name exists in pipeline configuration.',
      details: { phase: phaseName },
    })
  }
  if (phase.status !== 'in_progress') {
    throw new PipelineError(
      `Cannot complete phase "${phaseName}": status is "${phase.status}" (expected "in_progress")`,
      'state_transition_error',
      {
        recovery_action: phase.status === 'pending'
          ? 'Call pipeline_start_phase first.'
          : `Phase is "${phase.status}". Only in_progress phases can be completed.`,
        details: { phase: phaseName, current_status: phase.status, expected_status: 'in_progress' },
      },
    )
  }

  phase.status = 'completed'
  phase.completed_at = new Date().toISOString()

  const allDone = Object.values(state.phases).every(
    p => p.status === 'completed' || p.status === 'skipped',
  )
  if (allDone) {
    state.status = 'completed'
    state.completed_at = new Date().toISOString()
  }

  persist(state, stateDir)
  return state
}

export function failPhase(state: RunState, phaseName: string, error: string, stateDir: string): RunState {
  const phase = state.phases[phaseName]
  if (!phase) {
    throw new PipelineError(`Phase "${phaseName}" not found in run state`, 'state_transition_error', {
      recovery_action: 'Verify phase name exists in pipeline configuration.',
      details: { phase: phaseName },
    })
  }
  if (phase.status !== 'in_progress') {
    throw new PipelineError(
      `Cannot fail phase "${phaseName}": status is "${phase.status}" (expected "in_progress")`,
      'state_transition_error',
      {
        recovery_action: phase.status === 'pending'
          ? 'Call pipeline_start_phase first before marking as failed.'
          : `Phase is "${phase.status}". Only in_progress phases can be failed.`,
        details: { phase: phaseName, current_status: phase.status, expected_status: 'in_progress' },
      },
    )
  }

  phase.status = 'failed'
  phase.error = error
  state.status = 'failed'
  state.completed_at = new Date().toISOString()

  persist(state, stateDir)
  return state
}

export function retryPhase(state: RunState, phaseName: string, stateDir: string): RunState {
  const phase = state.phases[phaseName]
  if (!phase) {
    throw new PipelineError(`Phase "${phaseName}" not found in run state`, 'state_transition_error', {
      recovery_action: 'Verify phase name exists in pipeline configuration.',
      details: { phase: phaseName },
    })
  }
  if (phase.status !== 'failed') {
    throw new PipelineError(
      `Cannot retry phase "${phaseName}": status is "${phase.status}" (expected "failed")`,
      'state_transition_error',
      {
        recovery_action: phase.status === 'in_progress'
          ? 'Call pipeline_fail_phase first, then retry.'
          : `Phase is "${phase.status}". Only failed phases can be retried.`,
        details: { phase: phaseName, current_status: phase.status, expected_status: 'failed' },
      },
    )
  }

  phase.status = 'pending'
  delete phase.error
  phase.retry_count += 1

  persist(state, stateDir)
  return state
}

export function skipPhase(state: RunState, phaseName: string, stateDir: string): RunState {
  const phase = state.phases[phaseName]
  if (!phase) {
    throw new PipelineError(`Phase "${phaseName}" not found in run state`, 'state_transition_error', {
      recovery_action: 'Verify phase name exists in pipeline configuration.',
      details: { phase: phaseName },
    })
  }
  if (phase.status !== 'pending') {
    throw new PipelineError(
      `Cannot skip phase "${phaseName}": status is "${phase.status}" (expected "pending")`,
      'state_transition_error',
      {
        recovery_action: phase.status === 'in_progress'
          ? 'Call pipeline_fail_phase or pipeline_complete_phase first.'
          : `Phase is "${phase.status}". Only pending phases can be skipped.`,
        details: { phase: phaseName, current_status: phase.status, expected_status: 'pending' },
      },
    )
  }

  phase.status = 'skipped'
  persist(state, stateDir)
  return state
}

export function addArtifact(state: RunState, ref: ArtifactRef, stateDir: string): RunState {
  state.available_artifacts.push(ref)

  const phase = state.phases[ref.phase]
  if (phase) {
    phase.output_artifacts.push(ref.path)
  }

  persist(state, stateDir)
  return state
}

/**
 * Remove all artifact references for a specific phase from run state.
 * Used during quality gate rollback to prevent orphaned artifacts from
 * satisfying downstream DAG inputs.
 * Returns the list of removed artifact paths.
 */
export function removePhaseArtifacts(state: RunState, phaseName: string, stateDir: string): string[] {
  const removed: string[] = []
  state.available_artifacts = state.available_artifacts.filter(ref => {
    if (ref.phase === phaseName) {
      removed.push(ref.path)
      return false
    }
    return true
  })

  const phase = state.phases[phaseName]
  if (phase) {
    phase.output_artifacts = []
  }

  persist(state, stateDir)
  return removed
}

/**
 * Validate structural integrity of a parsed run-state JSON object.
 * Returns an error message string if invalid, or null if valid.
 */
export function validateRunStateIntegrity(state: unknown): string | null {
  if (state === null || typeof state !== 'object') {
    return 'Run state is not a valid object'
  }

  const obj = state as Record<string, unknown>

  // updated_at must be a valid ISO date string
  if (typeof obj.updated_at !== 'string') {
    return 'Run state missing updated_at field'
  }
  const updatedDate = new Date(obj.updated_at)
  if (isNaN(updatedDate.getTime())) {
    return `Run state updated_at is not a valid ISO date: "${obj.updated_at}"`
  }

  // phases must be a non-empty object
  if (obj.phases === null || typeof obj.phases !== 'object' || Array.isArray(obj.phases)) {
    return 'Run state phases is not a valid object'
  }
  if (Object.keys(obj.phases as Record<string, unknown>).length === 0) {
    return 'Run state phases is empty — at least one phase is required'
  }

  // run_id must be present
  if (typeof obj.run_id !== 'string' || obj.run_id.length === 0) {
    return 'Run state missing or empty run_id'
  }

  // status must be a valid run status
  const validStatuses = ['initialized', 'running', 'completed', 'failed']
  if (typeof obj.status !== 'string' || !validStatuses.includes(obj.status)) {
    return `Run state has invalid status: "${String(obj.status)}"`
  }

  return null
}

export function loadRunState(stateDir: string): RunState | null {
  const filePath = join(stateDir, STATE_FILE)
  if (!existsSync(filePath)) return null

  let raw: string
  try {
    raw = readFileSync(filePath, 'utf-8')
  } catch {
    return null
  }

  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    throw new PipelineError(
      `Corrupt run-state.json: invalid JSON in ${filePath}`,
      'file_io_error',
      { recovery_action: 'Delete the corrupt file and re-initialize the run.', details: { path: filePath } },
    )
  }

  const integrityError = validateRunStateIntegrity(parsed)
  if (integrityError) {
    throw new PipelineError(
      `Corrupt run-state.json: ${integrityError}`,
      'file_io_error',
      { recovery_action: 'Delete the corrupt file and re-initialize the run.', details: { path: filePath } },
    )
  }

  const state = parsed as RunState

  // Normalize phases loaded from disk that predate retry_count field
  for (const phase of Object.values(state.phases)) {
    if (phase.retry_count === undefined) {
      phase.retry_count = 0
    }
  }

  // Normalize artifacts loaded from disk that predate version/parent_artifact fields
  for (const artifact of state.available_artifacts) {
    if (artifact.version === undefined) {
      artifact.version = 1
    }
  }

  return state
}

// ── Recovery ────────────────────────────────────────────────

export interface RecoveryResult {
  state: RunState
  recovered_phases: string[]
  warnings: string[]
}

/**
 * Recover a run from its persisted state on disk.
 * Loads and validates the run-state.json, then applies recovery logic:
 * - Phases left in 'in_progress' (from a crash/restart) are reset to 'pending'
 *   with retry_count incremented, since their work is assumed incomplete.
 * - Returns the recovered state along with a list of phases that were reset
 *   and any warnings.
 *
 * Throws PipelineError if the state file is missing, corrupt, or fails integrity checks.
 */
export function recoverRun(runDir: string): RecoveryResult {
  const state = loadRunState(runDir)
  if (!state) {
    throw new PipelineError(
      `No run state found in ${runDir}`,
      'file_io_error',
      { recovery_action: 'Initialize a new run with pipeline_init_run.', details: { path: runDir } },
    )
  }

  const recoveredPhases: string[] = []
  const warnings: string[] = []

  // Reset any in_progress phases back to pending (crash recovery)
  for (const [name, phase] of Object.entries(state.phases)) {
    if (phase.status === 'in_progress') {
      phase.status = 'pending'
      phase.retry_count += 1
      delete phase.started_at
      recoveredPhases.push(name)
      warnings.push(`Phase "${name}" was in_progress at shutdown — reset to pending (retry #${phase.retry_count})`)
    }
  }

  // If any phases were recovered and the run was 'running', check if it should revert to 'initialized'
  if (recoveredPhases.length > 0 && state.status === 'running') {
    const hasInProgress = Object.values(state.phases).some(p => p.status === 'in_progress')
    if (!hasInProgress) {
      // No more in_progress phases after recovery
      const hasCompleted = Object.values(state.phases).some(p => p.status === 'completed')
      state.status = hasCompleted ? 'running' : 'initialized'
    }
  }

  // If the run had failed status but no phases are actually failed, fix the status
  if (state.status === 'failed') {
    const hasFailed = Object.values(state.phases).some(p => p.status === 'failed')
    if (!hasFailed) {
      const hasCompleted = Object.values(state.phases).some(p => p.status === 'completed')
      state.status = hasCompleted ? 'running' : 'initialized'
      warnings.push('Run status was "failed" but no phases are failed — status corrected')
    }
  }

  // Persist the recovered state
  persist(state, runDir)

  return { state, recovered_phases: recoveredPhases, warnings }
}

// ── Phase-status extraction helpers (pure) ──────────────────

/** Return names of all phases with 'completed' status. */
export function getCompletedPhases(state: RunState): string[] {
  return Object.entries(state.phases)
    .filter(([, p]) => p.status === 'completed')
    .map(([name]) => name)
}

/** Return names of all phases with 'skipped' status. */
export function getSkippedPhases(state: RunState): string[] {
  return Object.entries(state.phases)
    .filter(([, p]) => p.status === 'skipped')
    .map(([name]) => name)
}

/** Return names of all phases with 'in_progress' status. */
export function getInProgressPhases(state: RunState): string[] {
  return Object.entries(state.phases)
    .filter(([, p]) => p.status === 'in_progress')
    .map(([name]) => name)
}

/** Return all artifact types from available_artifacts. */
export function getArtifactTypes(state: RunState): string[] {
  return state.available_artifacts.map(a => a.type)
}

// ── Run state derivations (pure, for recommendations and warnings) ──

export interface RecommendedAction {
  action: string
  phase?: string
  reason: string
}

export const STALE_PHASE_THRESHOLD_MS = 3600_000 // 1 hour

export function computeRecommendedAction(state: RunState, nextPhases: string[]): RecommendedAction {
  const inProgress = Object.entries(state.phases).find(([, p]) => p.status === 'in_progress')
  if (inProgress) {
    return { action: 'continue', phase: inProgress[0], reason: `Phase "${inProgress[0]}" is in progress.` }
  }
  const failed = Object.entries(state.phases).find(([, p]) => p.status === 'failed')
  if (failed) {
    return {
      action: 'retry',
      phase: failed[0],
      reason: `Phase "${failed[0]}" failed. Call pipeline_retry_phase to retry.`,
    }
  }
  if (nextPhases.length > 0) {
    return { action: 'start_phase', phase: nextPhases[0], reason: `Next available phase: ${nextPhases[0]}.` }
  }
  if (state.status === 'completed') {
    return { action: 'validate', reason: 'All phases complete. Call pipeline_validate_run.' }
  }
  return { action: 'review', reason: 'No phases available. Check for blocked or failed phases.' }
}

export function computeRunWarnings(
  state: RunState,
  optionalPhases: Set<string>,
  nowMs: number = Date.now(),
): string[] {
  const warnings: string[] = []
  const staleThreshold = nowMs - STALE_PHASE_THRESHOLD_MS

  // Detect stale in-progress phases
  for (const [name, p] of Object.entries(state.phases)) {
    if (p.status === 'in_progress' && p.started_at && new Date(p.started_at).getTime() < staleThreshold) {
      warnings.push(`Phase "${name}" has been in_progress for over 1 hour (started ${p.started_at}).`)
    }
  }

  // Detect failed optional phases
  for (const [name, p] of Object.entries(state.phases)) {
    if (p.status === 'failed' && optionalPhases.has(name)) {
      warnings.push(`Optional phase "${name}" failed: ${p.error ?? 'unknown'}. Consider skipping.`)
    }
  }

  return warnings
}

// ── Run terminal-status detection (DAG-aware) ───────────────

/**
 * Determine whether the run as a whole has reached a terminal state.
 *
 * Precedence:
 *   1. Any phase 'failed'        → 'failed'
 *   2. Any phase 'in_progress'   → 'running'
 *   3. resolveNextPhases empty   → 'completed'   (no more forward progress possible;
 *                                                 pending phases from untaken DAG
 *                                                 branches are treated as effectively
 *                                                 done because the DAG will never make
 *                                                 them runnable)
 *   4. otherwise                 → 'running'
 *
 * Note: The plan document labels the terminal success state 'done', but the concrete
 * RunState.status union uses 'completed'. This helper returns 'completed' to stay
 * consistent with the existing type and `validateRunStateIntegrity`.
 */
export function computeRunTerminalStatus(
  state: RunState,
  config: PipelineConfig,
): 'running' | 'completed' | 'failed' {
  const phases = Object.values(state.phases)

  if (phases.some(p => p.status === 'failed')) return 'failed'
  if (phases.some(p => p.status === 'in_progress')) return 'running'

  const completed = getCompletedPhases(state)
  const skipped = getSkippedPhases(state)
  const artifactTypes = getArtifactTypes(state)
  const nextPhases = resolveNextPhases(config, completed, artifactTypes, skipped)

  return nextPhases.length === 0 ? 'completed' : 'running'
}

/**
 * Set the run's terminal status fields and persist atomically.
 * Used by lifecycle handlers after computeRunTerminalStatus reports a terminal result.
 */
export function finalizeRunTerminalStatus(
  state: RunState,
  newStatus: 'completed' | 'failed',
  stateDir: string,
): RunState {
  state.status = newStatus
  state.completed_at = new Date().toISOString()
  persist(state, stateDir)
  return state
}

/**
 * Resolve the set of artifact paths that `phaseName` can consume, based on
 * the DAG's incoming edges. Walks `config.edges` for edges whose `to === phaseName`,
 * then collects every `output_artifacts[]` path from the upstream phases
 * (`state.phases[edge.from]`). The returned list is deduplicated while
 * preserving first-seen order.
 *
 * Notes:
 *  - Upstream phases whose status is 'pending' are not skipped — if they
 *    happen to have output_artifacts recorded (e.g. via addArtifact), those
 *    paths are included. In practice only completed/skipped upstream phases
 *    contribute, because phase output_artifacts are populated by addArtifact
 *    during execution.
 *  - Initial artifacts registered with phase === 'pre-existing' are NOT
 *    included, because 'pre-existing' is not a real DAG node.
 *  - Returns an empty array for entry_point phases that have no incoming edges.
 */
export function resolveInputArtifacts(
  state: RunState,
  phaseName: string,
  config: PipelineConfig,
): string[] {
  const resolved: string[] = []
  const seen = new Set<string>()

  for (const edge of config.edges) {
    if (edge.to !== phaseName) continue
    const upstream = state.phases[edge.from]
    if (!upstream) continue
    for (const path of upstream.output_artifacts) {
      if (seen.has(path)) continue
      seen.add(path)
      resolved.push(path)
    }
  }

  return resolved
}
