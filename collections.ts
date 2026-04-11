import { readFileSync } from 'node:fs'
import { resolve, basename } from 'node:path'
import type { FieldMap, ResearchItem, ResearchCollection } from './cc-types.ts'

// ── In-memory store ─────────────────────────────────────────────

const store = new Map<string, ResearchCollection>()

// ── Candidate lists for auto-detection ──────────────────────────

const ID_CANDIDATES = ['id', 'tweet_id', 'post_id', 'article_id', 'item_id', 'uid', 'key']
const CONTENT_CANDIDATES = ['content', 'body', 'text', 'description', 'full_text', 'article_text']
const TITLE_CANDIDATES = ['summary', 'title', 'name', 'headline', 'subject']
const TAGS_CANDIDATES = ['tags', 'categories', 'labels', 'keywords', 'topics']
const AUTHOR_CANDIDATES = ['profile', 'author', 'user', 'creator', 'poster']
const DATE_CANDIDATES = ['date', 'created_at', 'published', 'timestamp', 'published_at', 'created']
const URL_CANDIDATES = ['url', 'link', 'href', 'source_url', 'permalink']

// ── Auto-detect field mapping ───────────────────────────────────

function findField(sample: Record<string, unknown>, candidates: string[]): string | undefined {
  for (const c of candidates) {
    if (c in sample) return c
  }
  return undefined
}

function findIdField(sample: Record<string, unknown>): string {
  const direct = findField(sample, ID_CANDIDATES)
  if (direct) return direct
  const suffixed = Object.keys(sample).find((k) => k.endsWith('_id'))
  return suffixed ?? 'id'
}

export function detectFieldMap(
  data: Record<string, unknown>,
  overrides?: Partial<FieldMap>,
): FieldMap {
  // Find the items array
  let itemsKey = 'items'
  if (!Array.isArray(data[itemsKey])) {
    const arrayKey = Object.keys(data).find((k) => Array.isArray(data[k]))
    if (arrayKey) itemsKey = arrayKey
  }

  const items = data[itemsKey] as Record<string, unknown>[] | undefined
  if (!items || items.length === 0) {
    throw new Error(`No items array found in data (tried key "${itemsKey}")`)
  }

  const sample = items[0]

  return {
    items_key: overrides?.items_key ?? itemsKey,
    id_field: overrides?.id_field ?? findIdField(sample),
    content_field: overrides?.content_field ?? findField(sample, CONTENT_CANDIDATES) ?? 'content',
    title_field: overrides?.title_field ?? findField(sample, TITLE_CANDIDATES) ?? 'title',
    tags_field: overrides?.tags_field ?? findField(sample, TAGS_CANDIDATES) ?? 'tags',
    author_field: overrides?.author_field ?? findField(sample, AUTHOR_CANDIDATES),
    date_field: overrides?.date_field ?? findField(sample, DATE_CANDIDATES),
    url_field: overrides?.url_field ?? findField(sample, URL_CANDIDATES),
  }
}

// ── Normalize a raw item ────────────────────────────────────────

function normalizeItem(
  raw: Record<string, unknown>,
  fm: FieldMap,
  index: number,
): ResearchItem {
  const get = (field: string | undefined): unknown =>
    field ? raw[field] : undefined

  // Author can be string or object with name/handle/bio
  let author: ResearchItem['author'] | undefined
  const rawAuthor = get(fm.author_field)
  if (typeof rawAuthor === 'string') {
    author = { name: rawAuthor }
  } else if (rawAuthor && typeof rawAuthor === 'object') {
    const a = rawAuthor as Record<string, unknown>
    author = {
      name: (a.display_name ?? a.name ?? a.username ?? a.handle ?? 'Unknown') as string,
      handle: (a.handle ?? a.username) as string | undefined,
      bio: a.bio as string | undefined,
    }
  }

  // Collect all remaining fields as metadata
  const knownFields = new Set([
    fm.id_field, fm.content_field, fm.title_field,
    fm.tags_field, fm.author_field, fm.date_field, fm.url_field,
  ].filter(Boolean))

  const metadata: Record<string, unknown> = {}
  for (const [k, v] of Object.entries(raw)) {
    if (!knownFields.has(k)) metadata[k] = v
  }

  return {
    id: String(get(fm.id_field) ?? `item_${index}`),
    title: String(get(fm.title_field) ?? ''),
    content: String(get(fm.content_field) ?? ''),
    url: get(fm.url_field) as string | undefined,
    date: get(fm.date_field) as string | undefined,
    author,
    tags: (get(fm.tags_field) as string[] | undefined) ?? [],
    metadata,
  }
}

// ── Public API ──────────────────────────────────────────────────

export function loadCollection(
  filePath: string,
  name?: string,
  fieldOverrides?: Partial<FieldMap>,
): ResearchCollection {
  const absPath = resolve(filePath)
  const raw = JSON.parse(readFileSync(absPath, 'utf-8')) as Record<string, unknown>
  const collectionName = name ?? basename(absPath, '.json')

  const fm = detectFieldMap(raw, fieldOverrides)
  const rawItems = raw[fm.items_key] as Record<string, unknown>[]
  const items = rawItems.map((item, i) => normalizeItem(item, fm, i))

  // Collect all unique tags
  const tagSet = new Set<string>()
  for (const item of items) {
    for (const tag of item.tags) tagSet.add(tag)
  }

  const collection: ResearchCollection = {
    name: collectionName,
    file_path: absPath,
    items,
    field_map: fm,
    available_tags: [...tagSet].sort(),
  }

  store.set(collectionName, collection)
  return collection
}

export function queryItems(options: {
  collection?: string
  tags?: string[]
  search?: string
  fields?: Record<string, string>
  limit?: number
}): ResearchItem[] {
  const { tags, search, fields, limit = 20 } = options
  const searchLower = search?.toLowerCase()

  let sources: ResearchCollection[]
  if (options.collection) {
    const c = store.get(options.collection)
    if (!c) throw new Error(`Collection "${options.collection}" not loaded`)
    sources = [c]
  } else {
    sources = [...store.values()]
  }

  const results: ResearchItem[] = []

  for (const col of sources) {
    for (const item of col.items) {
      // Tag filter
      if (tags && tags.length > 0) {
        if (!tags.some((t) => item.tags.includes(t))) continue
      }

      // Full-text search
      if (searchLower) {
        const haystack = `${item.title} ${item.content} ${item.tags.join(' ')}`.toLowerCase()
        if (!haystack.includes(searchLower)) continue
      }

      // Metadata field filters
      if (fields) {
        let match = true
        for (const [k, v] of Object.entries(fields)) {
          const val = String(item.metadata[k] ?? '').toLowerCase()
          if (!val.includes(v.toLowerCase())) { match = false; break }
        }
        if (!match) continue
      }

      results.push(item)
      if (results.length >= limit) break
    }
    if (results.length >= limit) break
  }

  return results
}

export function getItems(
  collectionName: string,
  itemIds: string[],
): ResearchItem[] {
  const col = store.get(collectionName)
  if (!col) throw new Error(`Collection "${collectionName}" not loaded`)

  const idSet = new Set(itemIds)
  return col.items.filter((item) => idSet.has(item.id))
}

export function getCollection(name: string): ResearchCollection | undefined {
  return store.get(name)
}

export function listCollections(): {
  name: string
  item_count: number
  available_tags: string[]
  field_map: FieldMap
}[] {
  return [...store.values()].map((c) => ({
    name: c.name,
    item_count: c.items.length,
    available_tags: c.available_tags,
    field_map: c.field_map,
  }))
}
