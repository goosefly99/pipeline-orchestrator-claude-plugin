import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, existsSync, readFileSync } from 'node:fs'
import { join, resolve, relative, isAbsolute } from 'node:path'
import { tmpdir } from 'node:os'
import {
  saveSpec,
  specFileName,
  resolveSpecsDir,
  persistSpec,
} from '../synth.ts'
import type { DesignSpec } from '../synth-types.ts'

let tempDir: string

beforeEach(() => {
  tempDir = mkdtempSync(join(tmpdir(), 'synth-test-'))
})

afterEach(() => {
  rmSync(tempDir, { recursive: true, force: true })
})

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

// ── specFileName ──────────────────────────────────────────────

describe('specFileName', () => {
  it('derives a slug-based filename with the first 8 chars of spec_id appended', () => {
    const spec = makeSpec({ spec_id: 'abcdef12-rest-of-uuid', title: 'My Spec' })
    assert.equal(specFileName(spec), 'my-spec--abcdef12.json')
  })

  it('strips special characters and collapses runs of non-alphanumerics', () => {
    const spec = makeSpec({ spec_id: 'xyzxyzxy-rest', title: 'Hello! @World # 2026' })
    assert.equal(specFileName(spec), 'hello-world-2026--xyzxyzxy.json')
  })

  it('trims leading and trailing hyphens from the slug', () => {
    const spec = makeSpec({ spec_id: '11111111-rest', title: '!!!trim me!!!' })
    assert.equal(specFileName(spec), 'trim-me--11111111.json')
  })

  it('truncates slug to at most 60 characters', () => {
    const spec = makeSpec({ spec_id: '22222222-rest', title: 'a'.repeat(100) })
    const name = specFileName(spec)
    // slug is 60 'a's, then '--22222222.json'
    assert.equal(name, `${'a'.repeat(60)}--22222222.json`)
  })
})

// ── resolveSpecsDir ───────────────────────────────────────────

describe('resolveSpecsDir', () => {
  it('routes to run_data_dir/specs when runDataDir is set', () => {
    const runDataDir = join(tempDir, 'runs', 'my-run-2026-04-10')
    const dir = resolveSpecsDir(runDataDir, join(tempDir, 'legacy'))
    assert.equal(dir, join(runDataDir, 'specs'))
  })

  it('falls back to legacyBaseDir/specs when runDataDir is undefined', () => {
    const legacyBaseDir = join(tempDir, 'legacy')
    const dir = resolveSpecsDir(undefined, legacyBaseDir)
    assert.equal(dir, join(legacyBaseDir, 'specs'))
  })

  it('treats empty-string runDataDir as absent (falsy) and falls back', () => {
    const legacyBaseDir = join(tempDir, 'legacy')
    const dir = resolveSpecsDir('', legacyBaseDir)
    assert.equal(dir, join(legacyBaseDir, 'specs'))
  })
})

// ── persistSpec ───────────────────────────────────────────────

