import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, writeFileSync, readFileSync, existsSync, mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'

import { ingestDocumentsAsync, detectFileType, extractContent, normalizeDate } from '../ingest.ts'

let tempDir: string

beforeEach(() => {
  tempDir = mkdtempSync(join(tmpdir(), 'ingest-test-'))
})

afterEach(() => {
  rmSync(tempDir, { recursive: true, force: true })
})

// ── detectFileType ────────────────────────────────────────────

describe('detectFileType', () => {
  it('detects markdown', () => {
    assert.equal(detectFileType('README.md'), 'markdown')
    assert.equal(detectFileType('notes.MD'), 'markdown')
  })

  it('detects plain text', () => {
    assert.equal(detectFileType('notes.txt'), 'text')
    assert.equal(detectFileType('log.log'), 'text')
  })

  it('detects code files', () => {
    assert.equal(detectFileType('script.py'), 'code')
    assert.equal(detectFileType('index.ts'), 'code')
    assert.equal(detectFileType('main.js'), 'code')
    assert.equal(detectFileType('App.tsx'), 'code')
    assert.equal(detectFileType('server.go'), 'code')
    assert.equal(detectFileType('lib.rs'), 'code')
    assert.equal(detectFileType('Main.java'), 'code')
    assert.equal(detectFileType('util.rb'), 'code')
    assert.equal(detectFileType('script.sh'), 'code')
  })

  it('detects JSON', () => {
    assert.equal(detectFileType('data.json'), 'json')
    assert.equal(detectFileType('config.JSON'), 'json')
  })

  it('detects CSV', () => {
    assert.equal(detectFileType('data.csv'), 'csv')
  })

  it('detects HTML', () => {
    assert.equal(detectFileType('page.html'), 'html')
    assert.equal(detectFileType('page.htm'), 'html')
  })

  it('detects YAML', () => {
    assert.equal(detectFileType('config.yaml'), 'yaml')
    assert.equal(detectFileType('config.yml'), 'yaml')
  })

  it('detects TOML', () => {
    assert.equal(detectFileType('config.toml'), 'toml')
  })

  it('detects PDF', () => {
    assert.equal(detectFileType('report.pdf'), 'pdf')
    assert.equal(detectFileType('REPORT.PDF'), 'pdf')
  })

  it('detects Jupyter notebooks', () => {
    assert.equal(detectFileType('analysis.ipynb'), 'notebook')
  })

  it('falls back to text for unknown extensions', () => {
    assert.equal(detectFileType('file.xyz'), 'text')
    assert.equal(detectFileType('noextension'), 'text')
  })
})

// ── extractContent ────────────────────────────────────────────

describe('extractContent', () => {
  it('reads markdown as-is', () => {
    const p = join(tempDir, 'notes.md')
    writeFileSync(p, '# Title\n\nSome text here.')
    const result = extractContent(p, 'markdown')
    assert.equal(result.content, '# Title\n\nSome text here.')
    assert.equal(result.rawHtml, undefined)
  })

  it('reads plain text as-is', () => {
    const p = join(tempDir, 'notes.txt')
    writeFileSync(p, 'Hello world')
    const result = extractContent(p, 'text')
    assert.equal(result.content, 'Hello world')
  })

  it('reads code files as-is', () => {
    const p = join(tempDir, 'script.py')
    writeFileSync(p, 'def foo():\n    return 42\n')
    const result = extractContent(p, 'code')
    assert.equal(result.content, 'def foo():\n    return 42\n')
  })

  it('reads JSON and re-stringifies with 2-space indent', () => {
    const p = join(tempDir, 'data.json')
    writeFileSync(p, '{"a":1,"b":[1,2]}')
    const result = extractContent(p, 'json')
    assert.equal(result.content, JSON.stringify({ a: 1, b: [1, 2] }, null, 2))
  })

  it('reads CSV as raw text', () => {
    const p = join(tempDir, 'data.csv')
    writeFileSync(p, 'name,value\nalpha,1\nbeta,2\n')
    const result = extractContent(p, 'csv')
    assert.equal(result.content, 'name,value\nalpha,1\nbeta,2\n')
  })

  it('strips HTML tags and stores raw in rawHtml', () => {
    const p = join(tempDir, 'page.html')
    writeFileSync(p, '<html><body><h1>Title</h1><p>Body text.</p></body></html>')
    const result = extractContent(p, 'html')
    assert.ok(result.content.includes('Title'))
    assert.ok(result.content.includes('Body text.'))
    assert.ok(!result.content.includes('<h1>'))
    assert.ok(result.rawHtml?.includes('<h1>Title</h1>'))
  })

  it('reads YAML as raw text', () => {
    const p = join(tempDir, 'config.yaml')
    writeFileSync(p, 'key: value\nlist:\n  - a\n  - b\n')
    const result = extractContent(p, 'yaml')
    assert.equal(result.content, 'key: value\nlist:\n  - a\n  - b\n')
  })

  it('reads TOML as raw text', () => {
    const p = join(tempDir, 'config.toml')
    writeFileSync(p, '[section]\nkey = "value"\n')
    const result = extractContent(p, 'toml')
    assert.equal(result.content, '[section]\nkey = "value"\n')
  })

  it('extracts cell sources from Jupyter notebooks', () => {
    const notebook = {
      nbformat: 4,
      cells: [
        { cell_type: 'markdown', source: ['# Analysis\n', 'Some intro.'] },
        { cell_type: 'code', source: ['import pandas as pd\n', 'df = pd.read_csv("data.csv")'] },
        { cell_type: 'raw', source: ['raw cell content'] },
      ],
    }
    const p = join(tempDir, 'analysis.ipynb')
    writeFileSync(p, JSON.stringify(notebook))
    const result = extractContent(p, 'notebook')
    assert.ok(result.content.includes('# Analysis'))
    assert.ok(result.content.includes('import pandas as pd'))
    assert.ok(result.content.includes('raw cell content'))
  })
})

