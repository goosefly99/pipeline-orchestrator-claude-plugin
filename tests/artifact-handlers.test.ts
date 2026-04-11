import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, writeFileSync, mkdirSync, readFileSync, existsSync } from 'node:fs'
import { join, normalize, resolve, basename } from 'node:path'
import { tmpdir } from 'node:os'
import {
  computeNextVersion,
  handleStoreArtifact,
  handleListArtifacts,
  handleLoadArtifact,
  handleValidateArtifact,
  handleRegisterArtifact,
} from '../artifact-handlers.ts'
import type { ArtifactContext } from '../artifact-handlers.ts'
import { PipelineError } from '../types.ts'
import type { ArtifactRef, RunState, StorageConfig } from '../types.ts'
import { storeArtifact, loadArtifact, listArtifacts, buildArtifactSummary } from '../storage.ts'
import { initRun, addArtifact, loadRunState } from '../run-state.ts'
import type { ArtifactSummary } from '../storage.ts'

describe('computeNextVersion', () => {
  it('returns 1 when no existing artifacts', () => {
    const version = computeNextVersion([], 'raw-collection', 'discovery')
    assert.equal(version, 1)
  })

  it('returns 1 when no artifacts match the type+phase pair', () => {
    const artifacts: ArtifactRef[] = [
      { type: 'design-spec', path: '/specs/v1.json', phase: 'synthesis', created_at: '2026-01-01T00:00:00.000Z', version: 1 },
    ]
    const version = computeNextVersion(artifacts, 'raw-collection', 'discovery')
    assert.equal(version, 1)
  })

  it('returns 1 when type matches but phase differs', () => {
    const artifacts: ArtifactRef[] = [
      { type: 'raw-collection', path: '/raw/v1.json', phase: 'curation', created_at: '2026-01-01T00:00:00.000Z', version: 1 },
    ]
    const version = computeNextVersion(artifacts, 'raw-collection', 'discovery')
    assert.equal(version, 1)
  })

  it('returns 1 when phase matches but type differs', () => {
    const artifacts: ArtifactRef[] = [
      { type: 'design-spec', path: '/specs/v1.json', phase: 'discovery', created_at: '2026-01-01T00:00:00.000Z', version: 1 },
    ]
    const version = computeNextVersion(artifacts, 'raw-collection', 'discovery')
    assert.equal(version, 1)
  })

  it('returns 2 when one existing artifact of the same type+phase exists', () => {
    const artifacts: ArtifactRef[] = [
      { type: 'raw-collection', path: '/raw/v1.json', phase: 'discovery', created_at: '2026-01-01T00:00:00.000Z', version: 1 },
    ]
    const version = computeNextVersion(artifacts, 'raw-collection', 'discovery')
    assert.equal(version, 2)
  })

  it('returns max version + 1 when multiple versions exist', () => {
    const artifacts: ArtifactRef[] = [
      { type: 'design-spec', path: '/specs/v1.json', phase: 'synthesis', created_at: '2026-01-01T00:00:00.000Z', version: 1 },
      { type: 'design-spec', path: '/specs/v2.json', phase: 'synthesis', created_at: '2026-01-01T00:00:00.000Z', version: 2 },
      { type: 'design-spec', path: '/specs/v3.json', phase: 'synthesis', created_at: '2026-01-01T00:00:00.000Z', version: 3 },
    ]
    const version = computeNextVersion(artifacts, 'design-spec', 'synthesis')
    assert.equal(version, 4)
  })

  it('handles non-sequential version numbers and picks the true max', () => {
    const artifacts: ArtifactRef[] = [
      { type: 'design-spec', path: '/specs/v1.json', phase: 'synthesis', created_at: '2026-01-01T00:00:00.000Z', version: 1 },
      { type: 'design-spec', path: '/specs/v5.json', phase: 'synthesis', created_at: '2026-01-01T00:00:00.000Z', version: 5 },
      { type: 'design-spec', path: '/specs/v3.json', phase: 'synthesis', created_at: '2026-01-01T00:00:00.000Z', version: 3 },
    ]
    const version = computeNextVersion(artifacts, 'design-spec', 'synthesis')
    assert.equal(version, 6)
  })

  it('treats missing version field as 1 via nullish coalesce', () => {
    const artifacts: ArtifactRef[] = [
      // version is omitted (legacy artifact)
      { type: 'raw-collection', path: '/raw/legacy.json', phase: 'discovery', created_at: '2026-01-01T00:00:00.000Z' },
    ]
    const version = computeNextVersion(artifacts, 'raw-collection', 'discovery')
    assert.equal(version, 2)
  })

  it('does not count artifacts from unrelated phases toward the version', () => {
    const artifacts: ArtifactRef[] = [
      { type: 'design-spec', path: '/specs/curation-v1.json', phase: 'curation', created_at: '2026-01-01T00:00:00.000Z', version: 1 },
      { type: 'design-spec', path: '/specs/curation-v2.json', phase: 'curation', created_at: '2026-01-01T00:00:00.000Z', version: 2 },
      { type: 'design-spec', path: '/specs/synth-v1.json', phase: 'synthesis', created_at: '2026-01-01T00:00:00.000Z', version: 1 },
    ]
    // curation has v1+v2, synthesis has v1 → storing another 'design-spec' in 'synthesis' → version 2
    const version = computeNextVersion(artifacts, 'design-spec', 'synthesis')
    assert.equal(version, 2)
  })

  it('each unique type+phase pair is versioned independently', () => {
    const artifacts: ArtifactRef[] = [
      { type: 'raw-collection', path: '/raw/v1.json', phase: 'discovery', created_at: '2026-01-01T00:00:00.000Z', version: 1 },
      { type: 'raw-collection', path: '/raw/v2.json', phase: 'discovery', created_at: '2026-01-01T00:00:00.000Z', version: 2 },
      { type: 'design-spec', path: '/specs/v1.json', phase: 'synthesis', created_at: '2026-01-01T00:00:00.000Z', version: 1 },
    ]
    assert.equal(computeNextVersion(artifacts, 'raw-collection', 'discovery'), 3)
    assert.equal(computeNextVersion(artifacts, 'design-spec', 'synthesis'), 2)
    assert.equal(computeNextVersion(artifacts, 'design-spec', 'discovery'), 1)
  })
})

