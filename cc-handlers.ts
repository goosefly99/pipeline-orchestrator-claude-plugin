// cc-handlers.ts — Concepts Collector handler logic extracted from server.ts

import type { HandlerResponse } from './types.ts'
import type { ResearchItem, KnowledgeOverview } from './cc-types.ts'
import {
  loadCollection,
  queryItems,
  getItems,
} from './collections.ts'
import {
  collectConcepts,
  saveOverview,
  listOverviews,
  getOverview,
} from './concepts.ts'
import { BM25Index, bm25IndexDir } from './bm25.ts'

// ── Context provided by server.ts ────────────────────────────

export interface CCContext {
  loadCollection(
    filePath: string,
    name?: string,
    fieldOverrides?: Record<string, string>,
  ): {
    name: string
    file_path: string
    items: { length: number }
    field_map: {
      items_key: string
      id_field: string
      content_field: string
      title_field: string
      tags_field: string
      author_field?: string
      date_field?: string
      url_field?: string
    }
    available_tags: string[]
  }

  queryItems(options: {
    collection?: string
    tags?: string[]
    search?: string
    fields?: Record<string, string>
    limit?: number
  }): ResearchItem[]

  getItems(collection: string, itemIds: string[]): ResearchItem[]

  collectConcepts(
    sourceGroups: { collection: string; items: ResearchItem[] }[],
    options: { title: string; focus?: string; depth?: 'brief' | 'standard' | 'deep'; chunk_index?: number },
  ): { synthesis_prompt: string; chunk?: { chunk_index: number; total_chunks: number; item_count: number; total_items: number; continuation_token?: string; is_final_chunk: boolean } }

  saveOverview(overview: KnowledgeOverview, outputDir?: string): string

  listOverviews(directory?: string): {
    overview_id: string
    title: string
    status: string
    concept_count: number
    source_count: number
    created_date: string
    file: string
  }[]

  getOverview(overviewId: string, directory?: string): KnowledgeOverview | null

  /** Optional: return the storage base directory for vector index lookup. */
  getBaseDir?(): string | null
}

// Re-export HandlerResponse for backward compatibility
export type { HandlerResponse } from './types.ts'

// ── Handlers ─────────────────────────────────────────────────

export function handleCCLoadCollection(args: Record<string, unknown>, ctx: CCContext): HandlerResponse {
  const filePath = args.file_path as string
  if (!filePath) throw new Error('file_path is required')

  const col = ctx.loadCollection(
    filePath,
    args.name as string | undefined,
    args.field_overrides as Record<string, string> | undefined,
  )

  const lines = [
    `Loaded collection "${col.name}"`,
    `  Items: ${col.items.length}`,
    `  Source: ${col.file_path}`,
    `  Field map:`,
    `    items_key: ${col.field_map.items_key}`,
    `    id: ${col.field_map.id_field}`,
    `    content: ${col.field_map.content_field}`,
    `    title: ${col.field_map.title_field}`,
    `    tags: ${col.field_map.tags_field}`,
    col.field_map.author_field ? `    author: ${col.field_map.author_field}` : '',
    col.field_map.date_field ? `    date: ${col.field_map.date_field}` : '',
    col.field_map.url_field ? `    url: ${col.field_map.url_field}` : '',
    '',
    `  Available tags (${col.available_tags.length}):`,
    `    ${col.available_tags.join(', ')}`,
  ]

  return { json: lines.filter(Boolean).join('\n') }
}

export function handleCCQuery(args: Record<string, unknown>, ctx: CCContext): HandlerResponse {
  const results = ctx.queryItems({
    collection: args.collection as string | undefined,
    tags: args.tags as string[] | undefined,
    search: args.search as string | undefined,
    fields: args.fields as Record<string, string> | undefined,
    limit: args.limit as number | undefined,
  })

  if (results.length === 0) {
    return { json: 'No items matched the query.' }
  }

  const lines = [`Found ${results.length} item(s):\n`]
  for (const item of results) {
    const preview = item.content.length > 200
      ? item.content.slice(0, 200) + '...'
      : item.content
    lines.push(`[${item.id}] ${item.title || '(untitled)'}`)
    if (item.author) lines.push(`  Author: ${item.author.name}`)
    if (item.tags.length > 0) lines.push(`  Tags: ${item.tags.join(', ')}`)
    lines.push(`  ${preview}`)
    lines.push('')
  }

  return { json: lines.join('\n') }
}

const CC_GET_ITEMS_DEFAULT_MAX_CONTENT_LENGTH = 500

export function handleCCGetItems(args: Record<string, unknown>, ctx: CCContext): HandlerResponse {
  const collection = args.collection as string
  const itemIds = args.item_ids as string[]
  if (!collection || !itemIds) throw new Error('collection and item_ids are required')

  const full = args.full === true
  const maxContentLength = full
    ? Infinity
    : typeof args.max_content_length === 'number'
      ? args.max_content_length
      : CC_GET_ITEMS_DEFAULT_MAX_CONTENT_LENGTH

  const items = ctx.getItems(collection, itemIds)

  if (items.length === 0) {
    return { json: `No items found with the given IDs in "${collection}".` }
  }

  const lines: string[] = []
  for (const item of items) {
    lines.push(`=== [${item.id}] ${item.title || '(untitled)'} ===`)
    if (item.author) {
      lines.push(`Author: ${item.author.name}${item.author.handle ? ` (@${item.author.handle})` : ''}`)
    }
    if (item.date) lines.push(`Date: ${item.date}`)
    if (item.url) lines.push(`URL: ${item.url}`)
    if (item.tags.length > 0) lines.push(`Tags: ${item.tags.join(', ')}`)
    const content = item.content.length > maxContentLength
      ? item.content.slice(0, maxContentLength) + '...'
      : item.content
    lines.push('', content, '')
  }

  return { json: lines.join('\n') }
}

