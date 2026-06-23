// synth-handlers.ts — Synth tool handlers for pipeline-orchestrator (embedded from synth-mcp)

import type { HandlerResponse } from './types.ts'
import type { ResearchItem, ResearchCollection, FieldMap } from './cc-types.ts'
import type { DesignSpec } from './synth-types.ts'
import {
  loadCollection as loadCollectionFn,
  queryItems as queryItemsFn,
  getItems as getItemsFn,
} from './collections.ts'
import {
  createSpecSynthesis,
  saveSpec,
  listSpecs,
} from './synth.ts'

// ── Context provided by server.ts ────────────────────────────

export interface SynthContext {
  loadCollection(
    filePath: string,
    name?: string,
    fieldOverrides?: Partial<FieldMap>,
  ): ResearchCollection

  queryItems(options: {
    collection?: string
    tags?: string[]
    search?: string
    fields?: Record<string, string>
    limit?: number
  }): ResearchItem[]

  getItems(collection: string, itemIds: string[]): ResearchItem[]

  getSpecsDir(): string

  /**
   * Per-run spec persistence (Feature C). When present, takes precedence
   * over `saveSpec` in `handleSynthSaveSpec`. Server.ts wires this to
   * `persistSpec(runDataDir, legacyBaseDir, spec, outputDirOverride)` so
   * callers get automatic run-scoped routing without plumbing context
   * through the handler.
   *
   * Optional so older test fixtures that mock the context without this
   * field continue to fall back to `saveSpec`.
   */
  persistSpec?(spec: DesignSpec, outputDirOverride?: string): string
}

// Re-export HandlerResponse for backward compatibility
export type { HandlerResponse } from './types.ts'

// ── Handlers ─────────────────────────────────────────────────

export function handleSynthLoadCollection(
  args: Record<string, unknown>,
  ctx: SynthContext,
): HandlerResponse {
  const filePath = args.file_path as string
  if (!filePath) throw new Error('file_path is required')

  const overrides: Partial<FieldMap> = {}
  for (const key of ['items_key', 'id_field', 'content_field', 'title_field', 'tags_field']) {
    if (args[key]) (overrides as Record<string, string>)[key] = args[key] as string
  }

  const col = ctx.loadCollection(filePath, args.name as string | undefined, overrides)

  const lines = [
    `Collection "${col.name}" loaded successfully`,
    `  Items: ${col.items.length}`,
    `  Source: ${col.file_path}`,
    `  Field map: ${JSON.stringify(col.field_map)}`,
    '',
    `  Tags (${col.available_tags.length}): ${col.available_tags.join(', ')}`,
    '',
    '  First 5 items:',
    ...col.items.slice(0, 5).map(
      (i: ResearchItem) => `    [${i.id}] ${i.title}${i.tags.length ? ` (${i.tags.slice(0, 4).join(', ')})` : ''}`,
    ),
    col.items.length > 5 ? `    ... and ${col.items.length - 5} more` : '',
  ]

  return { json: lines.join('\n') }
}

export function handleSynthQuery(
  args: Record<string, unknown>,
  ctx: SynthContext,
): HandlerResponse {
  const results = ctx.queryItems({
    collection: args.collection as string | undefined,
    tags: args.tags as string[] | undefined,
    search: args.search as string | undefined,
    fields: args.fields as Record<string, string> | undefined,
    limit: args.limit as number | undefined,
  })

  if (results.length === 0) {
    return { json: 'No matching items found.' }
  }

  const lines = [`Found ${results.length} item(s):`, '']
  for (const item of results) {
    lines.push(`[${item.id}] ${item.title}`)
    if (item.author) lines.push(`  Author: ${item.author.name}${item.author.handle ? ` (${item.author.handle})` : ''}`)
    if (item.tags.length) lines.push(`  Tags: ${item.tags.join(', ')}`)
    lines.push('')
  }

  return { json: lines.join('\n') }
}

