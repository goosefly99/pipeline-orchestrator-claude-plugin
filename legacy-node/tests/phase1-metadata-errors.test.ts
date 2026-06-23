// tests/phase1-metadata-errors.test.ts — Phase 1 verification tests
// Covers: annotation presence in all schemas, _meta.max_result_chars,
//         PipelineError instanceof chain, and toJSON serialization.

import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { toolSchemas } from '../tool-schemas.ts'
import type { ToolSchema } from '../tool-schemas.ts'
import { PipelineError } from '../types.ts'
import type { ErrorClass } from '../types.ts'

// ── Tool schema annotation tests ────────────────────────────────

describe('tool schema annotations', () => {
  it('every schema has _meta.max_result_chars defined', () => {
    const missing = toolSchemas.filter(s => s._meta?.max_result_chars == null)
    assert.equal(
      missing.length, 0,
      `Schemas missing _meta.max_result_chars: ${missing.map(s => s.name).join(', ')}`,
    )
  })

  it('_meta.max_result_chars is 50000 for read-only tools and 10000 for mutating tools', () => {
    for (const schema of toolSchemas) {
      const chars = schema._meta?.max_result_chars
      assert.ok(chars === 50000 || chars === 10000,
        `${schema.name}: expected max_result_chars to be 50000 or 10000, got ${chars}`)
    }
  })

  it('read-only annotated schemas use max_result_chars=50000', () => {
    const readOnly = toolSchemas.filter(s => s.annotations?.readOnlyHint === true)
    const wrong = readOnly.filter(s => s._meta?.max_result_chars !== 50000)
    assert.equal(
      wrong.length, 0,
      `Read-only schemas with wrong max_result_chars: ${wrong.map(s => `${s.name}=${s._meta?.max_result_chars}`).join(', ')}`,
    )
  })

  it('destructive annotated schemas use max_result_chars=10000', () => {
    const destructive = toolSchemas.filter(s => s.annotations?.destructiveHint === true)
    const wrong = destructive.filter(s => s._meta?.max_result_chars !== 10000)
    assert.equal(
      wrong.length, 0,
      `Destructive schemas with wrong max_result_chars: ${wrong.map(s => `${s.name}=${s._meta?.max_result_chars}`).join(', ')}`,
    )
  })

  it('schemas with annotations have exactly one of readOnlyHint or destructiveHint', () => {
    const annotated = toolSchemas.filter(s => s.annotations != null)
    for (const schema of annotated) {
      const ro = schema.annotations!.readOnlyHint === true
      const dest = schema.annotations!.destructiveHint === true
      assert.ok(
        (ro && !dest) || (!ro && dest),
        `${schema.name}: should have exactly one of readOnlyHint or destructiveHint, got readOnly=${ro}, destructive=${dest}`,
      )
    }
  })

  it('at least 38 of 43 schemas have annotations', () => {
    const annotated = toolSchemas.filter(s => s.annotations != null)
    assert.ok(
      annotated.length >= 38,
      `Expected at least 38 annotated schemas, got ${annotated.length}`,
    )
  })

  it('total schema count is 43', () => {
    assert.equal(toolSchemas.length, 43, `Expected 43 tool schemas, got ${toolSchemas.length}`)
  })

  it('all schema names are unique', () => {
    const names = toolSchemas.map(s => s.name)
    const unique = new Set(names)
    assert.equal(names.length, unique.size, 'Duplicate schema names found')
  })

  it('every schema has a non-empty name, description, and inputSchema', () => {
    for (const schema of toolSchemas) {
      assert.ok(schema.name.length > 0, 'Schema name must not be empty')
      assert.ok(schema.description.length > 0, `${schema.name}: description must not be empty`)
      assert.ok(schema.inputSchema != null, `${schema.name}: inputSchema must not be null`)
      assert.equal(schema.inputSchema.type, 'object', `${schema.name}: inputSchema.type must be "object"`)
    }
  })
})

// ── PipelineError tests ─────────────────────────────────────────

