import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { loadQualityGates } from '../toml-loader.ts'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const QUALITY_GATES_TOML = resolve(__dirname, '../pipeline/quality-gates.toml')

// Canonical artifact types — derived from pipeline/schemas/*.json.
// Update this set whenever a new pipeline/schemas/*.json file is added.
const KNOWN_ARTIFACT_TYPES = new Set<string>([
  'raw-collection',
  'curated-collection',
  'knowledge-overview',
  'design-spec',
  'debate-transcript',
  'codebase-requirements',
  'validation-report',
  'research-manifest',
  'scaffold-document',
  'scaffold-manifest',
])

describe('quality-gates.toml config integrity', () => {
  it('every artifact_type param references a known registered artifact type', () => {
    const gates = loadQualityGates(QUALITY_GATES_TOML)
    assert.ok(gates.length > 0, 'expected quality-gates.toml to load at least one gate')

    for (const gate of gates) {
      const phaseName = gate.phase
      for (const check of gate.checks ?? []) {
        const gateId = check.check_type
        const params = (check.params ?? {}) as Record<string, unknown>
        const rawArtifactType = params.artifact_type
        if (rawArtifactType === undefined) continue
        const artifactType = String(rawArtifactType)
        assert.ok(
          KNOWN_ARTIFACT_TYPES.has(artifactType),
          `Phase "${phaseName}" gate "${gateId}" references unknown artifact_type="${artifactType}". Known: ${[...KNOWN_ARTIFACT_TYPES].sort().join(", ")}`
        )
      }
    }
  })
})
