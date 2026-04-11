import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, writeFileSync, mkdirSync, readFileSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import {
  checkFieldPresent,
  checkMinItems,
  checkCrossRefValid,
  runCheck,
  runGateChecks,
  loadArtifactForGate,
} from '../quality-gates.ts'
import { loadQualityGates } from '../toml-loader.ts'
import {
  initRun,
  startPhase,
  addArtifact,
  removePhaseArtifacts,
  loadRunState,
  completePhase as completePhaseState,
} from '../run-state.ts'
import { handleCompletePhase, type LifecycleContext } from '../lifecycle-handlers.ts'
import type {
  QualityCheck,
  QualityGate,
  ArtifactRef,
  GateResult,
  RunState,
  PipelineConfig,
  PhaseDefinition,
  StorageConfig,
} from '../types.ts'
import { PipelineError } from '../types.ts'
import type { InputSatisfaction } from '../dag.ts'
import type { RecommendedAction, RecoveryResult } from '../run-state.ts'

let tempDir: string

beforeEach(() => {
  tempDir = mkdtempSync(join(tmpdir(), 'quality-gates-test-'))
})

afterEach(() => {
  rmSync(tempDir, { recursive: true, force: true })
})

// ── checkFieldPresent ────────────────────────────────────────

describe('checkFieldPresent', () => {
  it('passes when field exists and is non-empty string', () => {
    const result = checkFieldPresent({ name: 'test' }, { field: 'name' })
    assert.equal(result.passed, true)
    assert.equal(result.check_type, 'field_present')
  })

  it('passes when field exists and is non-empty array', () => {
    const result = checkFieldPresent({ items: [1, 2, 3] }, { field: 'items' })
    assert.equal(result.passed, true)
  })

  it('fails when field is missing', () => {
    const result = checkFieldPresent({ name: 'test' }, { field: 'missing' })
    assert.equal(result.passed, false)
    assert.ok(result.message.includes('missing'))
  })

  it('fails when field is null', () => {
    const result = checkFieldPresent({ name: null }, { field: 'name' })
    assert.equal(result.passed, false)
  })

  it('fails when field is empty string', () => {
    const result = checkFieldPresent({ name: '  ' }, { field: 'name' })
    assert.equal(result.passed, false)
    assert.ok(result.message.includes('empty'))
  })

  it('fails when field is empty array', () => {
    const result = checkFieldPresent({ items: [] }, { field: 'items' })
    assert.equal(result.passed, false)
    assert.ok(result.message.includes('empty array'))
  })

  it('fails when field param is missing', () => {
    const result = checkFieldPresent({ name: 'test' }, {})
    assert.equal(result.passed, false)
    assert.ok(result.message.includes('misconfigured'))
  })
})

// ── checkMinItems ────────────────────────────────────────────

describe('checkMinItems', () => {
  it('passes when array has at least min items', () => {
    const result = checkMinItems({ items: [1, 2, 3] }, { field: 'items', min: 2 })
    assert.equal(result.passed, true)
    assert.ok(result.message.includes('3 items'))
  })

  it('passes when array has exactly min items', () => {
    const result = checkMinItems({ items: [1, 2] }, { field: 'items', min: 2 })
    assert.equal(result.passed, true)
  })

  it('fails when array has fewer than min items', () => {
    const result = checkMinItems({ items: [1] }, { field: 'items', min: 3 })
    assert.equal(result.passed, false)
    assert.ok(result.message.includes('1 items'))
    assert.ok(result.message.includes('minimum: 3'))
  })

  it('fails when field is not an array', () => {
    const result = checkMinItems({ items: 'not-array' }, { field: 'items', min: 1 })
    assert.equal(result.passed, false)
    assert.ok(result.message.includes('not an array'))
  })

  it('fails when params are missing', () => {
    const result = checkMinItems({ items: [1] }, {})
    assert.equal(result.passed, false)
    assert.ok(result.message.includes('misconfigured'))
  })
})

// ── checkCrossRefValid ───────────────────────────────────────

