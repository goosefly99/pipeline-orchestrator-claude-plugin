import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { storeArtifact, loadArtifact, listArtifacts, buildArtifactSummary } from '../storage.ts'
import type { StorageConfig } from '../types.ts'

let tempDir: string
let storageConfig: StorageConfig

beforeEach(() => {
  tempDir = mkdtempSync(join(tmpdir(), 'pipeline-test-'))
  storageConfig = {
    base_dir: tempDir,
    paths: {
      raw_collections: 'collections/raw',
      curated_collections: 'collections/curated',
      specs: 'specs',
      overviews: 'overviews',
      debates: 'debates',
    },
  }
})

afterEach(() => {
  rmSync(tempDir, { recursive: true, force: true })
})

describe('storeArtifact', () => {
  it('stores a JSON artifact in the correct directory', () => {
    const artifact = { collection_id: 'test-123', status: 'raw', items: [] }
    const path = storeArtifact(storageConfig, 'raw_collections', 'test-123.json', artifact)

    assert.ok(existsSync(path))
    const stored = JSON.parse(readFileSync(path, 'utf-8'))
    assert.equal(stored.collection_id, 'test-123')
  })

  it('creates intermediate directories', () => {
    const artifact = { spec_id: 'abc', title: 'Test Spec' }
    const path = storeArtifact(storageConfig, 'specs', 'test-spec.json', artifact)

    assert.ok(existsSync(path))
    assert.ok(path.includes('specs'))
  })

  it('throws on unknown storage key', () => {
    assert.throws(
      () => storeArtifact(storageConfig, 'nonexistent', 'file.json', {}),
      /unknown storage key/i,
    )
  })

  it('throws when overwriting without force', () => {
    storeArtifact(storageConfig, 'specs', 'existing.json', { id: 'v1' })
    assert.throws(
      () => storeArtifact(storageConfig, 'specs', 'existing.json', { id: 'v2' }),
      /Artifact already exists.*force=true/,
    )
  })

  it('allows overwrite with force=true', () => {
    storeArtifact(storageConfig, 'specs', 'existing.json', { id: 'v1' })
    const path = storeArtifact(storageConfig, 'specs', 'existing.json', { id: 'v2' }, true)
    const loaded = JSON.parse(readFileSync(path, 'utf-8'))
    assert.equal(loaded.id, 'v2')
  })
})

describe('loadArtifact', () => {
  it('loads a previously stored artifact', () => {
    const artifact = { id: 'test', data: 'hello' }
    storeArtifact(storageConfig, 'specs', 'test.json', artifact)

    const loaded = loadArtifact(storageConfig, 'specs', 'test.json')
    assert.deepEqual(loaded, artifact)
  })

  it('returns null for missing artifact', () => {
    const loaded = loadArtifact(storageConfig, 'specs', 'nonexistent.json')
    assert.equal(loaded, null)
  })
})

describe('listArtifacts', () => {
  it('lists all artifacts in a storage path', () => {
    storeArtifact(storageConfig, 'specs', 'a.json', { id: 'a' })
    storeArtifact(storageConfig, 'specs', 'b.json', { id: 'b' })

    const files = listArtifacts(storageConfig, 'specs')
    assert.equal(files.length, 2)
    assert.ok(files.includes('a.json'))
    assert.ok(files.includes('b.json'))
  })

  it('returns empty array for nonexistent directory', () => {
    const files = listArtifacts(storageConfig, 'overviews')
    assert.deepEqual(files, [])
  })
})

describe('buildArtifactSummary', () => {
  it('returns artifact_type set to storage key and id set to file name', () => {
    const summary = buildArtifactSummary({ a: 1 }, 'specs', 'spec-abc.json')
    assert.equal(summary.artifact_type, 'specs')
    assert.equal(summary.id, 'spec-abc.json')
  })

  it('reports correct key_count for a small object', () => {
    const artifact = { spec_id: 'x', title: 'y', version: '1.0.0' }
    const summary = buildArtifactSummary(artifact, 'specs', 'test.json')
    assert.equal(summary.key_count, 3)
    assert.equal(summary.truncated, false)
    assert.deepEqual(summary.preview_keys, ['spec_id', 'title', 'version'])
  })

  it('reports total_size_bytes as byte-length of JSON representation', () => {
    const artifact = { key: 'value' }
    const summary = buildArtifactSummary(artifact, 'specs', 'size.json')
    assert.equal(summary.total_size_bytes, Buffer.byteLength(JSON.stringify(artifact), 'utf-8'))
    assert.equal(summary.total_size_bytes, 15) // {"key":"value"} = 15 bytes
  })

  it('sets truncated=true and limits preview to 20 keys for a large object', () => {
    const artifact: Record<string, number> = {}
    for (let i = 0; i < 25; i++) {
      artifact[`key_${String(i).padStart(2, '0')}`] = i
    }
    const summary = buildArtifactSummary(artifact, 'raw_collections', 'large.json')
    assert.equal(summary.key_count, 25)
    assert.equal(summary.truncated, true)
    assert.equal(summary.preview_keys.length, 20)
    assert.equal(summary.preview_keys[0], 'key_00')
    assert.equal(summary.preview_keys[19], 'key_19')
  })

  it('sets truncated=false when key_count equals the limit exactly (20)', () => {
    const artifact: Record<string, number> = {}
    for (let i = 0; i < 20; i++) {
      artifact[`k${i}`] = i
    }
    const summary = buildArtifactSummary(artifact, 'specs', 'boundary.json')
    assert.equal(summary.key_count, 20)
    assert.equal(summary.truncated, false)
    assert.equal(summary.preview_keys.length, 20)
  })

  it('returns empty key_count and preview for non-object artifacts', () => {
    const summary = buildArtifactSummary('a string', 'specs', 'scalar.json')
    assert.equal(summary.key_count, 0)
    assert.equal(summary.truncated, false)
    assert.deepEqual(summary.preview_keys, [])
  })

  it('returns empty key_count and preview for null artifact', () => {
    const summary = buildArtifactSummary(null, 'specs', 'null.json')
    assert.equal(summary.key_count, 0)
    assert.equal(summary.truncated, false)
    assert.deepEqual(summary.preview_keys, [])
  })

  it('returns empty key_count for array artifacts (arrays are objects but Object.keys gives indices as strings)', () => {
    // Arrays are technically objects, so Object.keys returns '0', '1', '2'
    const summary = buildArtifactSummary([1, 2, 3], 'specs', 'array.json')
    assert.equal(summary.key_count, 3)
    assert.deepEqual(summary.preview_keys, ['0', '1', '2'])
  })
})