// ── Handler-level versioning and lineage tests ───────────────

/**
 * Build a minimal ArtifactContext backed by real storage and run-state helpers.
 * Only the methods used by handleStoreArtifact and handleListArtifacts are wired.
 */
function makeCtx(tempDir: string, runState: { current: RunState }, runDir: string): ArtifactContext {
  const sc: StorageConfig = {
    base_dir: tempDir,
    paths: { specs: 'specs', raw_collections: 'collections/raw' },
  }

  return {
    getSchemas: () => ({}),
    validateArtifact: () => ({ valid: true, errors: [] }),
    storeArtifact: (config, key, name, artifact, force) => storeArtifact(config, key, name, artifact, force),
    loadArtifact: (config, key, name) => loadArtifact(config, key, name),
    listArtifacts: (config, key) => listArtifacts(config, key),
    buildArtifactSummary: (artifact, key, name): ArtifactSummary => buildArtifactSummary(artifact, key, name),
    getStorageConfig: () => sc,
    baseDirFromRunDir: () => tempDir,
    getProjectRoot: () => tempDir,
    getConfigStorageBaseDir: () => 'artifacts',
    getActiveRun: () => runState.current,
    setActiveRun: (state) => { runState.current = state },
    getActiveRunDir: () => runDir,
    requireRun: () => runState.current,
    addArtifact: (state, ref, dir) => {
      const updated = addArtifact(state, ref, dir)
      return updated
    },
  }
}

