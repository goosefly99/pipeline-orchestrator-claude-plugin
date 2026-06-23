# Document Ingestion & Source Discovery — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `pipeline_ingest_documents` tool to the pipeline-mcp server. Given a list of local file paths, it reads each file, extracts text content by type, and produces a `raw-collection` JSON conforming to the raw-collection schema. Supported types: Markdown, plain text, code files, JSON, CSV, HTML, YAML, TOML, PDF, and Jupyter notebooks (.ipynb). The collection can then be stored and used as input to subsequent pipeline phases exactly like a collection produced by `research_discovery`.

**Architecture:** A new `ingest.ts` module handles all file reading, type detection, and content extraction. It exports a single `ingestDocuments` function that takes a list of file paths and returns a valid `RawCollection` object. `server.ts` receives one new tool handler (`pipeline_ingest_documents`) that calls `ingestDocuments` and optionally stores the result. PDF extraction uses the `pdf-parse` npm package; all other formats use built-in Node.js APIs.

**Tech Stack:** Node.js v24 (native TypeScript), `pdf-parse` (PDF text extraction), `node:fs`, `node:path`, `node:crypto` (UUID for collection IDs), `node:test` (testing). No other new dependencies.

**For agentic workers:** Read `ingest.ts` and `server.ts` before each edit. Never trust memory of file contents. Run `node --test tests/ingest.test.ts` after each implementation step and fix all failures before proceeding.

---

## File Structure

```
pipeline-mcp/
  ingest.ts               — Document ingestion: detect type, extract content, produce RawCollection
  tests/ingest.test.ts    — Unit and integration tests for document ingestion
  server.ts               — (modified) add pipeline_ingest_documents tool
  package.json            — (modified) add pdf-parse dependency
```

---

### Task 1: Create ingest.ts (all types except PDF)

**Files:**
- Create: `pipeline-mcp/tests/ingest.test.ts`
- Create: `pipeline-mcp/ingest.ts`

The test file uses real fixture files written inline via `writeFileSync` into a temp directory so there are no fixture files to manage.

- [ ] **Step 1: Write failing tests**

`pipeline-mcp/tests/ingest.test.ts`:

```typescript
import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, writeFileSync, mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'

import { ingestDocuments, detectFileType, extractContent } from '../ingest.ts'

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

// ── ingestDocuments ───────────────────────────────────────────

describe('ingestDocuments', () => {
  it('produces a valid raw collection from multiple files', () => {
    const mdPath = join(tempDir, 'report.md')
    const csvPath = join(tempDir, 'data.csv')
    writeFileSync(mdPath, '# Research Report\n\nFindings here.')
    writeFileSync(csvPath, 'symbol,price\nBTC,60000\n')

    const collection = ingestDocuments([mdPath, csvPath], 'test-ingest')

    assert.equal(collection.status, 'raw')
    assert.ok(collection.collection_id.startsWith('test-ingest--'))
    assert.equal(collection.items.length, 2)
    assert.ok(typeof collection.created_date === 'string')
  })

  it('each item has correct fields', () => {
    const p = join(tempDir, 'notes.md')
    writeFileSync(p, '# Notes\n\nSome content.')

    const collection = ingestDocuments([p], 'my-ingest')
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

  it('source_runs audit trail has one entry per file', () => {
    const p1 = join(tempDir, 'a.txt')
    const p2 = join(tempDir, 'b.txt')
    writeFileSync(p1, 'alpha')
    writeFileSync(p2, 'beta')

    const collection = ingestDocuments([p1, p2], 'audit-test')

    assert.equal(collection.source_runs?.length, 2)
    const run = collection.source_runs![0]
    assert.equal(run.source_type, 'local_ingest')
    assert.equal(run.items_found, 1)
    assert.equal(run.items_stored, 1)
    assert.ok(typeof run.executed_at === 'string')
    assert.ok(run.path?.includes('a.txt') || run.path?.includes('b.txt'))
  })

  it('stores raw HTML in metadata for html files', () => {
    const p = join(tempDir, 'page.html')
    writeFileSync(p, '<html><body><h1>Hi</h1></body></html>')

    const collection = ingestDocuments([p], 'html-test')
    const item = collection.items[0]

    assert.ok(!item.content.includes('<h1>'))
    assert.ok(item.metadata?.raw_html)
  })

  it('item title is the filename without extension', () => {
    const p = join(tempDir, 'quarterly-report.md')
    writeFileSync(p, '# Q4 Results')

    const collection = ingestDocuments([p], 'title-test')
    assert.equal(collection.items[0].title, 'quarterly-report')
  })

  it('throws if a file does not exist', () => {
    assert.throws(
      () => ingestDocuments([join(tempDir, 'nonexistent.txt')], 'err-test'),
      /ENOENT|no such file/,
    )
  })

  it('handles empty file list and returns empty collection', () => {
    const collection = ingestDocuments([], 'empty-test')
    assert.equal(collection.items.length, 0)
    assert.equal(collection.source_runs?.length, 0)
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && node --test tests/ingest.test.ts`
Expected: FAIL — `ingest.ts` does not exist

