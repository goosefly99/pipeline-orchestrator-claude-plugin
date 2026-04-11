import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { generateHandoff } from '../handoff.ts'
import { generatePhaseBrief } from '../phase-brief.ts'
import { enforceResponseSize } from '../artifact-handlers.ts'
import { initRun, startPhase, completePhase, addArtifact } from '../run-state.ts'
import type { PipelineConfig, QualityGate, RunState, PhaseDefinition } from '../types.ts'

let tempDir: string

beforeEach(() => {
  tempDir = mkdtempSync(join(tmpdir(), 'token-efficiency-test-'))
})

afterEach(() => {
  rmSync(tempDir, { recursive: true, force: true })
})

// ── Test config factory ──────────────────────────────────────

function makeConfig(): PipelineConfig {
  return {
    pipeline: { id: 'test-pipeline', version: '1.0.0', description: 'Test pipeline' },
    phases: {
      discovery: {
        id: 0, description: 'Discover and ingest documents', inputs: [], outputs: ['raw-collection'],
        tools: ['pipeline_ingest_documents'], entry_point: true, model_tier: 'sonnet',
      } as PhaseDefinition,
      curation: {
        id: 1, description: 'Curate raw collection', inputs: ['raw-collection'], outputs: ['curated-collection'],
        tools: ['pipeline_store_artifact'], entry_point: false, model_tier: 'haiku',
      } as PhaseDefinition,
      synthesis: {
        id: 2, description: 'Synthesize design spec', inputs: ['curated-collection'], outputs: ['design-spec'],
        tools: ['pipeline_synth_create_spec'], entry_point: false, model_tier: 'opus',
      } as PhaseDefinition,
    },
    edges: [
      { from: 'discovery', to: 'curation' },
      { from: 'curation', to: 'synthesis' },
    ],
    debate: { agents: {}, rounds: {}, output: { includes_transcript: true, includes_refined_artifact: true, artifact_version_bump: 'minor' } },
    knowledge_bases: { available_in: [], max_queries_per_phase: 20, query_mode: 'proactive' },
    schemas: {},
    storage: { base_dir: 'test', paths: { collections_raw: 'collections/raw', collections_curated: 'collections/curated', specs: 'specs' } },
  }
}

function makeGates(): QualityGate[] {
  return [
    {
      phase: 'curation',
      on_failure: 'block',
      checks: [
        { check_type: 'field_present', description: 'Sources must be present', params: { field: 'sources' } },
        { check_type: 'min_items', description: 'At least 1 item', params: { field: 'items', min: 1 } },
      ],
    },
  ]
}

// ── generateHandoff ──────────────────────────────────────────

