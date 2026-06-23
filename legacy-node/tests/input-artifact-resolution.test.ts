import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { handleStartPhase, type LifecycleContext } from '../lifecycle-handlers.ts'
import {
  initRun,
  startPhase as startPhaseState,
  completePhase as completePhaseState,
  addArtifact as addArtifactState,
  loadRunState,
  resolveInputArtifacts,
} from '../run-state.ts'
import type {
  RunState,
  PipelineConfig,
  PhaseDefinition,
  EdgeDefinition,
  ArtifactRef,
  StorageConfig,
  GateResult,
} from '../types.ts'
import type { InputSatisfaction } from '../dag.ts'
import type { RecommendedAction, RecoveryResult } from '../run-state.ts'

let tempDir: string

beforeEach(() => {
  tempDir = mkdtempSync(join(tmpdir(), 'input-artifact-resolution-test-'))
})

afterEach(() => {
  rmSync(tempDir, { recursive: true, force: true })
})

// ── Minimal config factory ────────────────────────────────────

function makeConfig(
  phases: Record<string, Partial<PhaseDefinition>>,
  edges: EdgeDefinition[] = [],
): PipelineConfig {
  const fullPhases: Record<string, PhaseDefinition> = {}
  for (const [name, partial] of Object.entries(phases)) {
    fullPhases[name] = {
      id: name,
      description: partial.description ?? '',
      inputs: partial.inputs ?? [],
      outputs: partial.outputs ?? [],
      tools: partial.tools ?? [],
      entry_point: partial.entry_point ?? false,
      optional: partial.optional ?? false,
      input_mode: partial.input_mode,
    }
  }
  return {
    pipeline: { id: 'test', version: '1.0.0', description: '' },
    phases: fullPhases,
    edges,
    debate: {
      agents: {},
      rounds: {},
      output: {
        includes_transcript: true,
        includes_refined_artifact: true,
        artifact_version_bump: 'minor',
      },
    },
    knowledge_bases: { available_in: [], max_queries_per_phase: 20, query_mode: 'proactive' },
    schemas: {},
    storage: { base_dir: 'test', paths: {} },
  }
}

// ── Mock context factory backed by real run-state helpers ─────

