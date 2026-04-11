import { randomBytes } from 'node:crypto'
import { readFileSync, writeFileSync, readdirSync, mkdirSync, existsSync } from 'node:fs'
import { join, resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import type {
  ResearchItem,
  KnowledgeOverview,
  OverviewSource,
  Concept,
  Theme,
} from './cc-types.ts'
import { getArtifactDir } from './storage.ts'

// ── Configurable output directory ───────────────────────────────

const __cc_dirname = dirname(fileURLToPath(import.meta.url))

function loadOutputDir(): string {
  // 1. Environment variable takes highest priority
  if (process.env.CONCEPTS_OUTPUT_DIR) {
    return resolve(process.env.CONCEPTS_OUTPUT_DIR)
  }

  // 2. Local config file (not committed to git)
  const configPath = join(__cc_dirname, 'config.local.json')
  if (existsSync(configPath)) {
    try {
      const config = JSON.parse(readFileSync(configPath, 'utf-8')) as { output_dir?: string }
      if (config.output_dir) return resolve(config.output_dir)
    } catch {
      // Ignore malformed config, fall through to default
    }
  }

  // 3. Default: relative ./overviews directory
  return resolve('./overviews')
}

const OUTPUT_DIR = loadOutputDir()

// ── Collect concepts from research items ────────────────────────

interface SourceGroup {
  collection: string
  items: ResearchItem[]
}

interface CollectOptions {
  title: string
  focus?: string
  depth?: 'brief' | 'standard' | 'deep'
  chunk_index?: number
}

interface ChunkInfo {
  chunk_index: number
  total_chunks: number
  item_count: number
  total_items: number
  continuation_token?: string
  is_final_chunk: boolean
}

const CHUNK_SIZE = 8

export function collectConcepts(
  sourceGroups: SourceGroup[],
  options: CollectOptions,
): { template: KnowledgeOverview; synthesis_prompt: string; chunk?: ChunkInfo } {
  const overviewId = randomBytes(4).toString('hex')
  const depth = options.depth ?? 'standard'

  // Flatten all items for chunking logic
  const allItems = sourceGroups.flatMap(g => g.items.map(item => ({ collection: g.collection, item })))
  const totalItems = allItems.length

  let chunkInfo: ChunkInfo | undefined
  let itemsToProcess: typeof allItems

  if (totalItems > 15) {
    // Chunked mode
    const totalChunks = Math.ceil(totalItems / CHUNK_SIZE)
    const chunkIndex = options.chunk_index ?? 0
    const start = chunkIndex * CHUNK_SIZE
    const end = Math.min(start + CHUNK_SIZE, totalItems)
    itemsToProcess = allItems.slice(start, end)

    const isFinal = end >= totalItems
    chunkInfo = {
      chunk_index: chunkIndex,
      total_chunks: totalChunks,
      item_count: itemsToProcess.length,
      total_items: totalItems,
      is_final_chunk: isFinal,
      continuation_token: isFinal ? undefined : Buffer.from(String(chunkIndex + 1)).toString('base64'),
    }

    // Rebuild sourceGroups from chunked items (preserve collection grouping)
    const chunkGroups = new Map<string, ResearchItem[]>()
    for (const { collection, item } of itemsToProcess) {
      if (!chunkGroups.has(collection)) chunkGroups.set(collection, [])
      chunkGroups.get(collection)!.push(item)
    }
    sourceGroups = [...chunkGroups.entries()].map(([collection, items]) => ({ collection, items }))
  } else {
    itemsToProcess = allItems
  }

  // Flatten sources for reference (from processed items only)
  const sources: OverviewSource[] = []
  for (const { collection, item } of itemsToProcess) {
    sources.push({
      collection,
      item_id: item.id,
      title: item.title || item.content.slice(0, 80),
    })
  }

  // Build formatted source material
  const sourceSections: string[] = []
  for (const group of sourceGroups) {
    sourceSections.push(`\n## Collection: ${group.collection}\n`)
    for (const item of group.items) {
      const parts = [`### [${item.id}] ${item.title || '(untitled)'}`]
      if (item.author) {
        parts.push(`Author: ${item.author.name}${item.author.handle ? ` (@${item.author.handle})` : ''}`)
        if (item.author.bio) parts.push(`Bio: ${item.author.bio}`)
      }
      if (item.date) parts.push(`Date: ${item.date}`)
      if (item.url) parts.push(`URL: ${item.url}`)
      if (item.tags.length > 0) parts.push(`Tags: ${item.tags.join(', ')}`)

      parts.push('', item.content)

      // Include relevant metadata fields
      const metaKeys = Object.keys(item.metadata).filter((k) =>
        typeof item.metadata[k] === 'string' ||
        typeof item.metadata[k] === 'number' ||
        Array.isArray(item.metadata[k]),
      )
      if (metaKeys.length > 0) {
        parts.push('', 'Metadata:')
        for (const k of metaKeys) {
          const v = item.metadata[k]
          const formatted = Array.isArray(v) ? (v as unknown[]).join(', ') : String(v)
          parts.push(`  ${k}: ${formatted}`)
        }
      }

      sourceSections.push(parts.join('\n'))
    }
  }

  // Cross-reference analysis
  const crossRef = buildCrossReferences(sourceGroups)

  // Build the template
  const template: KnowledgeOverview = {
    overview_id: overviewId,
    title: options.title,
    created_date: new Date().toISOString(),
    status: 'draft',
    sources,
    summary: '',
    concepts: [],
    themes: [],
    key_findings: [],
    knowledge_gaps: [],
    open_questions: [],
  }

  // Depth-specific instructions
  const depthInstructions: Record<string, string> = {
    brief: [
      'Extract 3-5 top-level concepts. Keep descriptions concise (1-2 sentences each).',
      'Identify 1-2 major themes. List 3-5 key findings.',
    ].join(' '),
    standard: [
      'Extract all distinct concepts with clear descriptions and key details.',
      'Group into logical themes. Identify relationships between concepts.',
      'List key findings and any knowledge gaps.',
    ].join(' '),
    deep: [
      'Extract every distinct concept, technique, and pattern with thorough descriptions.',
      'Include implementation details, edge cases, and nuances in key_details.',
      'Map all relationships between concepts. Identify dependencies and prerequisites.',
      'List comprehensive findings, knowledge gaps, and open questions worth investigating.',
    ].join(' '),
  }

  const focusClause = options.focus
    ? `\n\nFocus area: ${options.focus} — prioritize concepts and findings related to this focus, but do not exclude other salient content.`
    : ''

  const chunkClause = chunkInfo
    ? `\n**Chunk:** ${chunkInfo.chunk_index + 1} of ${chunkInfo.total_chunks} (items ${chunkInfo.chunk_index * CHUNK_SIZE + 1}–${chunkInfo.chunk_index * CHUNK_SIZE + chunkInfo.item_count} of ${chunkInfo.total_items})`
    : ''

  const prompt = `# Concepts Collection Task

**Title:** ${options.title}
**Depth:** ${depth}${chunkClause}${focusClause}

## Instructions

Analyze the source material below and produce a structured knowledge overview.
${depthInstructions[depth]}

For each concept extracted:
- **name**: Clear, specific name for the concept
- **category**: Broad category (e.g., "strategy", "technique", "architecture", "data source", "risk management")
- **description**: What this concept is and why it matters
- **key_details**: Specific details, numbers, parameters, or implementation notes
- **source_items**: Which source item IDs contributed to this concept
- **relationships**: How this concept relates to other concepts you've identified

For themes:
- Group related concepts under broader themes
- Each theme should have a clear description of what unifies its concepts

${crossRef}

## Source Material

${sourceSections.join('\n\n---\n\n')}

## Output

Fill in the following JSON template. Return ONLY the completed JSON — no surrounding text.

\`\`\`json
${JSON.stringify(template, null, 2)}
\`\`\`
`

  return { template, synthesis_prompt: prompt, ...(chunkInfo ? { chunk: chunkInfo } : {}) }
}

// ── Cross-reference analysis ────────────────────────────────────

function buildCrossReferences(sourceGroups: SourceGroup[]): string {
  const allItems = sourceGroups.flatMap((g) => g.items)

  if (allItems.length < 2) return ''

  // Tag frequency
  const tagCounts = new Map<string, number>()
  for (const item of allItems) {
    for (const tag of item.tags) {
      tagCounts.set(tag, (tagCounts.get(tag) ?? 0) + 1)
    }
  }

  // Metadata value frequency (for string fields like strategy_type, platform, instrument)
  const metaFreq = new Map<string, Map<string, number>>()
  for (const item of allItems) {
    for (const [k, v] of Object.entries(item.metadata)) {
      if (typeof v !== 'string') continue
      if (!metaFreq.has(k)) metaFreq.set(k, new Map())
      const fieldMap = metaFreq.get(k)!
      fieldMap.set(v, (fieldMap.get(v) ?? 0) + 1)
    }
  }

  // Dependencies / tools mentioned
  const depCounts = new Map<string, number>()
  for (const item of allItems) {
    const deps = item.metadata.dependencies
    if (Array.isArray(deps)) {
      for (const d of deps as string[]) {
        depCounts.set(d, (depCounts.get(d) ?? 0) + 1)
      }
    }
  }

  const sections: string[] = ['## Cross-Reference Analysis']

  // Shared tags
  const sharedTags = [...tagCounts.entries()]
    .filter(([, count]) => count >= 2)
    .sort((a, b) => b[1] - a[1])
  if (sharedTags.length > 0) {
    sections.push('\n**Recurring tags:**')
    for (const [tag, count] of sharedTags) {
      sections.push(`- ${tag} (${count} items)`)
    }
  }

  // Metadata field distributions
  for (const [field, values] of metaFreq) {
    const sorted = [...values.entries()].sort((a, b) => b[1] - a[1])
    if (sorted.length >= 2 && sorted.some(([, c]) => c >= 2)) {
      sections.push(`\n**${field} distribution:**`)
      for (const [val, count] of sorted.slice(0, 10)) {
        sections.push(`- ${val} (${count})`)
      }
    }
  }

  // Shared dependencies
  const sharedDeps = [...depCounts.entries()]
    .filter(([, count]) => count >= 2)
    .sort((a, b) => b[1] - a[1])
  if (sharedDeps.length > 0) {
    sections.push('\n**Common dependencies:**')
    for (const [dep, count] of sharedDeps) {
      sections.push(`- ${dep} (${count} items)`)
    }
  }

  return sections.length > 1 ? sections.join('\n') : ''
}

// ── Persistence ─────────────────────────────────────────────────

/**
 * Compute the on-disk filename (not a full path) for a given overview.
 *
 * Derives a slug from `overview.title` — lowercased, non-alphanumerics
 * collapsed to `-`, leading/trailing `-` trimmed, truncated to 60 chars —
 * and appends `--{overview_id}.json`. Pure helper: no I/O, no module state.
 */
export function overviewFileName(overview: KnowledgeOverview): string {
  const slug = overview.title
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '')
    .slice(0, 60)

  return `${slug}--${overview.overview_id}.json`
}

