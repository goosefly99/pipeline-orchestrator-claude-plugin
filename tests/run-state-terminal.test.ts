import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import {
  initRun,
  startPhase,
  completePhase,
  failPhase,
  skipPhase,
  loadRunState,
  computeRunTerminalStatus,
  finalizeRunTerminalStatus,
} from '../run-state.ts'
import type { PipelineConfig, PhaseDefinition, EdgeDefinition } from '../types.ts'

let tempDir: string

beforeEach(() => {
  tempDir = mkdtempSync(join(tmpdir(), 'pipeline-run-terminal-test-'))
})

afterEach(() => {
  rmSync(tempDir, { recursive: true, force: true })
})

function makeConfig(
  phases: Record<string, Partial<PhaseDefinition>>,
  edges: EdgeDefinition[] = [],
): PipelineConfig {
  const fullPhases: Record<string, PhaseDefinition> = {}
  for (const [name, partial] of Object.entries(phases)) {
    fullPhases[name] = {
      id: name,
      description: '',
      inputs: partial.inputs ?? [],
      outputs: partial.outputs ?? [],
      tools: [],
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

describe('computeRunTerminalStatus', () => {
  it('returns "completed" when all phases are completed', () => {
    const config = makeConfig({
      discovery: { entry_point: true },
      curation: { entry_point: false },
    })
    let state = initRun('test', '1.0.0', ['discovery', 'curation'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    state = completePhase(state, 'discovery', tempDir)
    state = startPhase(state, 'curation', tempDir)
    state = completePhase(state, 'curation', tempDir)

    assert.equal(computeRunTerminalStatus(state, config), 'completed')
  })

  it('returns "completed" when some phases are pending but unreachable via DAG', () => {
    // research_discovery requires a "research_brief" input that no completed phase produces.
    // Its DAG branch is untaken, so it can never run — terminal status should still be 'completed'.
    const config = makeConfig({
      discovery: { entry_point: true },
      research_discovery: { entry_point: false, inputs: ['research_brief'] },
    })
    let state = initRun('test', '1.0.0', ['discovery', 'research_discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    state = completePhase(state, 'discovery', tempDir)

    // research_discovery is still pending with no satisfiable inputs
    assert.equal(state.phases.research_discovery.status, 'pending')
    assert.equal(computeRunTerminalStatus(state, config), 'completed')
  })

  it('returns "running" when a phase is in_progress', () => {
    const config = makeConfig({
      discovery: { entry_point: true },
      curation: { entry_point: false },
    })
    let state = initRun('test', '1.0.0', ['discovery', 'curation'], tempDir)
    state = startPhase(state, 'discovery', tempDir)

    assert.equal(computeRunTerminalStatus(state, config), 'running')
  })

  it('returns "running" when more phases can still run', () => {
    // Two entry-point phases, only one completed — the other is still eligible.
    const config = makeConfig({
      discovery: { entry_point: true },
      curation: { entry_point: true },
    })
    let state = initRun('test', '1.0.0', ['discovery', 'curation'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    state = completePhase(state, 'discovery', tempDir)

    assert.equal(computeRunTerminalStatus(state, config), 'running')
  })

  it('returns "failed" when any phase failed, even if others completed', () => {
    const config = makeConfig({
      discovery: { entry_point: true },
      curation: { entry_point: true },
    })
    let state = initRun('test', '1.0.0', ['discovery', 'curation'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    state = completePhase(state, 'discovery', tempDir)
    state = startPhase(state, 'curation', tempDir)
    state = failPhase(state, 'curation', 'boom', tempDir)

    assert.equal(computeRunTerminalStatus(state, config), 'failed')
  })

  it('returns "running" for a freshly-initialized run with an entry-point phase', () => {
    const config = makeConfig({
      discovery: { entry_point: true },
    })
    const state = initRun('test', '1.0.0', ['discovery'], tempDir)
    assert.equal(computeRunTerminalStatus(state, config), 'running')
  })

  it('returns "completed" when every phase is skipped', () => {
    const config = makeConfig({
      discovery: { entry_point: true },
    })
    let state = initRun('test', '1.0.0', ['discovery'], tempDir)
    state = skipPhase(state, 'discovery', tempDir)
    assert.equal(computeRunTerminalStatus(state, config), 'completed')
  })

  it('returns "completed" when a terminal phase (no outgoing edges) completes, even with reachable feedback-loop phases still pending', () => {
    // Simulates: implementation_scaffold (terminal) completed, but research_discovery
    // (entry_point=true, reachable via validation feedback edge) is still pending.
    const config = makeConfig(
      {
        validation: { entry_point: true },
        implementation_scaffold: { entry_point: false, inputs: ['design-spec'] },
        research_discovery: { entry_point: true },
      },
      [
        { from: 'validation', to: 'research_discovery' }, // feedback edge makes it reachable
        // no edges FROM implementation_scaffold — it is the terminal node
      ],
    )
    let state = initRun('test', '1.0.0', ['validation', 'implementation_scaffold', 'research_discovery'], tempDir)
    state = startPhase(state, 'validation', tempDir)
    state = completePhase(state, 'validation', tempDir)
    state = startPhase(state, 'implementation_scaffold', tempDir)
    state = completePhase(state, 'implementation_scaffold', tempDir)

    // research_discovery is pending and technically reachable via the feedback edge,
    // but implementation_scaffold has no outgoing edges — run must be 'completed'.
    assert.equal(state.phases.research_discovery.status, 'pending')
    assert.equal(computeRunTerminalStatus(state, config), 'completed')
  })
})

describe('finalizeRunTerminalStatus', () => {
  it('sets status and completed_at and persists to disk', () => {
    // Build a multi-phase run where the inline allDone block doesn't fire,
    // so completed_at isn't populated before finalize runs.
    let state = initRun('test', '1.0.0', ['discovery', 'curation'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    state = completePhase(state, 'discovery', tempDir)

    // curation is still pending; run status should be 'running' and completed_at unset
    assert.equal(state.status, 'running')
    assert.equal(state.completed_at, undefined)

    const finalized = finalizeRunTerminalStatus(state, 'completed', tempDir)

    assert.equal(finalized.status, 'completed')
    assert.ok(finalized.completed_at, 'completed_at should be populated')

    const loaded = loadRunState(tempDir)
    assert.ok(loaded)
    assert.equal(loaded.status, 'completed')
    assert.equal(loaded.completed_at, finalized.completed_at)
  })
})

describe('completePhase terminal transition (direct + via handler)', () => {
  it('completePhase alone populates run-level completed_at when all phases finish', () => {
    let state = initRun('test', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    state = completePhase(state, 'discovery', tempDir)

    // The inline "all done" block inside completePhase should flip both
    // status AND completed_at for the happy path.
    assert.equal(state.status, 'completed')
    assert.ok(state.completed_at, 'run.completed_at should be set after the last phase completes')

    const loaded = loadRunState(tempDir)
    assert.ok(loaded)
    assert.equal(loaded.status, 'completed')
    assert.equal(loaded.completed_at, state.completed_at)
  })

  it('failPhase populates run-level completed_at', () => {
    let state = initRun('test', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    state = failPhase(state, 'discovery', 'boom', tempDir)

    assert.equal(state.status, 'failed')
    assert.ok(state.completed_at, 'run.completed_at should be set after a phase fails')
  })
})