describe('checkCrossRefValid', () => {
  it('passes when all cross-references are valid (array target)', () => {
    const artifact = { refs: ['a', 'b'] }
    const targetArtifact = { ids: ['a', 'b', 'c'] }
    const allArtifacts = new Map([['target-type', targetArtifact]])

    const result = checkCrossRefValid(
      artifact as unknown as Record<string, unknown>,
      { source_field: 'refs', target_artifact_type: 'target-type', target_field: 'ids' },
      allArtifacts as unknown as Map<string, Record<string, unknown>>,
    )
    assert.equal(result.passed, true)
  })

  it('passes when all cross-references are valid (object target)', () => {
    const artifact = { refs: ['key1', 'key2'] }
    const targetArtifact = { items: { key1: 'v1', key2: 'v2', key3: 'v3' } }
    const allArtifacts = new Map([['target-type', targetArtifact]])

    const result = checkCrossRefValid(
      artifact as unknown as Record<string, unknown>,
      { source_field: 'refs', target_artifact_type: 'target-type', target_field: 'items' },
      allArtifacts as unknown as Map<string, Record<string, unknown>>,
    )
    assert.equal(result.passed, true)
  })

  it('fails when cross-references are invalid', () => {
    const artifact = { refs: ['a', 'missing'] }
    const targetArtifact = { ids: ['a', 'b'] }
    const allArtifacts = new Map([['target-type', targetArtifact]])

    const result = checkCrossRefValid(
      artifact as unknown as Record<string, unknown>,
      { source_field: 'refs', target_artifact_type: 'target-type', target_field: 'ids' },
      allArtifacts as unknown as Map<string, Record<string, unknown>>,
    )
    assert.equal(result.passed, false)
    assert.ok(result.message.includes('1 value(s)'))
  })

  it('fails when target artifact is not found', () => {
    const artifact = { refs: ['a'] }
    const allArtifacts = new Map<string, Record<string, unknown>>()

    const result = checkCrossRefValid(artifact, { source_field: 'refs', target_artifact_type: 'missing-type', target_field: 'ids' }, allArtifacts)
    assert.equal(result.passed, false)
    assert.ok(result.message.includes('not found'))
  })

  it('fails when params are missing', () => {
    const result = checkCrossRefValid({}, {}, new Map())
    assert.equal(result.passed, false)
    assert.ok(result.message.includes('misconfigured'))
  })
})

// ── runCheck dispatcher ──────────────────────────────────────

describe('runCheck', () => {
  it('dispatches to field_present check', () => {
    const check: QualityCheck = { check_type: 'field_present', description: 'test', params: { field: 'name' } }
    const result = runCheck(check, { name: 'value' }, new Map())
    assert.equal(result.passed, true)
    assert.equal(result.check_type, 'field_present')
  })

  it('dispatches to min_items check', () => {
    const check: QualityCheck = { check_type: 'min_items', description: 'test', params: { field: 'items', min: 1 } }
    const result = runCheck(check, { items: [1, 2] }, new Map())
    assert.equal(result.passed, true)
    assert.equal(result.check_type, 'min_items')
  })

  it('returns failure for unknown check type', () => {
    const check = { check_type: 'unknown_check', description: 'test', params: {} } as unknown as QualityCheck
    const result = runCheck(check, {}, new Map())
    assert.equal(result.passed, false)
    assert.ok(result.message.includes('Unknown check type'))
  })
})

// ── loadArtifactForGate ──────────────────────────────────────

describe('loadArtifactForGate', () => {
  it('loads a valid JSON artifact from disk', () => {
    const path = join(tempDir, 'artifact.json')
    writeFileSync(path, JSON.stringify({ name: 'test', items: [1, 2] }), 'utf-8')

    const result = loadArtifactForGate(path)
    assert.ok(result)
    assert.equal(result.name, 'test')
  })

  it('returns null for nonexistent file', () => {
    const result = loadArtifactForGate(join(tempDir, 'nonexistent.json'))
    assert.equal(result, null)
  })

  it('returns null for invalid JSON', () => {
    const path = join(tempDir, 'bad.json')
    writeFileSync(path, '{ broken json', 'utf-8')

    const result = loadArtifactForGate(path)
    assert.equal(result, null)
  })
})

// ── runGateChecks orchestrator ───────────────────────────────

