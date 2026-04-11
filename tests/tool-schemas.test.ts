import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { toolSchemas } from '../tool-schemas.ts'

describe('toolSchemas static integrity', () => {
  it('has at least 25 tool definitions', () => {
    assert.ok(toolSchemas.length >= 25, `expected >= 25 tools, got ${toolSchemas.length}`)
  })

  it('all tool names are unique', () => {
    const names = toolSchemas.map(t => t.name)
    const unique = new Set(names)
    assert.equal(unique.size, names.length, `duplicate tool names: ${names.filter((n, i) => names.indexOf(n) !== i).join(', ')}`)
  })

  it('all tool names use the pipeline_ prefix', () => {
    for (const tool of toolSchemas) {
      assert.ok(
        tool.name.startsWith('pipeline_'),
        `tool "${tool.name}" does not start with pipeline_`,
      )
    }
  })

  it('no tool has an empty description', () => {
    for (const tool of toolSchemas) {
      assert.ok(
        typeof tool.description === 'string' && tool.description.length > 0,
        `tool "${tool.name}" has empty or missing description`,
      )
    }
  })

  it('every inputSchema.type is "object"', () => {
    for (const tool of toolSchemas) {
      assert.equal(
        tool.inputSchema.type,
        'object',
        `tool "${tool.name}" inputSchema.type is "${tool.inputSchema.type}", expected "object"`,
      )
    }
  })

  it('every inputSchema has a properties object', () => {
    for (const tool of toolSchemas) {
      assert.ok(
        typeof tool.inputSchema.properties === 'object' && tool.inputSchema.properties !== null,
        `tool "${tool.name}" inputSchema.properties is not an object`,
      )
    }
  })

  it('required arrays only reference properties that exist', () => {
    for (const tool of toolSchemas) {
      const required = tool.inputSchema.required ?? []
      const propNames = Object.keys(tool.inputSchema.properties)
      for (const req of required) {
        assert.ok(
          propNames.includes(req),
          `tool "${tool.name}" requires "${req}" but it is not in properties [${propNames.join(', ')}]`,
        )
      }
    }
  })

  it('pipeline_ tools include the core orchestration set', () => {
    const pipelineNames = toolSchemas.filter(t => t.name.startsWith('pipeline_')).map(t => t.name)
    const expected = [
      'pipeline_get_config',
      'pipeline_init_run',
      'pipeline_next_phases',
      'pipeline_start_phase',
      'pipeline_complete_phase',
      'pipeline_fail_phase',
      'pipeline_validate_artifact',
      'pipeline_store_artifact',
      'pipeline_load_artifact',
      'pipeline_run_status',
    ]
    for (const name of expected) {
      assert.ok(pipelineNames.includes(name), `missing core pipeline tool: ${name}`)
    }
  })

  it('pipeline_cc_ tools include the collector set', () => {
    const ccNames = toolSchemas.filter(t => t.name.startsWith('pipeline_cc_')).map(t => t.name)
    const expected = [
      'pipeline_cc_load_collection',
      'pipeline_cc_query',
      'pipeline_cc_get_items',
      'pipeline_cc_collect_concepts',
      'pipeline_cc_save_overview',
      'pipeline_cc_list_overviews',
      'pipeline_cc_get_overview',
    ]
    for (const name of expected) {
      assert.ok(ccNames.includes(name), `missing pipeline_cc tool: ${name}`)
    }
  })

  it('pipeline_synth_ tools include the synthesis set', () => {
    const synthNames = toolSchemas.filter(t => t.name.startsWith('pipeline_synth_')).map(t => t.name)
    const expected = [
      'pipeline_synth_load_collection',
      'pipeline_synth_query',
      'pipeline_synth_get_items',
      'pipeline_synth_create_spec',
      'pipeline_synth_save_spec',
      'pipeline_synth_list_specs',
    ]
    for (const name of expected) {
      assert.ok(synthNames.includes(name), `missing pipeline_synth tool: ${name}`)
    }
  })
})