export async function handleCCCollectConcepts(
  args: Record<string, unknown>,
  ctx: CCContext,
): Promise<HandlerResponse> {
  const title = args.title as string
  const itemGroups = args.items as { collection: string; item_ids: string[] }[]
  if (!title || !itemGroups) throw new Error('title and items are required')

  const sourceGroups = itemGroups.map((group) => ({
    collection: group.collection,
    items: ctx.getItems(group.collection, group.item_ids),
  }))

  const totalItems = sourceGroups.reduce((sum, g) => sum + g.items.length, 0)
  if (totalItems === 0) throw new Error('No items found for the given IDs')

  const focus = args.focus as string | undefined

  // BM25 retrieval path: focus + large collection + index exists
  if (focus && totalItems > 15) {
    const baseDir = ctx.getBaseDir?.() ?? null
    if (baseDir) {
      const uniqueCollections = [...new Set(itemGroups.map((g) => g.collection))]

      for (const collectionName of uniqueCollections) {
        const indexDir = bm25IndexDir(baseDir, collectionName)
        const idx = new BM25Index()

        if (idx.isBuilt(indexDir)) {
          idx.loadIndex(indexDir)
          const topK = typeof args.top_k === 'number' ? args.top_k : 10
          const hits = idx.search(focus, topK)
          const hitIds = hits.map((h) => h.item_id)

          const filteredGroups = sourceGroups
            .map((g) => ({
              collection: g.collection,
              items: g.items.filter((item) => hitIds.includes(item.id)),
            }))
            .filter((g) => g.items.length > 0)

          const selectedCount = filteredGroups.reduce((s, g) => s + g.items.length, 0)

          if (filteredGroups.length > 0 && selectedCount > 0) {
            const { synthesis_prompt } = ctx.collectConcepts(filteredGroups, {
              title,
              focus,
              depth: args.depth as 'brief' | 'standard' | 'deep' | undefined,
            })

            return {
              json: JSON.stringify(
                {
                  synthesis_prompt,
                  vector_retrieval: {
                    collection: collectionName,
                    total_items: totalItems,
                    selected_items: selectedCount,
                    hit_ids: hitIds,
                  },
                },
                null,
                2,
              ),
            }
          }
        }
      }
    }
  }

  // Fall through to existing chunked/full behavior

  // Resolve chunk_index: continuation_token takes priority over raw chunk_index
  let chunkIndex: number | undefined
  if (typeof args.continuation_token === 'string') {
    chunkIndex = parseInt(Buffer.from(args.continuation_token, 'base64').toString('utf-8'), 10)
  } else if (typeof args.chunk_index === 'number') {
    chunkIndex = args.chunk_index
  }

  const { synthesis_prompt, chunk } = ctx.collectConcepts(sourceGroups, {
    title,
    focus,
    depth: args.depth as 'brief' | 'standard' | 'deep' | undefined,
    chunk_index: chunkIndex,
  })

  if (chunk) {
    const responseObj: Record<string, unknown> = { synthesis_prompt }
    responseObj.chunk = chunk
    responseObj.instruction = chunk.is_final_chunk
      ? 'Final chunk. Merge concept results from all chunks into a single KnowledgeOverview.'
      : `Chunk ${chunk.chunk_index + 1}/${chunk.total_chunks}. After processing, call pipeline_cc_collect_concepts again with continuation_token "${chunk.continuation_token}" to get the next chunk.`
    return { json: JSON.stringify(responseObj, null, 2) }
  }

  return { json: synthesis_prompt }
}

export function handleCCSaveOverview(args: Record<string, unknown>, ctx: CCContext): HandlerResponse {
  const overview = args.overview as KnowledgeOverview
  if (!overview) throw new Error('overview is required')

  overview.status = 'complete'
  const filePath = ctx.saveOverview(overview, args.output_dir as string | undefined)

  return {
    json: [
      `Overview saved: ${filePath}`,
      `  ID: ${overview.overview_id}`,
      `  Title: ${overview.title}`,
      `  Concepts: ${overview.concepts?.length ?? 0}`,
      `  Themes: ${overview.themes?.length ?? 0}`,
    ].join('\n'),
  }
}

export function handleCCListOverviews(args: Record<string, unknown>, ctx: CCContext): HandlerResponse {
  const overviews = ctx.listOverviews(args.directory as string | undefined)

  if (overviews.length === 0) {
    return { json: 'No saved overviews found.' }
  }

  const lines = [`${overviews.length} overview(s):\n`]
  for (const o of overviews) {
    lines.push(`[${o.overview_id}] ${o.title}`)
    lines.push(`  Status: ${o.status} | Concepts: ${o.concept_count} | Sources: ${o.source_count}`)
    lines.push(`  Created: ${o.created_date}`)
    lines.push(`  File: ${o.file}`)
    lines.push('')
  }

  return { json: lines.join('\n') }
}

export function handleCCGetOverview(args: Record<string, unknown>, ctx: CCContext): HandlerResponse {
  const overviewId = args.overview_id as string
  if (!overviewId) throw new Error('overview_id is required')

  const overview = ctx.getOverview(overviewId, args.directory as string | undefined)
  if (!overview) {
    return { json: `Overview "${overviewId}" not found.`, isError: true }
  }

  return { json: JSON.stringify(overview, null, 2) }
}

// ── Context factory ──────────────────────────────────────────

/** Create the default CCContext wired to the module-level collection/concept functions. */
export function createCCContext(): CCContext {
  return {
    loadCollection,
    queryItems,
    getItems,
    collectConcepts,
    saveOverview,
    listOverviews,
    getOverview,
  }
}
