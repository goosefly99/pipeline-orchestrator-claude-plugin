import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { resolveNextPhases, getPhaseInputSatisfaction } from '../dag.ts'
import type { PipelineConfig, PhaseDefinition, EdgeDefinition } from '../types.ts'

// Minimal config for testing
function makeConfig(
  phases: Record<string, Partial<PhaseDefinition>>,
  edges: EdgeDefinition[],
): PipelineConfig {
  const fullPhases: Record<string, PhaseDefinition> = {}
  for (const [name, partial] of Object.entries(phases)) {
    fullPhases[name] = {
      id: partial.id ?? name,
      description: partial.description ?? '',
      inputs: partial.inputs ?? [],
      outputs: partial.outputs ?? [],
      tools: partial.tools ?? [],
      entry_point: partial.entry_point ?? false,
      ...partial,
    } as PhaseDefinition
  }
  return {
    pipeline: { id: 'test', version: '1.0.0', description: '' },
    phases: fullPhases,
    edges,
    debate: { agents: {}, rounds: {}, output: { includes_transcript: true, includes_refined_artifact: true, artifact_version_bump: 'minor' } },
    knowledge_bases: { available_in: [], max_queries_per_phase: 20, query_mode: 'proactive' },
    schemas: {},
    storage: { base_dir: 'test', paths: {} },
  }
}

