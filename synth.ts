// synth.ts — Spec synthesis, save, and list logic (embedded from synth-mcp)

import { readFileSync, writeFileSync, existsSync, mkdirSync, readdirSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { randomUUID } from 'node:crypto'
import type { DesignSpec, SpecSource, SpecType } from './synth-types.ts'
import type { ResearchItem } from './cc-types.ts'
import { getArtifactDir } from './storage.ts'

/**
 * Build a synthesis prompt containing:
 * - Full source material from all selected items
 * - Cross-reference analysis (shared tags, deps, platforms)
 * - A blank spec template pre-wired with source references
 *
 * Claude fills in the template using the source material.
 */
export function createSpecSynthesis(
  title: string,
  sourceGroups: Array<{
    collection: string
    items: ResearchItem[]
    relevance_notes?: Record<string, string>
  }>,
  options: {
    spec_type?: string
    domain?: string
    focus?: string
  } = {},
): { template: DesignSpec; synthesis_prompt: string } {
  const specId = randomUUID()
  const now = new Date().toISOString().split('T')[0]!

  const allItems = sourceGroups.flatMap(g =>
    g.items.map(item => ({
      item,
      collection: g.collection,
      relevance: g.relevance_notes?.[item.id] ?? '',
    })),
  )

  const specSources: SpecSource[] = allItems.map(({ item, collection, relevance }) => ({
    collection,
    item_id: item.id,
    title: item.title,
    relevance,
  }))

  const sourceContent = allItems
    .map(({ item, collection }) => {
      const lines: string[] = [
        `=== Source: ${item.title} ===`,
        `Collection: ${collection} | ID: ${item.id}`,
      ]
      if (item.author) {
        lines.push(`Author: ${item.author.name}${item.author.handle ? ` (${item.author.handle})` : ''}`)
        if (item.author.bio) lines.push(`Bio: ${item.author.bio}`)
      }
      if (item.date) lines.push(`Date: ${item.date}`)
      if (item.url) lines.push(`URL: ${item.url}`)
      if (item.tags.length) lines.push(`Tags: ${item.tags.join(', ')}`)
      lines.push('')
      lines.push(item.content)

      const domainKeys = [
        'strategy_type', 'instrument', 'platform', 'dependencies',
        'engagement', 'embedded_content', 'has_article_content',
      ]
      for (const k of domainKeys) {
        if (item.metadata[k] != null) {
          const v = item.metadata[k]
          lines.push(`${k}: ${typeof v === 'object' ? JSON.stringify(v) : String(v)}`)
        }
      }

      return lines.join('\n')
    })
    .join('\n\n---\n\n')

  const tagCounts = new Map<string, number>()
  allItems.forEach(({ item }) =>
    item.tags.forEach(t => tagCounts.set(t, (tagCounts.get(t) ?? 0) + 1)),
  )
  const sharedTags = [...tagCounts.entries()]
    .filter(([, c]) => c > 1)
    .sort(([, a], [, b]) => b - a)
    .map(([t, c]) => `${t} (${c})`)

  const allDeps = new Set<string>()
  const allPlatforms = new Set<string>()
  const allInstruments = new Set<string>()
  allItems.forEach(({ item }) => {
    const deps = item.metadata.dependencies
    if (Array.isArray(deps)) deps.forEach(d => allDeps.add(String(d)))
    if (item.metadata.platform) allPlatforms.add(String(item.metadata.platform))
    if (item.metadata.instrument) allInstruments.add(String(item.metadata.instrument))
  })

  const template: DesignSpec = {
    spec_id: specId,
    title,
    created_date: now,
    updated_date: now,
    version: '1.0',
    status: 'draft',
    domain: options.domain,
    spec_type: (options.spec_type as SpecType) ?? 'implementation',
    sources: specSources,
    overview: {
      description: '',
      objectives: [],
      constraints: options.focus ? [options.focus] : [],
      assumptions: [],
    },
    architecture: {
      components: [],
      data_flow: '',
      integration_points: [...allDeps, ...allPlatforms],
    },
    implementation: {
      phases: [],
      tech_stack: [...allDeps],
      complexity: 'medium',
    },
    risks: [],
    success_criteria: [],
    notes: '',
  }

  const synthesis = [
    `# Design Spec Synthesis: ${title}`,
    '',
    `**Spec ID:** ${specId}`,
    `**Type:** ${options.spec_type ?? 'implementation'}`,
    options.domain ? `**Domain:** ${options.domain}` : null,
    options.focus ? `**Focus:** ${options.focus}` : null,
    '',
    `## Source Material (${allItems.length} items from ${sourceGroups.length} collection(s))`,
    '',
    sourceContent,
    '',
    '## Cross-Reference Analysis',
    '',
    `**Shared tags:** ${sharedTags.join(', ') || 'None'}`,
    `**Platforms:** ${[...allPlatforms].join(', ') || 'None'}`,
    `**Instruments:** ${[...allInstruments].join(', ') || 'None'}`,
    `**Dependencies:** ${[...allDeps].join(', ') || 'None'}`,
    '',
    '## Spec Template',
    '',
    'Complete this spec by synthesising the source material above.',
    'Fill in all empty strings and arrays. Return the full JSON object.',
    '',
    '```json',
    JSON.stringify(template, null, 2),
    '```',
  ]
    .filter(line => line !== null)
    .join('\n')

  return { template, synthesis_prompt: synthesis }
}

/**
 * Compute the on-disk filename (not a full path) for a given design spec.
 *
 * Derives a slug from `spec.title` — lowercased, non-alphanumerics
 * collapsed to `-`, leading/trailing `-` trimmed, truncated to 60 chars —
 * and appends `--{spec_id[0..8]}.json`. Pure helper: no I/O, no module state.
 */
export function specFileName(spec: DesignSpec): string {
  const slug = spec.title
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '')
    .substring(0, 60)

  return `${slug}--${spec.spec_id.substring(0, 8)}.json`
}

