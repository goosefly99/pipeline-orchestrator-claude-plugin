import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { loadPipelineConfig } from '../toml-loader.ts'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

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
})
