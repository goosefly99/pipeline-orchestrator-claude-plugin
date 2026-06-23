import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { loadPipelineConfig } from '../toml-loader.ts'
import { resolve, dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'

const __dirname = dirname(fileURLToPath(import.meta.url))
const PIPELINE_TOML = resolve(__dirname, '../pipeline/pipeline.toml')

describe('loadPipelineConfig', () => {
  it('loads and parses pipeline.toml', () => {
    const config = loadPipelineConfig(PIPELINE_TOML)

    assert.equal(config.pipeline.id, 'research-to-implementation')
    assert.equal(config.pipeline.version, '1.0.0')
  })

  it('parses all 9 phases', () => {
    const config = loadPipelineConfig(PIPELINE_TOML)
    const phaseNames = Object.keys(config.phases)

    assert.equal(phaseNames.length, 9)
    assert.ok(phaseNames.includes('research_discovery'))
    assert.ok(phaseNames.includes('debate'))
    assert.ok(phaseNames.includes('implementation_scaffold'))
  })

  it('parses phase properties correctly', () => {
    const config = loadPipelineConfig(PIPELINE_TOML)
    const discovery = config.phases.research_discovery

    assert.equal(discovery.id, 0)
    assert.equal(discovery.entry_point, true)
    assert.ok(discovery.inputs.includes('research-manifest'))
    assert.ok(discovery.outputs.includes('raw-collection'))
    assert.ok(discovery.tools.length > 0)
  })

  it('parses edges', () => {
    const config = loadPipelineConfig(PIPELINE_TOML)

    assert.ok(config.edges.length > 0)
    const firstEdge = config.edges[0]
    assert.ok(typeof firstEdge.from === 'string')
    assert.ok(typeof firstEdge.to === 'string')
  })

  it('parses debate config', () => {
    const config = loadPipelineConfig(PIPELINE_TOML)

    assert.ok(config.debate.agents.advocate)
    assert.ok(config.debate.agents.synthesizer)
    assert.equal(config.debate.rounds.round_1, 'divergent')
    assert.equal(config.debate.rounds.round_2, 'convergent')
  })

  it('parses schema references', () => {
    const config = loadPipelineConfig(PIPELINE_TOML)

    assert.equal(Object.keys(config.schemas).length, 9)
    assert.ok(config.schemas.pipeline_run.endsWith('.json'))
  })

  it('parses storage config', () => {
    const config = loadPipelineConfig(PIPELINE_TOML)

    assert.equal(config.storage.base_dir, 'pipeline_mcp_data')
    assert.ok(config.storage.paths.specs)
    assert.ok(config.storage.paths.overviews)
  })

  it('throws on missing file', () => {
    assert.throws(
      () => loadPipelineConfig('/nonexistent/pipeline.toml'),
      /ENOENT|no such file/,
    )
  })

  it('parses optional per-phase model field (Feature B precedence top)', () => {
    const dir = mkdtempSync(join(tmpdir(), 'toml-loader-model-'))
    const tomlPath = join(dir, 'pipeline.toml')
    try {
      writeFileSync(
        tomlPath,
        [
          '[pipeline]',
          'id = "test-pipeline"',
          'version = "0.0.1"',
          'description = "fixture for B3 model-field parser test"',
          '',
          '[phases.phase_a]',
          'id = 0',
          'description = "no model override"',
          'inputs = []',
          'outputs = []',
          'tools = []',
          'entry_point = true',
          '',
          '[phases.phase_b]',
          'id = 1',
          'description = "with model override"',
          'inputs = []',
          'outputs = []',
          'tools = []',
          'entry_point = false',
          'model = "claude-opus-4-6"',
          '',
          '[[edges]]',
          'from = "phase_a"',
          'to = "phase_b"',
          '',
          '[debate]',
          '[debate.rounds]',
          '[debate.output]',
          'includes_transcript = true',
          'includes_refined_artifact = true',
          'artifact_version_bump = "minor"',
          '',
          '[schemas]',
          'pipeline_run = "pipeline/schemas/pipeline-run.json"',
          '',
          '[storage]',
          'base_dir = "pipeline_mcp_data"',
          '[storage.paths]',
          'specs = "specs/"',
          '',
        ].join('\n'),
        'utf-8',
      )

      const config = loadPipelineConfig(tomlPath)

      assert.equal(
        config.phases.phase_a.model,
        undefined,
        'phase without model field must parse to undefined for precedence fall-through',
      )
      assert.equal(
        config.phases.phase_b.model,
        'claude-opus-4-6',
        'phase with model field must parse the override string verbatim',
      )
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })
})
