// misc-handlers.ts — Ingestion, KB, codebase analysis, validate-run, feature-request, web-search, and get-config handlers

import { existsSync, readFileSync, writeFileSync, mkdirSync, statSync, readdirSync } from 'node:fs'
import { isAbsolute, resolve, join } from 'node:path'
import { randomBytes } from 'node:crypto'
import { parse as parseTOML, stringify as stringifyTOML } from 'smol-toml'
import { ingestDocumentsAsync } from './ingest.ts'
import { executeWebSearch } from './web-search.ts'
import { analyzeCodebase } from './codebase-analyzer.ts'
import { queryItems } from './collections.ts'
import { BM25Index, bm25IndexDir } from './bm25.ts'
import {
  createKBClient,
  appendSearchEntry,
  sqlQuery,
  exportQueryLog,
  type KBConfig,
} from './kb-client.ts'
import { validateRun, type RunValidationReport } from './cross-ref-validator.ts'
import { computeNextVersion } from './artifact-handlers.ts'
import { appendEvent } from './lifecycle-handlers.ts'
import { PipelineError } from './types.ts'
import type { RunState, StorageConfig, ResponseEnvelope, PipelineConfig, HandlerResponse, ArtifactRef } from './types.ts'
import type { SchemaMap, ValidationResult } from './validator.ts'

// ── Context provided by server.ts ────────────────────────────

export interface GetConfigContext {
  getProjectRoot(): string | null
  getPipelineDir(): string
  getConfig(): PipelineConfig
}

export interface IngestContext {
  validateArtifact(schemas: SchemaMap, schemaFile: string, artifact: unknown): ValidationResult
  getSchemas(): SchemaMap
  baseDirFromRunDir(runDir: string): string
  getProjectRoot(): string
  getConfigStorageBaseDir(): string
  getActiveRun(): RunState | null
  getActiveRunDir(): string
}

export interface ValidateRunContext {
  requireRun(): RunState
  getStorageConfig(baseOverride?: string): StorageConfig
  baseDirFromRunDir(runDir: string): string
  getActiveRunDir(): string
}

export interface KBBuildIndexContext {
  getProjectRoot(): string
  getConfigStorageBaseDir(): string
  getActiveRun(): RunState | null
  getActiveRunDir(): string
  baseDirFromRunDir(runDir: string): string
}

export interface WebSearchContext {}

export interface RegisterScaffoldOutputsContext {
  requireRun(): RunState
  setActiveRun(state: RunState): void
  getActiveRunDir(): string
  baseDirFromRunDir(runDir: string): string
  addArtifact(state: RunState, ref: ArtifactRef, stateDir: string): RunState
}

// Re-export HandlerResponse for backward compatibility
export type { HandlerResponse } from './types.ts'

// ── Scaffold anchor validation helpers (Fix 3.5) ──────────────

/**
 * Parse a markdown proposed-diff document and extract any anchor/heading references
 * it asserts exist in the target file. Looks for patterns like:
 *   - `under the existing "<heading>" section`
 *   - `under the existing '<heading>' section`
 *   - `under the "<heading>" heading`
 *   - `insert after the "<heading>" section`
 *
 * Only phrasing that claims an existing anchor is captured. Phrases that declare
 * the creation of a new section (e.g. `Create a new top-level \`## Quality Gates\`
 * section`) are deliberately not matched.
 *
 * Returns a deduplicated list of heading texts that the proposed diff claims
 * already exist in the target file. Matching is case-insensitive and the captured
 * heading is lowercased and trimmed in the output. Leading markdown hash markers
 * and whitespace are stripped, so backtick-quoted ATX headings such as
 * `\`## Quality Gates\`` normalize to `"quality gates"`.
 *
 * If the proposed diff contains no such references, returns [].
 *
 * Pure function — no I/O. Exported for unit testing.
 */