- [ ] **Step 3: Implement ingest.ts (no PDF yet)**

`pipeline-mcp/ingest.ts`:

```typescript
import { readFileSync, statSync } from 'node:fs'
import { extname, basename } from 'node:path'
import { randomBytes } from 'node:crypto'

// ── Types matching raw-collection.json schema ─────────────────

export interface SourceRun {
  source_type: string
  query?: string
  url?: string
  path?: string
  executed_at: string
  items_found: number
  items_stored: number
}

export interface RawItem {
  id: string
  source_type: string
  source_ref: string
  title: string
  content: string
  url: string
  date: string
  tags: string[]
  metadata: Record<string, unknown>
}

export interface RawCollection {
  collection_id: string
  manifest_id?: string
  created_date: string
  status: 'raw'
  source_runs: SourceRun[]
  items: RawItem[]
}

// ── File type detection ───────────────────────────────────────

export type FileType =
  | 'markdown'
  | 'text'
  | 'code'
  | 'json'
  | 'csv'
  | 'html'
  | 'yaml'
  | 'toml'
  | 'pdf'
  | 'notebook'

const EXT_MAP: Record<string, FileType> = {
  '.md': 'markdown',
  '.markdown': 'markdown',
  '.txt': 'text',
  '.log': 'text',
  '.py': 'code',
  '.ts': 'code',
  '.tsx': 'code',
  '.js': 'code',
  '.jsx': 'code',
  '.go': 'code',
  '.rs': 'code',
  '.java': 'code',
  '.rb': 'code',
  '.sh': 'code',
  '.bash': 'code',
  '.zsh': 'code',
  '.c': 'code',
  '.cpp': 'code',
  '.h': 'code',
  '.cs': 'code',
  '.swift': 'code',
  '.kt': 'code',
  '.scala': 'code',
  '.r': 'code',
  '.json': 'json',
  '.csv': 'csv',
  '.html': 'html',
  '.htm': 'html',
  '.yaml': 'yaml',
  '.yml': 'yaml',
  '.toml': 'toml',
  '.pdf': 'pdf',
  '.ipynb': 'notebook',
}

export function detectFileType(filePath: string): FileType {
  const ext = extname(filePath).toLowerCase()
  return EXT_MAP[ext] ?? 'text'
}

// ── Content extraction ────────────────────────────────────────

export interface ExtractResult {
  content: string
  rawHtml?: string
}

function stripHtmlTags(html: string): string {
  // Remove script and style blocks including their content
  let text = html.replace(/<script[\s\S]*?<\/script>/gi, ' ')
  text = text.replace(/<style[\s\S]*?<\/style>/gi, ' ')
  // Replace block-level tags with newlines for readability
  text = text.replace(/<(br|p|div|h[1-6]|li|tr|blockquote)[^>]*>/gi, '\n')
  // Strip all remaining tags
  text = text.replace(/<[^>]+>/g, '')
  // Decode common HTML entities
  text = text
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&nbsp;/g, ' ')
  // Collapse whitespace
  return text.replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim()
}

export function extractContent(filePath: string, fileType: FileType): ExtractResult {
  if (fileType === 'pdf') {
    // PDF extraction is handled by extractPdfContent (async, in ingestDocuments)
    // This synchronous path is a fallback — callers should use ingestDocuments for PDF
    throw new Error('PDF extraction must be done via ingestDocuments (async). Call extractContent only for non-PDF types.')
  }

  const raw = readFileSync(filePath, 'utf-8')

  switch (fileType) {
    case 'markdown':
    case 'text':
    case 'code':
    case 'csv':
    case 'yaml':
    case 'toml':
      return { content: raw }

    case 'json': {
      const parsed = JSON.parse(raw)
      return { content: JSON.stringify(parsed, null, 2) }
    }

    case 'html':
      return { content: stripHtmlTags(raw), rawHtml: raw }

    case 'notebook': {
      const nb = JSON.parse(raw) as {
        cells: Array<{ cell_type: string; source: string | string[] }>
      }
      const sections = nb.cells.map(cell => {
        const src = Array.isArray(cell.source)
          ? cell.source.join('')
          : cell.source
        const label = cell.cell_type === 'code' ? `[${cell.cell_type}]\n` : ''
        return `${label}${src}`
      })
      return { content: sections.join('\n\n') }
    }

    default: {
      // exhaustive fallback — should not be reached
      const _: never = fileType
      return { content: raw }
    }
  }
}

// ── Item ID generation ────────────────────────────────────────

function shortId(): string {
  return randomBytes(4).toString('hex')
}

function collectionId(name: string): string {
  return `${name}--${randomBytes(4).toString('hex')}`
}

// ── Main export ───────────────────────────────────────────────

/**
 * Ingest a list of local file paths and produce a raw-collection artifact.
 *
 * PDF files are extracted synchronously using pdf-parse if it is installed.
 * All other types use built-in Node.js APIs.
 *
 * @param filePaths  Absolute or relative paths to the files to ingest.
 * @param ingestName Name prefix for the collection_id (e.g. "strategy-docs").
 */
export function ingestDocuments(filePaths: string[], ingestName: string): RawCollection {
  const now = new Date().toISOString()
  const items: RawItem[] = []
  const sourceRuns: SourceRun[] = []

  for (const filePath of filePaths) {
    const executedAt = new Date().toISOString()
    const fileType = detectFileType(filePath)
    const stat = statSync(filePath) // throws ENOENT if missing — intentional
    const fileSize = stat.size

    let extracted: ExtractResult
    if (fileType === 'pdf') {
      extracted = extractPdfContentSync(filePath)
    } else {
      extracted = extractContent(filePath, fileType)
    }

    const fileBase = basename(filePath)
    const titleBase = fileBase.includes('.')
      ? fileBase.slice(0, fileBase.lastIndexOf('.'))
      : fileBase

    const metadata: Record<string, unknown> = {
      file_type: fileType,
      file_size: fileSize,
    }
    if (extracted.rawHtml !== undefined) {
      metadata.raw_html = extracted.rawHtml
    }

    const item: RawItem = {
      id: `local_${titleBase}_${shortId()}`,
      source_type: 'local_ingest',
      source_ref: `file:${filePath}`,
      title: titleBase,
      content: extracted.content,
      url: '',
      date: now,
      tags: [],
      metadata,
    }

    items.push(item)

    sourceRuns.push({
      source_type: 'local_ingest',
      path: filePath,
      executed_at: executedAt,
      items_found: 1,
      items_stored: 1,
    })
  }

  return {
    collection_id: collectionId(ingestName),
    created_date: now,
    status: 'raw',
    source_runs: sourceRuns,
    items,
  }
}

// ── PDF extraction (synchronous wrapper) ─────────────────────

/**
 * Extract text from a PDF using pdf-parse.
 * pdf-parse is an optional peer dependency. If not installed, throws a clear error.
 */
function extractPdfContentSync(filePath: string): ExtractResult {
  // pdf-parse is async; we use a sync workaround via execFileSync to avoid
  // making ingestDocuments async (which would require changes across the codebase).
  // Instead we use a small inline worker pattern: read the file buffer and call
  // pdf-parse's internal text extraction synchronously.
  //
  // In practice, pdf-parse exposes an async API; we resolve this via dynamic
  // require at call time and use Atomics + SharedArrayBuffer for sync resolution
  // (Node.js v24+). For most practical file sizes this is performant enough.
  //
  // NOTE: This function is implemented in Task 2 after pdf-parse is installed.
  throw new Error(
    'PDF extraction is not yet configured. Install pdf-parse: npm install pdf-parse @types/pdf-parse'
  )
}
```

