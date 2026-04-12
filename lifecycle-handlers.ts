// lifecycle-handlers.ts — lifecycle tool handler logic extracted from server.ts

import { join } from 'node:path'
import { appendFileSync, mkdirSync, existsSync, readFileSync } from 'node:fs'
import type {
  PipelineConfig,
  PhaseDefinition,
  RunState,
  StorageConfig,
  ResponseEnvelope,
  ArtifactRef,
  QualityGate,
  GateResult,
  LifecycleEvent,
  HookConfig,
  HookTrigger,
  HandlerResponse,
  PipelineRunParameters,
  AgentDirective,
} from './types.ts'
import { PipelineError } from './types.ts'
import type { InputSatisfaction } from './dag.ts'
import type { HookContext, HookResult, PreInitHookContext, PreInitHookResult } from './hooks.ts'
import { getCompletedPhases, getSkippedPhases, getArtifactTypes, getInProgressPhases, computeRunTerminalStatus, finalizeRunTerminalStatus, resolveInputArtifacts } from './run-state.ts'
import type { RecommendedAction, RecoveryResult } from './run-state.ts'
import { generateHandoff } from './handoff.ts'
import { generatePhaseBrief } from './phase-brief.ts'

// ── Context provided by server.ts ────────────────────────────

export interface LifecycleContext {
  getConfig(): PipelineConfig
  setProjectRoot(root: string): void
  getStorageConfig(baseDir?: string): StorageConfig
  getActiveRun(): RunState | null
  setActiveRun(state: RunState): void
  getActiveRunDir(): string
  setActiveRunDir(dir: string): void
  initRun(
    runId: string,
    pipelineVersion: string,
    phaseNames: string[],
    stateDir: string,
    runParameters?: PipelineRunParameters,
  ): RunState
  skipPhase(state: RunState, phase: string, stateDir: string): RunState
  addArtifact(state: RunState, ref: ArtifactRef, stateDir: string): RunState
  startPhase(state: RunState, phase: string, stateDir: string): RunState
  completePhase(state: RunState, phase: string, stateDir: string): RunState
  failPhase(state: RunState, phase: string, error: string, stateDir: string): RunState
  retryPhase(state: RunState, phase: string, stateDir: string): RunState
  resolveNextPhases(config: PipelineConfig, completed: string[], artifactTypes: string[], skips: string[]): string[]
  getPhaseInputSatisfaction(phase: PhaseDefinition, artifactTypes: string[]): InputSatisfaction
  loadRunState(stateDir: string): RunState | null
  recoverRun(runDir: string): RecoveryResult
  removePhaseArtifacts(state: RunState, phaseName: string, stateDir: string): string[]
  getQualityGates(): QualityGate[]
  runGateChecks(gate: QualityGate, artifactRefs: ArtifactRef[]): GateResult
  getHooksConfig(): HookConfig[]
  runHooks(hooks: HookConfig[], trigger: HookTrigger, context: HookContext, projectRoot: string): HookResult[]
  runPrePipelineInitHooks(
    hooks: HookConfig[],
    context: PreInitHookContext,
    projectRoot: string,
  ): PreInitHookResult
  getProjectRoot(): string | null
  computeRecommendedAction(state: RunState, nextPhases: string[]): RecommendedAction
  computeRunWarnings(state: RunState, optionalPhases: Set<string>, nowMs?: number): string[]
}

// Re-export HandlerResponse for backward compatibility
export type { HandlerResponse } from './types.ts'

// ── Events log ──────────────────────────────────────────────

/**
 * Append a lifecycle event to events.jsonl in the run directory.
 * Creates the file if it does not exist. Each event is one JSON line.
 */
export function appendEvent(runDir: string, event: LifecycleEvent): void {
  if (!existsSync(runDir)) mkdirSync(runDir, { recursive: true })
  const eventsPath = join(runDir, 'events.jsonl')
  appendFileSync(eventsPath, JSON.stringify(event) + '\n', 'utf-8')
}

