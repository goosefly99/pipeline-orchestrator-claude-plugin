import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import {
  storeArtifact,
  loadArtifact,
  listArtifacts,
  buildArtifactSummary,
  sanitizeRunName,
  sanitizeRunTimestamp,
  getRunDataDir,
  getArtifactDir,
} from '../storage.ts'
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

// ── Per-Run Hierarchical Artifact Directory Helpers (Feature C) ─────

describe('sanitizeRunName', () => {
  it('passes through plain ASCII letters and digits unchanged (lowercased)', () => {
    assert.equal(sanitizeRunName('abc123'), 'abc123')
    assert.equal(sanitizeRunName('ABC123'), 'abc123')
  })

  it('preserves single hyphens between word groups', () => {
    assert.equal(sanitizeRunName('fix-quality-gate'), 'fix-quality-gate')
  })

  it('replaces spaces and punctuation with hyphens and collapses runs', () => {
    assert.equal(sanitizeRunName('Fix Quality Gate'), 'fix-quality-gate')
    assert.equal(sanitizeRunName('fix:quality_gate.run'), 'fix-quality-gate-run')
  })

  it('strips Windows-unsafe characters (`\\ / : * ? " < > |`)', () => {
    const sanitized = sanitizeRunName('a\\b/c:d*e?f"g<h>i|j')
    // Each unsafe char becomes '-' and runs collapse.
    assert.equal(sanitized, 'a-b-c-d-e-f-g-h-i-j')
    // Spot-check none of the unsafe characters survive.
    for (const ch of ['\\', '/', ':', '*', '?', '"', '<', '>', '|']) {
      assert.ok(!sanitized.includes(ch), `unsafe char ${ch} should be stripped`)
    }
  })

  it('replaces non-ASCII / Unicode characters with hyphens', () => {
    // Cyrillic, accented Latin, CJK and emoji all collapse to hyphens.
    assert.equal(sanitizeRunName('тест-фикс'), 'unnamed-run')
    assert.equal(sanitizeRunName('café-fix'), 'caf-fix')
    assert.equal(sanitizeRunName('修复-bug'), 'bug')
    assert.equal(sanitizeRunName('rocket-🚀-launch'), 'rocket-launch')
  })

  it('trims leading and trailing hyphens after sanitization', () => {
    assert.equal(sanitizeRunName('  --fix--bug--  '), 'fix-bug')
    assert.equal(sanitizeRunName('---'), 'unnamed-run')
  })

  it('truncates to 64 characters', () => {
    const long = 'a'.repeat(200)
    const sanitized = sanitizeRunName(long)
    assert.equal(sanitized.length, 64)
    assert.ok(sanitized.split('').every(c => c === 'a'))
  })

  it('truncation does not leave a trailing hyphen', () => {
    // 63 chars of 'a' then a non-hyphen, then chars that become hyphens.
    // After replacement at exactly the 64-char boundary the helper trims any
    // trailing hyphen from the cut.
    const input = 'a'.repeat(63) + ' ' + 'b'.repeat(10)
    const sanitized = sanitizeRunName(input)
    assert.equal(sanitized.length, 63, 'trailing hyphen at slice boundary should be trimmed')
    assert.ok(!sanitized.endsWith('-'))
  })

  it('returns "unnamed-run" for empty / whitespace / non-string inputs', () => {
    assert.equal(sanitizeRunName(''), 'unnamed-run')
    assert.equal(sanitizeRunName('   '), 'unnamed-run')
    assert.equal(sanitizeRunName('!!!'), 'unnamed-run')
    // Defensive — runtime callers may pass undefined/null despite the type sig.
    assert.equal(sanitizeRunName(undefined as unknown as string), 'unnamed-run')
    assert.equal(sanitizeRunName(null as unknown as string), 'unnamed-run')
  })
})