describe('generateHandoff', () => {
  it('generates a valid handoff payload', () => {
    let state = initRun('handoff-test', '1.0.0', ['discovery', 'curation', 'synthesis'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    state = completePhase(state, 'discovery', tempDir)
    state = addArtifact(state, {
      type: 'raw-collection', path: 'collections/raw/test.json', phase: 'discovery', created_at: new Date().toISOString(),
    }, tempDir)

    const config = makeConfig()
    const gates = makeGates()
    const handoff = generateHandoff(state, config, gates, ['curation'])

    assert.equal(handoff.run_id, 'handoff-test')
    assert.equal(handoff.pipeline.id, 'test-pipeline')
    assert.equal(handoff.phases.length, 3)
    assert.deepEqual(handoff.completed_phases, ['discovery'])
    assert.deepEqual(handoff.next_available_phases, ['curation'])
    assert.ok(handoff.suggested_instruction.includes('curation'))
  })

  it('handoff payload is under 5K tokens (~20K chars)', () => {
    let state = initRun('size-test', '1.0.0', ['discovery', 'curation', 'synthesis'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    state = completePhase(state, 'discovery', tempDir)

    const config = makeConfig()
    const handoff = generateHandoff(state, config, makeGates(), ['curation'])
    const serialized = JSON.stringify(handoff, null, 2)

    // 5K tokens is approximately 20K characters
    assert.ok(
      serialized.length < 20000,
      `Handoff payload is ${serialized.length} chars — exceeds 20K char target`,
    )
  })

  it('includes model_tier for phases that have it', () => {
    const state = initRun('tier-test', '1.0.0', ['discovery', 'curation', 'synthesis'], tempDir)
    const config = makeConfig()
    const handoff = generateHandoff(state, config, [], ['discovery'])

    const discoveryPhase = handoff.phases.find(p => p.name === 'discovery')
    assert.ok(discoveryPhase)
    assert.equal(discoveryPhase.model_tier, 'sonnet')
  })

  it('includes quality gate summaries', () => {
    const state = initRun('gate-test', '1.0.0', ['discovery', 'curation'], tempDir)
    const config = makeConfig()
    const gates = makeGates()
    const handoff = generateHandoff(state, config, gates, ['discovery'])

    assert.equal(handoff.quality_gates.length, 1)
    assert.equal(handoff.quality_gates[0].phase, 'curation')
    assert.equal(handoff.quality_gates[0].on_failure, 'block')
    assert.equal(handoff.quality_gates[0].check_count, 2)
  })

  it('suggests continuing in_progress phase', () => {
    let state = initRun('ip-test', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)

    const config = makeConfig()
    const handoff = generateHandoff(state, config, [], [])

    assert.ok(handoff.suggested_instruction.includes('Continue'))
    assert.ok(handoff.suggested_instruction.includes('discovery'))
  })

  it('suggests validation when all phases complete', () => {
    let state = initRun('done-test', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    state = completePhase(state, 'discovery', tempDir)

    const config = makeConfig()
    const handoff = generateHandoff(state, config, [], [])

    assert.ok(handoff.suggested_instruction.includes('validate'))
  })

  it('includes artifact paths per phase', () => {
    let state = initRun('art-test', '1.0.0', ['discovery'], tempDir)
    state = addArtifact(state, {
      type: 'raw-collection', path: 'collections/raw/r1.json', phase: 'discovery', created_at: new Date().toISOString(),
    }, tempDir)
    state = addArtifact(state, {
      type: 'raw-collection', path: 'collections/raw/r2.json', phase: 'discovery', created_at: new Date().toISOString(),
    }, tempDir)

    const config = makeConfig()
    const handoff = generateHandoff(state, config, [], [])

    const discoveryPhase = handoff.phases.find(p => p.name === 'discovery')
    assert.ok(discoveryPhase)
    assert.equal(discoveryPhase.artifact_paths.length, 2)
  })
})

// ── generatePhaseBrief ───────────────────────────────────────

describe('generatePhaseBrief', () => {
  it('generates a valid phase brief', () => {
    let state = initRun('brief-test', '1.0.0', ['discovery', 'curation'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    state = completePhase(state, 'discovery', tempDir)
    state = addArtifact(state, {
      type: 'raw-collection', path: 'collections/raw/test.json', phase: 'discovery', created_at: new Date().toISOString(),
    }, tempDir)

    const config = makeConfig()
    const gates = makeGates()
    const brief = generatePhaseBrief('curation', state, config, gates)

    assert.equal(brief.phase_name, 'curation')
    assert.ok(brief.description.length > 0)
    assert.equal(brief.model_tier, 'haiku')
    assert.ok(brief.input_artifacts.length > 0)
    assert.deepEqual(brief.output_requirements.expected_types, ['curated-collection'])
    assert.ok(brief.quality_gate)
    assert.equal(brief.quality_gate!.on_failure, 'block')
    assert.ok(brief.instruction.includes('curation'))
  })

  it('throws for unknown phase', () => {
    const state = initRun('err-test', '1.0.0', ['discovery'], tempDir)
    const config = makeConfig()

    assert.throws(
      () => generatePhaseBrief('nonexistent', state, config, []),
      /not found/,
    )
  })

  it('includes all required fields', () => {
    const state = initRun('fields-test', '1.0.0', ['discovery'], tempDir)
    const config = makeConfig()
    const brief = generatePhaseBrief('discovery', state, config, [])

    assert.ok('phase_name' in brief)
    assert.ok('description' in brief)
    assert.ok('input_artifacts' in brief)
    assert.ok('output_requirements' in brief)
    assert.ok('storage_paths' in brief)
    assert.ok('tools' in brief)
    assert.ok('instruction' in brief)
  })

  it('resolves input artifacts from upstream phases', () => {
    let state = initRun('inputs-test', '1.0.0', ['discovery', 'curation'], tempDir)
    state = addArtifact(state, {
      type: 'raw-collection', path: 'collections/raw/test.json', phase: 'discovery', created_at: new Date().toISOString(),
    }, tempDir)

    const config = makeConfig()
    const brief = generatePhaseBrief('curation', state, config, [])

    // curation takes raw-collection as input; discovery produces it
    assert.ok(brief.input_artifacts.length > 0)
    assert.ok(brief.input_artifacts.some(a => a.type === 'raw-collection'))
  })

  it('omits quality_gate when no gate is configured', () => {
    const state = initRun('no-gate-test', '1.0.0', ['discovery'], tempDir)
    const config = makeConfig()
    const brief = generatePhaseBrief('discovery', state, config, [])

    assert.equal(brief.quality_gate, undefined)
  })
})

// ── enforceResponseSize ──────────────────────────────────────

describe('enforceResponseSize', () => {
  it('returns response unchanged when under limit', () => {
    const response = JSON.stringify({ data: 'small' })
    const result = enforceResponseSize(response, 1000)
    assert.equal(result, response)
  })

  it('truncates and adds continuation_ref when over limit', () => {
    const largeResponse = JSON.stringify({ data: 'x'.repeat(1000) })
    const result = enforceResponseSize(largeResponse, 100, { storage_key: 'test', file_name: 'test.json' })
    const parsed = JSON.parse(result)

    assert.equal(parsed.truncated, true)
    assert.ok(parsed.continuation_ref)
    assert.equal(parsed.continuation_ref.storage_key, 'test')
    assert.equal(parsed.continuation_ref.file_name, 'test.json')
  })

  it('truncates with ellipsis when no continuation_ref', () => {
    const largeResponse = JSON.stringify({ data: 'x'.repeat(1000) })
    const result = enforceResponseSize(largeResponse, 100)

    assert.ok(result.includes('truncated'))
    assert.ok(result.length <= 200) // truncated + some overflow for the message
  })

  it('reports full_length in truncated response', () => {
    const largeResponse = JSON.stringify({ data: 'x'.repeat(1000) })
    const result = enforceResponseSize(largeResponse, 100, { storage_key: 'k', file_name: 'f' })
    const parsed = JSON.parse(result)

    assert.equal(parsed.full_length, largeResponse.length)
    assert.equal(parsed.preview_length, 100)
  })
})

// ── inline parameter for pipeline_load_artifact ──────────────

describe('inline parameter alias', () => {
  it('tool schema includes inline parameter', async () => {
    const { toolSchemas } = await import('../tool-schemas.ts')
    const loadSchema = toolSchemas.find(t => t.name === 'pipeline_load_artifact')
    assert.ok(loadSchema)
    const props = loadSchema.inputSchema.properties
    assert.ok('inline' in props)
    assert.ok('full' in props)
  })
})

// ── New tool schemas registered ──────────────────────────────

describe('Phase 5 tool schemas', () => {
  it('pipeline_phase_handoff is registered with readOnlyHint', async () => {
    const { toolSchemas } = await import('../tool-schemas.ts')
    const handoffSchema = toolSchemas.find(t => t.name === 'pipeline_phase_handoff')
    assert.ok(handoffSchema, 'pipeline_phase_handoff tool must be registered')
    assert.equal(handoffSchema.annotations?.readOnlyHint, true)
    assert.equal(handoffSchema.annotations?.idempotentHint, true)
  })

  it('pipeline_phase_brief is registered with readOnlyHint', async () => {
    const { toolSchemas } = await import('../tool-schemas.ts')
    const briefSchema = toolSchemas.find(t => t.name === 'pipeline_phase_brief')
    assert.ok(briefSchema, 'pipeline_phase_brief tool must be registered')
    assert.equal(briefSchema.annotations?.readOnlyHint, true)
    assert.ok(briefSchema.inputSchema.required?.includes('phase'))
  })
})