function makeCtx(
  initialState: RunState,
  stateDir: string,
  config: PipelineConfig,
): { ctx: LifecycleContext; getState: () => RunState } {
  let activeRun: RunState | null = initialState
  let activeRunDir = stateDir

  const ctx: LifecycleContext = {
    getActiveRun: () => activeRun,
    setActiveRun: (s) => { activeRun = s },
    getActiveRunDir: () => activeRunDir,
    setActiveRunDir: (dir) => { activeRunDir = dir },
    getConfig: () => config,
    setProjectRoot: () => {},
    getStorageConfig: (): StorageConfig => ({ base_dir: stateDir, paths: {} }),
    initRun: () => initialState,
    skipPhase: (s) => s,
    addArtifact: (s, ref, dir) => {
      const updated = addArtifactState(s, ref, dir)
      activeRun = updated
      return updated
    },
    startPhase: (s, phase, dir) => {
      const updated = startPhaseState(s, phase, dir)
      activeRun = updated
      return updated
    },
    completePhase: (s, phase, dir) => {
      const updated = completePhaseState(s, phase, dir)
      activeRun = updated
      return updated
    },
    failPhase: (s) => s,
    retryPhase: (s) => s,
    resolveNextPhases: () => [],
    getPhaseInputSatisfaction: (phase): InputSatisfaction => ({
      satisfied: [],
      missing: phase.inputs,
      can_run: true,
    }),
    loadRunState: () => null,
    recoverRun: (): RecoveryResult => ({ state: activeRun!, recovered_phases: [], warnings: [] }),
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

  return { ctx, getState: () => activeRun! }
}

function makeArtifactRef(path: string, phase: string, type = 'doc'): ArtifactRef {
  return {
    type,
    path,
    phase,
    created_at: new Date().toISOString(),
    version: 1,
  }
}

// ── resolveInputArtifacts (pure helper) ───────────────────────

describe('resolveInputArtifacts (run-state helper)', () => {
  it('returns empty for an entry_point phase with no incoming edges', () => {
    const config = makeConfig({
      phase_a: { entry_point: true },
    })
    const state = initRun('t-empty', '1.0.0', ['phase_a'], tempDir)
    assert.deepEqual(resolveInputArtifacts(state, 'phase_a', config), [])
  })

  it('walks incoming edges and collects output_artifacts from upstream phases', () => {
    const config = makeConfig(
      {
        phase_a: { entry_point: true, outputs: ['doc'] },
        phase_b: { entry_point: false, inputs: ['doc'] },
      },
      [{ from: 'phase_a', to: 'phase_b' }],
    )

    let state = initRun('t-walk', '1.0.0', ['phase_a', 'phase_b'], tempDir)
    state = startPhaseState(state, 'phase_a', tempDir)
    state = addArtifactState(state, makeArtifactRef('/tmp/a1.json', 'phase_a'), tempDir)
    state = addArtifactState(state, makeArtifactRef('/tmp/a2.json', 'phase_a'), tempDir)
    state = completePhaseState(state, 'phase_a', tempDir)

    assert.deepEqual(
      resolveInputArtifacts(state, 'phase_b', config),
      ['/tmp/a1.json', '/tmp/a2.json'],
    )
  })

  it('deduplicates artifact paths across multiple upstream phases, preserving first-seen order', () => {
    const config = makeConfig(
      {
        up_a: { entry_point: true, outputs: ['doc'] },
        up_b: { entry_point: true, outputs: ['doc'] },
        phase_b: { entry_point: false, inputs: ['doc'] },
      },
      [
        { from: 'up_a', to: 'phase_b' },
        { from: 'up_b', to: 'phase_b' },
      ],
    )

    let state = initRun('t-dedup', '1.0.0', ['up_a', 'up_b', 'phase_b'], tempDir)
    state = startPhaseState(state, 'up_a', tempDir)
    state = addArtifactState(state, makeArtifactRef('/tmp/shared.json', 'up_a'), tempDir)
    state = addArtifactState(state, makeArtifactRef('/tmp/only-a.json', 'up_a'), tempDir)
    state = completePhaseState(state, 'up_a', tempDir)

    state = startPhaseState(state, 'up_b', tempDir)
    // up_b registers the SAME path as up_a (dedup case) plus a unique one.
    state = addArtifactState(state, makeArtifactRef('/tmp/shared.json', 'up_b'), tempDir)
    state = addArtifactState(state, makeArtifactRef('/tmp/only-b.json', 'up_b'), tempDir)
    state = completePhaseState(state, 'up_b', tempDir)

    const resolved = resolveInputArtifacts(state, 'phase_b', config)
    // First-seen order: up_a's shared.json → up_a's only-a.json → (up_b's shared.json is dedup'd) → up_b's only-b.json
    assert.deepEqual(resolved, ['/tmp/shared.json', '/tmp/only-a.json', '/tmp/only-b.json'])
    // The shared path should only appear once.
    assert.equal(resolved.filter(p => p === '/tmp/shared.json').length, 1)
  })
})

// ── handleStartPhase integration ──────────────────────────────

describe('handleStartPhase — input_artifacts wiring', () => {
  it('populates phase_b.input_artifacts from upstream phase_a outputs and persists to disk', () => {
    const config = makeConfig(
      {
        phase_a: { entry_point: true, outputs: ['doc'] },
        phase_b: { entry_point: false, inputs: ['doc'] },
      },
      [{ from: 'phase_a', to: 'phase_b' }],
    )

    let state = initRun('t-happy', '1.0.0', ['phase_a', 'phase_b'], tempDir)
    // Drive phase_a through start → add two artifacts → complete
    state = startPhaseState(state, 'phase_a', tempDir)
    state = addArtifactState(state, makeArtifactRef('/tmp/art1.json', 'phase_a'), tempDir)
    state = addArtifactState(state, makeArtifactRef('/tmp/art2.json', 'phase_a'), tempDir)
    state = completePhaseState(state, 'phase_a', tempDir)

    const { ctx, getState } = makeCtx(state, tempDir, config)

    const result = handleStartPhase({ phase: 'phase_b' }, ctx)
    const parsed = JSON.parse(result.json) as {
      phase: string
      status: string
      input_artifacts: string[]
    }

    // Response JSON surfaces the resolved artifacts.
    assert.equal(parsed.phase, 'phase_b')
    assert.equal(parsed.status, 'in_progress')
    assert.deepEqual(
      parsed.input_artifacts,
      ['/tmp/art1.json', '/tmp/art2.json'],
      'handleStartPhase response should include both upstream artifact paths in registration order',
    )

    // In-memory state reflects the persisted input_artifacts.
    const mem = getState()
    assert.deepEqual(
      mem.phases.phase_b.input_artifacts,
      ['/tmp/art1.json', '/tmp/art2.json'],
    )

    // Reload from disk and verify the same thing.
    const disk = loadRunState(tempDir)
    assert.ok(disk, 'run-state.json should be readable from disk')
    assert.deepEqual(
      disk.phases.phase_b.input_artifacts,
      ['/tmp/art1.json', '/tmp/art2.json'],
      'persisted run-state.json should contain the resolved input_artifacts for phase_b',
    )
  })

  it('entry_point phase with no upstream edges yields empty input_artifacts', () => {
    const config = makeConfig({
      phase_a: { entry_point: true, outputs: ['doc'] },
    })

    const state = initRun('t-entry', '1.0.0', ['phase_a'], tempDir)
    const { ctx, getState } = makeCtx(state, tempDir, config)

    const result = handleStartPhase({ phase: 'phase_a' }, ctx)
    const parsed = JSON.parse(result.json) as { input_artifacts: string[] }

    assert.deepEqual(parsed.input_artifacts, [])
    assert.deepEqual(getState().phases.phase_a.input_artifacts, [])

    const disk = loadRunState(tempDir)
    assert.ok(disk)
    assert.deepEqual(disk.phases.phase_a.input_artifacts, [])
  })

  it('deduplicates artifact paths when two upstream phases produced the same path', () => {
    const config = makeConfig(
      {
        up_a: { entry_point: true, outputs: ['doc'] },
        up_b: { entry_point: true, outputs: ['doc'] },
        phase_b: { entry_point: false, inputs: ['doc'] },
      },
      [
        { from: 'up_a', to: 'phase_b' },
        { from: 'up_b', to: 'phase_b' },
      ],
    )

    let state = initRun('t-dedup-int', '1.0.0', ['up_a', 'up_b', 'phase_b'], tempDir)

    state = startPhaseState(state, 'up_a', tempDir)
    state = addArtifactState(state, makeArtifactRef('/tmp/dup.json', 'up_a'), tempDir)
    state = addArtifactState(state, makeArtifactRef('/tmp/only-a.json', 'up_a'), tempDir)
    state = completePhaseState(state, 'up_a', tempDir)

    state = startPhaseState(state, 'up_b', tempDir)
    state = addArtifactState(state, makeArtifactRef('/tmp/dup.json', 'up_b'), tempDir)
    state = addArtifactState(state, makeArtifactRef('/tmp/only-b.json', 'up_b'), tempDir)
    state = completePhaseState(state, 'up_b', tempDir)

    const { ctx, getState } = makeCtx(state, tempDir, config)

    const result = handleStartPhase({ phase: 'phase_b' }, ctx)
    const parsed = JSON.parse(result.json) as { input_artifacts: string[] }

    assert.deepEqual(
      parsed.input_artifacts,
      ['/tmp/dup.json', '/tmp/only-a.json', '/tmp/only-b.json'],
    )
    assert.equal(
      parsed.input_artifacts.filter(p => p === '/tmp/dup.json').length,
      1,
      'shared path should appear only once after dedup',
    )

    // Persisted on disk too.
    const disk = loadRunState(tempDir)
    assert.ok(disk)
    assert.deepEqual(
      disk.phases.phase_b.input_artifacts,
      ['/tmp/dup.json', '/tmp/only-a.json', '/tmp/only-b.json'],
    )

    // In-memory state matches.
    assert.deepEqual(
      getState().phases.phase_b.input_artifacts,
      ['/tmp/dup.json', '/tmp/only-a.json', '/tmp/only-b.json'],
    )
  })
})