export function handleSynthGetItems(
  args: Record<string, unknown>,
  ctx: SynthContext,
): HandlerResponse {
  const collection = args.collection as string
  const itemIds = args.item_ids as string[]
  if (!collection) throw new Error('collection is required')
  if (!itemIds?.length) throw new Error('item_ids is required (non-empty array)')

  const items = ctx.getItems(collection, itemIds)

  const lines = items.map(item => {
    const parts = [`=== ${item.title} (${item.id}) ===`]
    if (item.author) {
      parts.push(`Author: ${item.author.name}${item.author.handle ? ` (${item.author.handle})` : ''}`)
      if (item.author.bio) parts.push(`Bio: ${item.author.bio}`)
    }
    if (item.date) parts.push(`Date: ${item.date}`)
    if (item.url) parts.push(`URL: ${item.url}`)
    if (item.tags.length) parts.push(`Tags: ${item.tags.join(', ')}`)
    parts.push('')
    parts.push(item.content)
    parts.push('')

    for (const [k, v] of Object.entries(item.metadata)) {
      parts.push(`${k}: ${typeof v === 'object' ? JSON.stringify(v) : String(v)}`)
    }

    return parts.join('\n')
  })

  return { json: lines.join('\n\n---\n\n') }
}

export function handleSynthCreateSpec(
  args: Record<string, unknown>,
  ctx: SynthContext,
): HandlerResponse {
  const title = args.title as string
  if (!title) throw new Error('title is required')

  const rawItems = args.items as Array<{ collection: string; item_id: string; relevance?: string }>
  if (!rawItems?.length) throw new Error('items is required (non-empty array)')

  const byCollection = new Map<string, { ids: string[]; relevance: Record<string, string> }>()
  for (const entry of rawItems) {
    if (!entry.collection || !entry.item_id) {
      throw new Error('Each item must have collection and item_id')
    }
    const existing = byCollection.get(entry.collection) ?? { ids: [], relevance: {} }
    existing.ids.push(entry.item_id)
    if (entry.relevance) existing.relevance[entry.item_id] = entry.relevance
    byCollection.set(entry.collection, existing)
  }

  const sourceGroups: Array<{
    collection: string
    items: ResearchItem[]
    relevance_notes: Record<string, string>
  }> = []

  for (const [collectionName, { ids, relevance }] of byCollection) {
    const items = ctx.getItems(collectionName, ids)
    sourceGroups.push({ collection: collectionName, items, relevance_notes: relevance })
  }

  const { synthesis_prompt } = createSpecSynthesis(title, sourceGroups, {
    spec_type: args.spec_type as string | undefined,
    domain: args.domain as string | undefined,
    focus: args.focus as string | undefined,
  })

  return { json: synthesis_prompt }
}

export function handleSynthSaveSpec(
  args: Record<string, unknown>,
  ctx: SynthContext,
): HandlerResponse {
  const spec = args.spec as DesignSpec
  if (!spec) throw new Error('spec is required')
  if (!spec.spec_id || !spec.title) throw new Error('spec must have spec_id and title')

  const outputDirOverride = args.output_dir as string | undefined
  // Feature C: prefer persistSpec (run-scoped) when the context supplies it;
  // fall back to the legacy saveSpec so older test fixtures keep working.
  const filePath = ctx.persistSpec
    ? ctx.persistSpec(spec, outputDirOverride)
    : saveSpec(spec, outputDirOverride ?? ctx.getSpecsDir())

  return {
    json: `Spec saved: ${filePath}\n  Title: ${spec.title}\n  Status: ${spec.status}\n  Sources: ${spec.sources?.length ?? 0}`,
  }
}

export function handleSynthListSpecs(
  args: Record<string, unknown>,
  ctx: SynthContext,
): HandlerResponse {
  const directory = (args.directory as string | undefined) ?? ctx.getSpecsDir()
  const specs = listSpecs(directory)

  if (specs.length === 0) {
    return { json: 'No saved specs found.' }
  }

  const lines = [`${specs.length} saved spec(s):`, '']
  for (const s of specs) {
    lines.push(`[${s.spec_id.substring(0, 8)}] ${s.title}`)
    lines.push(`  Type: ${s.spec_type} | Status: ${s.status} | Sources: ${s.source_count} | Created: ${s.created_date}`)
    lines.push(`  File: ${s.file_path}`)
    lines.push('')
  }

  return { json: lines.join('\n') }
}

// ── Context factory ──────────────────────────────────────────

export function createSynthContext(specsDir: string): SynthContext {
  return {
    loadCollection: loadCollectionFn,
    queryItems: queryItemsFn,
    getItems: getItemsFn,
    getSpecsDir: () => specsDir,
  }
}