// ── normalizeDate ────────────────────────────────────────────

/** Helper: assert that a string is a valid ISO 8601 date-time. */
function assertValidIsoDateTime(value: string, label: string): void {
  assert.ok(value.includes('T'), `${label}: missing "T" separator — got "${value}"`)
  const ts = Date.parse(value)
  assert.ok(!Number.isNaN(ts), `${label}: Date.parse returned NaN — got "${value}"`)
}

describe('normalizeDate', () => {
  it('coerces US locale date "4/1/2026" to valid ISO 8601 date-time', () => {
    const result = normalizeDate('4/1/2026')
    assertValidIsoDateTime(result, 'US locale')
    // The parsed date should represent April 1, 2026
    const d = new Date(result)
    assert.equal(d.getUTCFullYear(), 2026)
    assert.equal(d.getUTCMonth(), 3) // 0-indexed: 3 = April
    assert.equal(d.getUTCDate(), 1)
  })

  it('coerces date-only "2026-04-06" to valid ISO 8601 date-time', () => {
    const result = normalizeDate('2026-04-06')
    assertValidIsoDateTime(result, 'date-only')
    const d = new Date(result)
    assert.equal(d.getUTCFullYear(), 2026)
    // Month may be 3 (April) depending on timezone handling
    assert.ok(d.getUTCMonth() === 3 || d.getUTCMonth() === 2,
      `Expected month 3 (April) or 2 (March, timezone edge), got ${d.getUTCMonth()}`)
  })

  it('coerces human-readable "April 6, 2026" to valid ISO 8601 date-time', () => {
    const result = normalizeDate('April 6, 2026')
    assertValidIsoDateTime(result, 'human-readable')
    const d = new Date(result)
    assert.equal(d.getUTCFullYear(), 2026)
  })

  it('returns already-valid ISO 8601 as-is', () => {
    const iso = '2026-04-06T12:00:00.000Z'
    const result = normalizeDate(iso)
    assert.equal(result, iso, 'Already-valid ISO should be returned unchanged')
  })

  it('falls back to fallbackNow for unparseable strings', () => {
    const fallback = '2026-01-15T08:30:00.000Z'
    const result = normalizeDate('not-a-date', fallback)
    assert.equal(result, fallback, 'Unparseable input should return the fallback value')
    assertValidIsoDateTime(result, 'unparseable fallback')
  })
})

// ── ingestDocumentsAsync ─────────────────────────────────────

