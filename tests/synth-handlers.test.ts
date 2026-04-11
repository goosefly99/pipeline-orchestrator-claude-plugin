import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { handleSynthSaveSpec } from '../synth-handlers.ts'
import type { SynthContext } from '../synth-handlers.ts'
import type { DesignSpec } from '../synth-types.ts'

// ── Helpers ───────────────────────────────────────────────────

function makeCtx(overrides?: Partial<SynthContext>): SynthContext {
  return {
    loadCollection: () => { throw new Error('not implemented') },
    queryItems: () => [],
    getItems: () => [],
    getSpecsDir: () => '/default/specs',
    ...overrides,
  }
}

function makeSpec(overrides?: Partial<DesignSpec>): DesignSpec {
  return {
    spec_id: '12345678-90ab-cdef-1234-567890abcdef',
    title: 'Test Spec',
    created_date: '2026-04-10',
    updated_date: '2026-04-10',
    version: '1.0',
    status: 'draft',
    spec_type: 'implementation',
    sources: [],
    overview: {
      description: '',
      objectives: [],
      constraints: [],
      assumptions: [],
    },
    architecture: {
      components: [],
      data_flow: '',
      integration_points: [],
    },
    implementation: {
      phases: [],
      tech_stack: [],
      complexity: 'medium',
    },
    risks: [],
    success_criteria: [],
    notes: '',
    ...overrides,
  }
}

// ── handleSynthSaveSpec — persistSpec routing (Feature C) ─────

describe('handleSynthSaveSpec', () => {
  it('prefers ctx.persistSpec when present and passes the output_dir override through', () => {
    const persistCalls: { spec: DesignSpec; outputDirOverride?: string }[] = []
    const getSpecsDirCalls: number[] = []

    const ctx = makeCtx({
      getSpecsDir: () => {
        getSpecsDirCalls.push(1)
        return '/should/not/be/used/specs'
      },
      persistSpec: (spec, outputDirOverride) => {
        persistCalls.push({ spec, outputDirOverride })
        return '/tmp/runs/abc/specs/persisted--12345678.json'
      },
    })

    const spec = makeSpec({
      spec_id: '12345678-90ab-cdef-1234-567890abcdef',
      title: 'Persisted',
      status: 'approved',
      sources: [
        { collection: 'c1', item_id: 'i1', title: 'Item 1', relevance: '' },
        { collection: 'c1', item_id: 'i2', title: 'Item 2', relevance: '' },
      ],
    })

    const result = handleSynthSaveSpec(
      { spec, output_dir: '/custom/override' },
      ctx,
    )

    // Exactly one persistSpec call, zero getSpecsDir calls.
    assert.equal(persistCalls.length, 1, 'persistSpec should be called exactly once')
    assert.equal(
      getSpecsDirCalls.length,
      0,
      'getSpecsDir should not be consulted when persistSpec is present',
    )
    assert.equal(persistCalls[0].spec.spec_id, '12345678-90ab-cdef-1234-567890abcdef')
    assert.equal(persistCalls[0].outputDirOverride, '/custom/override')

    // Byte-identical response envelope.
    assert.equal(
      result.json,
      'Spec saved: /tmp/runs/abc/specs/persisted--12345678.json\n'
        + '  Title: Persisted\n'
        + '  Status: approved\n'
        + '  Sources: 2',
    )
  })

  it('prefers ctx.persistSpec with undefined override when output_dir is not supplied', () => {
    const persistCalls: { spec: DesignSpec; outputDirOverride?: string }[] = []

    const ctx = makeCtx({
      persistSpec: (spec, outputDirOverride) => {
        persistCalls.push({ spec, outputDirOverride })
        return '/tmp/runs/def/specs/noarg--abcdef12.json'
      },
    })

    const spec = makeSpec({
      spec_id: 'abcdef12-90ab-cdef-1234-567890abcdef',
      title: 'NoArg',
    })

    const result = handleSynthSaveSpec({ spec }, ctx)

    assert.equal(persistCalls.length, 1)
    assert.equal(persistCalls[0].outputDirOverride, undefined)
    assert.ok(result.json.startsWith('Spec saved: /tmp/runs/def/specs/noarg--abcdef12.json'))
  })

  it('falls back to module saveSpec when ctx.persistSpec is absent, using ctx.getSpecsDir', () => {
    // When ctx.persistSpec is absent, handleSynthSaveSpec delegates to the
    // module-level saveSpec(spec, outputDir ?? ctx.getSpecsDir()). This test
    // exercises the fallback branch end-to-end on disk via a hermetic temp
    // dir, then asserts both the disk artifact and the response envelope.
    const tempDir = mkdtempSync(join(tmpdir(), 'synth-handlers-fallback-'))
    try {
      const getSpecsDirCalls: number[] = []
      const ctx = makeCtx({
        getSpecsDir: () => {
          getSpecsDirCalls.push(1)
          return tempDir
        },
        // persistSpec intentionally omitted.
      })

      const spec = makeSpec({
        spec_id: 'fa11bac0-90ab-cdef-1234-567890abcdef',
        title: 'Fallback',
        status: 'draft',
      })

      const result = handleSynthSaveSpec({ spec }, ctx)

      assert.equal(getSpecsDirCalls.length, 1)
      const expectedFile = join(tempDir, 'fallback--fa11bac0.json')
      assert.ok(existsSync(expectedFile), `expected file at ${expectedFile}`)
      assert.equal(
        result.json,
        `Spec saved: ${expectedFile}\n`
          + '  Title: Fallback\n'
          + '  Status: draft\n'
          + '  Sources: 0',
      )
    } finally {
      rmSync(tempDir, { recursive: true, force: true })
    }
  })

  it('falls back to module saveSpec with the explicit output_dir arg when persistSpec is absent', () => {
    const tempDir = mkdtempSync(join(tmpdir(), 'synth-handlers-override-'))
    try {
      const getSpecsDirCalls: number[] = []
      const ctx = makeCtx({
        getSpecsDir: () => {
          getSpecsDirCalls.push(1)
          return '/should/not/be/used'
        },
        // persistSpec intentionally omitted.
      })

      const spec = makeSpec({
        spec_id: '0ver1de0-90ab-cdef-1234-567890abcdef',
        title: 'Overridden',
      })

      const result = handleSynthSaveSpec({ spec, output_dir: tempDir }, ctx)

      // When output_dir is supplied, getSpecsDir should not be consulted.
      assert.equal(getSpecsDirCalls.length, 0)
      const expectedFile = join(tempDir, 'overridden--0ver1de0.json')
      assert.ok(existsSync(expectedFile))
      assert.ok(result.json.startsWith(`Spec saved: ${expectedFile}`))
    } finally {
      rmSync(tempDir, { recursive: true, force: true })
    }
  })

  it('throws when spec is missing', () => {
    const ctx = makeCtx()
    assert.throws(
      () => handleSynthSaveSpec({}, ctx),
      /spec is required/,
    )
  })

  it('throws when spec is missing spec_id', () => {
    const ctx = makeCtx()
    const spec = makeSpec()
    // @ts-expect-error: deliberately invalid
    delete spec.spec_id
    assert.throws(
      () => handleSynthSaveSpec({ spec }, ctx),
      /spec must have spec_id and title/,
    )
  })

  it('throws when spec is missing title', () => {
    const ctx = makeCtx()
    const spec = makeSpec()
    // @ts-expect-error: deliberately invalid
    delete spec.title
    assert.throws(
      () => handleSynthSaveSpec({ spec }, ctx),
      /spec must have spec_id and title/,
    )
  })
})