export function saveSpec(spec: DesignSpec, outputDir: string): string {
  const dir = resolve(outputDir)
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true })

  const filePath = join(dir, specFileName(spec))

  spec.updated_date = new Date().toISOString().split('T')[0]!
  writeFileSync(filePath, JSON.stringify(spec, null, 2), 'utf-8')
  return filePath
}

// ── Per-run spec write helpers (Feature C) ──────────────────────
//
// Route spec persistence through `state.run_data_dir` when set,
// falling back to `{legacyBaseDir}/specs/` when a run predates
// Feature-A parameterization. Preserves the historical
// "disk-first, overwrite-ok" semantics of saveSpec — collision
// is avoided in practice by the spec_id-derived suffix on filenames.

/**
 * Resolve the on-disk directory for design specs.
 *
 * - When `runDataDir` is a non-empty string, returns
 *   `getArtifactDir(runDataDir, 'specs')`.
 * - Otherwise falls back to `{legacyBaseDir}/specs`.
 */
export function resolveSpecsDir(
  runDataDir: string | undefined,
  legacyBaseDir: string,
): string {
  if (runDataDir) {
    return getArtifactDir(runDataDir, 'specs')
  }
  return join(legacyBaseDir, 'specs')
}

/**
 * Persist a design spec under the run's data directory.
 *
 * Precedence for the target directory:
 *   1. `outputDirOverride` (resolved absolute) — preserves the explicit
 *      `output_dir` user-override semantics of `handleSynthSaveSpec`.
 *   2. `getArtifactDir(runDataDir, 'specs')` when `runDataDir` is set.
 *   3. `{legacyBaseDir}/specs` as the legacy fallback.
 *
 * When both `runDataDir` and `outputDirOverride` are absent, writes under
 * `legacyBaseDir` so callers like `server.ts` retain full control over the
 * fallback location.
 *
 * Mutates `spec.updated_date` to today's date before writing, matching the
 * existing behavior of `saveSpec`.
 *
 * @returns the absolute path written.
 */
export function persistSpec(
  runDataDir: string | undefined,
  legacyBaseDir: string,
  spec: DesignSpec,
  outputDirOverride?: string,
): string {
  const dir = outputDirOverride
    ? resolve(outputDirOverride)
    : resolveSpecsDir(runDataDir, legacyBaseDir)

  mkdirSync(dir, { recursive: true })
  const filePath = join(dir, specFileName(spec))

  spec.updated_date = new Date().toISOString().split('T')[0]!
  writeFileSync(filePath, JSON.stringify(spec, null, 2), 'utf-8')
  return filePath
}

export function listSpecs(
  directory: string,
): Array<{
  spec_id: string
  title: string
  created_date: string
  status: string
  spec_type: string
  source_count: number
  file_path: string
}> {
  const dir = resolve(directory)
  if (!existsSync(dir)) return []

  return readdirSync(dir)
    .filter((f: string) => f.endsWith('.json'))
    .map((f: string) => {
      const filePath = join(dir, f)
      try {
        const spec = JSON.parse(readFileSync(filePath, 'utf-8')) as DesignSpec
        return {
          spec_id: spec.spec_id,
          title: spec.title,
          created_date: spec.created_date,
          status: spec.status,
          spec_type: spec.spec_type,
          source_count: spec.sources.length,
          file_path: filePath,
        }
      } catch {
        return null
      }
    })
    .filter(Boolean) as Array<{
    spec_id: string
    title: string
    created_date: string
    status: string
    spec_type: string
    source_count: number
    file_path: string
  }>
}