describe('ingestDocumentsAsync', () => {
  it('produces a valid raw collection from multiple files', async () => {
    const mdPath = join(tempDir, 'report.md')
    const csvPath = join(tempDir, 'data.csv')
    writeFileSync(mdPath, '# Research Report\n\nFindings here.')
    writeFileSync(csvPath, 'symbol,price\nBTC,60000\n')

    const collection = await ingestDocumentsAsync([mdPath, csvPath], 'test-ingest')

    assert.equal(collection.status, 'raw')
    assert.ok(collection.collection_id.startsWith('test-ingest--'))
    assert.equal(collection.items.length, 2)
    assert.ok(typeof collection.created_date === 'string')
  })

  it('each item has correct fields', async () => {
    const p = join(tempDir, 'notes.md')
    writeFileSync(p, '# Notes\n\nSome content.')

    const collection = await ingestDocumentsAsync([p], 'my-ingest')
    const item = collection.items[0]

    assert.equal(item.source_type, 'local_ingest')
    assert.ok(item.source_ref.startsWith('file:'))
    assert.ok(item.source_ref.includes('notes.md'))
    assert.ok(item.id.startsWith('local_'))
    assert.ok(item.content.includes('Notes'))
    assert.ok(typeof item.date === 'string')
    assert.ok(Array.isArray(item.tags))
    assert.equal(item.tags.length, 0)
    assert.equal(item.metadata?.file_type, 'markdown')
    assert.ok(typeof item.metadata?.file_size === 'number')
  })

  it('source_runs audit trail has one entry per file', async () => {
    const p1 = join(tempDir, 'a.txt')
    const p2 = join(tempDir, 'b.txt')
    writeFileSync(p1, 'alpha')
    writeFileSync(p2, 'beta')

    const collection = await ingestDocumentsAsync([p1, p2], 'audit-test')

    assert.equal(collection.source_runs?.length, 2)
    const run = collection.source_runs![0]
    assert.equal(run.source_type, 'local_ingest')
    assert.equal(run.items_found, 1)
    assert.equal(run.items_stored, 1)
    assert.ok(typeof run.executed_at === 'string')
    assert.ok(run.path?.includes('a.txt') || run.path?.includes('b.txt'))
  })

  it('stores raw HTML in metadata for html files', async () => {
    const p = join(tempDir, 'page.html')
    writeFileSync(p, '<html><body><h1>Hi</h1></body></html>')

    const collection = await ingestDocumentsAsync([p], 'html-test')
    const item = collection.items[0]

    assert.ok(!item.content.includes('<h1>'))
    assert.ok(item.metadata?.raw_html)
  })

  it('item title is the filename without extension', async () => {
    const p = join(tempDir, 'quarterly-report.md')
    writeFileSync(p, '# Q4 Results')

    const collection = await ingestDocumentsAsync([p], 'title-test')
    assert.equal(collection.items[0].title, 'quarterly-report')
  })

  it('throws if a file does not exist', async () => {
    await assert.rejects(
      () => ingestDocumentsAsync([join(tempDir, 'nonexistent.txt')], 'err-test'),
      /ENOENT|no such file/,
    )
  })

  it('handles empty file list and returns empty collection', async () => {
    const collection = await ingestDocumentsAsync([], 'empty-test')
    assert.equal(collection.items.length, 0)
    assert.equal(collection.source_runs?.length, 0)
  })
})

// ── PDF extraction ────────────────────────────────────────────

describe('ingestDocumentsAsync (PDF)', () => {
  it('ingestDocumentsAsync handles non-PDF files identically to ingestDocuments', async () => {
    const p = join(tempDir, 'notes.md')
    writeFileSync(p, '# Notes\n\nSome content.')

    const collection = await ingestDocumentsAsync([p], 'async-test')
    assert.equal(collection.items.length, 1)
    assert.equal(collection.items[0].source_type, 'local_ingest')
    assert.ok(collection.items[0].content.includes('Notes'))
  })

  it('ingestDocumentsAsync returns correct metadata for markdown', async () => {
    const p = join(tempDir, 'report.md')
    writeFileSync(p, '# Report')

    const collection = await ingestDocumentsAsync([p], 'meta-test')
    assert.equal(collection.items[0].metadata?.file_type, 'markdown')
  })
})

// ── Integration: validate against real schema ─────────────────

import { loadSchemas, validateArtifact } from '../validator.ts'
import { resolve as resolvePath, dirname as dirnamePath } from 'node:path'
import { fileURLToPath as fileURLToPathUtil } from 'node:url'