- [ ] **Step 4: Run tests — all non-PDF tests should pass**

Run: `cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && node --test tests/ingest.test.ts`
Expected: All tests PASS except none should fail — detectFileType('report.pdf') and type detection for pdf works; PDF content extraction tests are not in this test file yet (added in Task 2). If any non-PDF test fails, fix before continuing.

- [ ] **Step 5: Verify TypeScript**

Run: `cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 6: Commit**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp
git add ingest.ts tests/ingest.test.ts
git commit -m "feat(pipeline-mcp): add ingest.ts — document ingestion for all non-PDF file types"
```

---

### Task 2: Add PDF Support

**Files:**
- Modify: `pipeline-mcp/package.json`
- Modify: `pipeline-mcp/ingest.ts`
- Modify: `pipeline-mcp/tests/ingest.test.ts`

pdf-parse is a CommonJS module with an async API. Node.js v24's `--experimental-vm-modules` is not required since we import it via a dynamic `import()` inside an async wrapper. However, `ingestDocuments` is synchronous. We solve this by providing a separate async entrypoint `ingestDocumentsAsync` and making the synchronous `ingestDocuments` throw a clear error for PDF files unless pdf-parse has been pre-loaded. The MCP tool handler (Task 3) uses the async version.

- [ ] **Step 1: Install pdf-parse**