describe('resolveNextPhases', () => {
  it('returns entry points when no phases completed', () => {
    const config = makeConfig(
      {
        discovery: { inputs: ['research-manifest'], outputs: ['raw-collection'], entry_point: true },
        curation: { inputs: ['raw-collection'], outputs: ['curated-collection'], entry_point: true },
      },
      [{ from: 'discovery', to: 'curation' }],
    )

    const next = resolveNextPhases(config, [], ['research-manifest'])
    assert.ok(next.includes('discovery'))
  })

  it('returns downstream phase after predecessor completes', () => {
    const config = makeConfig(
      {
        discovery: { inputs: ['research-manifest'], outputs: ['raw-collection'], entry_point: true },
        curation: { inputs: ['raw-collection'], outputs: ['curated-collection'], entry_point: true },
      },
      [{ from: 'discovery', to: 'curation' }],
    )

    const next = resolveNextPhases(config, ['discovery'], ['research-manifest', 'raw-collection'])
    assert.ok(next.includes('curation'))
    assert.ok(!next.includes('discovery'))
  })

  it('skips non-entry-point phases without required inputs', () => {
    const config = makeConfig(
      {
        discovery: { inputs: ['research-manifest'], outputs: ['raw-collection'], entry_point: true },
        synthesis: { inputs: ['curated-collection', 'knowledge-overview'], input_mode: 'any', outputs: ['design-spec'], entry_point: false },
      },
      [{ from: 'discovery', to: 'synthesis' }],
    )

    const next = resolveNextPhases(config, [], ['research-manifest'])
    assert.ok(next.includes('discovery'))
    assert.ok(!next.includes('synthesis'), 'non-entry-point phase without inputs should be skipped')
  })

  it('entry-point phases bypass input satisfaction — available even without inputs', () => {
    const config = makeConfig(
      {
        discovery: { inputs: ['research-manifest'], outputs: ['raw-collection'], entry_point: true },
        synthesis: { inputs: ['curated-collection', 'knowledge-overview'], input_mode: 'any', outputs: ['design-spec'], entry_point: true },
      },
      [],
    )

    const next = resolveNextPhases(config, [], [])
    assert.ok(next.includes('discovery'), 'entry-point phase available even with no artifacts')
    assert.ok(next.includes('synthesis'), 'entry-point phase available even with no matching inputs')
  })

  it('handles "any" input_mode — satisfied if at least one input available', () => {
    const config = makeConfig(
      {
        synthesis: { inputs: ['curated-collection', 'knowledge-overview'], input_mode: 'any', outputs: ['design-spec'], entry_point: true },
      },
      [],
    )

    const next = resolveNextPhases(config, [], ['curated-collection'])
    assert.ok(next.includes('synthesis'))
  })

  it('handles optional edges — does not block downstream', () => {
    const config = makeConfig(
      {
        codebase: { inputs: ['codebase-path'], outputs: ['codebase-requirements'], entry_point: true, optional: true },
        synthesis: { inputs: ['curated-collection', 'codebase-requirements'], input_mode: 'any', outputs: ['design-spec'], entry_point: true },
      },
      [{ from: 'codebase', to: 'synthesis', optional: true }],
    )

    const next = resolveNextPhases(config, [], ['curated-collection'])
    assert.ok(next.includes('synthesis'))
  })

  it('does not return already-completed phases', () => {
    const config = makeConfig(
      {
        discovery: { inputs: ['research-manifest'], outputs: ['raw-collection'], entry_point: true },
      },
      [],
    )

    const next = resolveNextPhases(config, ['discovery'], ['research-manifest', 'raw-collection'])
    assert.ok(!next.includes('discovery'))
  })

  it('respects skip list', () => {
    const config = makeConfig(
      {
        discovery: { inputs: ['research-manifest'], outputs: ['raw-collection'], entry_point: true },
        curation: { inputs: ['raw-collection'], outputs: ['curated-collection'], entry_point: true },
      },
      [{ from: 'discovery', to: 'curation' }],
    )

    const next = resolveNextPhases(config, ['discovery'], ['raw-collection'], ['curation'])
    assert.ok(!next.includes('curation'))
  })

  it('entry-point phase with unregistered input types appears in results (item 0.1 fix)', () => {
    const config = makeConfig(
      {
        ingestion: { inputs: ['file-paths'], outputs: ['raw-collection'], entry_point: true },
        curation: { inputs: ['raw-collection'], outputs: ['curated-collection'], entry_point: true },
        synthesis: { inputs: ['curated-collection', 'knowledge-overview'], input_mode: 'any', outputs: ['design-spec'], entry_point: true },
      },
      [
        { from: 'ingestion', to: 'curation' },
        { from: 'curation', to: 'synthesis' },
      ],
    )

    // No artifacts available at all — entry-point phases must still appear
    const next = resolveNextPhases(config, [], [])
    assert.ok(next.includes('ingestion'), 'entry-point phase with unregistered input "file-paths" must appear')
    assert.ok(next.includes('curation'), 'entry-point phase with unregistered input "raw-collection" must appear')
    assert.ok(next.includes('synthesis'), 'entry-point phase with unregistered inputs must appear')
  })

  it('non-entry-point phase with unregistered inputs does NOT appear', () => {
    const config = makeConfig(
      {
        discovery: { inputs: ['research-manifest'], outputs: ['raw-collection'], entry_point: true },
        debate: { inputs: ['knowledge-overview', 'design-spec'], input_mode: 'one_of_primary', outputs: ['debate-transcript'], entry_point: false },
      },
      [{ from: 'discovery', to: 'debate' }],
    )

    const next = resolveNextPhases(config, [], [])
    assert.ok(next.includes('discovery'), 'entry-point phase should appear')
    assert.ok(!next.includes('debate'), 'non-entry-point phase with missing inputs must NOT appear')
  })

  it('entry-point phase with DAG edges and no predecessors completed still appears', () => {
    const config = makeConfig(
      {
        codebase: { inputs: ['codebase-path'], outputs: ['codebase-requirements'], entry_point: true, optional: true },
        validation: { inputs: ['design-spec', 'codebase-requirements'], input_mode: 'required_optional', outputs: ['design-spec'], entry_point: true },
      },
      [
        { from: 'codebase', to: 'validation', optional: true },
      ],
    )

    // No artifacts, no completed phases — entry-point with incoming optional edge must still appear
    const next = resolveNextPhases(config, [], [])
    assert.ok(next.includes('codebase'), 'entry-point phase must appear with no artifacts')
    assert.ok(next.includes('validation'), 'entry-point phase with only optional incoming edges must appear')
  })

  it('skipped phases propagate edges to downstream phases', () => {
    const config = makeConfig(
      {
        phase_a: { inputs: ['input-a'], outputs: ['output-a'], entry_point: true },
        phase_b: { inputs: ['output-a'], outputs: ['output-b'] },
        phase_c: { inputs: ['output-b'], outputs: ['output-c'] },
      },
      [
        { from: 'phase_a', to: 'phase_b' },
        { from: 'phase_b', to: 'phase_c' },
      ],
    )

    // phase_a completed, phase_b skipped (done externally), output-b available
    const next = resolveNextPhases(
      config,
      ['phase_a'],
      ['input-a', 'output-a', 'output-b'],
      ['phase_b'],
    )
    assert.ok(!next.includes('phase_a'), 'phase_a should not appear (already completed)')
    assert.ok(!next.includes('phase_b'), 'phase_b should not appear (skipped)')
    assert.ok(next.includes('phase_c'), 'phase_c should be reachable via skipped phase_b edge')
  })
})

describe('getPhaseInputSatisfaction', () => {
  it('returns satisfied inputs for a phase', () => {
    const config = makeConfig(
      {
        synthesis: { inputs: ['curated-collection', 'knowledge-overview'], input_mode: 'any', outputs: ['design-spec'], entry_point: true },
      },
      [],
    )

    const result = getPhaseInputSatisfaction(config.phases.synthesis, ['curated-collection'])
    assert.deepEqual(result.satisfied, ['curated-collection'])
    assert.deepEqual(result.missing, ['knowledge-overview'])
    assert.equal(result.can_run, true)
  })

  it('returns can_run false when no inputs available and mode is not any', () => {
    const config = makeConfig(
      {
        curation: { inputs: ['raw-collection'], outputs: ['curated-collection'], entry_point: true },
      },
      [],
    )

    const result = getPhaseInputSatisfaction(config.phases.curation, [])
    assert.equal(result.can_run, false)
    assert.deepEqual(result.missing, ['raw-collection'])
  })
})