describe('handleStoreArtifact — versioning and lineage', () => {
  let tempDir: string
  let runDir: string

  beforeEach(() => {
    tempDir = mkdtempSync(join(tmpdir(), 'pipeline-handlers-test-'))
    runDir = join(tempDir, 'runs', 'test-run')
  })

  afterEach(() => {
    rmSync(tempDir, { recursive: true, force: true })
  })

  it('default version=1 for first artifact of a given type+phase', () => {
    const runState = { current: initRun('run-1', '1.0.0', ['discovery'], runDir) }
    const ctx = makeCtx(tempDir, runState, runDir)

    const result = handleStoreArtifact({
      storage_key: 'specs',
      file_name: 'spec-v1.json',
      artifact: { spec_id: 'first' },
      artifact_type: 'design-spec',
      phase: 'discovery',
    }, ctx)

    const parsed = JSON.parse(result.json) as { data: { version: number } }
    assert.equal(parsed.data.version, 1)
  })

  it('version auto-increments to 2 when the same type+phase is stored a second time', () => {
    const runState = { current: initRun('run-2', '1.0.0', ['synthesis'], runDir) }
    const ctx = makeCtx(tempDir, runState, runDir)

    // First store — version 1
    handleStoreArtifact({
      storage_key: 'specs',
      file_name: 'spec-v1.json',
      artifact: { spec_id: 'first' },
      artifact_type: 'design-spec',
      phase: 'synthesis',
    }, ctx)

    // Second store (same type+phase) — version 2
    const result = handleStoreArtifact({
      storage_key: 'specs',
      file_name: 'spec-v2.json',
      artifact: { spec_id: 'second' },
      artifact_type: 'design-spec',
      phase: 'synthesis',
    }, ctx)

    const parsed = JSON.parse(result.json) as { data: { version: number } }
    assert.equal(parsed.data.version, 2)
    assert.equal(runState.current.available_artifacts.length, 2)
    assert.equal(runState.current.available_artifacts[1].version, 2)
  })

  it('different type in same phase each gets version 1 independently', () => {
    const runState = { current: initRun('run-3', '1.0.0', ['discovery'], runDir) }
    const ctx = makeCtx(tempDir, runState, runDir)

    const r1 = handleStoreArtifact({
      storage_key: 'specs',
      file_name: 'spec-first.json',
      artifact: { spec_id: 'a' },
      artifact_type: 'design-spec',
      phase: 'discovery',
    }, ctx)

    const r2 = handleStoreArtifact({
      storage_key: 'raw_collections',
      file_name: 'raw-first.json',
      artifact: { collection_id: 'rc-001' },
      artifact_type: 'raw-collection',
      phase: 'discovery',
    }, ctx)

    const p1 = JSON.parse(r1.json) as { data: { version: number } }
    const p2 = JSON.parse(r2.json) as { data: { version: number } }
    assert.equal(p1.data.version, 1)
    assert.equal(p2.data.version, 1)
  })

  it('same type in different phases each gets version 1 independently', () => {
    const runState = { current: initRun('run-4', '1.0.0', ['phase-a', 'phase-b'], runDir) }
    const ctx = makeCtx(tempDir, runState, runDir)

    const r1 = handleStoreArtifact({
      storage_key: 'specs',
      file_name: 'spec-phase-a.json',
      artifact: { spec_id: 'a' },
      artifact_type: 'design-spec',
      phase: 'phase-a',
    }, ctx)

    const r2 = handleStoreArtifact({
      storage_key: 'specs',
      file_name: 'spec-phase-b.json',
      artifact: { spec_id: 'b' },
      artifact_type: 'design-spec',
      phase: 'phase-b',
    }, ctx)

    const p1 = JSON.parse(r1.json) as { data: { version: number } }
    const p2 = JSON.parse(r2.json) as { data: { version: number } }
    assert.equal(p1.data.version, 1)
    assert.equal(p2.data.version, 1)
  })

  it('parent_artifact is stored in ArtifactRef and included in response data', () => {
    const runState = { current: initRun('run-5', '1.0.0', ['synthesis'], runDir) }
    const ctx = makeCtx(tempDir, runState, runDir)

    const result = handleStoreArtifact({
      storage_key: 'specs',
      file_name: 'derived-spec.json',
      artifact: { spec_id: 'derived' },
      artifact_type: 'design-spec',
      phase: 'synthesis',
      parent_artifact: '/artifacts/raw/original.json',
    }, ctx)

    const parsed = JSON.parse(result.json) as { data: { parent_artifact: string } }
    assert.equal(parsed.data.parent_artifact, '/artifacts/raw/original.json')

    const ref = runState.current.available_artifacts[0]
    assert.equal(ref.parent_artifact, '/artifacts/raw/original.json')
  })

  it('version and parent_artifact are preserved through store→load round-trip via run state', () => {
    const runState = { current: initRun('run-6', '1.0.0', ['synthesis'], runDir) }
    const ctx = makeCtx(tempDir, runState, runDir)

    handleStoreArtifact({
      storage_key: 'specs',
      file_name: 'round-trip.json',
      artifact: { spec_id: 'rt' },
      artifact_type: 'design-spec',
      phase: 'synthesis',
      parent_artifact: 'raw/source.json',
    }, ctx)

    // Reload from disk to verify persistence (addArtifact persists to runDir)
    const reloaded = loadRunState(runDir)
    assert.ok(reloaded, 'run state should be reloadable from disk')
    assert.equal(reloaded.available_artifacts.length, 1)
    const ref = reloaded.available_artifacts[0]
    assert.equal(ref.version, 1)
    assert.equal(ref.parent_artifact, 'raw/source.json')
  })
})

