import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import {
  handleFailPhase,
  handleRetryPhase,
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

function makeConfig(optionalPhases: string[] = []): PipelineConfig {
  const phases: Record<string, PhaseDefinition> = {
    discovery: {
      id: 0, description: '', inputs: [], outputs: [], tools: [],
      entry_point: true, optional: false,
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
): LifecycleContext {
  let activeRun: RunState | null = initialState
  let activeRunDir = stateDir

  return {
    getActiveRun: () => activeRun,
    setActiveRun: (s) => { activeRun = s },
    getActiveRunDir: () => activeRunDir,
    setActiveRunDir: (dir) => { activeRunDir = dir },
    getConfig: () => makeConfig(optionalPhases),
    setProjectRoot: () => {},
    getStorageConfig: (): StorageConfig => ({ base_dir: stateDir, paths: {} }),
    initRun: () => initialState,
    skipPhase: (s) => s,
    addArtifact: (s) => s,
    startPhase: (s) => s,
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