describe('PipelineError class', () => {
  it('is an instance of Error', () => {
    const err = new PipelineError('test', 'validation_error')
    assert.ok(err instanceof Error, 'PipelineError should be instanceof Error')
    assert.ok(err instanceof PipelineError, 'PipelineError should be instanceof PipelineError')
  })

  it('has name set to "PipelineError"', () => {
    const err = new PipelineError('test', 'validation_error')
    assert.equal(err.name, 'PipelineError')
  })

  it('stores error_class correctly', () => {
    const err = new PipelineError('bad input', 'validation_error')
    assert.equal(err.error_class, 'validation_error')
  })

  it('defaults recovery_action to empty string', () => {
    const err = new PipelineError('test', 'unknown_error')
    assert.equal(err.recovery_action, '')
  })

  it('defaults retryable to false', () => {
    const err = new PipelineError('test', 'unknown_error')
    assert.equal(err.retryable, false)
  })

  it('defaults details to empty object', () => {
    const err = new PipelineError('test', 'unknown_error')
    assert.deepEqual(err.details, {})
  })

  it('accepts all optional fields', () => {
    const err = new PipelineError('timeout', 'file_io_error', {
      recovery_action: 'Retry the operation.',
      retryable: true,
      details: { path: '/tmp/artifact.json', attempt: 3 },
    })
    assert.equal(err.recovery_action, 'Retry the operation.')
    assert.equal(err.retryable, true)
    assert.deepEqual(err.details, { path: '/tmp/artifact.json', attempt: 3 })
  })

  it('preserves cause when provided', () => {
    const cause = new Error('original')
    const err = new PipelineError('wrapped', 'unknown_error', { cause })
    assert.equal(err.cause, cause)
  })

  it('does not set cause when omitted', () => {
    const err = new PipelineError('test', 'unknown_error')
    assert.equal(err.cause, undefined)
  })
})

describe('PipelineError.toJSON() serialization', () => {
  it('returns all required fields', () => {
    const err = new PipelineError('Phase not found', 'state_transition_error', {
      recovery_action: 'Check phase name.',
      retryable: false,
      details: { phase: 'discovery' },
    })
    const json = err.toJSON()
    assert.equal(json.error_class, 'state_transition_error')
    assert.equal(json.message, 'Phase not found')
    assert.equal(json.recovery_action, 'Check phase name.')
    assert.equal(json.retryable, false)
    assert.deepEqual(json.details, { phase: 'discovery' })
  })

  it('includes defaults when no options provided', () => {
    const err = new PipelineError('simple', 'artifact_not_found')
    const json = err.toJSON()
    assert.equal(json.error_class, 'artifact_not_found')
    assert.equal(json.message, 'simple')
    assert.equal(json.recovery_action, '')
    assert.equal(json.retryable, false)
    assert.deepEqual(json.details, {})
  })

  it('produces valid JSON via JSON.stringify round-trip', () => {
    const err = new PipelineError('test', 'gate_blocked', {
      recovery_action: 'Fix quality issues.',
      retryable: true,
      details: { checks_failed: ['min_items', 'cross_ref'] },
    })
    const serialized = JSON.stringify(err.toJSON())
    const parsed = JSON.parse(serialized) as Record<string, unknown>
    assert.equal(parsed.error_class, 'gate_blocked')
    assert.equal(parsed.message, 'test')
    assert.equal(parsed.retryable, true)
    assert.deepEqual(parsed.details, { checks_failed: ['min_items', 'cross_ref'] })
  })

  it('JSON.stringify(err) uses toJSON automatically', () => {
    const err = new PipelineError('auto', 'schema_mismatch')
    const serialized = JSON.stringify(err)
    const parsed = JSON.parse(serialized) as Record<string, unknown>
    assert.equal(parsed.error_class, 'schema_mismatch')
    assert.equal(parsed.message, 'auto')
  })
})

describe('PipelineError error classes coverage', () => {
  const allErrorClasses: ErrorClass[] = [
    'validation_error',
    'state_transition_error',
    'file_io_error',
    'schema_mismatch',
    'artifact_not_found',
    'phase_not_ready',
    'configuration_error',
    'gate_blocked',
    'unknown_error',
  ]

  it('all 9 error classes can be instantiated and serialized', () => {
    for (const cls of allErrorClasses) {
      const err = new PipelineError(`test ${cls}`, cls)
      assert.equal(err.error_class, cls)
      const json = err.toJSON()
      assert.equal(json.error_class, cls)
    }
  })
})
