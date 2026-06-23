// tests/per-run-paths.test.ts
//
// Feature C integration tests for the per-run hierarchical artifact
// directory structure. This file exists per roadmap item M1-C9 and
// covers three scopes in one place so the per-run layout is exercised
// end-to-end through the handler layer:
//
//   (a) Unit tests on sanitizeRunName for Windows-unsafe and Unicode
//       inputs (the primary safety surface).
//   (b) A two-run sequence that exercises initRun → handleStoreArtifact
//       across multiple ArtifactSubtypes, asserting zero path collisions
//       under pipeline_mcp_data/runs/{name-timestamp}/ even when both
//       runs use identical file names.
//   (c) A legacy-fallback test that seeds a pre-Feature-C run-state
//       (no run_data_dir field) on disk, pre-populates legacy top-level
//       directories with artifact files, and asserts handleListArtifacts
//       still resolves them.

import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import {
  mkdtempSync,
  mkdirSync,
  rmSync,
  readFileSync,
  writeFileSync,
  existsSync,
} from 'node:fs'
import { join, dirname } from 'node:path'
import { tmpdir } from 'node:os'

import {
  sanitizeRunName,
  sanitizeRunTimestamp,
  getRunDataDir,
  storeArtifact,
  loadArtifact,
  listArtifacts,
  buildArtifactSummary,
  persistArtifact,
  storageKeyToSubtype,
} from '../storage.ts'
import type { ArtifactSummary } from '../storage.ts'
import { initRun, addArtifact } from '../run-state.ts'
import {
  handleStoreArtifact,
  handleListArtifacts,
} from '../artifact-handlers.ts'
import type { ArtifactContext } from '../artifact-handlers.ts'
import type {
  RunState,
  StorageConfig,
  PipelineRunParameters,
} from '../types.ts'

// ── Helpers ──────────────────────────────────────────────────

/**
 * Build a minimal ArtifactContext wired to real storage + run-state
 * helpers and backed by a mutable `runState` cell. Mirrors the private
 * helper in tests/artifact-handlers.test.ts so per-run-paths tests
 * exercise the same handler code paths.
 *
 * `persistArtifact` is always wired so the handler can branch into the
 * run-scoped path when `runState.current.run_data_dir` is set and the
 * storage_key maps to a known ArtifactSubtype. When neither condition
 * holds, the handler falls through to the legacy ctx.storeArtifact path.
 */
function makeCtx(
  tempDir: string,
  runState: { current: RunState },
  runDir: string,
): ArtifactContext {
  const sc: StorageConfig = {
    base_dir: tempDir,
    paths: {
      specs: 'specs',
      overviews: 'overviews',
      debates: 'debates',
      raw_collections: 'collections/raw',
      curated_collections: 'collections/curated',
      scaffold: 'scaffold',
    },
  }

  const ctx: ArtifactContext = {
    getSchemas: () => ({}),
    validateArtifact: () => ({ valid: true, errors: [] }),
    storeArtifact: (config, key, name, artifact, force) =>
      storeArtifact(config, key, name, artifact, force),
    loadArtifact: (config, key, name) => loadArtifact(config, key, name),
    listArtifacts: (config, key) => listArtifacts(config, key),
    buildArtifactSummary: (artifact, key, name): ArtifactSummary =>
      buildArtifactSummary(artifact, key, name),
    getStorageConfig: () => sc,
    baseDirFromRunDir: () => tempDir,
    getProjectRoot: () => tempDir,
    getConfigStorageBaseDir: () => 'artifacts',
    getActiveRun: () => runState.current,
    setActiveRun: (state) => {
      runState.current = state
    },
    getActiveRunDir: () => runDir,
    requireRun: () => runState.current,
    addArtifact: (state, ref, dir) => addArtifact(state, ref, dir),
    persistArtifact: (storageKey, fileName, artifact, force) => {
      const runDataDir = runState.current.run_data_dir
      const subtype = storageKeyToSubtype(storageKey)
      if (subtype === null || typeof runDataDir !== 'string' || runDataDir.length === 0) {
        throw new Error(
          `persistArtifact: cannot route storageKey="${storageKey}" with run_data_dir="${runDataDir ?? ''}" to run-scoped path`,
        )
      }
      return persistArtifact(runDataDir, subtype, fileName, artifact, force)
    },
  }

  return ctx
}

