import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import {
  initRun,
  startPhase,
  completePhase,
  addArtifact,
} from '../run-state.ts'
import { resolveNextPhases } from '../dag.ts'
import type { PipelineConfig, PhaseDefinition, EdgeDefinition } from '../types.ts'

let tempDir: string

beforeEach(() => {
  tempDir = mkdtempSync(join(tmpdir(), 'pipeline-feedback-loop-test-'))
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
      output: { includes_transcript: true, includes_refined_artifact: true, artifact_version_bump: 'minor' },
    },
    knowledge_bases: { available_in: [], max_queries_per_phase: 20, query_mode: 'proactive' },
    schemas: {},
    storage: { base_dir: 'test', paths: {} },
  }
}

describe('validation feedback loop — DAG routing', () => {
  it('research_discovery is available and implementation_scaffold is not when validation produces only research-manifest', () => {
    // Build a minimal config representing the relevant DAG slice.
    // implementation_scaffold has entry_point: false so the required_optional
    // input check runs — can_run = availableArtifacts.includes('design-spec').
    // research_discovery has entry_point: true so it is gated only by edges
    // (predecessor completion), not by input satisfaction.
    const config = makeConfig(
      {
        validation: {
          entry_point: true,
          inputs: ['design-spec'],
          input_mode: 'required_optional',
          outputs: ['validation-report', 'design-spec', 'research-manifest'],
        },
        research_discovery: {
          entry_point: true,
          inputs: ['research-manifest'],
        },
        implementation_scaffold: {
          entry_point: false,
          inputs: ['design-spec'],
          input_mode: 'required_optional',
        },
      },
      [
        { from: 'validation', to: 'research_discovery' },    // feedback edge
        { from: 'validation', to: 'implementation_scaffold' },
      ],
    )

    // Simulate: validation started and completed, emitting only a research-manifest
    let state = initRun('test', '1.0.0', ['validation', 'research_discovery', 'implementation_scaffold'], tempDir)
    state = startPhase(state, 'validation', tempDir)

    // Register research-manifest only (NOT design-spec) — the knowledge-gaps path
    state = addArtifact(state, {
      type: 'research-manifest',
      path: `${tempDir}/research-manifest-001.json`,
      phase: 'validation',
      created_at: new Date().toISOString(),
      version: 1,
    }, tempDir)

    state = completePhase(state, 'validation', tempDir)

    // Resolve next phases given: validation completed, only research-manifest available
    const completedPhases = ['validation']
    const artifactTypes = ['research-manifest']  // no design-spec

    const nextPhases = resolveNextPhases(config, completedPhases, artifactTypes)

    // research_discovery IS available (entry_point=true, predecessor validation completed)
    assert.ok(
      nextPhases.includes('research_discovery'),
      `Expected research_discovery in next phases, got: [${nextPhases.join(', ')}]`,
    )

    // implementation_scaffold is NOT available (requires design-spec, none registered)
    assert.ok(
      !nextPhases.includes('implementation_scaffold'),
      `implementation_scaffold should NOT be in next phases when no design-spec is registered, got: [${nextPhases.join(', ')}]`,
    )
  })

  it('implementation_scaffold IS available when validation produces a design-spec (happy path)', () => {
    const config = makeConfig(
      {
        validation: {
          entry_point: true,
          inputs: ['design-spec'],
          input_mode: 'required_optional',
        },
        implementation_scaffold: {
          entry_point: false,
          inputs: ['design-spec'],
          input_mode: 'required_optional',
        },
      },
      [{ from: 'validation', to: 'implementation_scaffold' }],
    )

    // Validation produced a design-spec (happy path — spec validated and updated)
    const completedPhases = ['validation']
    const artifactTypes = ['design-spec', 'validation-report']

    const nextPhases = resolveNextPhases(config, completedPhases, artifactTypes)

    assert.ok(
      nextPhases.includes('implementation_scaffold'),
      `Expected implementation_scaffold in next phases when design-spec is available, got: [${nextPhases.join(', ')}]`,
    )
  })

  it('artifact registry reflects research-manifest registration after validation completes', () => {
    const config = makeConfig(
      { validation: { entry_point: true } },
      [],
    )

    let state = initRun('test', '1.0.0', ['validation'], tempDir)
    state = startPhase(state, 'validation', tempDir)
    state = addArtifact(state, {
      type: 'research-manifest',
      path: `${tempDir}/rm-001.json`,
      phase: 'validation',
      created_at: new Date().toISOString(),
      version: 1,
    }, tempDir)
    state = completePhase(state, 'validation', tempDir)

    const registered = state.available_artifacts.find(a => a.type === 'research-manifest')
    assert.ok(registered, 'research-manifest must be registered in available_artifacts')
    assert.equal(registered!.phase, 'validation')
  })
})