describe('handleListArtifacts — version and parent_artifact in response', () => {
  let tempDir: string
  let runDir: string

  beforeEach(() => {
    tempDir = mkdtempSync(join(tmpdir(), 'pipeline-list-test-'))
    runDir = join(tempDir, 'runs', 'list-run')
  })

  afterEach(() => {
    rmSync(tempDir, { recursive: true, force: true })
  })

  it('list response includes version and parent_artifact for stored artifacts', () => {
    const runState = { current: initRun('list-run', '1.0.0', ['synthesis'], runDir) }
    const ctx = makeCtx(tempDir, runState, runDir)

    // Store two artifacts: one plain, one with a parent
    handleStoreArtifact({
      storage_key: 'specs',
      file_name: 'spec-a.json',
      artifact: { spec_id: 'a' },
      artifact_type: 'design-spec',
      phase: 'synthesis',
    }, ctx)

    handleStoreArtifact({
      storage_key: 'specs',
      file_name: 'spec-b.json',
      artifact: { spec_id: 'b' },
      artifact_type: 'design-spec',
      phase: 'synthesis',
      parent_artifact: 'specs/spec-a.json',
    }, ctx)

    const listResult = handleListArtifacts({ storage_key: 'specs' }, ctx)
    const parsed = JSON.parse(listResult.json) as {
      storage_key: string
      artifacts: Array<{ name: string; version?: number; parent_artifact?: string }>
    }

    assert.equal(parsed.storage_key, 'specs')
    assert.equal(parsed.artifacts.length, 2)

    const a = parsed.artifacts.find(x => x.name === 'spec-a.json')
    const b = parsed.artifacts.find(x => x.name === 'spec-b.json')

    assert.ok(a, 'spec-a.json should appear in listing')
    assert.ok(b, 'spec-b.json should appear in listing')

    // Both have version field
    assert.equal(a?.version, 1)
    assert.equal(b?.version, 2)

    // Only spec-b has parent_artifact
    assert.equal(a?.parent_artifact, undefined)
    assert.equal(b?.parent_artifact, 'specs/spec-a.json')
  })
})

