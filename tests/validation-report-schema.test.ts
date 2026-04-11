import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { loadSchemas, validateArtifact } from '../validator.ts'
import { loadQualityGates } from '../toml-loader.ts'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const SCHEMAS_DIR = resolve(__dirname, '../pipeline/schemas')
const QUALITY_GATES_TOML = resolve(__dirname, '../pipeline/quality-gates.toml')

describe('validation-report.json schema', () => {
  it('loads validation-report.json from the schemas directory', () => {
    const schemas = loadSchemas(SCHEMAS_DIR)
    assert.ok(schemas['validation-report.json'], 'validation-report.json must exist in pipeline/schemas/')
  })

  it('validates a well-formed validation-report', () => {
    const schemas = loadSchemas(SCHEMAS_DIR)
    const artifact = {
      report_id: 'validation-report-spec-test',
      spec_id: 'spec-test',
      phase: 'validation',
      validated_date: '2026-04-10',
      results: [
        {
          check_id: 'VAL-1',
          claim: 'Sample claim',
          status: 'PASS',
          evidence: 'Sample evidence',
        },
      ],
      overall_status: 'PASS',
    }
    const result = validateArtifact(schemas, 'validation-report.json', artifact)
    assert.equal(result.valid, true, `Unexpected errors: ${result.errors.join(', ')}`)
  })

  it('rejects a validation-report missing required fields', () => {
    const schemas = loadSchemas(SCHEMAS_DIR)
    const artifact = {
      report_id: 'validation-report-bad',
      // missing: spec_id, phase, validated_date, results, overall_status
    }
    const result = validateArtifact(schemas, 'validation-report.json', artifact)
    assert.equal(result.valid, false)
    assert.ok(result.errors.length > 0)
  })

  it('validates optional fields when present', () => {
    const schemas = loadSchemas(SCHEMAS_DIR)
    const artifact = {
      report_id: 'validation-report-spec-test-2',
      spec_id: 'spec-test-2',
      phase: 'validation',
      validated_date: '2026-04-10T12:34:56Z',
      results: [],
      overall_status: 'PASS',
      spec_errors_found: [],
      validated_spec_path: 'pipeline_mcp_data/specs/test-spec.json',
    }
    const result = validateArtifact(schemas, 'validation-report.json', artifact)
    assert.equal(result.valid, true, `Unexpected errors: ${result.errors.join(', ')}`)
  })

  it('quality-gates.toml validation gate references validation-report artifact_type', () => {
    const gates = loadQualityGates(QUALITY_GATES_TOML)
    const validationGate = gates.find(g => g.phase === 'validation')
    assert.ok(validationGate, 'expected a validation gate entry')
    const fieldPresentCheck = validationGate!.checks.find(c => c.check_type === 'field_present')
    assert.ok(fieldPresentCheck, 'expected a field_present check in the validation gate')
    assert.equal(
      (fieldPresentCheck!.params as Record<string, unknown>).artifact_type,
      'validation-report',
      'validation gate must reference artifact_type="validation-report" to match the new schema',
    )
    assert.equal(
      (fieldPresentCheck!.params as Record<string, unknown>).field,
      'results',
      'validation gate must check the "results" field on validation-report artifacts',
    )
  })
})