describe('runGateChecks', () => {
  it('returns passed=true when all checks pass', () => {
    const artifactPath = join(tempDir, 'artifact.json')
    writeFileSync(artifactPath, JSON.stringify({ sources: ['s1'], items: [1, 2, 3] }), 'utf-8')

    const gate: QualityGate = {
      phase: 'curation',
      on_failure: 'block',
      checks: [
        { check_type: 'field_present', description: 'sources present', params: { field: 'sources' } },
        { check_type: 'min_items', description: 'min items', params: { field: 'items', min: 1 } },
      ],
    }

    const refs: ArtifactRef[] = [
      { type: 'curated-collection', path: artifactPath, phase: 'curation', created_at: new Date().toISOString() },
    ]

    const result = runGateChecks(gate, refs)
    assert.equal(result.passed, true)
    assert.equal(result.phase, 'curation')
    assert.equal(result.results.length, 2)
    assert.ok(result.results.every(r => r.passed))
  })

  it('returns passed=false when any check fails', () => {
    const artifactPath = join(tempDir, 'artifact.json')
    writeFileSync(artifactPath, JSON.stringify({ items: [] }), 'utf-8')

    const gate: QualityGate = {
      phase: 'curation',
      on_failure: 'block',
      checks: [
        { check_type: 'field_present', description: 'sources present', params: { field: 'sources' } },
        { check_type: 'min_items', description: 'min items', params: { field: 'items', min: 1 } },
      ],
    }

    const refs: ArtifactRef[] = [
      { type: 'curated-collection', path: artifactPath, phase: 'curation', created_at: new Date().toISOString() },
    ]

    const result = runGateChecks(gate, refs)
    assert.equal(result.passed, false)
    const failed = result.results.filter(r => !r.passed)
    assert.ok(failed.length >= 1)
  })

  it('fails check when no artifacts available', () => {
    const gate: QualityGate = {
      phase: 'curation',
      on_failure: 'block',
      checks: [
        { check_type: 'field_present', description: 'test', params: { field: 'name' } },
      ],
    }

    const result = runGateChecks(gate, [])
    assert.equal(result.passed, false)
    assert.ok(result.results[0].message.includes('No artifacts'))
  })

  it('uses artifact_type param to select correct artifact', () => {
    const art1Path = join(tempDir, 'art1.json')
    const art2Path = join(tempDir, 'art2.json')
    writeFileSync(art1Path, JSON.stringify({ sources: ['s1'] }), 'utf-8')
    writeFileSync(art2Path, JSON.stringify({ components: ['c1'] }), 'utf-8')

    const gate: QualityGate = {
      phase: 'design_synthesis',
      on_failure: 'block',
      checks: [
        { check_type: 'field_present', description: 'components check', params: { field: 'components', artifact_type: 'design-spec' } },
      ],
    }

    const refs: ArtifactRef[] = [
      { type: 'curated-collection', path: art1Path, phase: 'design_synthesis', created_at: new Date().toISOString() },
      { type: 'design-spec', path: art2Path, phase: 'design_synthesis', created_at: new Date().toISOString() },
    ]

    const result = runGateChecks(gate, refs)
    assert.equal(result.passed, true)
  })
})

// ── loadQualityGates (TOML parsing) ──────────────────────────

describe('loadQualityGates', () => {
  it('returns empty array for nonexistent file', () => {
    const gates = loadQualityGates(join(tempDir, 'nonexistent.toml'))
    assert.deepEqual(gates, [])
  })

  it('parses a valid quality-gates.toml', () => {
    const toml = `
[[gates]]
phase = "curation"
on_failure = "block"

[[gates.checks]]
check_type = "field_present"
description = "Sources must be present"
[gates.checks.params]
field = "sources"
artifact_type = "curated-collection"

[[gates.checks]]
check_type = "min_items"
description = "At least 1 item"
[gates.checks.params]
field = "items"
min = 1

[[gates]]
phase = "validation"
on_failure = "warn"

[[gates.checks]]
check_type = "field_present"
description = "Results required"
[gates.checks.params]
field = "results"
`
    const tomlPath = join(tempDir, 'quality-gates.toml')
    writeFileSync(tomlPath, toml, 'utf-8')

    const gates = loadQualityGates(tomlPath)
    assert.equal(gates.length, 2)

    assert.equal(gates[0].phase, 'curation')
    assert.equal(gates[0].on_failure, 'block')
    assert.equal(gates[0].checks.length, 2)
    assert.equal(gates[0].checks[0].check_type, 'field_present')
    assert.equal(gates[0].checks[0].params.field, 'sources')
    assert.equal(gates[0].checks[1].check_type, 'min_items')

    assert.equal(gates[1].phase, 'validation')
    assert.equal(gates[1].on_failure, 'warn')
    assert.equal(gates[1].checks.length, 1)
  })

  it('defaults on_failure to warn when not specified', () => {
    const toml = `
[[gates]]
phase = "test"

[[gates.checks]]
check_type = "field_present"
description = "test"
[gates.checks.params]
field = "name"
`
    const tomlPath = join(tempDir, 'gates.toml')
    writeFileSync(tomlPath, toml, 'utf-8')

    const gates = loadQualityGates(tomlPath)
    assert.equal(gates[0].on_failure, 'warn')
  })
})