Run: `cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && npm install pdf-parse && npm install --save-dev @types/pdf-parse`
Expected: `pdf-parse` appears in `node_modules/`, `package.json` dependencies updated

- [ ] **Step 2: Add PDF tests to ingest.test.ts**

Read `pipeline-mcp/tests/ingest.test.ts` first, then append the following block before the final closing of the file:

```typescript
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
```

Also add `ingestDocumentsAsync` to the import line at the top of the test file:

Change:
```typescript
import { ingestDocuments, detectFileType, extractContent } from '../ingest.ts'
```
To:
```typescript
import { ingestDocuments, ingestDocumentsAsync, detectFileType, extractContent } from '../ingest.ts'
```

- [ ] **Step 3: Run tests to verify new tests fail**

Run: `cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && node --test tests/ingest.test.ts`
Expected: FAIL — `ingestDocumentsAsync` is not exported

- [ ] **Step 4: Update ingest.ts — replace extractPdfContentSync and add ingestDocumentsAsync**

Read `pipeline-mcp/ingest.ts` first. Then apply these changes:

Replace the `extractPdfContentSync` function with:

```typescript
// ── PDF extraction ────────────────────────────────────────────

/**
 * Extract text from a PDF file using pdf-parse (async).
 * Throws a clear error if pdf-parse is not installed.
 */
async function extractPdfContent(filePath: string): Promise<ExtractResult> {
  let pdfParse: (buffer: Buffer) => Promise<{ text: string }>
  try {
    // pdf-parse is a CommonJS module; dynamic import works in ESM
    const mod = await import('pdf-parse')
    pdfParse = (mod.default ?? mod) as typeof pdfParse
  } catch {
    throw new Error(
      'pdf-parse is required for PDF ingestion. Run: npm install pdf-parse @types/pdf-parse'
    )
  }

  const buffer = readFileSync(filePath)
  const data = await pdfParse(buffer)
  return { content: data.text }
}
```

Also replace the `extractPdfContentSync` call stub (old synchronous function) entirely — remove it, and keep the existing `ingestDocuments` function but change the PDF branch to throw a helpful error:

In `ingestDocuments`, find the PDF branch:
```typescript
    if (fileType === 'pdf') {
      extracted = extractPdfContentSync(filePath)
    } else {
```

Replace with:
```typescript
    if (fileType === 'pdf') {
      throw new Error(
        `PDF file "${basename(filePath)}" requires async ingestion. Use ingestDocumentsAsync instead.`
      )
    } else {
```

Then add the following export after `ingestDocuments`:

