import { readFileSync, statSync, readdirSync } from 'node:fs'
import { extname, basename, join } from 'node:path'
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

// ── Directory enumeration ─────────────────────────────────────

/**
 * Recursively walk a directory and return all files whose extension is in the
 * supported-type map (EXT_MAP). Dot-directories (e.g. `.git`, `.obsidian`) and
 * any directory named `index` or ending in `.tmp` are skipped so we never
 * inadvertently ingest the COLD vector store at `/data/vault/index`.
 *
 * Returns an empty array if `dir` contains no supported files.
 */
export function enumerateDir(dir: string): string[] {
  const results: string[] = []
  const entries = readdirSync(dir, { withFileTypes: true })
  for (const entry of entries) {
    if (entry.isDirectory()) {
      const name = entry.name
      // Skip dot-dirs, the COLD store root, and tmp dirs
      if (name.startsWith('.') || name === 'index' || name.endsWith('.tmp')) {
        continue
      }
      results.push(...enumerateDir(join(dir, name)))
    } else if (entry.isFile()) {
      const ext = extname(entry.name).toLowerCase()
      if (ext in EXT_MAP) {
        results.push(join(dir, entry.name))
      }
    }
  }
  return results
}

// ── Content extraction ────────────────────────────────────────

export interface ExtractResult {
  content: string
  rawHtml?: string
}

export function stripHtmlTags(html: string): string {
  let text = html.replace(/<script[\s\S]*?<\/script>/gi, ' ')
  text = text.replace(/<style[\s\S]*?<\/style>/gi, ' ')
  text = text.replace(/<(br|p|div|h[1-6]|li|tr|blockquote)[^>]*>/gi, '\n')
  text = text.replace(/<[^>]+>/g, '')
  text = text
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&nbsp;/g, ' ')
  return text.replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim()
}

export function extractContent(filePath: string, fileType: FileType): ExtractResult {
  if (fileType === 'pdf') {
    throw new Error(
      'PDF extraction must be done via ingestDocuments (async). Call extractContent only for non-PDF types.',
    )
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
      const parsed = JSON.parse(raw) as unknown
      return { content: JSON.stringify(parsed, null, 2) }
    }

    case 'html':
      return { content: stripHtmlTags(raw), rawHtml: raw }

    case 'notebook': {
      const nb = JSON.parse(raw) as {
        cells: Array<{ cell_type: string; source: string | string[] }>
      }
      const sections = nb.cells.map(cell => {
        const src = Array.isArray(cell.source) ? cell.source.join('') : cell.source
        const label = cell.cell_type === 'code' ? `[${cell.cell_type}]\n` : ''
        return `${label}${src}`
      })
      return { content: sections.join('\n\n') }
    }

    default: {
      const _exhaustive: never = fileType
      return { content: raw }
    }
  }
}

// ── Date normalization ───────────────────────────────────────

/**
 * Normalize a raw date string to ISO 8601 date-time format.
 *
 * Resolution order:
 *  1. If the string is already a valid ISO 8601 date-time (contains 'T'),
 *     return it as-is.
 *  2. Attempt Date.parse() — if it produces a valid timestamp, convert to
 *     ISO 8601 and log a warning noting the coercion.
 *  3. On parse failure (NaN), fall back to `new Date().toISOString()` and
 *     log a warning.
 *
 * @param raw  The raw date string from source data.
 * @param fallbackNow  Optional ISO string to use as the fallback instead of
 *                     `new Date().toISOString()`. Useful for deterministic testing.
 * @returns A valid ISO 8601 date-time string.
 */
export function normalizeDate(raw: string, fallbackNow?: string): string {
  // Fast path: already a valid ISO 8601 date-time (e.g. "2026-04-06T12:00:00.000Z")
  if (raw.includes('T')) {
    const ts = Date.parse(raw)
    if (!Number.isNaN(ts)) {
      return raw
    }
  }

  // Attempt Date.parse() coercion
  const ts = Date.parse(raw)
  if (!Number.isNaN(ts)) {
    const normalized = new Date(ts).toISOString()
    console.warn(
      `[ingest] Date coerced: "${raw}" → "${normalized}"`,
    )
    return normalized
  }

  // Unparseable — fall back to current time
  const fallback = fallbackNow ?? new Date().toISOString()
  console.warn(
    `[ingest] Date unparseable, using fallback: "${raw}" → "${fallback}"`,
  )
  return fallback
}

// ── ID generation ─────────────────────────────────────────────

function shortId(): string {
  return randomBytes(4).toString('hex')
}

function collectionId(name: string): string {
  return `${name}--${randomBytes(4).toString('hex')}`
}

// ── JSON array splitting ─────────────────────────────────────

/**
 * Detect the array of items within a parsed JSON value.
 *
 * Resolution order:
 *  1. If `jsonItemsKey` is given, use that key.
 *  2. If the top-level value is an array, use it directly.
 *  3. If the top-level object has exactly one array-typed property
 *     with more than one element, auto-detect it.
 *  4. Otherwise return null (treat the file as a single blob).
 */