describe('handleLoadArtifact — summary-by-default, full, and fields', () => {
  let tempDir: string
  let runDir: string

  const sampleArtifact = {
    collection_id: 'rc-001',
    version: '1.0',
    items: [{ id: 'a', content: 'hello' }, { id: 'b', content: 'world' }],
    tags: ['test'],
    metadata: { created_by: 'test' },
  }

  beforeEach(() => {
    tempDir = mkdtempSync(join(tmpdir(), 'pipeline-load-test-'))
    runDir = join(tempDir, 'runs', 'load-run')
  })

  afterEach(() => {
    rmSync(tempDir, { recursive: true, force: true })
  })

  it('default (no full, no fields) returns a summary instead of full content', () => {
    const runState = { current: initRun('load-run-1', '1.0.0', ['discovery'], runDir) }
    const ctx = makeCtx(tempDir, runState, runDir)

    handleStoreArtifact({
      storage_key: 'raw_collections',
      file_name: 'rc-001.json',
      artifact: sampleArtifact,
      artifact_type: 'raw-collection',
      phase: 'discovery',
    }, ctx)

    const result = handleLoadArtifact({ storage_key: 'raw_collections', file_name: 'rc-001.json' }, ctx)
    assert.ok(!result.isError, 'should not be an error')
    const parsed = JSON.parse(result.json) as Record<string, unknown>
    // Summary should NOT include top-level artifact fields like 'items'
    assert.ok(!Object.prototype.hasOwnProperty.call(parsed, 'items'), 'summary should not contain full items array')
    // Summary should contain lightweight metadata fields from ArtifactSummary
    assert.ok(Object.prototype.hasOwnProperty.call(parsed, 'artifact_type'), 'summary should include artifact_type')
    assert.ok(Object.prototype.hasOwnProperty.call(parsed, 'id'), 'summary should include id')
    assert.ok(Object.prototype.hasOwnProperty.call(parsed, 'key_count'), 'summary should include key_count')
  })

  it('full=true returns the complete artifact content', () => {
    const runState = { current: initRun('load-run-2', '1.0.0', ['discovery'], runDir) }
    const ctx = makeCtx(tempDir, runState, runDir)

    handleStoreArtifact({
      storage_key: 'raw_collections',
      file_name: 'rc-002.json',
      artifact: sampleArtifact,
      artifact_type: 'raw-collection',
      phase: 'discovery',
    }, ctx)

    const result = handleLoadArtifact({ storage_key: 'raw_collections', file_name: 'rc-002.json', full: true }, ctx)
    assert.ok(!result.isError, 'should not be an error')
    const parsed = JSON.parse(result.json) as typeof sampleArtifact
    assert.equal(parsed.collection_id, 'rc-001')
    assert.equal(parsed.items.length, 2)
    assert.deepEqual(parsed.tags, ['test'])
  })

  it('fields param returns only the requested top-level keys', () => {
    const runState = { current: initRun('load-run-3', '1.0.0', ['discovery'], runDir) }
    const ctx = makeCtx(tempDir, runState, runDir)

    handleStoreArtifact({
      storage_key: 'raw_collections',
      file_name: 'rc-003.json',
      artifact: sampleArtifact,
      artifact_type: 'raw-collection',
      phase: 'discovery',
    }, ctx)

    const result = handleLoadArtifact({
      storage_key: 'raw_collections',
      file_name: 'rc-003.json',
      fields: ['collection_id', 'tags'],
    }, ctx)
    assert.ok(!result.isError, 'should not be an error')
    const parsed = JSON.parse(result.json) as Record<string, unknown>
    assert.equal(parsed.collection_id, 'rc-001')
    assert.deepEqual(parsed.tags, ['test'])
    assert.ok(!Object.prototype.hasOwnProperty.call(parsed, 'items'), 'items should not be present')
    assert.ok(!Object.prototype.hasOwnProperty.call(parsed, 'metadata'), 'metadata should not be present')
    assert.ok(!Object.prototype.hasOwnProperty.call(parsed, 'version'), 'version should not be present')
  })

  it('fields param with non-existent keys returns empty object', () => {
    const runState = { current: initRun('load-run-4', '1.0.0', ['discovery'], runDir) }
    const ctx = makeCtx(tempDir, runState, runDir)

    handleStoreArtifact({
      storage_key: 'raw_collections',
      file_name: 'rc-004.json',
      artifact: sampleArtifact,
      artifact_type: 'raw-collection',
      phase: 'discovery',
    }, ctx)

    const result = handleLoadArtifact({
      storage_key: 'raw_collections',
      file_name: 'rc-004.json',
      fields: ['nonexistent_key'],
    }, ctx)
    assert.ok(!result.isError, 'should not be an error')
    const parsed = JSON.parse(result.json) as Record<string, unknown>
    assert.deepEqual(parsed, {})
  })

  it('throws PipelineError with artifact_not_found when artifact is not found', () => {
    const runState = { current: initRun('load-run-5', '1.0.0', ['discovery'], runDir) }
    const ctx = makeCtx(tempDir, runState, runDir)

    assert.throws(
      () => handleLoadArtifact({ storage_key: 'raw_collections', file_name: 'missing.json' }, ctx),
      (err: unknown) => {
        assert.ok(err instanceof PipelineError, 'should be a PipelineError')
        assert.equal(err.error_class, 'artifact_not_found')
        assert.ok(err.message.includes('not found'), 'error message should indicate not found')
        return true
      },
    )
  })
})