describe('persistSpec', () => {
  it('writes under runDataDir/specs/{slug--id8}.json when runDataDir is set', () => {
    const runDataDir = join(tempDir, 'runs', 'persist-a')
    const spec = makeSpec({ spec_id: 'aaaaaaaa-1111-2222-3333-444444444444', title: 'Persist Test' })

    const writtenPath = persistSpec(
      runDataDir,
      join(tempDir, 'legacy'),
      spec,
    )

    const expected = join(runDataDir, 'specs', 'persist-test--aaaaaaaa.json')
    assert.equal(writtenPath, expected)
    assert.ok(existsSync(expected), `expected file at ${expected}`)
    const parsed = JSON.parse(readFileSync(expected, 'utf-8')) as DesignSpec
    assert.equal(parsed.spec_id, 'aaaaaaaa-1111-2222-3333-444444444444')
    assert.equal(parsed.title, 'Persist Test')
  })

  it('falls back to legacyBaseDir/specs/{slug--id8}.json when runDataDir is absent', () => {
    const legacyBaseDir = join(tempDir, 'legacy-base')
    const spec = makeSpec({ spec_id: 'bbbbbbbb-1111-2222-3333-444444444444', title: 'Legacy Test' })

    const writtenPath = persistSpec(undefined, legacyBaseDir, spec)

    const expected = join(legacyBaseDir, 'specs', 'legacy-test--bbbbbbbb.json')
    assert.equal(writtenPath, expected)
    assert.ok(existsSync(expected), `expected file at ${expected}`)
  })

  it('falls back to legacyBaseDir when runDataDir is an empty string', () => {
    const legacyBaseDir = join(tempDir, 'legacy-empty')
    const spec = makeSpec({ spec_id: 'cccccccc-1111-2222-3333-444444444444', title: 'Empty Rd' })

    const writtenPath = persistSpec('', legacyBaseDir, spec)

    const expected = join(legacyBaseDir, 'specs', 'empty-rd--cccccccc.json')
    assert.equal(writtenPath, expected)
    assert.ok(existsSync(expected))
  })

  it('honors outputDirOverride above both runDataDir and legacyBaseDir', () => {
    const runDataDir = join(tempDir, 'runs', 'should-be-ignored')
    const legacyBaseDir = join(tempDir, 'legacy-ignored')
    const overrideDir = join(tempDir, 'explicit-override')
    const spec = makeSpec({ spec_id: 'dddddddd-1111-2222-3333-444444444444', title: 'Override Test' })

    const writtenPath = persistSpec(runDataDir, legacyBaseDir, spec, overrideDir)

    const expected = join(resolve(overrideDir), 'override-test--dddddddd.json')
    assert.equal(writtenPath, expected)
    assert.ok(existsSync(expected))
    assert.ok(!existsSync(join(runDataDir, 'specs')), 'runDataDir path should not be created')
    assert.ok(!existsSync(join(legacyBaseDir, 'specs')), 'legacyBaseDir path should not be created')
  })

  it('honors outputDirOverride even when runDataDir is absent', () => {
    const overrideDir = join(tempDir, 'override-no-run')
    const spec = makeSpec({ spec_id: 'eeeeeeee-1111-2222-3333-444444444444', title: 'No Run Override' })

    const writtenPath = persistSpec(undefined, join(tempDir, 'legacy'), spec, overrideDir)

    const expected = join(resolve(overrideDir), 'no-run-override--eeeeeeee.json')
    assert.equal(writtenPath, expected)
    assert.ok(existsSync(expected))
  })

  it('resolves a relative outputDirOverride to an absolute path', () => {
    // Compute an actually-relative path from cwd to a subdir of tempDir so
    // the test exercises the resolve() call path, not the absolute-passthrough
    // (relative() returns a path like ../../tmp/... on POSIX or ..\..\tmp\...
    // on Windows; either way it's not absolute).
    const absoluteOverride = join(tempDir, 'rel-override')
    const relativeOverride = relative(process.cwd(), absoluteOverride)
    assert.ok(!isAbsolute(relativeOverride), 'test setup: override must be relative')

    const spec = makeSpec({ spec_id: 'ffffffff-1111-2222-3333-444444444444', title: 'Relative' })
    const writtenPath = persistSpec(undefined, tempDir, spec, relativeOverride)

    const expected = resolve(relativeOverride, 'relative--ffffffff.json')
    assert.equal(writtenPath, expected)
    assert.ok(isAbsolute(writtenPath), 'result should be an absolute path')
    assert.ok(existsSync(writtenPath))
  })

  it('creates the target directory tree if it does not exist', () => {
    const runDataDir = join(tempDir, 'runs', 'deep', 'nested', 'target')
    const spec = makeSpec({ spec_id: '99999999-1111-2222-3333-444444444444', title: 'Deep Nested' })

    persistSpec(runDataDir, tempDir, spec)

    const dir = join(runDataDir, 'specs')
    assert.ok(existsSync(dir), `expected mkdir to create ${dir}`)
  })

  it('returns an absolute path', () => {
    const runDataDir = join(tempDir, 'runs', 'abs')
    const spec = makeSpec({ spec_id: '88888888-1111-2222-3333-444444444444', title: 'Abs Path' })

    const writtenPath = persistSpec(runDataDir, tempDir, spec)

    assert.equal(resolve(writtenPath), writtenPath)
  })

  it('overwrites an existing spec file without raising', () => {
    const runDataDir = join(tempDir, 'runs', 'overwrite-spec')
    const first = makeSpec({
      spec_id: '77777777-1111-2222-3333-444444444444',
      title: 'Same Title',
      notes: 'first version',
    })
    const second = makeSpec({
      spec_id: '77777777-1111-2222-3333-444444444444',
      title: 'Same Title',
      notes: 'second version',
    })

    const path1 = persistSpec(runDataDir, tempDir, first)
    const path2 = persistSpec(runDataDir, tempDir, second)

    assert.equal(path1, path2)
    const parsed = JSON.parse(readFileSync(path2, 'utf-8')) as DesignSpec
    assert.equal(parsed.notes, 'second version', 'second write should overwrite the first')
  })

  it('uses the same filename as saveSpec (shared specFileName helper)', () => {
    const spec = makeSpec({ spec_id: '66666666-1111-2222-3333-444444444444', title: 'Match Test' })

    const savedPath = saveSpec(spec, tempDir)
    const persistedPath = persistSpec(undefined, tempDir, spec)

    // saveSpec writes directly into its outputDir arg; persistSpec treats
    // legacyBaseDir as a parent and appends 'specs/'. The intent of this check
    // is to confirm both routes derive the same on-disk filename from the shared
    // specFileName helper — the containing directories intentionally differ.
    const expectedName = specFileName(spec)
    assert.equal(savedPath, join(tempDir, expectedName))
    assert.equal(persistedPath, join(tempDir, 'specs', expectedName))
  })

  it('mutates spec.updated_date to today before writing', () => {
    const runDataDir = join(tempDir, 'runs', 'updated-date')
    const spec = makeSpec({
      spec_id: '55555555-1111-2222-3333-444444444444',
      title: 'Date Mutation',
      updated_date: '1999-01-01',
    })

    persistSpec(runDataDir, tempDir, spec)

    const today = new Date().toISOString().split('T')[0]
    assert.equal(spec.updated_date, today, 'persistSpec should mutate updated_date to today')

    const written = JSON.parse(
      readFileSync(join(runDataDir, 'specs', 'date-mutation--55555555.json'), 'utf-8'),
    ) as DesignSpec
    assert.equal(written.updated_date, today)
  })
})

// ── saveSpec ──────────────────────────────────────────────────
//
// Lightweight coverage of the legacy path — persistSpec is where the
// bulk of the Feature-C test surface lives. These tests exist to
// document current behavior before any future refactors touch
// saveSpec.

describe('saveSpec', () => {
  it('writes to the supplied outputDir using the same filename as specFileName', () => {
    const spec = makeSpec({ spec_id: '44444444-1111-2222-3333-444444444444', title: 'Save Test' })

    const writtenPath = saveSpec(spec, tempDir)

    const expected = join(tempDir, specFileName(spec))
    assert.equal(writtenPath, expected)
    assert.ok(existsSync(expected))
  })

  it('creates the outputDir if it does not exist', () => {
    const targetDir = join(tempDir, 'does', 'not', 'yet', 'exist')
    const spec = makeSpec({ spec_id: '33333333-1111-2222-3333-444444444444', title: 'Create Dir' })

    const writtenPath = saveSpec(spec, targetDir)

    assert.ok(existsSync(targetDir))
    assert.ok(existsSync(writtenPath))
  })
})