export function extractProposedDiffAnchorRefs(diffContent: string): string[] {
  if (!diffContent) return []

  // Quoted forms we care about. Supports double quotes, single quotes, and
  // backticks. The heading text itself cannot contain the matching quote char.
  const patterns: RegExp[] = [
    // "under the existing 'X' [section|heading]" — "existing" marks it as a
    // claim about an anchor that already exists.
    /under the existing\s+["'`]([^"'`]+?)["'`]/gi,
    // "insert after the 'X' [section|heading]" / "inserting after ..." — claims
    // X is already present in the target file.
    /insert(?:ing)? after the\s+["'`]([^"'`]+?)["'`]/gi,
    // Bare "under the 'X' section|heading" — quoted + explicit role word. This
    // is broader than the "existing" form but the trailing "section"/"heading"
    // keyword keeps it from firing on unrelated prose.
    /under the\s+["'`]([^"'`]+?)["'`]\s+(?:section|heading)/gi,
  ]

  const out = new Set<string>()
  for (const re of patterns) {
    // Using matchAll to iterate all captures across the string.
    for (const m of diffContent.matchAll(re)) {
      const raw = m[1]
      if (!raw) continue
      // Strip leading markdown hash markers + whitespace so backtick-quoted
      // ATX headings (`## Quality Gates`) normalize to "quality gates".
      const normalized = raw.replace(/^#+\s*/, '').trim().toLowerCase()
      if (normalized) out.add(normalized)
    }
  }
  return Array.from(out)
}

/**
 * Check whether a markdown document contains a heading matching the given text.
 * Matches any ATX heading level (#, ##, ###, ####, #####, ######). Matching is
 * case-insensitive and whitespace-trimmed.
 *
 * Returns true if any matching heading exists.
 *
 * Pure function — no I/O. Exported for unit testing.
 */
export function markdownHasHeading(
  markdownContent: string,
  headingText: string,
): boolean {
  if (!markdownContent || !headingText) return false
  const needle = headingText.trim().toLowerCase()
  if (!needle) return false

  const lines = markdownContent.split(/\r?\n/)
  const headingRe = /^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$/
  for (const line of lines) {
    const m = headingRe.exec(line)
    if (!m) continue
    const heading = m[1].trim().toLowerCase()
    if (heading === needle) return true
  }
  return false
}

// ── Get-config handler ────────────────────────────────────────

export function handleGetConfig(ctx: GetConfigContext): HandlerResponse {
  const cfg = ctx.getConfig()
  return {
    json: JSON.stringify({
      project_root: ctx.getProjectRoot() ?? '(not set — call pipeline_init_run first)',
      pipeline_dir: ctx.getPipelineDir(),
      pipeline: cfg.pipeline,
      phases: Object.entries(cfg.phases).map(([name, p]) => ({
        name,
        id: p.id,
        description: p.description,
        inputs: p.inputs,
        outputs: p.outputs,
        input_mode: p.input_mode,
        entry_point: p.entry_point,
        optional: p.optional,
        model_tier: p.model_tier,
      })),
      edges: cfg.edges,
      schemas: cfg.schemas,
      storage: cfg.storage,
    }, null, 2),
  }
}

// ── Ingestion handler ─────────────────────────────────────────

const FEATURE_REQUESTS_FILE = 'feature_requests.toml'

export async function handleIngestDocuments(
  args: Record<string, unknown>,
  ctx: IngestContext,
): Promise<HandlerResponse> {
  const filePaths = args.file_paths as string[]
  if (!Array.isArray(filePaths) || filePaths.length === 0) {
    throw new Error('file_paths must be a non-empty array of file path strings')
  }

  const ingestName = (args.ingest_name as string | undefined) ?? 'ingest'
  const shouldValidate = (args.validate as boolean | undefined) ?? true
  const jsonItemsKey = args.json_items_key as string | undefined

  const collection = await ingestDocumentsAsync(filePaths, ingestName, jsonItemsKey)

  if (shouldValidate) {
    const result = ctx.validateArtifact(ctx.getSchemas(), 'raw-collection.json', collection)
    if (!result.valid) {
      throw new Error(`Produced collection failed schema validation:\n${result.errors.join('\n')}`)
    }
  }

  // Write collection to disk first — keeps MCP response under 10KB
  const activeRun = ctx.getActiveRun()
  const baseDir = activeRun
    ? ctx.baseDirFromRunDir(ctx.getActiveRunDir())
    : resolve(ctx.getProjectRoot(), ctx.getConfigStorageBaseDir())
  const rawDir = join(baseDir, 'collections', 'raw')
  mkdirSync(rawDir, { recursive: true })
  const artifactPath = join(rawDir, `${collection.collection_id}.json`)
  writeFileSync(artifactPath, JSON.stringify(collection, null, 2))

  const sources =
    collection.source_runs
      ?.map((r) => r.path)
      .filter((p): p is string => typeof p === 'string') ?? []

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
  return { json: JSON.stringify(envelope, null, 2) }
}

// ── Codebase analysis handler ─────────────────────────────────

export function handleAnalyzeCodebase(args: Record<string, unknown>): HandlerResponse {
  const codebasePath = args.codebase_path as string
  if (!codebasePath) throw new Error('codebase_path is required')
  const result = analyzeCodebase(codebasePath)
  return { json: JSON.stringify(result, null, 2) }
}

// ── KB handlers ───────────────────────────────────────────────

export function handleKBSearch(
  args: Record<string, unknown>,
  ctx: KBBuildIndexContext,
): HandlerResponse {
  const runId = args.run_id as string
  const phase = args.phase as string
  const query = args.query as string
  const topK = (args.top_k as number | undefined) ?? 5
  const filter = args.filter as { tags?: string[] } | undefined
  if (!runId) throw new Error('run_id is required')
  if (!phase) throw new Error('phase is required')
  if (!query) throw new Error('query is required')

  // Resolve base dir
  const activeRun = ctx.getActiveRun()
  const baseDir = activeRun
    ? ctx.baseDirFromRunDir(ctx.getActiveRunDir())
    : resolve(ctx.getProjectRoot(), ctx.getConfigStorageBaseDir())

  // Scan for built indexes under kb/vectors/
  const kbDir = join(baseDir, 'kb', 'vectors')
  if (!existsSync(kbDir)) {
    return {
      json: JSON.stringify({
        status: 'error',
        results: [],
        results_count: 0,
        error: 'No BM25 indexes found. Build one using pipeline_kb_build_index first.',
      }, null, 2),
      isError: true,
    }
  }

  const collections = readdirSync(kbDir, { withFileTypes: true })
    .filter(d => d.isDirectory())
    .map(d => d.name)

  const allHits: Array<{ item_id: string; score: number; metadata: { title: string; tags: string }; collection: string }> = []

  for (const collectionName of collections) {
    const indexDir = bm25IndexDir(baseDir, collectionName)
    const idx = new BM25Index()
    if (!idx.isBuilt(indexDir)) continue

    idx.loadIndex(indexDir)
    const hits = idx.search(query, topK, filter)
    for (const hit of hits) {
      allHits.push({ ...hit, collection: collectionName })
    }
  }

  // Sort merged results by score descending, take topK
  allHits.sort((a, b) => b.score - a.score)
  const results = allHits.slice(0, topK)

  // Log the query
  appendSearchEntry(runId, phase, query, results.length)

  return {
    json: JSON.stringify({
      status: 'ok',
      results,
      results_count: results.length,
    }, null, 2),
  }
}

export async function handleKBSQLQuery(
  args: Record<string, unknown>,
): Promise<HandlerResponse> {
  const runId = args.run_id as string
  const phase = args.phase as string
  const templateName = args.template_name as string
  const parameters = (args.parameters as Record<string, unknown>) ?? {}
  if (!runId) throw new Error('run_id is required')
  if (!phase) throw new Error('phase is required')
  if (!templateName) throw new Error('template_name is required')
  const kbConfigRaw = args.kb_config as KBConfig | undefined
  if (!kbConfigRaw) throw new Error('kb_config is required')
  const client = createKBClient(kbConfigRaw)
  const result = await sqlQuery(client, {
    run_id: runId,
    phase,
    template_name: templateName,
    parameters,
  })
  return { json: JSON.stringify(result, null, 2), isError: result.status === 'error' }
}

export function handleKBExportQueryLog(args: Record<string, unknown>): HandlerResponse {
  const runId = args.run_id as string
  const phase = args.phase as string
  const debateTranscriptId = args.debate_transcript_id as string | undefined
  if (!runId) throw new Error('run_id is required')
  if (!phase) throw new Error('phase is required')
  const log = exportQueryLog(runId, phase, debateTranscriptId)
  return { json: JSON.stringify(log, null, 2) }
}

export function handleKBBuildIndex(
  args: Record<string, unknown>,
  ctx: KBBuildIndexContext,
): HandlerResponse {
  const collectionName = args.collection_name as string
  if (!collectionName) throw new Error('collection_name is required')

  // Get all items from the loaded collection
  const items = queryItems({ collection: collectionName, limit: 999999 })
  if (items.length === 0) {
    throw new Error(`Collection "${collectionName}" not found or empty. Load it with pipeline_cc_load_collection first.`)
  }

  // Resolve base dir
  const activeRun = ctx.getActiveRun()
  const baseDir = activeRun
    ? ctx.baseDirFromRunDir(ctx.getActiveRunDir())
    : resolve(ctx.getProjectRoot(), ctx.getConfigStorageBaseDir())

  // Build index
  const indexDir = bm25IndexDir(baseDir, collectionName)
  const idx = new BM25Index()
  const metadata = idx.build(items, collectionName)
  idx.persist(indexDir)

  return {
    json: JSON.stringify({
      status: 'ok',
      collection_name: collectionName,
      index_path: indexDir,
      item_count: metadata.item_count,
      avg_doc_length: metadata.avg_doc_length,
      next_step: `BM25 index built. Use pipeline_kb_search with query to search.`,
    }, null, 2),
  }
}

// ── Validate-run handler ──────────────────────────────────────

export function handleValidateRun(
  args: Record<string, unknown>,
  ctx: ValidateRunContext,
): HandlerResponse {
  const state = ctx.requireRun()
  const baseDir = ctx.baseDirFromRunDir(ctx.getActiveRunDir())
  const sc = ctx.getStorageConfig(baseDir)

  const defaultPhaseOutputs: Record<string, string[]> = {
    document_ingestion: ['raw-collection'],
    research_discovery: ['raw-collection'],
    curation: ['curated-collection'],
    concept_extraction: ['knowledge-overview'],
    codebase_analysis: ['codebase-requirements'],
    design_synthesis: ['design-spec'],
    debate: ['debate-transcript'],
    validation: ['validation-report'],
  }

  const overrides = (args.phase_output_overrides as Record<string, string[]>) ?? {}
  const phaseOutputMap = { ...defaultPhaseOutputs, ...overrides }

  const report: RunValidationReport = validateRun(state, sc, phaseOutputMap)

  return {
    json: JSON.stringify(
      { run_id: state.run_id, ...report },
      null,
      2,
    ),
    isError: !report.valid,
  }
}

// ── Feature-request handler ───────────────────────────────────

export function handleFeatureRequest(args: Record<string, unknown>): HandlerResponse {
  const targetDir = args.target_directory as string
  if (!targetDir) return { json: 'Error: target_directory is required', isError: true }

  const dirPath = resolve(targetDir)
  if (!existsSync(dirPath) || !statSync(dirPath).isDirectory()) {
    return {
      json: `Error: target_directory does not exist or is not a directory: ${dirPath}`,
      isError: true,
    }
  }

  const title = args.title as string
  const description = args.description as string
  const priority = args.priority as string
  const scope = args.scope as string
  const rationale = args.rationale as string
  const source = (args.source as string) || 'agent'

  if (!title || !description || !priority || !scope || !rationale) {
    return {
      json: 'Error: title, description, priority, scope, and rationale are all required',
      isError: true,
    }
  }
  if (!['low', 'medium', 'high'].includes(priority)) {
    return {
      json: `Error: priority must be "low", "medium", or "high" — got "${priority}"`,
      isError: true,
    }
  }

  const datePart = new Date().toISOString().slice(0, 10).replace(/-/g, '')
  const hexPart = randomBytes(3).toString('hex')
  const id = `fr-${datePart}-${hexPart}`

  const entry = {
    id,
    title,
    description,
    priority,
    scope,
    rationale,
    status: 'proposed',
    timestamp: new Date().toISOString(),
    source,
  }

  const filePath = join(dirPath, FEATURE_REQUESTS_FILE)

  // Read existing entries if file exists
  let entries: Record<string, unknown>[] = []
  if (existsSync(filePath)) {
    const raw = readFileSync(filePath, 'utf-8')
    try {
      const parsed = parseTOML(raw) as Record<string, unknown>
      const existing = parsed.feature_request
      if (Array.isArray(existing)) {
        entries = existing as Record<string, unknown>[]
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      return {
        json: `Error: existing ${FEATURE_REQUESTS_FILE} in ${dirPath} is malformed. Fix or remove it before adding new entries.\n  ${msg}`,
        isError: true,
      }
    }
  }

  entries.push(entry)
  const toml = stringifyTOML({ feature_request: entries })
  writeFileSync(filePath, toml, 'utf-8')

  return {
    json: [
      'Feature request recorded.',
      `  ID: ${id}`,
      `  Title: ${title}`,
      `  Priority: ${priority}`,
      `  File: ${filePath}`,
    ].join('\n'),
  }
}

// ── Web search handler ───────────────────────────────────────

export async function handleWebSearch(
  args: Record<string, unknown>,
  _ctx: WebSearchContext,
): Promise<HandlerResponse> {
  const collectionPath = args.collection_path as string | undefined
  if (!collectionPath) throw new Error('collection_path is required')

  const queries = args.queries as string[] | undefined
  const fetchUrls = args.fetch_urls as string[] | undefined
  const maxResultsPerQuery = args.max_results_per_query as number | undefined

  if (!queries?.length && !fetchUrls?.length) {
    throw new Error('Either queries or fetch_urls (or both) must be provided.')
  }

  // Validate collection_path exists when fetch mode will write to it
  if (fetchUrls?.length) {
    if (!existsSync(collectionPath)) {
      throw new Error(`Collection file not found: ${collectionPath}`)
    }
    // Quick-validate it's parseable JSON
    try {
      JSON.parse(readFileSync(collectionPath, 'utf-8'))
    } catch {
      throw new Error(`Collection file is not valid JSON: ${collectionPath}`)
    }
  }

  const { data, next_step } = await executeWebSearch({
    queries,
    collection_path: collectionPath,
    max_results_per_query: maxResultsPerQuery,
    fetch_urls: fetchUrls,
  })

  const envelope: ResponseEnvelope = { status: 'ok', data: data as Record<string, unknown>, next_step }
  return { json: JSON.stringify(envelope, null, 2) }
}

// ── Register scaffold outputs handler ────────────────────────

/**
 * Safe file size read. Mirrors the pattern in artifact-handlers.ts — if statSync
 * throws (file was moved/deleted between write and event emission), log a warning
 * and return 0 so the event log remains authoritative under races.
 */
function safeSizeBytes(path: string): number {
  try {
    return statSync(path).size
  } catch (err) {
    console.warn(
      `[misc-handlers] unable to stat scaffold artifact for size_bytes: ${path} — ${(err as Error).message}`,
    )
    return 0
  }
}

/**
 * Scan a scaffold directory and register every regular file as a pipeline artifact.
 * Files named "scaffold-manifest.json" are registered as "scaffold-manifest"; all
 * other files are registered as "scaffold-document". Idempotent — files whose
 * absolute path already appears in state.available_artifacts are skipped.
 *
 * Intended to be called once at the end of the implementation_scaffold phase by
 * an external subagent that wrote the scaffold files via the Write tool.
 */
export function handleRegisterScaffoldOutputs(
  args: Record<string, unknown>,
  ctx: RegisterScaffoldOutputsContext,
): HandlerResponse {
  const state = ctx.requireRun()
  const activeRunDir = ctx.getActiveRunDir()
  const baseDir = ctx.baseDirFromRunDir(activeRunDir)

  const scaffoldDirArg = args.scaffold_dir as string | undefined
  const phase = (args.phase as string | undefined) ?? 'implementation_scaffold'

  // Resolve scaffold dir: absolute wins, relative resolves against baseDir,
  // omitted defaults to "<baseDir>/scaffold".
  let scaffoldDir: string
  if (scaffoldDirArg === undefined || scaffoldDirArg === '') {
    scaffoldDir = join(baseDir, 'scaffold')
  } else if (isAbsolute(scaffoldDirArg)) {
    scaffoldDir = scaffoldDirArg
  } else {
    scaffoldDir = resolve(baseDir, scaffoldDirArg)
  }

  if (!existsSync(scaffoldDir) || !statSync(scaffoldDir).isDirectory()) {
    throw new PipelineError(
      `Scaffold directory not found: ${scaffoldDir}`,
      'artifact_not_found',
      {
        recovery_action:
          'Verify the scaffold_dir argument points to an existing directory containing scaffold files. ' +
          'By default this tool looks for "<storage.base_dir>/scaffold".',
        details: { scaffold_dir: scaffoldDir },
      },
    )
  }

  // Build a set of already-registered absolute paths for O(1) dedup lookup.
  const alreadyRegistered = new Set(
    state.available_artifacts.map(a => resolve(a.path)),
  )

  const entries = readdirSync(scaffoldDir, { withFileTypes: true })
  const registered: Array<{
    filename: string
    artifact_type: string
    path: string
    version: number
  }> = []
  const skippedExisting: string[] = []
  const anchorWarnings: Array<{
    filename: string
    target_file: string
    missing_anchor: string
  }> = []

  // Project root is one level above baseDir (which is the pipeline_mcp_data dir).
  // We look up proposed-diff target files relative to the project root so that
  // patches that reference files like AGENTS.md can be validated.
  const projectRoot = resolve(baseDir, '..')

  let workingState = state

  for (const entry of entries) {
    if (!entry.isFile()) continue

    const filename = entry.name
    const absPath = resolve(join(scaffoldDir, filename))

    if (alreadyRegistered.has(absPath)) {
      skippedExisting.push(filename)
      continue
    }

    // Anchor validation for proposed-diff files. This is best-effort and
    // advisory — registration always proceeds even if anchors are missing so
    // the reader can choose to fall back to a "create new section" instruction.
    if (filename.endsWith('.proposed-diff.md')) {
      const targetFilename = filename.slice(
        0,
        filename.length - '.proposed-diff.md'.length,
      )
      const targetPath = join(projectRoot, targetFilename)
      if (existsSync(targetPath) && statSync(targetPath).isFile()) {
        try {
          const diffContent = readFileSync(absPath, 'utf-8')
          const targetContent = readFileSync(targetPath, 'utf-8')
          const anchors = extractProposedDiffAnchorRefs(diffContent)
          for (const anchor of anchors) {
            if (!markdownHasHeading(targetContent, anchor)) {
              anchorWarnings.push({
                filename,
                target_file: targetFilename,
                missing_anchor: anchor,
              })
              console.warn(
                `[misc-handlers] scaffold anchor warning: proposed diff "${filename}" references heading "${anchor}" that does not exist in target "${targetFilename}"`,
              )
            }
          }
        } catch (err) {
          console.warn(
            `[misc-handlers] unable to validate scaffold anchors for ${filename}: ${(err as Error).message}`,
          )
        }
      }
    }

    const artifactType = filename === 'scaffold-manifest.json'
      ? 'scaffold-manifest'
      : 'scaffold-document'

    const version = computeNextVersion(
      workingState.available_artifacts,
      artifactType,
      phase,
    )

    const ref: ArtifactRef = {
      type: artifactType,
      path: absPath,
      phase,
      created_at: new Date().toISOString(),
      version,
    }

    workingState = ctx.addArtifact(workingState, ref, activeRunDir)

    // Emit artifact_stored audit event, mirroring artifact-handlers.ts shape.
    appendEvent(activeRunDir, {
      timestamp: new Date().toISOString(),
      event: 'artifact_stored',
      phase,
      run_id: workingState.run_id,
      details: {
        artifact_type: artifactType,
        path: absPath,
        size_bytes: safeSizeBytes(absPath),
      },
    })

    registered.push({
      filename,
      artifact_type: artifactType,
      path: absPath,
      version,
    })
    alreadyRegistered.add(absPath)
  }

  ctx.setActiveRun(workingState)

  const envelope: ResponseEnvelope = {
    status: 'ok',
    data: {
      scaffold_dir: scaffoldDir,
      registered,
      skipped_existing: skippedExisting,
      ...(anchorWarnings.length > 0 ? { anchor_warnings: anchorWarnings } : {}),
    },
    ...(anchorWarnings.length > 0
      ? {
          warnings: anchorWarnings.map(
            (w) =>
              `Proposed diff "${w.filename}" references heading "${w.missing_anchor}" that does not exist in target "${w.target_file}"`,
          ),
        }
      : {}),
    next_step: 'Call pipeline_complete_phase when all scaffold outputs are registered.',
  }
  return { json: JSON.stringify(envelope, null, 2) }
}