/**
 * Write a `phase_completed` event to events.jsonl with strict validation and
 * on-disk idempotency. Callers MUST go through this wrapper for `phase_completed`
 * events; raw `appendEvent` calls are reserved for other event types.
 *
 * Behavior:
 *  - Throws `PipelineError('state_transition_error')` if `phase` is not in
 *    `state.phases`, or if its status is not `'completed'`.
 *  - If a `phase_completed` event for `phase` already exists in
 *    `{runDir}/events.jsonl`, logs a warning and returns `false` WITHOUT writing.
 *  - Otherwise writes the standard event and returns `true`.
 */
export function appendPhaseCompletedEvent(
  runDir: string,
  state: RunState,
  phase: string,
): boolean {
  const phaseState = state.phases[phase]
  if (!phaseState) {
    throw new PipelineError(
      `cannot append phase_completed event: phase "${phase}" is not in run state`,
      'state_transition_error',
      {
        recovery_action: 'Verify phase name exists in the run state before completing.',
        details: { phase, known_phases: Object.keys(state.phases) },
      },
    )
  }
  if (phaseState.status !== 'completed') {
    throw new PipelineError(
      `cannot append phase_completed event: phase "${phase}" status is "${phaseState.status}", expected completed`,
      'state_transition_error',
      {
        recovery_action: 'Transition the phase to completed before writing a phase_completed event.',
        details: { phase, current_status: phaseState.status, expected_status: 'completed' },
      },
    )
  }

  // On-disk duplicate suppression — defense-in-depth against late-firing callers.
  try {
    const eventsPath = join(runDir, 'events.jsonl')
    if (existsSync(eventsPath)) {
      const content = readFileSync(eventsPath, 'utf-8')
      for (const line of content.split('\n')) {
        const trimmed = line.trim()
        if (!trimmed) continue
        try {
          const parsed = JSON.parse(trimmed) as { event?: unknown; phase?: unknown }
          if (parsed.event === 'phase_completed' && parsed.phase === phase) {
            const timestamp = new Date().toISOString()
            console.warn(
              `[hooks] suppressed duplicate phase_completed write for phase="${phase}" at ${timestamp}`,
            )
            return false
          }
        } catch {
          // Malformed line — skip defensively.
        }
      }
    }
  } catch {
    // If we can't read the file for any reason, fall through and attempt the write.
  }

  appendEvent(runDir, {
    timestamp: new Date().toISOString(),
    event: 'phase_completed',
    phase,
    run_id: state.run_id,
  })
  return true
}

// ── Helpers ──────────────────────────────────────────────────

/**
 * Check if the on-disk state has diverged from the in-memory activeRun.
 * Returns the disk state if stale, or null if in sync.
 */
export function detectStaleState(ctx: LifecycleContext): RunState | null {
  const activeRun = ctx.getActiveRun()
  const activeRunDir = ctx.getActiveRunDir()
  if (!activeRun || !activeRunDir) return null
  const disk = ctx.loadRunState(activeRunDir)
  if (!disk) return null
  if (disk.updated_at !== activeRun.updated_at) return disk
  return null
}

/**
 * Reload run state from disk, replacing the in-memory activeRun.
 * Returns a diff summary describing what changed.
 */
export function reloadStateFromDisk(ctx: LifecycleContext): { reloaded: RunState; changes: string[] } {
  const activeRunDir = ctx.getActiveRunDir()
  if (!activeRunDir) throw new Error('No active run directory')

  const disk = ctx.loadRunState(activeRunDir)
  if (!disk) throw new Error('No run-state.json found on disk')

  const changes: string[] = []
  const activeRun = ctx.getActiveRun()
  if (activeRun) {
    // Artifact count diff
    const oldCount = activeRun.available_artifacts.length
    const newCount = disk.available_artifacts.length
    if (newCount !== oldCount) {
      changes.push(`artifacts: ${oldCount} → ${newCount}`)
    }

    // Phase status changes
    for (const [name, diskPhase] of Object.entries(disk.phases)) {
      const memPhase = activeRun.phases[name]
      if (memPhase && memPhase.status !== diskPhase.status) {
        changes.push(`phase "${name}": ${memPhase.status} → ${diskPhase.status}`)
      } else if (!memPhase) {
        changes.push(`phase "${name}": new (${diskPhase.status})`)
      }
    }

    // Overall status
    if (activeRun.status !== disk.status) {
      changes.push(`run status: ${activeRun.status} → ${disk.status}`)
    }

    if (changes.length === 0) {
      changes.push('updated_at changed but no structural differences detected')
    }
  } else {
    changes.push('no previous in-memory state; loaded from disk')
  }

  ctx.setActiveRun(disk)
  return { reloaded: disk, changes }
}