export function saveOverview(
  overview: KnowledgeOverview,
  outputDir?: string,
): string {
  const dir = outputDir ? resolve(outputDir) : OUTPUT_DIR
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true })

  const filePath = join(dir, overviewFileName(overview))

  writeFileSync(filePath, JSON.stringify(overview, null, 2), 'utf-8')
  return filePath
}

// ── Per-run overview write helpers (Feature C) ──────────────────
//
// Route overview persistence through `state.run_data_dir` when set,
// falling back to `{legacyBaseDir}/overviews/` when a run predates
// Feature-A parameterization. Preserves the historical
// "disk-first, overwrite-ok" semantics of saveOverview — collision
// is avoided in practice by the random-hex suffix on overview IDs.

/**
 * Resolve the on-disk directory for overviews.
 *
 * - When `runDataDir` is a non-empty string, returns
 *   `getArtifactDir(runDataDir, 'overviews')`.
 * - Otherwise falls back to `{legacyBaseDir}/overviews`.
 */
export function resolveOverviewsDir(
  runDataDir: string | undefined,
  legacyBaseDir: string,
): string {
  if (runDataDir) {
    return getArtifactDir(runDataDir, 'overviews')
  }
  return join(legacyBaseDir, 'overviews')
}

/**
 * Persist an overview under the run's data directory.
 *
 * Precedence for the target directory:
 *   1. `outputDirOverride` (resolved absolute) — preserves the explicit
 *      `output_dir` user-override semantics of `handleCCSaveOverview`.
 *   2. `getArtifactDir(runDataDir, 'overviews')` when `runDataDir` is set.
 *   3. `{legacyBaseDir}/overviews` as the legacy fallback.
 *
 * When both `runDataDir` and `outputDirOverride` are absent, writes under
 * `legacyBaseDir` (not the module-level `OUTPUT_DIR`) so callers like
 * `server.ts` retain full control over the fallback location.
 *
 * @returns the absolute path written.
 */