// ── (a) sanitizeRunName: Windows-unsafe + Unicode ───────────

describe('per-run-paths — (a) sanitizeRunName Windows-unsafe + Unicode', () => {
  it('strips every Windows-reserved filename character', () => {
    // Windows disallows: \ / : * ? " < > | (plus control chars).
    const input = 'a\\b/c:d*e?f"g<h>i|j'
    const sanitized = sanitizeRunName(input)
    for (const ch of ['\\', '/', ':', '*', '?', '"', '<', '>', '|']) {
      assert.ok(
        !sanitized.includes(ch),
        `Windows-unsafe char ${JSON.stringify(ch)} must not appear in sanitized output`,
      )
    }
    // Each unsafe char becomes '-' then runs collapse.
    assert.equal(sanitized, 'a-b-c-d-e-f-g-h-i-j')
  })

  it('strips Windows-reserved characters when combined with timestamp in getRunDataDir', () => {
    // Full per-run-dir computation must also be free of unsafe chars.
    const dir = getRunDataDir('base', 'Fix:Quality/Gate*Mismatch?', '2026-04-10T09:13:58.123Z')
    // Extract just the final dir component; path separators are platform-native.
    const dirName = dir.split(/[\\/]/).pop() ?? ''
    for (const ch of [':', '.', '*', '?', '"', '<', '>', '|']) {
      assert.ok(
        !dirName.includes(ch),
        `final dir name must not contain ${JSON.stringify(ch)}: got ${JSON.stringify(dirName)}`,
      )
    }
    // Expect the compound sanitized form (run-name then timestamp).
    assert.equal(dirName, 'fix-quality-gate-mismatch-2026-04-10T09-13-58-123Z')
  })

  it('collapses Cyrillic and pure-Unicode names to unnamed-run fallback', () => {
    // All non-ASCII characters become hyphens, leaving nothing behind.
    assert.equal(sanitizeRunName('тест-фикс'), 'unnamed-run')
    assert.equal(sanitizeRunName('修复-bug-修复'), 'bug')
  })

  it('preserves ASCII letters adjacent to Unicode / emoji', () => {
    // Latin word with accents: accented chars get replaced but letters survive.
    assert.equal(sanitizeRunName('café-fix'), 'caf-fix')
    // Emoji + ASCII: emoji stripped, hyphens collapse.
    assert.equal(sanitizeRunName('rocket-🚀-launch'), 'rocket-launch')
    // Mixed CJK + ASCII + underscore: underscore is replaced too.
    assert.equal(sanitizeRunName('修复_bug_修复'), 'bug')
  })

  it('still falls back to "unnamed-run" when the input is purely Unicode punctuation', () => {
    // Smart quotes, ellipsis, em-dash — all non-ASCII punctuation.
    assert.equal(sanitizeRunName('\u201c\u2026\u2014\u201d'), 'unnamed-run')
  })

  it('sanitizeRunTimestamp strips colon and period (Windows-unsafe in filenames)', () => {
    const input = '2026-04-10T09:13:58.123Z'
    const result = sanitizeRunTimestamp(input)
    assert.ok(!result.includes(':'))
    assert.ok(!result.includes('.'))
    assert.equal(result, '2026-04-10T09-13-58-123Z')
  })
})

// ── (b) Two-run sequence: zero path collisions ───────────────

