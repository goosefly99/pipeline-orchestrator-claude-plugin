import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import {
  handleFailPhase,
  handleRetryPhase,
  handleStartPhase,
  handleRunStatus,
  type LifecycleContext,
} from '../lifecycle-handlers.ts'
import {
  initRun,
  startPhase,
  failPhase as failPhaseState,
  retryPhase as retryPhaseState,
  recoverRun as recoverRunState,
  removePhaseArtifacts as removePhaseArtifactsState,
} from '../run-state.ts'
import type { RunState, PipelineConfig, ArtifactRef, StorageConfig, PhaseDefinition, GateResult } from '../types.ts'
import type { InputSatisfaction } from '../dag.ts'
import type { RecommendedAction, RecoveryResult } from '../run-state.ts'

let tempDir: string

beforeEach(() => {
  tempDir = mkdtempSync(join(tmpdir(), 'lifecycle-handlers-test-'))
})

afterEach(() => {
  rmSync(tempDir, { recursive: true, force: true })
})

// ── Minimal config factory ────────────────────────────────────

function makeConfig(optionalPhases: string[] = [], phaseModel?: string): PipelineConfig {
  const phases: Record<string, PhaseDefinition> = {
    discovery: {
      id: 0, description: '', inputs: [], outputs: [], tools: [],
      entry_point: true, optional: false,
      model: phaseModel,
    },
    curation: {
      id: 1, description: '', inputs: [], outputs: [], tools: [],
      entry_point: false, optional: optionalPhases.includes('curation'),
    },
  }
  return {
    pipeline: { id: 'test', version: '1.0.0', description: '' },
    phases,
    edges: [],
    debate: {
      agents: {},
      rounds: {},
      output: { includes_transcript: true, includes_refined_artifact: true, artifact_version_bump: 'minor' },
    },
    knowledge_bases: { available_in: [], max_queries_per_phase: 20, query_mode: 'proactive' },
    schemas: {},
    storage: { base_dir: 'test', paths: {} },
  }
}

// ── Mock context factory ──────────────────────────────────────

function makeCtx(
  initialState: RunState,
  stateDir: string,
  optionalPhases: string[] = [],
  config?: PipelineConfig,
): LifecycleContext {
  let activeRun: RunState | null = initialState
  let activeRunDir = stateDir

  return {
    getActiveRun: () => activeRun,
    setActiveRun: (s) => { activeRun = s },
    getActiveRunDir: () => activeRunDir,
    setActiveRunDir: (dir) => { activeRunDir = dir },
    getConfig: () => config ?? makeConfig(optionalPhases),
    setProjectRoot: () => {},
    getStorageConfig: (): StorageConfig => ({ base_dir: stateDir, paths: {} }),
    initRun: () => initialState,
    skipPhase: (s) => s,
    addArtifact: (s) => s,
    startPhase: (s, phase, dir) => {
      const updated = startPhase(s, phase, dir)
      activeRun = updated
      return updated
    },
    completePhase: (s) => s,
    failPhase: (s, phase, error, dir) => {
      const updated = failPhaseState(s, phase, error, dir)
      activeRun = updated
      return updated
    },
    retryPhase: (s, phase, dir) => {
      const updated = retryPhaseState(s, phase, dir)
      activeRun = updated
      return updated
    },
    resolveNextPhases: () => [],
    getPhaseInputSatisfaction: (phase): InputSatisfaction => ({
      satisfied: [],
      missing: phase.inputs,
      can_run: false,
    }),
    loadRunState: () => null,
    recoverRun: (runDir: string): RecoveryResult => recoverRunState(runDir),
    removePhaseArtifacts: (s: RunState, phase: string, dir: string) => removePhaseArtifactsState(s, phase, dir),
    getQualityGates: () => [],
    runGateChecks: (): GateResult => ({ phase: '', passed: true, on_failure: 'warn', results: [] }),
    getHooksConfig: () => [],
    runHooks: () => [],
    runPrePipelineInitHooks: () => ({ parameters: {}, userPrompts: [] }),
    getProjectRoot: () => null,
    computeRecommendedAction: (): RecommendedAction => ({ action: 'review', reason: '' }),
    computeRunWarnings: () => [],
  }
}

// ── handleFailPhase ───────────────────────────────────────────