function detectJsonArray(
  parsed: unknown,
  jsonItemsKey?: string,
): { items: unknown[]; key: string | null } | null {
  // 1. Caller-specified key
  if (jsonItemsKey !== undefined) {
    if (
      parsed !== null &&
      typeof parsed === 'object' &&
      !Array.isArray(parsed) &&
      jsonItemsKey in (parsed as Record<string, unknown>)
    ) {
      const value = (parsed as Record<string, unknown>)[jsonItemsKey]
      if (Array.isArray(value)) {
        return { items: value, key: jsonItemsKey }
      }
    }
    // Key specified but not found / not an array — fall through to null
    return null
  }

  // 2. Top-level array
  if (Array.isArray(parsed)) {
    return { items: parsed, key: null }
  }

  // 3. Auto-detect: single array property with >1 element
  if (parsed !== null && typeof parsed === 'object') {
    const obj = parsed as Record<string, unknown>
    const arrayKeys = Object.keys(obj).filter(
      k => Array.isArray(obj[k]) && (obj[k] as unknown[]).length > 1,
    )
    if (arrayKeys.length === 1) {
      return { items: obj[arrayKeys[0]] as unknown[], key: arrayKeys[0] }
    }
  }

  return null
}

/**
 * Derive a human-readable ID from a JSON item, falling back to an
 * index-based ID.
 */
function deriveItemId(item: unknown, index: number, fileBase: string): string {
  if (item !== null && typeof item === 'object') {
    const obj = item as Record<string, unknown>
    for (const key of ['id', 'tweet_id', '_id']) {
      if (obj[key] !== undefined && obj[key] !== null) {
        return `local_${fileBase}_${String(obj[key])}`
      }
    }
  }
  return `local_${fileBase}_${index}_${shortId()}`
}

/**
 * Derive a title from a JSON item, falling back to the first 80 chars
 * of its stringified content.
 */
function deriveItemTitle(item: unknown): string {
  if (item !== null && typeof item === 'object') {
    const obj = item as Record<string, unknown>
    for (const key of ['title', 'name', 'summary']) {
      if (typeof obj[key] === 'string' && (obj[key] as string).length > 0) {
        return obj[key] as string
      }
    }
  }
  const serialized = typeof item === 'string' ? item : JSON.stringify(item)
  return serialized.length > 80 ? serialized.slice(0, 80) + '...' : serialized
}

/**
 * Build RawItems from the individual entries of a JSON array.
 */
function splitJsonArray(
  items: unknown[],
  fileBase: string,
  filePath: string,
  fileSize: number,
  now: string,
  arrayKey: string | null,
): RawItem[] {
  return items.map((entry, idx) => {
    const obj = (entry !== null && typeof entry === 'object')
      ? entry as Record<string, unknown>
      : null

    const url = obj && typeof obj['url'] === 'string' ? obj['url'] as string : ''
    const rawDate = obj && typeof obj['date'] === 'string' ? obj['date'] as string
      : obj && typeof obj['created_at'] === 'string' ? obj['created_at'] as string
      : now
    const date = normalizeDate(rawDate)
    const author = obj && typeof obj['author'] === 'string' ? obj['author'] as string
      : obj && typeof obj['user'] === 'string' ? obj['user'] as string
      : undefined
    const tags: string[] =
      obj && Array.isArray(obj['tags']) ? (obj['tags'] as unknown[]).filter(t => typeof t === 'string') as string[]
      : obj && Array.isArray(obj['hashtags']) ? (obj['hashtags'] as unknown[]).filter(t => typeof t === 'string') as string[]
      : []

    const metadata: Record<string, unknown> = {
      file_type: 'json' as FileType,
      file_size: fileSize,
      json_split: true,
      json_array_key: arrayKey,
      item_index: idx,
    }
    if (author !== undefined) metadata.author = author

    return {
      id: deriveItemId(entry, idx, fileBase),
      source_type: 'local_ingest',
      source_ref: `file:${filePath}`,
      title: deriveItemTitle(entry),
      content: JSON.stringify(entry, null, 2),
      url,
      date,
      tags,
      metadata,
    }
  })
}

// ── PDF extraction ────────────────────────────────────────────

async function extractPdfContent(filePath: string): Promise<ExtractResult> {
  let PDFParse: new (opts: { data: Uint8Array }) => { getText(): Promise<{ text: string }> }
  try {
    const mod = await import('pdf-parse')
    PDFParse = mod.PDFParse as typeof PDFParse
  } catch {
    throw new Error(
      'pdf-parse is required for PDF ingestion. Run: npm install pdf-parse @types/pdf-parse'
    )
  }

  const buffer = readFileSync(filePath)
  const parser = new PDFParse({ data: new Uint8Array(buffer) })
  const result = await parser.getText()
  return { content: result.text }
}

export async function ingestDocumentsAsync(
  filePaths: string[],
  ingestName: string,
  jsonItemsKey?: string,
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

    // ── JSON array splitting ──
    if (fileType === 'json') {
      const parsed = JSON.parse(extracted.content) as unknown
      const detected = detectJsonArray(parsed, jsonItemsKey)
      if (detected !== null && detected.items.length > 0) {
        const splitItems = splitJsonArray(
          detected.items, titleBase, filePath, fileSize, now, detected.key,
        )
        items.push(...splitItems)
        sourceRuns.push({
          source_type: 'local_ingest',
          path: filePath,
          executed_at: executedAt,
          items_found: detected.items.length,
          items_stored: splitItems.length,
        })
        continue
      }
    }

    // ── Default: single-item ingestion ──
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