describe('per-run-paths — (b) two-run sequence asserts zero collisions', () => {
  let tempDir: string

  beforeEach(() => {
    tempDir = mkdtempSync(join(tmpdir(), 'per-run-paths-collision-'))
  })

  afterEach(() => {
    rmSync(tempDir, { recursive: true, force: true })
  })

  it('initRun with distinct run_name values produces distinct run_data_dir paths', () => {
    // Both runs use the same legacy stateDir layout so getRunDataDir's
    // baseDir = dirname(dirname(stateDir)) = tempDir.
    const run1Dir = join(tempDir, 'runs', 'run-1-id')
    const run2Dir = join(tempDir, 'runs', 'run-2-id')

    const run1Params: PipelineRunParameters = {
      run_name: 'fix quality gate',
      run_directory_timestamp: '2026-04-10T09:13:58Z',
    }
    const run2Params: PipelineRunParameters = {
      run_name: 'Migrate Auth / v2',
      run_directory_timestamp: '2026-04-10T10:00:00Z',
    }

    const state1 = initRun('run-1-id', '1.0.0', ['discovery'], run1Dir, run1Params)
    const state2 = initRun('run-2-id', '1.0.0', ['discovery'], run2Dir, run2Params)

    assert.ok(state1.run_data_dir, 'run 1 must have run_data_dir populated')
    assert.ok(state2.run_data_dir, 'run 2 must have run_data_dir populated')

    // Different sanitized names AND different timestamps → must diverge.
    assert.notEqual(state1.run_data_dir, state2.run_data_dir)

    // Both dirs must sit under {tempDir}/runs/.
    const runsBase = join(tempDir, 'runs')
    assert.ok(
      state1.run_data_dir!.startsWith(runsBase),
      `run 1 data dir must nest under ${runsBase}, got ${state1.run_data_dir}`,
    )
    assert.ok(
      state2.run_data_dir!.startsWith(runsBase),
      `run 2 data dir must nest under ${runsBase}, got ${state2.run_data_dir}`,
    )

    // Sanitized final segments match our expectations.
    assert.equal(
      state1.run_data_dir,
      join(runsBase, 'fix-quality-gate-2026-04-10T09-13-58Z'),
    )
    assert.equal(
      state2.run_data_dir,
      join(runsBase, 'migrate-auth-v2-2026-04-10T10-00-00Z'),
    )
  })

  it('stores artifacts under each run\'s own run_data_dir with identical file names and zero overlap', () => {
    const run1Dir = join(tempDir, 'runs', 'run-1-id')
    const run2Dir = join(tempDir, 'runs', 'run-2-id')

    const state1 = initRun(
      'run-1-id',
      '1.0.0',
      ['discovery', 'synthesis'],
      run1Dir,
      { run_name: 'alpha-run', run_directory_timestamp: '2026-04-10T09:00:00Z' },
    )
    const state2 = initRun(
      'run-2-id',
      '1.0.0',
      ['discovery', 'synthesis'],
      run2Dir,
      { run_name: 'beta-run', run_directory_timestamp: '2026-04-10T09:00:00Z' },
    )

    const run1State = { current: state1 }
    const run2State = { current: state2 }
    const ctx1 = makeCtx(tempDir, run1State, run1Dir)
    const ctx2 = makeCtx(tempDir, run2State, run2Dir)

    // Store the same file_name under multiple subtypes in BOTH runs.
    // If routing leaked between runs, the second store for any pair
    // would either throw (collision) or clobber the first.
    const writes: Array<{ storageKey: string; artifactType: string; phase: string }> = [
      { storageKey: 'specs', artifactType: 'design-spec', phase: 'synthesis' },
      { storageKey: 'overviews', artifactType: 'knowledge-overview', phase: 'discovery' },
      { storageKey: 'debates', artifactType: 'debate-transcript', phase: 'discovery' },
      { storageKey: 'raw_collections', artifactType: 'raw-collection', phase: 'discovery' },
      { storageKey: 'curated_collections', artifactType: 'curated-collection', phase: 'discovery' },
    ]

    const run1Paths: string[] = []
    const run2Paths: string[] = []
    for (const w of writes) {
      const r1 = handleStoreArtifact(
        {
          storage_key: w.storageKey,
          file_name: 'shared.json',
          artifact: { id: 'run-1', storage_key: w.storageKey },
          artifact_type: w.artifactType,
          phase: w.phase,
        },
        ctx1,
      )
      const r2 = handleStoreArtifact(
        {
          storage_key: w.storageKey,
          file_name: 'shared.json',
          artifact: { id: 'run-2', storage_key: w.storageKey },
          artifact_type: w.artifactType,
          phase: w.phase,
        },
        ctx2,
      )
      const p1 = (JSON.parse(r1.json) as { data: { path: string } }).data.path
      const p2 = (JSON.parse(r2.json) as { data: { path: string } }).data.path
      run1Paths.push(p1)
      run2Paths.push(p2)
    }

    // Every pairwise combination across the two runs must be different.
    for (let i = 0; i < writes.length; i++) {
      for (let j = 0; j < writes.length; j++) {
        assert.notEqual(
          run1Paths[i],
          run2Paths[j],
          `run 1 "${writes[i].storageKey}" path must not match run 2 "${writes[j].storageKey}" path`,
        )
      }
    }

    // Every stored file must live under its own run's run_data_dir tree.
    for (const p of run1Paths) {
      assert.ok(
        p.startsWith(run1State.current.run_data_dir!),
        `run 1 artifact ${p} must nest under ${run1State.current.run_data_dir}`,
      )
      assert.ok(existsSync(p), `run 1 artifact ${p} must exist on disk`)
    }
    for (const p of run2Paths) {
      assert.ok(
        p.startsWith(run2State.current.run_data_dir!),
        `run 2 artifact ${p} must nest under ${run2State.current.run_data_dir}`,
      )
      assert.ok(existsSync(p), `run 2 artifact ${p} must exist on disk`)
    }

    // Content did not cross over — each file holds its own run's payload.
    for (const p of run1Paths) {
      const content = JSON.parse(readFileSync(p, 'utf-8')) as { id: string }
      assert.equal(content.id, 'run-1', `run 1 file ${p} must contain run 1 content`)
    }
    for (const p of run2Paths) {
      const content = JSON.parse(readFileSync(p, 'utf-8')) as { id: string }
      assert.equal(content.id, 'run-2', `run 2 file ${p} must contain run 2 content`)
    }
  })

  it('run_data_dir is stable across multiple stores — dir name does not drift', () => {
    // Ensure repeated writes in a single run reuse the same run_data_dir
    // rather than recomputing on every call (which would drift if any
    // caller passed a different timestamp by accident).
    const runDir = join(tempDir, 'runs', 'stable-run-id')
    const state = initRun(
      'stable-run-id',
      '1.0.0',
      ['synthesis'],
      runDir,
      { run_name: 'stable-run', run_directory_timestamp: '2026-04-10T09:13:58Z' },
    )
    const originalDataDir = state.run_data_dir
    assert.ok(originalDataDir)

    const runState = { current: state }
    const ctx = makeCtx(tempDir, runState, runDir)

    handleStoreArtifact(
      {
        storage_key: 'specs',
        file_name: 'one.json',
        artifact: { n: 1 },
        artifact_type: 'design-spec',
        phase: 'synthesis',
      },
      ctx,
    )
    handleStoreArtifact(
      {
        storage_key: 'specs',
        file_name: 'two.json',
        artifact: { n: 2 },
        artifact_type: 'design-spec',
        phase: 'synthesis',
      },
      ctx,
    )

    // Both files land under the same run_data_dir.
    assert.ok(existsSync(join(originalDataDir, 'specs', 'one.json')))
    assert.ok(existsSync(join(originalDataDir, 'specs', 'two.json')))
    // run_data_dir in state is unchanged after both writes.
    assert.equal(runState.current.run_data_dir, originalDataDir)
  })
})