// ── Handlers ─────────────────────────────────────────────────

export function handleInitRun(args: Record<string, unknown>, ctx: LifecycleContext): HandlerResponse {
  const runId = args.run_id as string
  if (!runId) throw new Error('run_id is required')

  const projectRoot = args.project_root as string | undefined
  if (!projectRoot) throw new Error('project_root is required — absolute path to the project directory')
  ctx.setProjectRoot(projectRoot)

  const baseDir = args.base_dir as string | undefined
  const skipPhasesList = (args.skip_phases as string[]) ?? []
  const initialArtifacts = (args.initial_artifacts as Array<{ type: string; path: string }>) ?? []

  const config = ctx.getConfig()
  const sc = ctx.getStorageConfig(baseDir)
  const legacyRunDir = join(sc.base_dir, 'runs', runId)
  ctx.setActiveRunDir(legacyRunDir)
  const phaseNames = Object.keys(config.phases).filter(p => !skipPhasesList.includes(p))

  // Collect run parameters from pre_pipeline_init hooks (if any are registered).
  // Hooks can either return parameters directly or request user input via userPrompts.
  // If the hook demands user input AND the caller has not yet provided run_parameters,
  // short-circuit with a user_input_required envelope so the client can collect answers.
  let runParameters: PipelineRunParameters = {}
  const hooks = ctx.getHooksConfig()
  const preInitHooks = hooks.filter(h => h.trigger === 'pre_pipeline_init')
  const userSuppliedParams = args.run_parameters as Record<string, unknown> | undefined

  if (preInitHooks.length > 0) {
    const preInitContext: PreInitHookContext = {
      trigger: 'pre_pipeline_init',
      project_root: projectRoot,
      requested_args: args,
    }
    const hookResult = ctx.runPrePipelineInitHooks(hooks, preInitContext, projectRoot)

    // Short-circuit when the hook needs user input and the caller hasn't provided it yet.
    if (hookResult.userPrompts.length > 0 && !userSuppliedParams) {
      const envelope: ResponseEnvelope = {
        status: 'ok',
        data: {
          status: 'user_input_required',
          prompts: hookResult.userPrompts,
          partial_parameters: hookResult.parameters,
        },
        next_step:
          'Collect answers to the prompts above from the user and re-invoke pipeline_init_run with a run_parameters object containing the answers.',
      }
      return { json: JSON.stringify(envelope, null, 2) }
    }

    // Merge hook parameters with caller-supplied run_parameters.
    // User-supplied values take precedence over hook defaults.
    runParameters = {
      ...hookResult.parameters,
      ...((userSuppliedParams ?? {}) as PipelineRunParameters),
    }
  } else if (userSuppliedParams) {
    runParameters = userSuppliedParams as PipelineRunParameters
  }

  let activeRun = ctx.initRun(runId, config.pipeline.version, phaseNames, legacyRunDir, runParameters)

  // Feature C step C8: once initRun has computed state.run_data_dir from
  // run_parameters.run_name + run_directory_timestamp, redirect the stored
  // activeRunDir to that canonical per-run tree. All subsequent
  // appendEvent / persist callers read activeRunDir from ctx, so this one
  // redirect routes events.jsonl and downstream run-state.json writes into
  // pipeline_mcp_data/runs/{name-timestamp}/ without touching each callsite.
  // Legacy runs (without run_parameters) keep legacyRunDir and the existing
  // pipeline_mcp_data/runs/{run_id}/ layout remains unchanged.
  const activeRunDir = activeRun.run_data_dir ?? legacyRunDir
  if (activeRunDir !== legacyRunDir) {
    ctx.setActiveRunDir(activeRunDir)
  }

  // Skip explicitly listed phases
  for (const phase of skipPhasesList) {
    if (activeRun.phases[phase]) {
      activeRun = ctx.skipPhase(activeRun, phase, activeRunDir)
    }
  }

  // Register initial artifacts
  for (const art of initialArtifacts) {
    activeRun = ctx.addArtifact(activeRun, {
      type: art.type,
      path: art.path,
      phase: 'pre-existing',
      created_at: new Date().toISOString(),
      version: 1,
    }, activeRunDir)
  }

  ctx.setActiveRun(activeRun)

  const artifactTypes = getArtifactTypes(activeRun)
  const completedPhases = getCompletedPhases(activeRun)

  const nextPhases = ctx.resolveNextPhases(config, completedPhases, artifactTypes, skipPhasesList)

  return {
    json: JSON.stringify({
      run_id: activeRun.run_id,
      status: activeRun.status,
      total_phases: Object.keys(activeRun.phases).length,
      skipped: skipPhasesList,
      initial_artifacts: artifactTypes,
      available_next_phases: nextPhases,
      run_parameters: activeRun.run_parameters ?? {},
    }, null, 2),
  }
}