describe('sanitizeRunTimestamp', () => {
  it('replaces colons and dots with hyphens', () => {
    assert.equal(sanitizeRunTimestamp('2026-04-10T09:13:58Z'), '2026-04-10T09-13-58Z')
    assert.equal(sanitizeRunTimestamp('2026-04-10T09:13:58.123Z'), '2026-04-10T09-13-58-123Z')
  })

  it('passes through hyphenated date and timezone marker', () => {
    const result = sanitizeRunTimestamp('2026-04-10T09:13:58Z')
    assert.ok(result.startsWith('2026-04-10T'))
    assert.ok(result.endsWith('Z'))
  })

  it('contains no Windows-unsafe characters', () => {
    const result = sanitizeRunTimestamp('2026-04-10T09:13:58.123Z')
    for (const ch of [':', '.', '/', '\\', '*', '?', '"', '<', '>', '|']) {
      assert.ok(!result.includes(ch), `unsafe char ${ch} should be stripped`)
    }
  })

  it('returns empty string for empty / non-string input', () => {
    assert.equal(sanitizeRunTimestamp(''), '')
    assert.equal(sanitizeRunTimestamp(undefined as unknown as string), '')
    assert.equal(sanitizeRunTimestamp(null as unknown as string), '')
  })
})

describe('getRunDataDir', () => {
  it('joins baseDir + "runs" + sanitized name-timestamp segment', () => {
    const dir = getRunDataDir('pipeline_mcp_data', 'fix-quality-gate', '2026-04-10T09:13:58Z')
    assert.equal(dir, join('pipeline_mcp_data', 'runs', 'fix-quality-gate-2026-04-10T09-13-58Z'))
  })

  it('always sits under {baseDir}/runs/', () => {
    const dir = getRunDataDir('pipeline_mcp_data', 'My Run', '2026-04-10T09:13:58Z')
    assert.ok(dir.includes(join('pipeline_mcp_data', 'runs')))
  })

  it('sanitizes the runName component (lowercases, replaces unsafe chars)', () => {
    const dir = getRunDataDir('base', 'Fix:Quality/Gate*Mismatch', '2026-04-10T09:13:58Z')
    assert.ok(dir.endsWith(join('runs', 'fix-quality-gate-mismatch-2026-04-10T09-13-58Z')))
  })

  it('omits the timestamp suffix when timestamp is empty', () => {
    const dir = getRunDataDir('base', 'fix-bug', '')
    assert.equal(dir, join('base', 'runs', 'fix-bug'))
  })

  it('falls back to "unnamed-run" when runName sanitizes to empty', () => {
    const dir = getRunDataDir('base', '!!!', '2026-04-10T09:13:58Z')
    assert.equal(dir, join('base', 'runs', 'unnamed-run-2026-04-10T09-13-58Z'))
  })

  it('produces no Windows-unsafe characters in the final dir name', () => {
    const dir = getRunDataDir('base', 'a/b\\c:d', '2026-04-10T09:13:58.123Z')
    // Strip the join separators (which are platform-native) before checking.
    const dirName = dir.split(/[\\/]/).pop() ?? ''
    for (const ch of [':', '.', '*', '?', '"', '<', '>', '|']) {
      assert.ok(!dirName.includes(ch), `unsafe char ${ch} should not appear in dir name`)
    }
  })
})

describe('getArtifactDir', () => {
  const runDataDir = join('pipeline_mcp_data', 'runs', 'my-run-2026-04-10T09-13-58Z')

  it('joins runDataDir with the curated collections subtype', () => {
    assert.equal(
      getArtifactDir(runDataDir, 'collections/curated'),
      join(runDataDir, 'collections', 'curated'),
    )
  })

  it('joins runDataDir with the raw collections subtype', () => {
    assert.equal(
      getArtifactDir(runDataDir, 'collections/raw'),
      join(runDataDir, 'collections', 'raw'),
    )
  })

  it('joins runDataDir with the debates subtype', () => {
    assert.equal(getArtifactDir(runDataDir, 'debates'), join(runDataDir, 'debates'))
  })

  it('joins runDataDir with the overviews subtype', () => {
    assert.equal(getArtifactDir(runDataDir, 'overviews'), join(runDataDir, 'overviews'))
  })

  it('joins runDataDir with the specs subtype', () => {
    assert.equal(getArtifactDir(runDataDir, 'specs'), join(runDataDir, 'specs'))
  })

  it('joins runDataDir with the scaffold subtype', () => {
    assert.equal(getArtifactDir(runDataDir, 'scaffold'), join(runDataDir, 'scaffold'))
  })

  it('uses platform-native separators on the runDataDir prefix', () => {
    // The leading runDataDir is platform-joined; we should not see a stray
    // POSIX slash injected by the helper itself.
    const dir = getArtifactDir(runDataDir, 'overviews')
    assert.ok(dir.startsWith(runDataDir))
  })
})