// ── (c) Legacy fallback: pre-Feature-C runs ──────────────────

describe('per-run-paths — (c) legacy fallback for pre-Feature-C run-state', () => {
  let tempDir: string
  let runDir: string

  beforeEach(() => {
    tempDir = mkdtempSync(join(tmpdir(), 'per-run-paths-legacy-'))
    runDir = join(tempDir, 'runs', 'legacy-run-id')
  })

  afterEach(() => {
    rmSync(tempDir, { recursive: true, force: true })
  })

  it('handleListArtifacts resolves legacy top-level paths when run_data_dir is absent', () => {
    // Simulate a pre-Feature-C run: initRun WITHOUT run_parameters so
    // run_data_dir is never set. (initRun emits a console.warn in this
    // branch — that's expected and not a test concern here.)
    const state = initRun('legacy-run-id', '1.0.0', ['discovery'], runDir)
    assert.equal(
      state.run_data_dir,
      undefined,
      'pre-Feature-C fixture must NOT have run_data_dir populated',
    )

    // Seed the legacy top-level dirs with artifacts as a pre-Feature-C
    // run would have done.
    mkdirSync(join(tempDir, 'specs'), { recursive: true })
    writeFileSync(
      join(tempDir, 'specs', 'legacy-a.json'),
      JSON.stringify({ id: 'a', layout: 'legacy' }, null, 2),
      'utf-8',
    )
    writeFileSync(
      join(tempDir, 'specs', 'legacy-b.json'),
      JSON.stringify({ id: 'b', layout: 'legacy' }, null, 2),
      'utf-8',
    )

    const runState = { current: state }
    const ctx = makeCtx(tempDir, runState, runDir)

    const result = handleListArtifacts({ storage_key: 'specs' }, ctx)
    const parsed = JSON.parse(result.json) as {
      storage_key: string
      artifacts: Array<{ name: string }>
    }

    assert.equal(parsed.storage_key, 'specs')
    const names = parsed.artifacts.map((a) => a.name).sort()
    assert.deepEqual(
      names,
      ['legacy-a.json', 'legacy-b.json'],
      'legacy fallback must list files from {baseDir}/specs/ when run_data_dir is absent',
    )
  })

  it('legacy fallback works across multiple subtype-mapped storage keys', () => {
    // Same as above but verifies the fallback is consistent across
    // every storage_key that has a mapped ArtifactSubtype.
    const state = initRun('legacy-run-id', '1.0.0', ['discovery'], runDir)
    assert.equal(state.run_data_dir, undefined)

    const seedPaths: Array<{ storageKey: string; subPath: string; file: string }> = [
      { storageKey: 'specs', subPath: 'specs', file: 'spec.json' },
      { storageKey: 'overviews', subPath: 'overviews', file: 'ov.json' },
      { storageKey: 'debates', subPath: 'debates', file: 'db.json' },
      { storageKey: 'raw_collections', subPath: join('collections', 'raw'), file: 'rc.json' },
      { storageKey: 'curated_collections', subPath: join('collections', 'curated'), file: 'cc.json' },
    ]
    for (const s of seedPaths) {
      mkdirSync(join(tempDir, s.subPath), { recursive: true })
      writeFileSync(
        join(tempDir, s.subPath, s.file),
        JSON.stringify({ id: s.storageKey, layout: 'legacy' }, null, 2),
        'utf-8',
      )
    }

    const runState = { current: state }
    const ctx = makeCtx(tempDir, runState, runDir)

    for (const s of seedPaths) {
      const result = handleListArtifacts({ storage_key: s.storageKey }, ctx)
      const parsed = JSON.parse(result.json) as {
        storage_key: string
        artifacts: Array<{ name: string }>
      }
      assert.equal(parsed.storage_key, s.storageKey)
      const names = parsed.artifacts.map((a) => a.name)
      assert.deepEqual(
        names,
        [s.file],
        `legacy fallback for storage_key="${s.storageKey}" must return ${s.file}`,
      )
    }
  })

  it('legacy fallback is disabled for runs whose run_data_dir IS set', () => {
    // Sanity check in the opposite direction: when run_data_dir is
    // populated, handleListArtifacts must NOT bleed legacy files into
    // the listing. This backstops the fallback behavior so a buggy
    // future change that always hits legacy paths would fail this test.
    const state = initRun(
      'feature-c-run-id',
      '1.0.0',
      ['discovery'],
      runDir,
      { run_name: 'c-run', run_directory_timestamp: '2026-04-10T09:13:58Z' },
    )
    assert.ok(state.run_data_dir, 'Feature-C run must have run_data_dir populated')

    // Seed legacy directory with a file that must NOT appear in listing.
    mkdirSync(join(tempDir, 'specs'), { recursive: true })
    writeFileSync(
      join(tempDir, 'specs', 'legacy-leak.json'),
      JSON.stringify({ id: 'leak' }),
      'utf-8',
    )

    const runState = { current: state }
    const ctx = makeCtx(tempDir, runState, runDir)

    const result = handleListArtifacts({ storage_key: 'specs' }, ctx)
    const parsed = JSON.parse(result.json) as {
      storage_key: string
      artifacts: Array<{ name: string }>
    }

    const names = parsed.artifacts.map((a) => a.name)
    assert.ok(
      !names.includes('legacy-leak.json'),
      'Feature-C run listing must not include legacy-only files',
    )
    assert.deepEqual(
      names,
      [],
      'run-scoped directory is empty, so listing must be empty (not leaking legacy)',
    )
  })
})
