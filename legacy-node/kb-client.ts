// ── Types ─────────────────────────────────────────────────────

export type KBType = 'vector' | 'sql'
export type KBProvider = 'pinecone' | 'chroma' | 'postgres' | 'sqlite' | string

export interface KBConfig {
  name: string
  type: KBType
  provider: KBProvider
  config: Record<string, unknown>
  sql_templates?: Record<string, string>
}

export interface KBClient {
  name: string
  type: KBType
  provider: KBProvider
  config: Record<string, unknown>
  sql_templates?: Record<string, string>
}

export interface VectorSearchParams {
  run_id: string
  phase: string
  query: string
  top_k: number
  filter?: Record<string, unknown>
  debate_transcript_id?: string
}

export interface VectorSearchResult {
  status: 'ok' | 'provider_not_configured' | 'error'
  results: unknown[]
  results_count: number
  error?: string
}

export interface SQLQueryParams {
  run_id: string
  phase: string
  template_name: string
  parameters: Record<string, unknown>
  debate_transcript_id?: string
}

export interface SQLQueryResult {
  status: 'ok' | 'provider_not_configured' | 'error'
  rows: unknown[]
  rows_count: number
  error?: string
  template_used?: string
  sql_expanded?: string
}

export interface KBQueryLogEntry {
  timestamp: string
  source: string
  query_type: 'vector_search' | 'sql_template' | 'sql_raw'
  query: string
  parameters?: Record<string, unknown>
  results_count: number
  results_used: string[]
  influence?: string
}

export interface KBQueryLog {
  run_id: string
  phase: string
  debate_transcript_id?: string
  queries: KBQueryLogEntry[]
}

// ── Provider Adapter Interface ────────────────────────────────

export interface KBProviderAdapter {
  type: KBType
  provider: KBProvider
  vectorSearch?(client: KBClient, params: VectorSearchParams): Promise<VectorSearchResult>
  sqlQuery?(client: KBClient, params: SQLQueryParams): Promise<SQLQueryResult>
}

// ── In-Memory Stores ──────────────────────────────────────────

/** Keyed by run_id — holds all entries for that run */
const queryLogStore = new Map<string, KBQueryLogEntry[]>()

/** Keyed by "run_id:phase" — index for fast phase-filtered lookups */
const phaseIndex = new Map<string, KBQueryLogEntry[]>()

/** Keyed by "type:provider" — registered real provider adapters */
const adapterRegistry = new Map<string, KBProviderAdapter>()

// ── Internal Helpers ──────────────────────────────────────────

function appendEntry(run_id: string, phase: string, entry: KBQueryLogEntry): void {
  // Append to run-level store
  let runEntries = queryLogStore.get(run_id)
  if (!runEntries) {
    runEntries = []
    queryLogStore.set(run_id, runEntries)
  }
  runEntries.push(entry)

  // Append to phase index
  const phaseKey = `${run_id}:${phase}`
  let phaseEntries = phaseIndex.get(phaseKey)
  if (!phaseEntries) {
    phaseEntries = []
    phaseIndex.set(phaseKey, phaseEntries)
  }
  phaseEntries.push(entry)
}

/** Log a BM25 search query. Called by handleKBSearch in misc-handlers.ts. */
export function appendSearchEntry(run_id: string, phase: string, query: string, results_count: number): void {
  const entry: KBQueryLogEntry = {
    timestamp: new Date().toISOString(),
    source: 'bm25',
    query_type: 'vector_search',
    query,
    results_count,
    results_used: [],
  }
  appendEntry(run_id, phase, entry)
}

// ── Public API ────────────────────────────────────────────────

/** Register a real provider adapter for future use. */
export function registerAdapter(adapter: KBProviderAdapter): void {
  const key = `${adapter.type}:${adapter.provider}`
  adapterRegistry.set(key, adapter)
}

/** Create a KBClient from a KBConfig. */
export function createKBClient(config: KBConfig): KBClient {
  return {
    name: config.name,
    type: config.type,
    provider: config.provider,
    config: config.config,
    sql_templates: config.sql_templates,
  }
}