export function handleNextPhases(args: Record<string, unknown>, ctx: LifecycleContext): HandlerResponse {
  const activeRun = ctx.getActiveRun()
  if (!activeRun) throw new Error('No active run. Call pipeline_init_run first.')

  const extraSkips = (args.skip_phases as string[]) ?? []

  // Auto-reload if disk state has diverged
  let staleNote: string | null = null
  const stale = detectStaleState(ctx)
  if (stale) {
    const { changes } = reloadStateFromDisk(ctx)
    staleNote = `State auto-reloaded from disk (${changes.join('; ')})`
  }
  const state = ctx.getActiveRun()!

  const config = ctx.getConfig()
  const completed = getCompletedPhases(state)
  const artifactTypes = getArtifactTypes(state)
  const allSkips = [
    ...getSkippedPhases(state),
    ...extraSkips,
  ]

  const nextPhases = ctx.resolveNextPhases(config, completed, artifactTypes, allSkips)

  const details = nextPhases.map(name => {
    const phase = config.phases[name]
    const { satisfied, missing } = ctx.getPhaseInputSatisfaction(phase, artifactTypes)
    return { name, inputs_satisfied: satisfied, inputs_missing: missing, description: phase.description, entry_point: phase.entry_point }
  })

  const result: Record<string, unknown> = { available_phases: details, completed, artifacts: artifactTypes }
  if (staleNote) result.state_reloaded = staleNote

  return { json: JSON.stringify(result, null, 2) }
}