// ── removePhaseArtifacts (rollback) ──────────────────────────

describe('removePhaseArtifacts', () => {
  it('removes all artifacts for a specific phase', () => {
    let state = initRun('rollback-test', '1.0.0', ['discovery', 'curation'], tempDir)
    state = addArtifact(state, {
      type: 'raw-collection', path: 'raw/test1.json', phase: 'discovery', created_at: new Date().toISOString(),
    }, tempDir)
    state = addArtifact(state, {
      type: 'curated-collection', path: 'curated/test1.json', phase: 'curation', created_at: new Date().toISOString(),
    }, tempDir)

    assert.equal(state.available_artifacts.length, 2)

    const removed = removePhaseArtifacts(state, 'curation', tempDir)

    assert.deepEqual(removed, ['curated/test1.json'])
    assert.equal(state.available_artifacts.length, 1)
    assert.equal(state.available_artifacts[0].phase, 'discovery')
    assert.deepEqual(state.phases.curation.output_artifacts, [])
  })

  it('preserves artifacts from other phases', () => {
    let state = initRun('rollback-preserve', '1.0.0', ['discovery', 'curation'], tempDir)
    state = addArtifact(state, {
      type: 'raw-collection', path: 'raw/test1.json', phase: 'discovery', created_at: new Date().toISOString(),
    }, tempDir)
    state = addArtifact(state, {
      type: 'raw-collection', path: 'raw/test2.json', phase: 'discovery', created_at: new Date().toISOString(),
    }, tempDir)

    const removed = removePhaseArtifacts(state, 'curation', tempDir)

    assert.deepEqual(removed, [])
    assert.equal(state.available_artifacts.length, 2)
  })

  it('persists changes to disk after removal', () => {
    let state = initRun('rollback-persist', '1.0.0', ['discovery'], tempDir)
    state = addArtifact(state, {
      type: 'raw-collection', path: 'raw/test1.json', phase: 'discovery', created_at: new Date().toISOString(),
    }, tempDir)

    removePhaseArtifacts(state, 'discovery', tempDir)

    const loaded = loadRunState(tempDir)
    assert.ok(loaded)
    assert.equal(loaded.available_artifacts.length, 0)
  })

  it('handles phase with no artifacts gracefully', () => {
    let state = initRun('rollback-empty', '1.0.0', ['discovery'], tempDir)

    const removed = removePhaseArtifacts(state, 'discovery', tempDir)

    assert.deepEqual(removed, [])
    assert.equal(state.available_artifacts.length, 0)
  })
})

// ── Bundled quality-gates.toml loading ───────────────────────

describe('bundled quality-gates.toml', () => {
  it('loads the bundled quality-gates.toml without errors', () => {
    const gatesPath = join(import.meta.dirname!, '..', 'pipeline', 'quality-gates.toml')
    const gates = loadQualityGates(gatesPath)
    assert.ok(gates.length > 0, 'Expected at least one gate definition')

    // Each gate should have phase and checks
    for (const gate of gates) {
      assert.ok(gate.phase, 'Gate must have a phase')
      assert.ok(gate.checks.length > 0, `Gate for phase "${gate.phase}" must have at least one check`)
      assert.ok(['warn', 'block'].includes(gate.on_failure), `Gate on_failure must be "warn" or "block"`)
    }
  })
})

