import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, existsSync, readFileSync } from 'node:fs'
import { join, normalize } from 'node:path'
import { tmpdir } from 'node:os'
import {
  handleFailPhase,
  handleRetryPhase,
  handleInitRun,
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
import { getRunDataDir } from '../storage.ts'
import type { RunState, PipelineConfig, ArtifactRef, StorageConfig, PhaseDefinition, GateResult, PipelineRunParameters, PhaseState } from '../types.ts'
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

// ── Feature C step C8 — active run dir redirect ─────────────
//
// After initRun has computed state.run_data_dir from run_parameters
// (run_name + run_directory_timestamp), handleInitRun must redirect the
// stored activeRunDir to that canonical per-run tree so downstream
// appendEvent / persist callers land in pipeline_mcp_data/runs/{name-ts}/
// instead of the legacy pipeline_mcp_data/runs/{run_id}/ path. Legacy runs
// (no run_parameters, state.run_data_dir undefined) must keep the legacy
// path unchanged — the redirect is guarded by the inequality check.

describe('Feature C step C8 — active run dir redirect', () => {
  /**
   * Build a stub RunState with minimal required fields for initRun return
   * mocking. Optional run_data_dir lets callers simulate either the
   * Feature-C canonical layout (non-undefined) or legacy layout (undefined).
   */
  function makeStubRunState(
    runId: string,
    phaseNames: string[],
    runDataDir: string | undefined,
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
      pipeline_version: '1.0.0',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      status: 'initialized',
      phases,
      available_artifacts: [],
      config_path: '',
    }
    if (runDataDir !== undefined) state.run_data_dir = runDataDir
    if (runParameters) state.run_parameters = runParameters
    return state
  }

  /**
   * Build a LifecycleContext that records every call to setActiveRunDir and
   * captures the runParameters argument passed to initRun. Tests can
   * inspect the recorded data afterwards. The stubbed initRun returns the
   * provided RunState verbatim so callers can control whether run_data_dir
   * is populated.
   */
  function makeRecordingCtx(
    baseDir: string,
    stubbedInitRunState: RunState,
    optionalPhases: string[] = [],
  ): LifecycleContext & {
    setActiveRunDirCalls: string[]
    initRunCalls: Array<{
      runId: string
      pipelineVersion: string
      phaseNames: string[]
      stateDir: string
      runParameters: PipelineRunParameters | undefined
    }>
  } {
    const setActiveRunDirCalls: string[] = []
    const initRunCalls: Array<{
      runId: string
      pipelineVersion: string
      phaseNames: string[]
      stateDir: string
      runParameters: PipelineRunParameters | undefined
    }> = []
    let activeRun: RunState | null = null
    let activeRunDir = ''

    const ctx: LifecycleContext = {
      getActiveRun: () => activeRun,
      setActiveRun: (s) => { activeRun = s },
      getActiveRunDir: () => activeRunDir,
      setActiveRunDir: (dir) => {
        setActiveRunDirCalls.push(dir)
        activeRunDir = dir
      },
      getConfig: () => makeConfig(optionalPhases),
      setProjectRoot: () => {},
      getStorageConfig: (): StorageConfig => ({ base_dir: baseDir, paths: {} }),
      initRun: (runId, pipelineVersion, phaseNames, stateDir, runParameters) => {
        initRunCalls.push({ runId, pipelineVersion, phaseNames, stateDir, runParameters })
        return stubbedInitRunState
      },
      skipPhase: (s) => s,
      addArtifact: (s) => s,
      startPhase: (s) => s,
      completePhase: (s) => s,
      failPhase: (s) => s,
      retryPhase: (s) => s,
      resolveNextPhases: () => [],
      getPhaseInputSatisfaction: (phase): InputSatisfaction => ({
        satisfied: [],
        missing: phase.inputs,
        can_run: false,
      }),
      loadRunState: () => null,
      recoverRun: (): RecoveryResult => ({
        state: stubbedInitRunState,
        recovered_phases: [],
        warnings: [],
      }),
      removePhaseArtifacts: () => [],
      getQualityGates: () => [],
      runGateChecks: (): GateResult => ({ phase: '', passed: true, on_failure: 'warn', results: [] }),
      getHooksConfig: () => [],
      runHooks: () => [],
      runPrePipelineInitHooks: () => ({ parameters: {}, userPrompts: [] }),
      getProjectRoot: () => null,
      computeRecommendedAction: (): RecommendedAction => ({ action: 'review', reason: '' }),
      computeRunWarnings: () => [],
    }

    return Object.assign(ctx, { setActiveRunDirCalls, initRunCalls })
  }

  it('handleInitRun with run_parameters redirects activeRunDir to canonical run_data_dir', () => {
    // Caller-supplied run_parameters — handler must forward these through to
    // ctx.initRun AND consume the resulting run_data_dir to redirect activeRunDir.
    const runId = 'feature-c-run'
    const runName = 'feature-c-test'
    const runDirectoryTimestamp = '2026-04-10T09-13-58-000Z'
    const runParameters: PipelineRunParameters = {
      run_name: runName,
      run_directory_timestamp: runDirectoryTimestamp,
    }

    // Canonical path the redirect should land on. Computed via the same
    // helper production code uses so the sanitization rules stay in sync.
    const canonicalRunDataDir = getRunDataDir(tempDir, runName, runDirectoryTimestamp)
    const legacyRunDir = join(tempDir, 'runs', runId)

    // Stub initRun so it returns a RunState carrying the canonical path in
    // run_data_dir — simulating the production behavior from run-state.ts.
    const stubbedState = makeStubRunState(runId, ['discovery', 'curation'], canonicalRunDataDir, runParameters)
    const ctx = makeRecordingCtx(tempDir, stubbedState)

    handleInitRun(
      {
        run_id: runId,
        project_root: tempDir,
        base_dir: tempDir,
        run_parameters: runParameters as unknown as Record<string, unknown>,
      },
      ctx,
    )

    // initRun must have been called exactly once, and must have received the
    // caller-supplied run_parameters object verbatim. This is what lets the
    // production initRun compute run_data_dir internally.
    assert.equal(ctx.initRunCalls.length, 1, 'ctx.initRun should be called exactly once')
    assert.deepEqual(
      ctx.initRunCalls[0].runParameters,
      runParameters,
      'run_parameters must be forwarded to ctx.initRun',
    )
    // Sanity: the stateDir argument passed to initRun must be the legacy path
    // so that run-state.ts can derive baseDir via dirname(dirname(stateDir)).
    assert.equal(
      normalize(ctx.initRunCalls[0].stateDir),
      normalize(legacyRunDir),
      'stateDir passed to ctx.initRun must be the legacy runs/{runId} path',
    )

    // setActiveRunDir must be called at least twice:
    //   1. First with legacyRunDir (initial assignment before initRun runs).
    //   2. Then with the canonical run_data_dir path (the redirect).
    assert.ok(
      ctx.setActiveRunDirCalls.length >= 2,
      `expected at least 2 setActiveRunDir calls, got ${ctx.setActiveRunDirCalls.length}`,
    )
    assert.equal(
      normalize(ctx.setActiveRunDirCalls[0]),
      normalize(legacyRunDir),
      'first setActiveRunDir call must be the legacy path',
    )
    // Last call must be the canonical path — any intermediate calls are fine.
    const lastCall = ctx.setActiveRunDirCalls[ctx.setActiveRunDirCalls.length - 1]
    assert.equal(
      normalize(lastCall),
      normalize(canonicalRunDataDir),
      'last setActiveRunDir call must be the canonical run_data_dir',
    )
    // And the canonical path must not equal the legacy path — otherwise the
    // redirect is a no-op and we'd be asserting the wrong thing.
    assert.notEqual(
      normalize(canonicalRunDataDir),
      normalize(legacyRunDir),
      'test precondition: canonical run_data_dir must differ from legacy path',
    )
  })

  it('handleInitRun without run_parameters leaves activeRunDir on the legacy path', () => {
    // No run_parameters → production initRun leaves state.run_data_dir
    // undefined. The handler guard `if (activeRunDir !== legacyRunDir)` is
    // therefore false (both sides equal legacyRunDir), so the second
    // setActiveRunDir call is skipped entirely.
    const runId = 'legacy-run'
    const legacyRunDir = join(tempDir, 'runs', runId)

    const stubbedState = makeStubRunState(runId, ['discovery', 'curation'], undefined)
    const ctx = makeRecordingCtx(tempDir, stubbedState)

    handleInitRun(
      {
        run_id: runId,
        project_root: tempDir,
        base_dir: tempDir,
      },
      ctx,
    )

    // initRun is called, but with undefined run_parameters (or {} — the
    // handler forwards the result of merging hook params with caller params,
    // which for a no-hook, no-caller-params run is an empty object literal).
    assert.equal(ctx.initRunCalls.length, 1, 'ctx.initRun should be called exactly once')

    // setActiveRunDir must fire exactly once — the initial assignment to
    // legacyRunDir. The guarded redirect block is skipped because the
    // stubbed initRun returned no run_data_dir.
    assert.equal(
      ctx.setActiveRunDirCalls.length,
      1,
      `expected exactly 1 setActiveRunDir call for legacy path, got ${ctx.setActiveRunDirCalls.length}: ${JSON.stringify(ctx.setActiveRunDirCalls)}`,
    )
    assert.equal(
      normalize(ctx.setActiveRunDirCalls[0]),
      normalize(legacyRunDir),
      'the single setActiveRunDir call must be the legacy path',
    )
  })

  it('handleStartPhase after redirect writes events.jsonl under run_data_dir (real disk)', () => {
    // Full integration test: wire real run-state.ts initRun and startPhase
    // into the LifecycleContext so the handler's appendEvent/persist calls
    // land on actual disk. Then assert events.jsonl is under the canonical
    // run_data_dir and NOT under the legacy runs/{runId}/ path.
    const runId = 'c8-integration'
    const runName = 'integration-test'
    const runDirectoryTimestamp = '2026-04-10T10-00-00-000Z'
    const runParameters: PipelineRunParameters = {
      run_name: runName,
      run_directory_timestamp: runDirectoryTimestamp,
    }

    const canonicalRunDataDir = getRunDataDir(tempDir, runName, runDirectoryTimestamp)
    const legacyRunDir = join(tempDir, 'runs', runId)

    // Real disk-backed LifecycleContext — initRun and startPhase come from
    // run-state.ts, not stubs. activeRun / activeRunDir are held in closure
    // state so setActiveRunDir actually updates the dir observed by
    // subsequent getActiveRunDir calls.
    let activeRun: RunState | null = null
    let activeRunDir = ''

    const ctx: LifecycleContext = {
      getActiveRun: () => activeRun,
      setActiveRun: (s) => { activeRun = s },
      getActiveRunDir: () => activeRunDir,
      setActiveRunDir: (dir) => { activeRunDir = dir },
      getConfig: () => makeConfig(),
      setProjectRoot: () => {},
      getStorageConfig: (): StorageConfig => ({ base_dir: tempDir, paths: {} }),
      initRun: (rId, pv, phases, stateDir, rp) =>
        initRun(rId, pv, phases, stateDir, rp),
      skipPhase: (s) => s,
      addArtifact: (s) => s,
      startPhase: (s, phase, dir) => startPhase(s, phase, dir),
      completePhase: (s) => s,
      failPhase: (s) => s,
      retryPhase: (s) => s,
      resolveNextPhases: () => [],
      getPhaseInputSatisfaction: (phase): InputSatisfaction => ({
        satisfied: [],
        missing: phase.inputs,
        can_run: false,
      }),
      loadRunState: () => null,
      recoverRun: (): RecoveryResult => ({
        state: activeRun as RunState,
        recovered_phases: [],
        warnings: [],
      }),
      removePhaseArtifacts: () => [],
      getQualityGates: () => [],
      runGateChecks: (): GateResult => ({ phase: '', passed: true, on_failure: 'warn', results: [] }),
      getHooksConfig: () => [],
      runHooks: () => [],
      runPrePipelineInitHooks: () => ({ parameters: {}, userPrompts: [] }),
      getProjectRoot: () => tempDir,
      computeRecommendedAction: (): RecommendedAction => ({ action: 'review', reason: '' }),
      computeRunWarnings: () => [],
    }

    handleInitRun(
      {
        run_id: runId,
        project_root: tempDir,
        base_dir: tempDir,
        run_parameters: runParameters as unknown as Record<string, unknown>,
      },
      ctx,
    )

    // After init, activeRunDir must point to the canonical run_data_dir and
    // the persisted run-state.json must live there — not under legacyRunDir.
    assert.equal(
      normalize(activeRunDir),
      normalize(canonicalRunDataDir),
      'activeRunDir must have been redirected to canonical run_data_dir',
    )
    // Also assert the RunState field itself was populated by real initRun's
    // dirname(dirname(stateDir)) → getRunDataDir() chain (not a hand-baked
    // stub). This is what the fast unit tests above can't verify, so it
    // anchors the real production contract in this integration test.
    assert.ok(activeRun, 'activeRun must be set after handleInitRun')
    assert.equal(
      normalize((activeRun as RunState).run_data_dir ?? ''),
      normalize(canonicalRunDataDir),
      'activeRun.run_data_dir must be the canonical path computed by real initRun',
    )
    assert.equal(
      existsSync(join(canonicalRunDataDir, 'run-state.json')),
      true,
      'run-state.json must be written under canonical run_data_dir',
    )
    assert.equal(
      existsSync(join(legacyRunDir, 'run-state.json')),
      false,
      'run-state.json must NOT be written under legacy runs/{runId} path',
    )

    // Now kick off a phase — handler appends phase_started to events.jsonl
    // at activeRunDir, which the redirect has pointed at canonicalRunDataDir.
    handleStartPhase({ phase: 'discovery' }, ctx)

    // events.jsonl must exist under the canonical run_data_dir...
    const canonicalEventsPath = join(canonicalRunDataDir, 'events.jsonl')
    assert.equal(
      existsSync(canonicalEventsPath),
      true,
      'events.jsonl must be written under canonical run_data_dir',
    )
    // ...and must NOT exist under the legacy runs/{runId}/ path.
    const legacyEventsPath = join(legacyRunDir, 'events.jsonl')
    assert.equal(
      existsSync(legacyEventsPath),
      false,
      'events.jsonl must NOT be written under legacy runs/{runId} path',
    )

    // Parse the first event line and confirm it is a phase_started event
    // for the phase we just started.
    const raw = readFileSync(canonicalEventsPath, 'utf-8')
    const firstLine = raw.split('\n').find(l => l.trim().length > 0)
    assert.ok(firstLine, 'events.jsonl must contain at least one non-empty line')
    const parsedEvent = JSON.parse(firstLine!) as {
      event: string
      phase: string
      run_id: string
      timestamp: string
    }
    assert.equal(parsedEvent.event, 'phase_started', 'first event must be phase_started')
    assert.equal(parsedEvent.phase, 'discovery', 'event phase must match the started phase')
    assert.equal(parsedEvent.run_id, runId, 'event run_id must match')
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