export function handleStartPhase(args: Record<string, unknown>, ctx: LifecycleContext): HandlerResponse {
  const state = ctx.getActiveRun()
  if (!state) throw new Error('No active run. Call pipeline_init_run first.')

  const phaseName = args.phase as string
  if (!phaseName) throw new Error('phase is required')

  const config = ctx.getConfig()
  const activeRunDir = ctx.getActiveRunDir()

  // Resolve upstream input artifacts from the DAG and record them on the
  // phase before the start-phase transition persists. This makes the set
  // of consumable artifacts explicit in run-state.json and surfaces them
  // in the phase brief + handoff.
  const inputArtifacts = resolveInputArtifacts(state, phaseName, config)
  const phaseStateSlot = state.phases[phaseName]
  if (phaseStateSlot) {
    phaseStateSlot.input_artifacts = inputArtifacts
  }

  // Feature B: resolve the concrete Claude model ID via precedence chain:
  //   1. Phase-specific `model` in pipeline.toml                    (highest)
  //   2. run_parameters.phase_model set by pre_pipeline_init hook   (run default)
  //   3. Hard-coded 'claude-sonnet-4-6'                             (fallback)
  const phaseDef = config.phases[phaseName]
  const phaseModelParam = state.run_parameters?.phase_model
  const resolvedModel: string =
    (typeof phaseDef?.model === 'string' && phaseDef.model.length > 0
      ? phaseDef.model
      : undefined) ??
    (typeof phaseModelParam === 'string' && phaseModelParam.length > 0
      ? phaseModelParam
      : undefined) ??
    'claude-sonnet-4-6'
  const runDataDir = state.run_data_dir ?? activeRunDir

  // Generate the phase brief so the pre_start hook can embed it in the
  // subagent's prompt. Uses the new options arg landed in B6.
  const phaseBrief = generatePhaseBrief(
    phaseName,
    state,
    config,
    ctx.getQualityGates(),
    { resolvedModel, runDataDir },
  )

  // Run pre_start hooks (blocking: failure throws PipelineError). Collect the
  // first agent_directive from any matching hook; hooks beyond the first with
  // a directive are ignored but still executed for their side effects.
  let agentDirective: AgentDirective | undefined
  const projectRoot = ctx.getProjectRoot()
  const hooks = ctx.getHooksConfig()
  if (projectRoot && hooks.length > 0) {
    const hookContext: HookContext = {
      run_id: state.run_id,
      phase: phaseName,
      trigger: 'pre_start',
      project_root: projectRoot,
      run_dir: activeRunDir,
      run_parameters: state.run_parameters ?? {},
      phase_brief: phaseBrief,
      resolved_model: resolvedModel,
    }
    const hookResults = ctx.runHooks(hooks, 'pre_start', hookContext, projectRoot)
    for (const hr of hookResults) {
      if (hr.agent_directive) {
        agentDirective = hr.agent_directive
        break
      }
    }
  }

  const updated = ctx.startPhase(state, phaseName, activeRunDir)
  ctx.setActiveRun(updated)

  // Log phase_started event
  appendEvent(activeRunDir, {
    timestamp: new Date().toISOString(),
    event: 'phase_started',
    phase: phaseName,
    run_id: updated.run_id,
  })

  const phase = config.phases[phaseName]

  const responseBody: Record<string, unknown> = {
    phase: phaseName,
    status: 'in_progress',
    description: phase.description,
    inputs: phase.inputs,
    input_mode: phase.input_mode,
    outputs: phase.outputs,
    tools: phase.tools,
    input_artifacts: inputArtifacts,
    resolved_model: resolvedModel,
  }
  if (agentDirective) {
    responseBody.agent_directive = agentDirective
  }

  return { json: JSON.stringify(responseBody, null, 2) }
}