describe('handleFailPhase — next_step guidance', () => {
  it('non-optional phase: next_step mentions pipeline_retry_phase, not pipeline_init_run', () => {
    let state = initRun('t1', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    const ctx = makeCtx(state, tempDir)

    const result = handleFailPhase({ phase: 'discovery', error: 'API timeout' }, ctx)
    const envelope = JSON.parse(result.json) as { status: string; next_step: string }

    assert.equal(envelope.status, 'error')
    assert.ok(
      envelope.next_step.includes('pipeline_retry_phase'),
      `next_step must mention pipeline_retry_phase — got: "${envelope.next_step}"`,
    )
    assert.ok(
      !envelope.next_step.includes('pipeline_init_run'),
      `next_step must not mention pipeline_init_run — got: "${envelope.next_step}"`,
    )
  })

  it('optional phase: next_step mentions pipeline_next_phases to skip and continue', () => {
    let state = initRun('t2', '1.0.0', ['curation'], tempDir)
    state = startPhase(state, 'curation', tempDir)
    const ctx = makeCtx(state, tempDir, ['curation'])

    const result = handleFailPhase({ phase: 'curation', error: 'external service down' }, ctx)
    const envelope = JSON.parse(result.json) as { status: string; next_step: string }

    assert.equal(envelope.status, 'error')
    assert.ok(
      envelope.next_step.includes('pipeline_next_phases'),
      `optional phase next_step must mention pipeline_next_phases — got: "${envelope.next_step}"`,
    )
  })

  it('response data includes phase name and error string', () => {
    let state = initRun('t3', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    const ctx = makeCtx(state, tempDir)

    const result = handleFailPhase({ phase: 'discovery', error: 'Connection refused' }, ctx)
    const envelope = JSON.parse(result.json) as { data: { phase: string; error: string } }

    assert.equal(envelope.data.phase, 'discovery')
    assert.equal(envelope.data.error, 'Connection refused')
  })
})

// ── handleRetryPhase ──────────────────────────────────────────

describe('handleRetryPhase', () => {
  it('returns ok envelope with pending status and incremented retry_count', () => {
    let state = initRun('r1', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    state = failPhaseState(state, 'discovery', 'first failure', tempDir)
    const ctx = makeCtx(state, tempDir)

    const result = handleRetryPhase({ phase: 'discovery' }, ctx)
    const envelope = JSON.parse(result.json) as {
      status: string
      data: { phase: string; status: string; retry_count: number }
      next_step: string
    }

    assert.equal(envelope.status, 'ok')
    assert.equal(envelope.data.phase, 'discovery')
    assert.equal(envelope.data.status, 'pending')
    assert.equal(envelope.data.retry_count, 1)
    assert.ok(
      envelope.next_step.includes('pipeline_start_phase'),
      `next_step must mention pipeline_start_phase — got: "${envelope.next_step}"`,
    )
  })

  it('retry_count increments on successive retries', () => {
    let state = initRun('r2', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    state = failPhaseState(state, 'discovery', 'fail #1', tempDir)

    // First retry
    state = retryPhaseState(state, 'discovery', tempDir)
    state = startPhase(state, 'discovery', tempDir)
    state = failPhaseState(state, 'discovery', 'fail #2', tempDir)

    const ctx = makeCtx(state, tempDir)
    const result = handleRetryPhase({ phase: 'discovery' }, ctx)
    const envelope = JSON.parse(result.json) as { data: { retry_count: number } }

    assert.equal(envelope.data.retry_count, 2)
  })

  it('throws when no active run', () => {
    const ctx = makeCtx(null as unknown as RunState, tempDir)
    // Override getActiveRun to return null
    ctx.getActiveRun = () => null

    assert.throws(
      () => handleRetryPhase({ phase: 'discovery' }, ctx),
      /No active run/,
    )
  })

  it('propagates retryPhase error when phase is not in failed status', () => {
    const state = initRun('r3', '1.0.0', ['discovery'], tempDir)
    // discovery is 'pending', not 'failed'
    const ctx = makeCtx(state, tempDir)

    assert.throws(
      () => handleRetryPhase({ phase: 'discovery' }, ctx),
      /Cannot retry phase "discovery": status is "pending"/,
    )
  })
})

// ── handleStartPhase — resolved_model in phase_started event ──

describe('handleStartPhase — resolved_model (M5.2)', () => {
  it('phase_started event contains resolved_model defaulting to claude-sonnet-4-6', () => {
    const state = initRun('m52-default', '1.0.0', ['discovery'], tempDir)
    const ctx = makeCtx(state, tempDir)

    handleStartPhase({ phase: 'discovery' }, ctx)

    const eventsPath = join(tempDir, 'events.jsonl')
    const lines = readFileSync(eventsPath, 'utf-8').trim().split('\n')
    const phaseStartedEvent = lines
      .map(l => JSON.parse(l) as Record<string, unknown>)
      .find(e => e.event === 'phase_started')

    assert.ok(phaseStartedEvent, 'phase_started event must exist in events.jsonl')
    assert.equal(
      phaseStartedEvent.resolved_model,
      'claude-sonnet-4-6',
      'resolved_model must default to claude-sonnet-4-6',
    )
  })

  it('phase_started event uses run_parameters.phase_model when no phase-level model', () => {
    let state = initRun('m52-run-param', '1.0.0', ['discovery'], tempDir)
    state = { ...state, run_parameters: { phase_model: 'claude-haiku-4-5' } }
    const ctx = makeCtx(state, tempDir)

    handleStartPhase({ phase: 'discovery' }, ctx)

    const eventsPath = join(tempDir, 'events.jsonl')
    const lines = readFileSync(eventsPath, 'utf-8').trim().split('\n')
    const phaseStartedEvent = lines
      .map(l => JSON.parse(l) as Record<string, unknown>)
      .find(e => e.event === 'phase_started')

    assert.ok(phaseStartedEvent, 'phase_started event must exist')
    assert.equal(
      phaseStartedEvent.resolved_model,
      'claude-haiku-4-5',
      'resolved_model must match run_parameters.phase_model',
    )
  })

  it('phase_started event uses phase-level model over run_parameters.phase_model', () => {
    let state = initRun('m52-phase-model', '1.0.0', ['discovery'], tempDir)
    state = { ...state, run_parameters: { phase_model: 'claude-haiku-4-5' } }
    const config = makeConfig([], 'claude-opus-4-5')
    const ctx = makeCtx(state, tempDir, [], config)

    handleStartPhase({ phase: 'discovery' }, ctx)

    const eventsPath = join(tempDir, 'events.jsonl')
    const lines = readFileSync(eventsPath, 'utf-8').trim().split('\n')
    const phaseStartedEvent = lines
      .map(l => JSON.parse(l) as Record<string, unknown>)
      .find(e => e.event === 'phase_started')

    assert.ok(phaseStartedEvent, 'phase_started event must exist')
    assert.equal(
      phaseStartedEvent.resolved_model,
      'claude-opus-4-5',
      'phase-level model must take precedence over run_parameters.phase_model',
    )
  })

  it('handleStartPhase response body includes resolved_model', () => {
    const state = initRun('m52-response', '1.0.0', ['discovery'], tempDir)
    const ctx = makeCtx(state, tempDir)

    const result = handleStartPhase({ phase: 'discovery' }, ctx)
    const body = JSON.parse(result.json) as Record<string, unknown>

    assert.equal(body.resolved_model, 'claude-sonnet-4-6')
  })
})

// ── handleRunStatus — current_phase_resolved_model (M5.3) ────

describe('handleRunStatus — current_phase_resolved_model (M5.3)', () => {
  it('no current_phase_resolved_model when no phase is in_progress', () => {
    const state = initRun('m53-no-phase', '1.0.0', ['discovery'], tempDir)
    const ctx = makeCtx(state, tempDir)

    const result = handleRunStatus(ctx)
    const body = JSON.parse(result.json) as Record<string, unknown>

    assert.equal(
      body.current_phase_resolved_model,
      undefined,
      'current_phase_resolved_model must be absent when no phase is in_progress',
    )
  })

  it('current_phase_resolved_model defaults to claude-sonnet-4-6 when phase is in_progress', () => {
    let state = initRun('m53-default', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    const ctx = makeCtx(state, tempDir)

    const result = handleRunStatus(ctx)
    const body = JSON.parse(result.json) as Record<string, unknown>

    assert.equal(
      body.current_phase_resolved_model,
      'claude-sonnet-4-6',
      'current_phase_resolved_model must default to claude-sonnet-4-6',
    )
  })

  it('current_phase_resolved_model uses run_parameters.phase_model when no phase-level model', () => {
    let state = initRun('m53-run-param', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    state = { ...state, run_parameters: { phase_model: 'claude-haiku-4-5' } }
    const ctx = makeCtx(state, tempDir)

    const result = handleRunStatus(ctx)
    const body = JSON.parse(result.json) as Record<string, unknown>

    assert.equal(
      body.current_phase_resolved_model,
      'claude-haiku-4-5',
      'current_phase_resolved_model must match run_parameters.phase_model',
    )
  })

  it('current_phase_resolved_model uses phase-level model over run_parameters.phase_model', () => {
    let state = initRun('m53-phase-model', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    state = { ...state, run_parameters: { phase_model: 'claude-haiku-4-5' } }
    const config = makeConfig([], 'claude-opus-4-5')
    const ctx = makeCtx(state, tempDir, [], config)

    const result = handleRunStatus(ctx)
    const body = JSON.parse(result.json) as Record<string, unknown>

    assert.equal(
      body.current_phase_resolved_model,
      'claude-opus-4-5',
      'phase-level model must take precedence in handleRunStatus',
    )
  })
})