export function persistOverview(
  runDataDir: string | undefined,
  legacyBaseDir: string,
  overview: KnowledgeOverview,
  outputDirOverride?: string,
): string {
  const dir = outputDirOverride
    ? resolve(outputDirOverride)
    : resolveOverviewsDir(runDataDir, legacyBaseDir)

  mkdirSync(dir, { recursive: true })
  const filePath = join(dir, overviewFileName(overview))
  writeFileSync(filePath, JSON.stringify(overview, null, 2), 'utf-8')
  return filePath
}

export function listOverviews(directory?: string): {
  overview_id: string
  title: string
  created_date: string
  status: string
  concept_count: number
  source_count: number
  file: string
}[] {
  const dir = directory ? resolve(directory) : OUTPUT_DIR
  if (!existsSync(dir)) return []

  return readdirSync(dir)
    .filter((f: string) => f.endsWith('.json'))
    .map((f: string) => {
      const data = JSON.parse(readFileSync(join(dir, f), 'utf-8')) as KnowledgeOverview
      return {
        overview_id: data.overview_id,
        title: data.title,
        created_date: data.created_date,
        status: data.status,
        concept_count: data.concepts?.length ?? 0,
        source_count: data.sources?.length ?? 0,
        file: f,
      }
    })
}

export function getOverview(
  overviewId: string,
  directory?: string,
): KnowledgeOverview | null {
  const dir = directory ? resolve(directory) : OUTPUT_DIR
  if (!existsSync(dir)) return null

  const files = readdirSync(dir).filter((f: string) => f.endsWith('.json'))
  for (const f of files) {
    if (f.includes(overviewId)) {
      return JSON.parse(readFileSync(join(dir, f), 'utf-8')) as KnowledgeOverview
    }
  }
  return null
}