describe('handleValidateArtifact — file_path alternative', () => {
  let tempDir: string
  let runDir: string

  beforeEach(() => {
    tempDir = mkdtempSync(join(tmpdir(), 'pipeline-validate-test-'))
    runDir = join(tempDir, 'runs', 'validate-run')
  })

  afterEach(() => {
    rmSync(tempDir, { recursive: true, force: true })
  })

  it('validates successfully when file_path points to a valid artifact file', () => {
    const runState = { current: initRun('validate-run-1', '1.0.0', ['discovery'], runDir) }
    const ctx = makeCtx(tempDir, runState, runDir)

    // Write a JSON artifact file to disk
    const artifactFile = join(tempDir, 'my-artifact.json')
    writeFileSync(artifactFile, JSON.stringify({ collection_id: 'rc-001', items: [] }))

    const result = handleValidateArtifact({
      schema: 'raw-collection.json',
      file_path: artifactFile,
    }, ctx)

    assert.ok(!result.isError, 'should not be an error')
    const parsed = JSON.parse(result.json) as { status: string; data: { valid: boolean } }
    assert.equal(parsed.status, 'ok')
    assert.equal(parsed.data.valid, true)
  })

  it('throws when neither artifact nor file_path is provided', () => {
    const runState = { current: initRun('validate-run-2', '1.0.0', ['discovery'], runDir) }
    const ctx = makeCtx(tempDir, runState, runDir)

    assert.throws(
      () => handleValidateArtifact({ schema: 'raw-collection.json' }, ctx),
      /Either artifact \(inline JSON\) or file_path must be provided/,
    )
  })

  it('throws when file_path points to a non-existent file', () => {
    const runState = { current: initRun('validate-run-3', '1.0.0', ['discovery'], runDir) }
    const ctx = makeCtx(tempDir, runState, runDir)

    assert.throws(
      () => handleValidateArtifact({
        schema: 'raw-collection.json',
        file_path: join(tempDir, 'does-not-exist.json'),
      }, ctx),
      /File not found/,
    )
  })

  it('validates inline artifact as before when artifact is provided', () => {
    const runState = { current: initRun('validate-run-4', '1.0.0', ['discovery'], runDir) }
    const ctx = makeCtx(tempDir, runState, runDir)

    const result = handleValidateArtifact({
      schema: 'raw-collection.json',
      artifact: { collection_id: 'rc-inline', items: [] },
    }, ctx)

    assert.ok(!result.isError, 'should not be an error')
    const parsed = JSON.parse(result.json) as { status: string; data: { valid: boolean } }
    assert.equal(parsed.status, 'ok')
    assert.equal(parsed.data.valid, true)
  })
})

// ── artifact_stored events (Fix 3.6) ─────────────────────────

function readEvents(runDir: string): Array<Record<string, unknown>> {
  const eventsPath = join(runDir, 'events.jsonl')
  if (!existsSync(eventsPath)) return []
  const content = readFileSync(eventsPath, 'utf-8')
  const events: Array<Record<string, unknown>> = []
  for (const line of content.split('\n')) {
    const trimmed = line.trim()
    if (!trimmed) continue
    try {
      events.push(JSON.parse(trimmed) as Record<string, unknown>)
    } catch {
      // ignore
    }
  }
  return events
}