```typescript
/**
 * Async version of ingestDocuments — supports PDF files via pdf-parse.
 * Use this from async contexts (e.g. MCP tool handlers).
 */
export async function ingestDocumentsAsync(
  filePaths: string[],
  ingestName: string,
): Promise<RawCollection> {
  const now = new Date().toISOString()
  const items: RawItem[] = []
  const sourceRuns: SourceRun[] = []

  for (const filePath of filePaths) {
    const executedAt = new Date().toISOString()
    const fileType = detectFileType(filePath)
    const stat = statSync(filePath)
    const fileSize = stat.size

    let extracted: ExtractResult
    if (fileType === 'pdf') {
      extracted = await extractPdfContent(filePath)
    } else {
      extracted = extractContent(filePath, fileType)
    }

    const fileBase = basename(filePath)
    const titleBase = fileBase.includes('.')
      ? fileBase.slice(0, fileBase.lastIndexOf('.'))
      : fileBase

    const metadata: Record<string, unknown> = {
      file_type: fileType,
      file_size: fileSize,
    }
    if (extracted.rawHtml !== undefined) {
      metadata.raw_html = extracted.rawHtml
    }

    const item: RawItem = {
      id: `local_${titleBase}_${shortId()}`,
      source_type: 'local_ingest',
      source_ref: `file:${filePath}`,
      title: titleBase,
      content: extracted.content,
      url: '',
      date: now,
      tags: [],
      metadata,
    }

    items.push(item)

    sourceRuns.push({
      source_type: 'local_ingest',
      path: filePath,
      executed_at: executedAt,
      items_found: 1,
      items_stored: 1,
    })
  }

  return {
    collection_id: collectionId(ingestName),
    created_date: now,
    status: 'raw',
    source_runs: sourceRuns,
    items,
  }
}
```

- [ ] **Step 5: Run all ingest tests — all should pass**

Run: `cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && node --test tests/ingest.test.ts`
Expected: All tests PASS

- [ ] **Step 6: Run full test suite to confirm no regressions**

Run: `cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && node --test tests/*.test.ts`
Expected: All 39 existing tests + new ingest tests PASS

- [ ] **Step 7: Verify TypeScript**

Run: `cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 8: Commit**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp
git add package.json package-lock.json ingest.ts tests/ingest.test.ts
git commit -m "feat(pipeline-mcp): add PDF support via pdf-parse and ingestDocumentsAsync"
```

---

### Task 3: Add pipeline_ingest_documents Tool to server.ts

**Files:**
- Modify: `pipeline-mcp/server.ts`

The tool accepts a list of file paths and an optional ingest name, calls `ingestDocumentsAsync`, optionally validates the result against the raw-collection schema, and returns the collection JSON. It does not automatically store the artifact — the caller uses `pipeline_store_artifact` for that, consistent with the existing pattern.

- [ ] **Step 1: Read server.ts before editing**

Read the full file: `pipeline-mcp/server.ts`

- [ ] **Step 2: Add import for ingestDocumentsAsync**

At the top of `server.ts`, after the existing import block (after the `run-state.ts` import), add:

```typescript
import { ingestDocumentsAsync } from './ingest.ts'
```

- [ ] **Step 3: Add tool definition to ListToolsRequestSchema handler**

In the `tools: [...]` array inside `server.setRequestHandler(ListToolsRequestSchema, ...)`, add the following entry after the last existing tool definition (before the closing `]`):

```typescript
    {
      name: 'pipeline_ingest_documents',
      description: [
        'Ingest local files (PDF, Markdown, text, CSV, HTML, JSON, YAML, TOML, Jupyter notebooks) into a raw-collection artifact.',
        'Each file becomes one item in the collection with extracted text content.',
        'Returns the raw-collection JSON. Use pipeline_store_artifact afterwards to persist it.',
        'Supports PDF via pdf-parse. All other types use built-in Node.js APIs.',
      ].join(' '),
      inputSchema: {
        type: 'object' as const,
        properties: {
          file_paths: {
            type: 'array',
            items: { type: 'string' },
            description: 'Absolute paths to the files to ingest.',
          },
          ingest_name: {
            type: 'string',
            description: 'Name prefix for the collection_id (e.g. "strategy-docs", "market-research"). Defaults to "ingest".',
          },
          validate: {
            type: 'boolean',
            description: 'If true, validate the produced collection against raw-collection.json schema before returning. Defaults to true.',
          },
        },
        required: ['file_paths'],
      },
    },
```

- [ ] **Step 4: Add case to CallToolRequestSchema switch**

In the `switch (req.params.name)` block, add before `default:`:

```typescript
      case 'pipeline_ingest_documents':
        return await handleIngestDocuments(args)
```