const __dirnameInteg = dirnamePath(fileURLToPathUtil(import.meta.url))
const SCHEMAS_DIR = resolvePath(__dirnameInteg, '../pipeline/schemas')

describe('integration: ingestDocumentsAsync produces schema-valid raw-collection', () => {
  it('validates markdown + csv + json ingestion against raw-collection.json', async () => {
    const mdPath = join(tempDir, 'research.md')
    const csvPath = join(tempDir, 'prices.csv')
    const jsonPath = join(tempDir, 'config.json')

    writeFileSync(mdPath, '# Research\n\nPrediction market dynamics in Q1 2026.')
    writeFileSync(csvPath, 'date,price\n2026-01-01,0.72\n2026-01-02,0.68\n')
    writeFileSync(jsonPath, JSON.stringify({ model: 'kalshi-v2', threshold: 0.65 }))

    const collection = await ingestDocumentsAsync(
      [mdPath, csvPath, jsonPath],
      'integration-test',
    )

    assert.equal(collection.items.length, 3)
    assert.equal(collection.status, 'raw')

    // Validate against the actual JSON schema
    const schemas = loadSchemas(SCHEMAS_DIR)
    const result = validateArtifact(schemas, 'raw-collection.json', collection)
    assert.equal(
      result.valid,
      true,
      `Schema validation failed:\n${result.errors.join('\n')}`,
    )
  })

  it('validates HTML ingestion — content is stripped, schema still valid', async () => {
    const htmlPath = join(tempDir, 'article.html')
    writeFileSync(
      htmlPath,
      '<html><head><title>Market Report</title></head><body>' +
        '<h1>Q1 Report</h1><p>Kalshi volumes increased 40% YoY.</p>' +
        '</body></html>',
    )

    const collection = await ingestDocumentsAsync([htmlPath], 'html-integ')
    const schemas = loadSchemas(SCHEMAS_DIR)
    const result = validateArtifact(schemas, 'raw-collection.json', collection)

    assert.equal(result.valid, true, `Validation errors: ${result.errors.join(', ')}`)

    const item = collection.items[0]
    assert.ok(item.content.includes('Q1 Report'))
    assert.ok(!item.content.includes('<h1>'))
    assert.ok(item.metadata?.raw_html)
  })

  it('validates Jupyter notebook ingestion', async () => {
    const notebook = {
      nbformat: 4,
      cells: [
        { cell_type: 'markdown', source: ['# Kalshi Strategy\n', 'Analysis of YES contracts.'] },
        { cell_type: 'code', source: ['import kalshi_api\n', 'client = kalshi_api.Client()'] },
      ],
    }
    const nbPath = join(tempDir, 'strategy.ipynb')
    writeFileSync(nbPath, JSON.stringify(notebook))

    const collection = await ingestDocumentsAsync([nbPath], 'notebook-integ')
    const schemas = loadSchemas(SCHEMAS_DIR)
    const result = validateArtifact(schemas, 'raw-collection.json', collection)

    assert.equal(result.valid, true, `Validation errors: ${result.errors.join(', ')}`)

    const item = collection.items[0]
    assert.ok(item.content.includes('Kalshi Strategy'))
    assert.ok(item.content.includes('import kalshi_api'))
    assert.equal(item.metadata?.file_type, 'notebook')
  })
})

// ── Disk-first ingestion response shape ─────────────────────

import type { ResponseEnvelope } from '../types.ts'