describe('handleStoreArtifact — emits artifact_stored event', () => {
  let tempDir: string
  let runDir: string

  beforeEach(() => {
    tempDir = mkdtempSync(join(tmpdir(), 'artifact-event-test-'))
    runDir = join(tempDir, 'runs', 'evt-run')
  })

  afterEach(() => {
    rmSync(tempDir, { recursive: true, force: true })
  })

  it('writes exactly one artifact_stored event after a successful store', () => {
    const runState = { current: initRun('evt-store-1', '1.0.0', ['discovery'], runDir) }
    const ctx = makeCtx(tempDir, runState, runDir)

    const result = handleStoreArtifact({
      storage_key: 'specs',
      file_name: 'evt-spec.json',
      artifact: { spec_id: 'evt-1' },
      artifact_type: 'design-spec',
      phase: 'discovery',
    }, ctx)

    const parsed = JSON.parse(result.json) as { data: { path: string } }
    const storedPath = parsed.data.path

    const events = readEvents(runDir)
    const storedEvents = events.filter(e => e.event === 'artifact_stored')
    assert.equal(storedEvents.length, 1, 'expected exactly one artifact_stored event')

    const evt = storedEvents[0]
    assert.equal(evt.phase, 'discovery')
    assert.equal(evt.run_id, 'evt-store-1')

    const details = evt.details as Record<string, unknown>
    assert.equal(details.artifact_type, 'design-spec')
    assert.equal(details.path, storedPath)
    assert.equal(typeof details.size_bytes, 'number')
    // size_bytes for a non-empty JSON file must be > 0
    assert.ok((details.size_bytes as number) > 0, 'size_bytes should be greater than zero')
    // content_hash is skipped per spec — it must be absent, not null or empty.
    assert.equal(
      Object.prototype.hasOwnProperty.call(details, 'content_hash'),
      false,
      'content_hash should not be present',
    )
  })

  it('writes a second artifact_stored event for a second store call', () => {
    const runState = { current: initRun('evt-store-2', '1.0.0', ['synthesis'], runDir) }
    const ctx = makeCtx(tempDir, runState, runDir)

    handleStoreArtifact({
      storage_key: 'specs',
      file_name: 'v1.json',
      artifact: { spec_id: 'a' },
      artifact_type: 'design-spec',
      phase: 'synthesis',
    }, ctx)

    handleStoreArtifact({
      storage_key: 'specs',
      file_name: 'v2.json',
      artifact: { spec_id: 'b' },
      artifact_type: 'design-spec',
      phase: 'synthesis',
    }, ctx)

    const events = readEvents(runDir)
    const storedEvents = events.filter(e => e.event === 'artifact_stored')
    assert.equal(storedEvents.length, 2, 'expected one artifact_stored event per store call')
  })
})

describe('handleRegisterArtifact — emits artifact_stored event', () => {
  let tempDir: string
  let runDir: string

  beforeEach(() => {
    tempDir = mkdtempSync(join(tmpdir(), 'artifact-register-evt-test-'))
    runDir = join(tempDir, 'runs', 'reg-run')
  })

  afterEach(() => {
    rmSync(tempDir, { recursive: true, force: true })
  })

  it('writes one artifact_stored event after a successful register', () => {
    const runState = { current: initRun('evt-reg-1', '1.0.0', ['discovery'], runDir) }
    const ctx = makeCtx(tempDir, runState, runDir)

    // Write a file to disk that can be registered.
    const externalFile = join(tempDir, 'external-artifact.json')
    writeFileSync(externalFile, JSON.stringify({ collection_id: 'ext-1', items: ['a', 'b'] }), 'utf-8')

    const result = handleRegisterArtifact({
      file_path: externalFile,
      artifact_type: 'raw-collection',
      phase: 'discovery',
      storage_key: 'raw_collections',
    }, ctx)

    const parsed = JSON.parse(result.json) as { registered: boolean; path: string }
    assert.equal(parsed.registered, true)

    const events = readEvents(runDir)
    const storedEvents = events.filter(e => e.event === 'artifact_stored')
    assert.equal(storedEvents.length, 1, 'expected exactly one artifact_stored event after register')

    const evt = storedEvents[0]
    assert.equal(evt.phase, 'discovery')
    assert.equal(evt.run_id, 'evt-reg-1')

    const details = evt.details as Record<string, unknown>
    assert.equal(details.artifact_type, 'raw-collection')
    assert.equal(details.path, parsed.path)
    assert.equal(typeof details.size_bytes, 'number')
    assert.ok((details.size_bytes as number) > 0, 'size_bytes should be greater than zero')
  })
})