Note: The outer handler function passed to `setRequestHandler` must be `async`. Verify the existing handler signature: if it is already `async (req) => { ... }` no change is needed; if not, add `async`.

- [ ] **Step 5: Add handler function**

After the last existing `function handle...` in the file (before `const transport` or server startup), add:

```typescript
async function handleIngestDocuments(args: Record<string, unknown>) {
  const filePaths = args.file_paths as string[]
  if (!Array.isArray(filePaths) || filePaths.length === 0) {
    throw new Error('file_paths must be a non-empty array of file path strings')
  }

  const ingestName = (args.ingest_name as string | undefined) ?? 'ingest'
  const shouldValidate = (args.validate as boolean | undefined) ?? true

  const collection = await ingestDocumentsAsync(filePaths, ingestName)

  if (shouldValidate) {
    const result = validateArtifact(schemas, 'raw-collection.json', collection)
    if (!result.valid) {
      throw new Error(`Produced collection failed schema validation:\n${result.errors.join('\n')}`)
    }
  }

  return text(JSON.stringify(collection, null, 2))
}
```

- [ ] **Step 6: Re-read server.ts to confirm all edits applied correctly**

Read `pipeline-mcp/server.ts` in full. Verify:
- Import line for `ingestDocumentsAsync` is present
- Tool definition for `pipeline_ingest_documents` is in the tools array
- `case 'pipeline_ingest_documents'` is in the switch
- `handleIngestDocuments` function exists and is async
- No duplicate code, no stray edits

- [ ] **Step 7: Verify TypeScript**

Run: `cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 8: Run full test suite**

Run: `cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && node --test tests/*.test.ts`
Expected: All tests PASS (server.ts changes do not break existing tests)

- [ ] **Step 9: Commit**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp
git add server.ts
git commit -m "feat(pipeline-mcp): add pipeline_ingest_documents tool to MCP server"
```

---

### Task 4: Integration Test with Real Files

**Files:**
- Modify: `pipeline-mcp/tests/ingest.test.ts`

Add a describe block that exercises `ingestDocumentsAsync` end-to-end against real fixture content written into a temp directory, then validates the result with the actual raw-collection schema. This is the equivalent of the existing `integration.test.ts` but scoped to the ingest path.

- [ ] **Step 1: Read tests/ingest.test.ts before editing**

- [ ] **Step 2: Add integration block**

Append to `pipeline-mcp/tests/ingest.test.ts`:

```typescript
// ── Integration: validate against real schema ─────────────────

import { loadSchemas, validateArtifact } from '../validator.ts'
import { resolve as resolvePath, dirname as dirnamePath } from 'node:path'
import { fileURLToPath as fileURLToPathUtil } from 'node:url'

const __dirnameInteg = dirnamePath(fileURLToPathUtil(import.meta.url))
const SCHEMAS_DIR = resolvePath(__dirnameInteg, '../../strategies/pipeline/schemas')

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
```

- [ ] **Step 3: Run all tests including new integration block**

Run: `cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && node --test tests/*.test.ts`
Expected: All tests PASS, including the 3 new integration assertions

- [ ] **Step 4: Verify TypeScript one final time (project-wide)**

Run: `cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && npx tsc --noEmit`
Expected: No errors

- [ ] **Step 5: Commit**

```bash
cd /c/Users/olive/Documents\ai_trading\pipeline-mcp
git add tests/ingest.test.ts
git commit -m "test(pipeline-mcp): add schema-validated integration tests for document ingestion"
```

---

## Summary

After all 4 tasks complete, the pipeline-mcp server has:

| New export | Location | Purpose |
|---|---|---|
| `detectFileType` | `ingest.ts` | Maps file extension → FileType enum |
| `extractContent` | `ingest.ts` | Sync content extraction for non-PDF types |
| `ingestDocuments` | `ingest.ts` | Sync ingestion (non-PDF only; throws for PDF) |
| `ingestDocumentsAsync` | `ingest.ts` | Async ingestion including PDF via pdf-parse |
| `pipeline_ingest_documents` | `server.ts` | MCP tool: ingest files → raw-collection JSON |

The manifest auto-generation workflow (reading the collection and deriving search queries) is Claude's responsibility at runtime — the ingest module only produces the raw-collection artifact. Claude reads the collection items and calls the pipeline's existing research/discovery tools to follow up with targeted searches.