export function handleCompletePhase(args: Record<string, unknown>, ctx: LifecycleContext): HandlerResponse {
  const state = ctx.getActiveRun()
  if (!state) throw new Error('No active run. Call pipeline_init_run first.')

  const phaseName = args.phase as string
  if (!phaseName) throw new Error('phase is required')

  // State-level idempotency guard: if the phase is already completed, return a
  // no-op envelope with a warning. Do NOT re-run gates, re-write events, or
  // re-run hooks. Protects against duplicate pipeline_complete_phase calls
  // (e.g. from a late subagent or a retried tool call).
  if (state.phases[phaseName]?.status === 'completed') {
    const envelope: ResponseEnvelope = {
      status: 'ok',
      data: {
        phase: phaseName,
        completed_at: state.phases[phaseName].completed_at,
        run_status: state.status,
      },
      next_step: 'Phase already completed. Call pipeline_next_phases to continue.',
      warnings: [
        `phase already completed — ignoring duplicate pipeline_complete_phase call for phase "${phaseName}"`,
      ],
    }
    return { json: JSON.stringify(envelope, null, 2) }
  }

  // Run quality gate checks before completing
  const gates = ctx.getQualityGates()
  const gate = gates.find(g => g.phase === phaseName)
  const gateWarnings: string[] = []

  if (gate) {
    const phaseArtifacts = state.available_artifacts.filter(a => a.phase === phaseName)
    const gateResult = ctx.runGateChecks(gate, phaseArtifacts)

    // Emit one gate_evaluated event per check BEFORE any block decision, so the
    // audit trail captures the reason for a block even when the phase rolls back.
    // gate.checks and gateResult.results are aligned by index (see runGateChecks).
    const gateRunDir = ctx.getActiveRunDir()
    for (let i = 0; i < gateResult.results.length; i++) {
      const checkResult = gateResult.results[i]
      const checkConfig = gate.checks[i]
      const artifactTypeParam = checkConfig?.params.artifact_type as string | undefined
      const resolvedArtifactType =
        artifactTypeParam ?? (phaseArtifacts[0]?.type ?? undefined)
      const result: 'pass' | 'fail' | 'warn' = checkResult.passed
        ? 'pass'
        : gate.on_failure === 'block'
          ? 'fail'
          : 'warn'
      const details: Record<string, unknown> = {
        gate_name: gate.phase,
        check_type: checkResult.check_type,
        result,
        message: checkResult.message,
      }
      if (resolvedArtifactType !== undefined) {
        details.artifact_type = resolvedArtifactType
      }
      appendEvent(gateRunDir, {
        timestamp: new Date().toISOString(),
        event: 'gate_evaluated',
        phase: phaseName,
        run_id: state.run_id,
        details,
      })
    }

    if (!gateResult.passed) {
      if (gate.on_failure === 'block') {
        // Atomic rollback: revert phase status to in_progress and remove artifacts
        const activeRunDir = ctx.getActiveRunDir()
        ctx.removePhaseArtifacts(state, phaseName, activeRunDir)

        const failedChecks = gateResult.results.filter(r => !r.passed)
        throw new PipelineError(
          `Quality gate blocked completion of phase "${phaseName}": ${failedChecks.map(c => c.message).join('; ')}`,
          'gate_blocked',
          {
            recovery_action: 'Fix the failing quality checks and retry pipeline_complete_phase.',
            details: {
              phase: phaseName,
              gate_result: gateResult,
              removed_artifacts: state.available_artifacts.filter(a => a.phase === phaseName).map(a => a.path),
            },
          },
        )
      }
      // on_failure === 'warn': proceed with completion but capture warnings from this result
      const failedChecks = gateResult.results.filter(r => !r.passed)
      gateWarnings.push(...failedChecks.map(c => `Quality gate warning: ${c.message}`))
    }
  }

  const config = ctx.getConfig()
  const activeRunDir = ctx.getActiveRunDir()
  const updated = ctx.completePhase(state, phaseName, activeRunDir)
  ctx.setActiveRun(updated)

  // Order: run post_complete hooks before appending phase_completed. The
  // hooks.ts idempotency guard compares against events.jsonl; writing the
  // event first would cause it to trip in the normal flow and skip every
  // legitimate hook. With hooks before event, the guard only trips on
  // genuine re-invocations (e.g. duplicate runHooks calls after the
  // legitimate completion has already written the event).
  // Run post_complete hooks (non-blocking: failures are warnings)
  const hookWarnings: string[] = []
  const projectRoot = ctx.getProjectRoot()
  const hooks = ctx.getHooksConfig()
  if (projectRoot && hooks.length > 0) {
    const hookContext: HookContext = {
      run_id: updated.run_id,
      phase: phaseName,
      trigger: 'post_complete',
      project_root: projectRoot,
      run_dir: activeRunDir,
    }
    const hookResults = ctx.runHooks(hooks, 'post_complete', hookContext, projectRoot)
    for (const hr of hookResults) {
      if (!hr.success) {
        hookWarnings.push(`Post-complete hook warning: ${hr.command} — ${hr.error}`)
      }
    }
  }

  // Log phase_completed event via the validated wrapper (throws on invalid
  // state, suppresses on-disk duplicates).
  appendPhaseCompletedEvent(activeRunDir, updated, phaseName)

  // Re-evaluate whether the run as a whole has reached a terminal state.
  // Uses DAG reachability so pending phases from untaken branches don't block termination.
  const terminal = computeRunTerminalStatus(updated, config)
  let finalState = updated
  if ((terminal === 'completed' || terminal === 'failed') && updated.status !== terminal) {
    finalState = finalizeRunTerminalStatus(updated, terminal, activeRunDir)
    ctx.setActiveRun(finalState)
  }

  const completed = getCompletedPhases(finalState)
  const artifactTypes = getArtifactTypes(finalState)
  const skips = getSkippedPhases(finalState)
  const nextPhases = ctx.resolveNextPhases(config, completed, artifactTypes, skips)

  const allWarnings = [...gateWarnings, ...hookWarnings]

  const envelope: ResponseEnvelope = {
    status: 'ok',
    data: {
      phase: phaseName,
      completed_at: finalState.phases[phaseName].completed_at,
      run_status: finalState.status,
    },
    next_step: nextPhases.length > 0
      ? `Available phases: ${nextPhases.join(', ')}. Call pipeline_start_phase to begin.`
      : 'All phases complete. Call pipeline_validate_run for final validation.',
  }
  if (allWarnings.length > 0) {
    envelope.warnings = allWarnings
  }
  return { json: JSON.stringify(envelope, null, 2) }
}