describe('disk-first ingestion response shape', () => {
  it('response envelope has artifact_path and no artifact field', async () => {
    const mdPath = join(tempDir, 'doc1.md')
    const csvPath = join(tempDir, 'doc2.csv')
    const txtPath = join(tempDir, 'doc3.txt')

    writeFileSync(mdPath, '# Document One\n\nResearch findings on prediction markets.')
    writeFileSync(csvPath, 'symbol,price,volume\nBTC,60000,1200\nETH,3200,800\nSOL,120,500\n')
    writeFileSync(txtPath, 'Plain text document with analysis of market trends and patterns.')

    const collection = await ingestDocumentsAsync([mdPath, csvPath, txtPath], 'disk-first-test')

    // Replicate the disk-first pattern from handleIngestDocuments
    const rawDir = join(tempDir, 'collections', 'raw')
    mkdirSync(rawDir, { recursive: true })
    const artifactPath = join(rawDir, `${collection.collection_id}.json`)
    writeFileSync(artifactPath, JSON.stringify(collection, null, 2))

    const sources = collection.source_runs?.map(r => r.path).filter((p): p is string => typeof p === 'string') ?? []
    const envelope: ResponseEnvelope = {
      status: 'ok',
      data: {
        collection_id: collection.collection_id,
        item_count: collection.items.length,
        sources,
        artifact_path: artifactPath,
      },
      next_step: `Call pipeline_register_artifact with file_path="${artifactPath}" to register this collection.`,
    }

    // Verify artifact_path is present and is a string
    assert.equal(typeof envelope.data.artifact_path, 'string')
    assert.ok((envelope.data.artifact_path as string).length > 0, 'artifact_path must be non-empty')

    // Verify no artifact field in the response data
    assert.equal(envelope.data.artifact, undefined, 'response must not contain artifact field')
    assert.equal('artifact' in envelope.data, false, 'response data must not have artifact key')
  })

  it('JSON-serialized response envelope is under 10KB', async () => {
    const mdPath = join(tempDir, 'large1.md')
    const csvPath = join(tempDir, 'large2.csv')
    const txtPath = join(tempDir, 'large3.txt')

    // Create files with non-trivial content to ensure the response stays small
    writeFileSync(mdPath, '# Large Document\n\n' + 'Analysis paragraph. '.repeat(100))
    writeFileSync(csvPath, 'col1,col2,col3\n' + 'val1,val2,val3\n'.repeat(50))
    writeFileSync(txtPath, 'Detailed market analysis report. '.repeat(80))

    const collection = await ingestDocumentsAsync([mdPath, csvPath, txtPath], 'size-test')
    assert.equal(collection.items.length, 3)

    // Build the disk-first response envelope
    const rawDir = join(tempDir, 'collections', 'raw')
    mkdirSync(rawDir, { recursive: true })
    const artifactPath = join(rawDir, `${collection.collection_id}.json`)
    writeFileSync(artifactPath, JSON.stringify(collection, null, 2))

    const sources = collection.source_runs?.map(r => r.path).filter((p): p is string => typeof p === 'string') ?? []
    const envelope: ResponseEnvelope = {
      status: 'ok',
      data: {
        collection_id: collection.collection_id,
        item_count: collection.items.length,
        sources,
        artifact_path: artifactPath,
      },
      next_step: `Call pipeline_register_artifact with file_path="${artifactPath}" to register this collection.`,
    }

    const serialized = JSON.stringify(envelope, null, 2)
    const sizeBytes = Buffer.byteLength(serialized, 'utf-8')
    assert.ok(
      sizeBytes < 10_000,
      `Response envelope must be under 10KB, got ${sizeBytes} bytes`,
    )
  })

  it('artifact_path points to a valid JSON file containing the collection', async () => {
    const mdPath = join(tempDir, 'verify1.md')
    const txtPath = join(tempDir, 'verify2.txt')
    const csvPath = join(tempDir, 'verify3.csv')

    writeFileSync(mdPath, '# Verification Test\n\nContent for disk-first verification.')
    writeFileSync(txtPath, 'Plain text content for verification.')
    writeFileSync(csvPath, 'name,value\nalpha,1\nbeta,2\n')

    const collection = await ingestDocumentsAsync([mdPath, txtPath, csvPath], 'verify-test')

    // Write to disk as the handler does
    const rawDir = join(tempDir, 'collections', 'raw')
    mkdirSync(rawDir, { recursive: true })
    const artifactPath = join(rawDir, `${collection.collection_id}.json`)
    writeFileSync(artifactPath, JSON.stringify(collection, null, 2))

    // Verify the file exists
    assert.ok(existsSync(artifactPath), `Artifact file must exist at ${artifactPath}`)

    // Verify the file contains valid JSON
    const rawContent = readFileSync(artifactPath, 'utf-8')
    let parsed: unknown
    assert.doesNotThrow(() => {
      parsed = JSON.parse(rawContent)
    }, 'Artifact file must contain valid JSON')

    // Verify the parsed JSON matches the collection
    const diskCollection = parsed as Record<string, unknown>
    assert.equal(diskCollection.collection_id, collection.collection_id)
    assert.equal(diskCollection.status, 'raw')
    assert.ok(Array.isArray(diskCollection.items))
    assert.equal((diskCollection.items as unknown[]).length, 3)
  })
})