// ── gate_evaluated events (Fix 3.6) ──────────────────────────

/**
 * Minimal PipelineConfig stub for handleCompletePhase-driven gate tests.
 */
function makeGateTestConfig(): PipelineConfig {
  const phases: Record<string, PhaseDefinition> = {
    curation: {
      id: 0,
      description: '',
      inputs: [],
      outputs: ['curated-collection'],
      tools: [],
      entry_point: true,
      optional: false,
    },
  }
  return {
    pipeline: { id: 'gate-evt-test', version: '1.0.0', description: '' },
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

/**
 * Build a real-backed LifecycleContext wrapping run-state helpers plus a
 * configurable gate list. runGateChecks remains the real pure implementation.
 */
function makeGateTestCtx(
  initialState: RunState,
  stateDir: string,
  gates: QualityGate[],
): LifecycleContext {
  let activeRun: RunState | null = initialState
  let activeRunDir = stateDir

  return {
    getActiveRun: () => activeRun,
    setActiveRun: (s) => { activeRun = s },
    getActiveRunDir: () => activeRunDir,
    setActiveRunDir: (dir) => { activeRunDir = dir },
    getConfig: () => makeGateTestConfig(),
    setProjectRoot: () => {},
    getStorageConfig: (): StorageConfig => ({ base_dir: stateDir, paths: {} }),
    initRun: () => initialState,
    skipPhase: (s) => s,
    addArtifact: (s, ref, dir) => addArtifact(s, ref, dir),
    startPhase: (s, phase, dir) => startPhase(s, phase, dir),
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
    recoverRun: (): RecoveryResult => ({ state: initialState, recovered_phases: [], warnings: [] }),
    removePhaseArtifacts: (s: RunState, phase: string, dir: string) => removePhaseArtifacts(s, phase, dir),
    getQualityGates: () => gates,
    runGateChecks: (gate: QualityGate, refs: ArtifactRef[]): GateResult => runGateChecks(gate, refs),
    getHooksConfig: () => [],
    runHooks: () => [],
    runPrePipelineInitHooks: () => ({ parameters: {}, userPrompts: [] }),
    getProjectRoot: () => null,
    computeRecommendedAction: (): RecommendedAction => ({ action: 'review', reason: '' }),
    computeRunWarnings: () => [],
  }
}

function readEvents(runDir: string): Array<Record<string, unknown>> {
  const eventsPath = join(runDir, 'events.jsonl')
  if (!existsSync(eventsPath)) return []
  const content = readFileSync(eventsPath, 'utf-8')
  const events: Array<Record<string, unknown>> = []
  for (const line of content.split('\n')) {
    const trimmed = line.trim()
    if (!trimmed) continue
    try {
      events.push(JSON.parse(trimmed) as Record<string, unknown>)
    } catch {
      // ignore malformed lines
    }
  }
  return events
}

describe('gate_evaluated events (Fix 3.6)', () => {
  it('writes one gate_evaluated event per check for a passing gate', () => {
    // Arrange: put an artifact on disk that will satisfy both checks.
    const artifactPath = join(tempDir, 'curated.json')
    writeFileSync(
      artifactPath,
      JSON.stringify({ sources: ['s1', 's2'], items: [1, 2, 3] }),
      'utf-8',
    )

    let state = initRun('gate-evt-pass', '1.0.0', ['curation'], tempDir)
    state = startPhase(state, 'curation', tempDir)
    state = addArtifact(
      state,
      {
        type: 'curated-collection',
        path: artifactPath,
        phase: 'curation',
        created_at: new Date().toISOString(),
      },
      tempDir,
    )

    const gate: QualityGate = {
      phase: 'curation',
      on_failure: 'block',
      checks: [
        {
          check_type: 'field_present',
          description: 'sources present',
          params: { field: 'sources', artifact_type: 'curated-collection' },
        },
        {
          check_type: 'min_items',
          description: 'min items',
          params: { field: 'items', min: 1, artifact_type: 'curated-collection' },
        },
      ],
    }

    const ctx = makeGateTestCtx(state, tempDir, [gate])

    // Act
    handleCompletePhase({ phase: 'curation' }, ctx)

    // Assert: events.jsonl contains two gate_evaluated events for curation.
    const events = readEvents(tempDir)
    const gateEvents = events.filter(e => e.event === 'gate_evaluated' && e.phase === 'curation')
    assert.equal(gateEvents.length, 2, 'expected one gate_evaluated event per check')

    // Both should be 'pass' for a passing gate.
    for (const evt of gateEvents) {
      const details = evt.details as Record<string, unknown>
      assert.equal(details.gate_name, 'curation')
      assert.equal(details.result, 'pass')
      assert.equal(details.artifact_type, 'curated-collection')
    }

    // check_type values should be the two we configured.
    const checkTypes = gateEvents.map(e => (e.details as Record<string, unknown>).check_type)
    assert.deepEqual(
      checkTypes.sort(),
      ['field_present', 'min_items'],
    )
  })

  it('writes gate_evaluated events with result="fail" before a blocking gate throws', () => {
    // Artifact is missing the required field so field_present will fail.
    const artifactPath = join(tempDir, 'curated-bad.json')
    writeFileSync(artifactPath, JSON.stringify({ items: [1] }), 'utf-8')

    let state = initRun('gate-evt-fail', '1.0.0', ['curation'], tempDir)
    state = startPhase(state, 'curation', tempDir)
    state = addArtifact(
      state,
      {
        type: 'curated-collection',
        path: artifactPath,
        phase: 'curation',
        created_at: new Date().toISOString(),
      },
      tempDir,
    )

    const gate: QualityGate = {
      phase: 'curation',
      on_failure: 'block',
      checks: [
        {
          check_type: 'field_present',
          description: 'sources present',
          params: { field: 'sources', artifact_type: 'curated-collection' },
        },
      ],
    }

    const ctx = makeGateTestCtx(state, tempDir, [gate])

    // Act: handleCompletePhase should throw gate_blocked because the gate fails.
    assert.throws(
      () => handleCompletePhase({ phase: 'curation' }, ctx),
      (err: unknown) => err instanceof PipelineError && err.error_class === 'gate_blocked',
    )

    // Assert: the gate_evaluated event was written BEFORE the throw.
    const events = readEvents(tempDir)
    const gateEvents = events.filter(e => e.event === 'gate_evaluated' && e.phase === 'curation')
    assert.equal(gateEvents.length, 1, 'expected one gate_evaluated event for the failing check')
    const details = gateEvents[0].details as Record<string, unknown>
    assert.equal(details.gate_name, 'curation')
    assert.equal(details.check_type, 'field_present')
    assert.equal(details.result, 'fail', "block-mode gate failure should map to result='fail'")
    assert.equal(details.artifact_type, 'curated-collection')
  })

  it('writes gate_evaluated events with result="warn" for a warn-mode failure', () => {
    const artifactPath = join(tempDir, 'curated-warn.json')
    writeFileSync(artifactPath, JSON.stringify({ items: [] }), 'utf-8')

    let state = initRun('gate-evt-warn', '1.0.0', ['curation'], tempDir)
    state = startPhase(state, 'curation', tempDir)
    state = addArtifact(
      state,
      {
        type: 'curated-collection',
        path: artifactPath,
        phase: 'curation',
        created_at: new Date().toISOString(),
      },
      tempDir,
    )

    const gate: QualityGate = {
      phase: 'curation',
      on_failure: 'warn',
      checks: [
        {
          check_type: 'min_items',
          description: 'min items',
          params: { field: 'items', min: 1, artifact_type: 'curated-collection' },
        },
      ],
    }

    const ctx = makeGateTestCtx(state, tempDir, [gate])

    // warn-mode: handleCompletePhase should NOT throw; it should return a
    // response envelope and still emit the gate_evaluated event.
    handleCompletePhase({ phase: 'curation' }, ctx)

    const events = readEvents(tempDir)
    const gateEvents = events.filter(e => e.event === 'gate_evaluated' && e.phase === 'curation')
    assert.equal(gateEvents.length, 1)
    const details = gateEvents[0].details as Record<string, unknown>
    assert.equal(details.result, 'warn', "warn-mode gate failure should map to result='warn'")
  })
})
