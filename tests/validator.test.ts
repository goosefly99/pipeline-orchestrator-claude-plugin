import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { loadSchemas, validateArtifact } from '../validator.ts'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const SCHEMAS_DIR = resolve(__dirname, '../pipeline/schemas')

describe('loadSchemas', () => {
  it('loads all 12 schema files', () => {
    const schemas = loadSchemas(SCHEMAS_DIR)
    assert.equal(Object.keys(schemas).length, 12)
    assert.ok(schemas['pipeline-run.json'])
    assert.ok(schemas['raw-collection.json'])
    assert.ok(schemas['design-spec.json'])
    assert.ok(schemas['scaffold-document.json'])
    assert.ok(schemas['scaffold-manifest.json'])
    assert.ok(schemas['validation-report.json'])
  })
})

describe('validateArtifact', () => {
  it('validates a valid raw collection', () => {
    const schemas = loadSchemas(SCHEMAS_DIR)
    const artifact = {
      collection_id: 'test--12345678',
      created_date: '2026-04-03T14:30:00Z',
      status: 'raw',
      source_runs: [],
      items: [],
    }

    const result = validateArtifact(schemas, 'raw-collection.json', artifact)
    assert.equal(result.valid, true)
    assert.equal(result.errors.length, 0)
  })

  it('rejects a raw collection missing required fields', () => {
    const schemas = loadSchemas(SCHEMAS_DIR)
    const artifact = {
      collection_id: 'test--12345678',
      // missing: created_date, status, items
    }

    const result = validateArtifact(schemas, 'raw-collection.json', artifact)
    assert.equal(result.valid, false)
    assert.ok(result.errors.length > 0)
  })

  it('validates a valid debate transcript', () => {
    const schemas = loadSchemas(SCHEMAS_DIR)
    const artifact = {
      transcript_id: 'dt-12345678',
      input_type: 'design-spec',
      input_id: 'spec-123',
      created_date: '2026-04-03T16:00:00Z',
      rounds: [],
      synthesis: {
        changes_accepted: [],
        changes_rejected: [],
      },
    }

    const result = validateArtifact(schemas, 'debate-transcript.json', artifact)
    assert.equal(result.valid, true)
  })

  it('throws on unknown schema', () => {
    const schemas = loadSchemas(SCHEMAS_DIR)

    assert.throws(
      () => validateArtifact(schemas, 'nonexistent.json', {}),
      /not found/i,
    )
  })
})