export function handleFailPhase(args: Record<string, unknown>, ctx: LifecycleContext): HandlerResponse {
  const state = ctx.getActiveRun()
  if (!state) throw new Error('No active run. Call pipeline_init_run first.')

  const phaseName = args.phase as string
  const error = args.error as string
  if (!phaseName) throw new Error('phase is required')
  if (!error) throw new Error('error is required')

  const config = ctx.getConfig()
  const activeRunDir = ctx.getActiveRunDir()
  const updated = ctx.failPhase(state, phaseName, error, activeRunDir)
  ctx.setActiveRun(updated)

  // Log phase_failed event
  appendEvent(activeRunDir, {
    timestamp: new Date().toISOString(),
    event: 'phase_failed',
    phase: phaseName,
    run_id: updated.run_id,
    details: { error },
  })

  // Run on_fail hooks (non-blocking: failures are warnings)
  const projectRoot = ctx.getProjectRoot()
  const hooks = ctx.getHooksConfig()
  const hookWarnings: string[] = []
  if (projectRoot && hooks.length > 0) {
    const hookContext: HookContext = {
      run_id: updated.run_id,
      phase: phaseName,
      trigger: 'on_fail',
      project_root: projectRoot,
      run_dir: activeRunDir,
      error,
    }
    const hookResults = ctx.runHooks(hooks, 'on_fail', hookContext, projectRoot)
    for (const hr of hookResults) {
      if (!hr.success) {
        hookWarnings.push(`On-fail hook warning: ${hr.command} — ${hr.error}`)
      }
    }
  }

  const isOptional = config.phases[phaseName]?.optional === true
  const envelope: ResponseEnvelope = {
    status: 'error',
    data: { phase: phaseName, error },
    next_step: isOptional
      ? `Phase "${phaseName}" is optional. Call pipeline_next_phases to skip and continue.`
      : `Phase "${phaseName}" failed. Fix the underlying issue and call pipeline_retry_phase to retry.`,
  }
  if (hookWarnings.length > 0) {
    envelope.warnings = hookWarnings
  }
  return { json: JSON.stringify(envelope, null, 2) }
}

export function handleRetryPhase(args: Record<string, unknown>, ctx: LifecycleContext): HandlerResponse {
  const state = ctx.getActiveRun()
  if (!state) throw new Error('No active run. Call pipeline_init_run first.')

  const phaseName = args.phase as string
  if (!phaseName) throw new Error('phase is required')

  const activeRunDir = ctx.getActiveRunDir()
  const updated = ctx.retryPhase(state, phaseName, activeRunDir)
  ctx.setActiveRun(updated)

  const retryCount = updated.phases[phaseName].retry_count

  const envelope: ResponseEnvelope = {
    status: 'ok',
    data: { phase: phaseName, status: 'pending', retry_count: retryCount },
    next_step: 'Call pipeline_start_phase to re-execute the phase.',
  }
  return { json: JSON.stringify(envelope, null, 2) }
}