/** Execute a vector similarity search against a KB client. Always logs the query. */
export async function vectorSearch(
  client: KBClient,
  params: VectorSearchParams,
): Promise<VectorSearchResult> {
  const { run_id, phase, query } = params

  // Type guard: must be a vector KB
  if (client.type !== 'vector') {
    const entry: KBQueryLogEntry = {
      timestamp: new Date().toISOString(),
      source: client.name,
      query_type: 'vector_search',
      query,
      results_count: 0,
      results_used: [],
    }
    appendEntry(run_id, phase, entry)
    return {
      status: 'error',
      results: [],
      results_count: 0,
      error: `"${client.name}" is not a vector KB (type: ${client.type})`,
    }
  }

  let result: VectorSearchResult

  // Try registered adapter
  const adapterKey = `vector:${client.provider}`
  const adapter = adapterRegistry.get(adapterKey)
  if (adapter?.vectorSearch) {
    result = await adapter.vectorSearch(client, params)
  } else {
    // Stub fallback — provider not configured
    result = {
      status: 'provider_not_configured',
      results: [],
      results_count: 0,
      error: `Vector provider "${client.provider}" is not configured. Register a KBProviderAdapter to enable real queries.`,
    }
  }

  // Log the query
  const entry: KBQueryLogEntry = {
    timestamp: new Date().toISOString(),
    source: client.name,
    query_type: 'vector_search',
    query,
    results_count: result.results_count,
    results_used: [],
  }
  appendEntry(run_id, phase, entry)

  return result
}

/** Execute a SQL template query against a KB client. Always logs the query. */
export async function sqlQuery(
  client: KBClient,
  params: SQLQueryParams,
): Promise<SQLQueryResult> {
  const { run_id, phase, template_name, parameters } = params

  // Type guard: must be a sql KB
  if (client.type !== 'sql') {
    const entry: KBQueryLogEntry = {
      timestamp: new Date().toISOString(),
      source: client.name,
      query_type: 'sql_template',
      query: template_name,
      parameters,
      results_count: 0,
      results_used: [],
    }
    appendEntry(run_id, phase, entry)
    return {
      status: 'error',
      rows: [],
      rows_count: 0,
      error: `"${client.name}" is not a sql KB (type: ${client.type})`,
    }
  }

  // Validate template exists (only if templates are defined)
  const templateSql = client.sql_templates?.[template_name]
  if (client.sql_templates !== undefined && templateSql === undefined) {
    const entry: KBQueryLogEntry = {
      timestamp: new Date().toISOString(),
      source: client.name,
      query_type: 'sql_template',
      query: template_name,
      parameters,
      results_count: 0,
      results_used: [],
    }
    appendEntry(run_id, phase, entry)
    return {
      status: 'error',
      rows: [],
      rows_count: 0,
      error: `SQL template "${template_name}" not found in client "${client.name}"`,
    }
  }

  let result: SQLQueryResult

  // Try registered adapter
  const adapterKey = `sql:${client.provider}`
  const adapter = adapterRegistry.get(adapterKey)
  if (adapter?.sqlQuery) {
    result = await adapter.sqlQuery(client, params)
  } else {
    // Stub fallback — provider not configured
    result = {
      status: 'provider_not_configured',
      rows: [],
      rows_count: 0,
      error: `SQL provider "${client.provider}" is not configured. Register a KBProviderAdapter to enable real queries.`,
      template_used: templateSql,
    }
  }

  // Log the query
  const entry: KBQueryLogEntry = {
    timestamp: new Date().toISOString(),
    source: client.name,
    query_type: 'sql_template',
    query: template_name,
    parameters,
    results_count: result.rows_count,
    results_used: [],
  }
  appendEntry(run_id, phase, entry)

  return result
}

/**
 * Retrieve query log entries for a run, optionally filtered by phase.
 * Returns [] for unknown run_id or unknown phase.
 */
export function getQueryLog(run_id: string, phase?: string): KBQueryLogEntry[] {
  if (phase !== undefined) {
    return phaseIndex.get(`${run_id}:${phase}`) ?? []
  }
  return queryLogStore.get(run_id) ?? []
}

/** Clear all query log entries from all in-memory stores. */
export function clearQueryLog(): void {
  queryLogStore.clear()
  phaseIndex.clear()
}

/**
 * Export the query log for a run+phase in the kb-query-log schema format.
 * If debate_transcript_id is provided, it is included in the export.
 */
export function exportQueryLog(
  run_id: string,
  phase: string,
  debate_transcript_id?: string,
): KBQueryLog {
  const queries = getQueryLog(run_id, phase)
  const log: KBQueryLog = { run_id, phase, queries }
  if (debate_transcript_id !== undefined) {
    log.debate_transcript_id = debate_transcript_id
  }
  return log
}