export function handleRunStatus(ctx: LifecycleContext): HandlerResponse {
  const activeRun = ctx.getActiveRun()
  if (!activeRun) {
    return {
      json: JSON.stringify({
        status: 'no_run',
        recommended_action: { action: 'init', reason: 'No active run. Call pipeline_init_run to start.' },
      }, null, 2),
    }
  }

  // Auto-reload if disk state has diverged
  let staleNote: string | null = null
  const stale = detectStaleState(ctx)
  if (stale) {
    const { changes } = reloadStateFromDisk(ctx)
    staleNote = `State auto-reloaded from disk (${changes.join('; ')})`
  }
  const state = ctx.getActiveRun()!

  const config = ctx.getConfig()
  const summary = Object.entries(state.phases).map(([name, p]) => ({
    phase: name,
    status: p.status,
    started: p.started_at ?? null,
    completed: p.completed_at ?? null,
    error: p.error ?? null,
    outputs: p.output_artifacts,
  }))

  // Compute recommended action
  const completed = getCompletedPhases(state)
  const artifactTypes = getArtifactTypes(state)
  const skips = getSkippedPhases(state)
  const nextPhases = ctx.resolveNextPhases(config, completed, artifactTypes, skips)

  const optionalPhases = new Set(
    Object.entries(config.phases)
      .filter(([, p]) => p.optional === true)
      .map(([name]) => name),
  )
  const warnings = ctx.computeRunWarnings(state, optionalPhases)
  const recommended_action = ctx.computeRecommendedAction(state, nextPhases)

  const result: Record<string, unknown> = {
    run_id: state.run_id,
    status: state.status,
    created: state.created_at,
    updated: state.updated_at,
    phases: summary,
    artifacts: state.available_artifacts,
    recommended_action,
  }
  if (warnings.length > 0) result.warnings = warnings
  if (staleNote) result.state_reloaded = staleNote

  return { json: JSON.stringify(result, null, 2) }
}

export function handleReloadState(ctx: LifecycleContext): HandlerResponse {
  const activeRunDir = ctx.getActiveRunDir()
  if (!activeRunDir) {
    throw new Error('No active run. Call pipeline_init_run first.')
  }

  // Use recoverRun for full recovery (integrity check + in_progress reset)
  const { state, recovered_phases, warnings } = ctx.recoverRun(activeRunDir)
  ctx.setActiveRun(state)

  // Build changes list from recovery for backward compatibility
  const changes: string[] = []
  if (recovered_phases.length > 0) {
    changes.push(`recovered phases: ${recovered_phases.join(', ')}`)
  }
  if (warnings.length > 0) {
    changes.push(...warnings)
  }
  if (changes.length === 0) {
    changes.push('state reloaded from disk — no recovery needed')
  }

  return {
    json: JSON.stringify({
      run_id: state.run_id,
      status: state.status,
      updated_at: state.updated_at,
      artifact_count: state.available_artifacts.length,
      recovered_phases,
      changes,
    }, null, 2),
  }
}

export function handlePhaseHandoff(ctx: LifecycleContext): HandlerResponse {
  const state = ctx.getActiveRun()
  if (!state) throw new Error('No active run. Call pipeline_init_run first.')

  const config = ctx.getConfig()
  const completed = getCompletedPhases(state)
  const artifactTypes = getArtifactTypes(state)
  const skips = getSkippedPhases(state)
  const nextPhases = ctx.resolveNextPhases(config, completed, artifactTypes, skips)
  const handoff = generateHandoff(state, config, ctx.getQualityGates(), nextPhases)

  return { json: JSON.stringify(handoff, null, 2) }
}

export function handlePhaseBrief(args: Record<string, unknown>, ctx: LifecycleContext): HandlerResponse {
  const state = ctx.getActiveRun()
  if (!state) throw new Error('No active run. Call pipeline_init_run first.')

  const phaseName = args.phase as string
  if (!phaseName) throw new Error('phase is required')

  const config = ctx.getConfig()
  const brief = generatePhaseBrief(phaseName, state, config, ctx.getQualityGates())

  return { json: JSON.stringify(brief, null, 2) }
}
