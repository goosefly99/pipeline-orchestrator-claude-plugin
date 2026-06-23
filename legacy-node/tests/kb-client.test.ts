import { describe, it, before, beforeEach } from 'node:test'
import assert from 'node:assert/strict'

import {
  createKBClient,
  vectorSearch,
  sqlQuery,
  getQueryLog,
  clearQueryLog,
  exportQueryLog,
  type KBConfig,
  type VectorSearchResult,
  type SQLQueryResult,
  type KBQueryLogEntry,
} from '../kb-client.ts'

// ── Fixtures ──────────────────────────────────────────────────

const vectorConfig: KBConfig = {
  name: 'strategy-vectors',
  type: 'vector',
  provider: 'pinecone',
  config: { index: 'kalshi-research', dimensions: 1536 },
}

const sqlConfig: KBConfig = {
  name: 'market-db',
  type: 'sql',
  provider: 'postgres',
  config: { host: 'localhost', port: 5432, database: 'kalshi' },
  sql_templates: {
    get_contract_history: 'SELECT * FROM contracts WHERE ticker = :ticker',
    list_open_markets: 'SELECT * FROM markets WHERE status = \'open\'',
  },
}

const chromaConfig: KBConfig = {
  name: 'chroma-store',
  type: 'vector',
  provider: 'chroma',
  config: { collection: 'research-docs' },
}

const sqliteConfig: KBConfig = {
  name: 'local-db',
  type: 'sql',
  provider: 'sqlite',
  config: { path: '/tmp/local.db' },
  sql_templates: {
    get_cached_prices: 'SELECT * FROM prices WHERE date = :date',
  },
}

// ── createKBClient ────────────────────────────────────────────

describe('createKBClient', () => {
  it('creates a vector client from config', () => {
    const client = createKBClient(vectorConfig)
    assert.equal(client.name, 'strategy-vectors')
    assert.equal(client.type, 'vector')
    assert.equal(client.provider, 'pinecone')
    assert.deepEqual(client.config, vectorConfig.config)
  })

  it('creates a sql client with templates', () => {
    const client = createKBClient(sqlConfig)
    assert.equal(client.name, 'market-db')
    assert.equal(client.type, 'sql')
    assert.equal(client.provider, 'postgres')
    assert.ok(client.sql_templates)
    assert.ok('get_contract_history' in client.sql_templates)
    assert.ok('list_open_markets' in client.sql_templates)
  })

  it('creates a chroma vector client', () => {
    const client = createKBClient(chromaConfig)
    assert.equal(client.provider, 'chroma')
    assert.equal(client.type, 'vector')
  })

  it('creates a sqlite sql client', () => {
    const client = createKBClient(sqliteConfig)
    assert.equal(client.provider, 'sqlite')
    assert.equal(client.type, 'sql')
  })

  it('preserves sql_templates as undefined for vector clients', () => {
    const client = createKBClient(vectorConfig)
    assert.equal(client.sql_templates, undefined)
  })
})

// ── vectorSearch ──────────────────────────────────────────────

describe('vectorSearch', () => {
  beforeEach(() => { clearQueryLog() })

  it('returns provider_not_configured for pinecone stub', async () => {
    const client = createKBClient(vectorConfig)
    const result = await vectorSearch(client, {
      run_id: 'run-001',
      phase: 'discovery',
      query: 'kalshi prediction market volume trends',
      top_k: 5,
    })

    assert.equal(result.status, 'provider_not_configured')
    assert.ok(result.error)
    assert.ok(result.error.toLowerCase().includes('pinecone'))
    assert.equal(result.results.length, 0)
    assert.equal(result.results_count, 0)
  })

  it('returns provider_not_configured for chroma stub', async () => {
    const client = createKBClient(chromaConfig)
    const result = await vectorSearch(client, {
      run_id: 'run-002',
      phase: 'curation',
      query: 'market sentiment analysis',
      top_k: 3,
    })

    assert.equal(result.status, 'provider_not_configured')
    assert.ok(result.error)
    assert.ok(result.error.toLowerCase().includes('chroma'))
    assert.equal(result.results_count, 0)
  })

  it('returns error when called on a sql client (not a vector KB)', async () => {
    const client = createKBClient(sqlConfig)
    const result = await vectorSearch(client, {
      run_id: 'run-003',
      phase: 'discovery',
      query: 'some query',
      top_k: 5,
    })

    assert.equal(result.status, 'error')
    assert.ok(result.error)
    assert.ok(result.error.toLowerCase().includes('not a vector'))
  })

  it('logs the query after a vector search', async () => {
    const client = createKBClient(vectorConfig)
    await vectorSearch(client, {
      run_id: 'run-log-001',
      phase: 'synthesis',
      query: 'election market liquidity',
      top_k: 10,
    })

    const entries = getQueryLog('run-log-001', 'synthesis')
    assert.equal(entries.length, 1)
    assert.equal(entries[0].source, 'strategy-vectors')
    assert.equal(entries[0].query_type, 'vector_search')
    assert.equal(entries[0].query, 'election market liquidity')
    assert.equal(entries[0].results_count, 0)
  })

  it('logs query even when wrong type (sql client used for vector search)', async () => {
    const client = createKBClient(sqlConfig)
    await vectorSearch(client, {
      run_id: 'run-log-type-error',
      phase: 'discovery',
      query: 'query that fails',
      top_k: 5,
    })

    const entries = getQueryLog('run-log-type-error', 'discovery')
    assert.equal(entries.length, 1)
    assert.equal(entries[0].results_count, 0)
  })

  it('accepts optional debate_transcript_id and top_k', async () => {
    const client = createKBClient(vectorConfig)
    const result = await vectorSearch(client, {
      run_id: 'run-debate',
      phase: 'debate',
      query: 'contract YES probability',
      top_k: 3,
      debate_transcript_id: 'debate-abc-123',
    })

    assert.equal(result.status, 'provider_not_configured')
    assert.equal(result.results_count, 0)
  })
})

// ── sqlQuery ──────────────────────────────────────────────────

describe('sqlQuery', () => {
  beforeEach(() => { clearQueryLog() })

  it('returns provider_not_configured for postgres stub', async () => {
    const client = createKBClient(sqlConfig)
    const result = await sqlQuery(client, {
      run_id: 'run-sql-001',
      phase: 'synthesis',
      template_name: 'get_contract_history',
      parameters: { ticker: 'KXBTC-23DEC31' },
    })

    assert.equal(result.status, 'provider_not_configured')
    assert.ok(result.error)
    assert.ok(result.error.toLowerCase().includes('postgres'))
    assert.equal(result.rows.length, 0)
    assert.equal(result.rows_count, 0)
  })

  it('returns provider_not_configured for sqlite stub', async () => {
    const client = createKBClient(sqliteConfig)
    const result = await sqlQuery(client, {
      run_id: 'run-sql-002',
      phase: 'curation',
      template_name: 'get_cached_prices',
      parameters: { date: '2026-01-15' },
    })

    assert.equal(result.status, 'provider_not_configured')
    assert.ok(result.error)
    assert.ok(result.error.toLowerCase().includes('sqlite'))
    assert.equal(result.rows_count, 0)
  })

  it('returns error for unknown template name', async () => {
    const client = createKBClient(sqlConfig)
    const result = await sqlQuery(client, {
      run_id: 'run-sql-003',
      phase: 'discovery',
      template_name: 'nonexistent_template',
      parameters: {},
    })

    assert.equal(result.status, 'error')
    assert.ok(result.error)
    assert.ok(result.error.includes('nonexistent_template'))
  })

  it('returns error when called on a vector client (not a sql KB)', async () => {
    const client = createKBClient(vectorConfig)
    const result = await sqlQuery(client, {
      run_id: 'run-sql-004',
      phase: 'discovery',
      template_name: 'get_contract_history',
      parameters: {},
    })

    assert.equal(result.status, 'error')
    assert.ok(result.error)
    assert.ok(result.error.toLowerCase().includes('not a sql'))
  })

  it('logs the query after a sql query', async () => {
    const client = createKBClient(sqlConfig)
    await sqlQuery(client, {
      run_id: 'run-sql-log',
      phase: 'synthesis',
      template_name: 'list_open_markets',
      parameters: {},
    })

    const entries = getQueryLog('run-sql-log', 'synthesis')
    assert.equal(entries.length, 1)
    assert.equal(entries[0].source, 'market-db')
    assert.equal(entries[0].query_type, 'sql_template')
    assert.equal(entries[0].query, 'list_open_markets')
    assert.equal(entries[0].results_count, 0)
  })

  it('logs query even when type error (vector client for sql query)', async () => {
    const client = createKBClient(vectorConfig)
    await sqlQuery(client, {
      run_id: 'run-sql-type-err',
      phase: 'discovery',
      template_name: 'some_template',
      parameters: {},
    })

    const entries = getQueryLog('run-sql-type-err', 'discovery')
    assert.equal(entries.length, 1)
    assert.equal(entries[0].results_count, 0)
  })

  it('logs query even when template unknown', async () => {
    const client = createKBClient(sqlConfig)
    await sqlQuery(client, {
      run_id: 'run-sql-tmpl-err',
      phase: 'curation',
      template_name: 'bad_template',
      parameters: { x: 1 },
    })

    const entries = getQueryLog('run-sql-tmpl-err', 'curation')
    assert.equal(entries.length, 1)
    assert.deepEqual(entries[0].parameters, { x: 1 })
  })

  it('includes template_used in result for valid template', async () => {
    const client = createKBClient(sqlConfig)
    const result = await sqlQuery(client, {
      run_id: 'run-sql-tmpl-used',
      phase: 'curation',
      template_name: 'get_contract_history',
      parameters: { ticker: 'KXBTC' },
    })

    // Even though provider not configured, template was found
    assert.equal(result.status, 'provider_not_configured')
    assert.ok(result.template_used)
    assert.ok(result.template_used.includes('contracts'))
  })
})

// ── getQueryLog ───────────────────────────────────────────────

describe('getQueryLog', () => {
  beforeEach(() => { clearQueryLog() })

  it('returns all entries for a run_id when no phase filter', async () => {
    const client = createKBClient(vectorConfig)
    await vectorSearch(client, { run_id: 'run-get-log', phase: 'discovery', query: 'q1', top_k: 5 })
    await vectorSearch(client, { run_id: 'run-get-log', phase: 'synthesis', query: 'q2', top_k: 5 })
    await vectorSearch(client, { run_id: 'run-get-log', phase: 'synthesis', query: 'q3', top_k: 3 })

    const all = getQueryLog('run-get-log')
    assert.equal(all.length, 3)
  })

  it('filters by phase when phase is provided', async () => {
    const client = createKBClient(vectorConfig)
    await vectorSearch(client, { run_id: 'run-filter', phase: 'discovery', query: 'q1', top_k: 5 })
    await vectorSearch(client, { run_id: 'run-filter', phase: 'synthesis', query: 'q2', top_k: 5 })
    await vectorSearch(client, { run_id: 'run-filter', phase: 'synthesis', query: 'q3', top_k: 3 })

    const synthesis = getQueryLog('run-filter', 'synthesis')
    assert.equal(synthesis.length, 2)

    const discovery = getQueryLog('run-filter', 'discovery')
    assert.equal(discovery.length, 1)
  })

  it('returns empty array for unknown run_id', () => {
    const entries = getQueryLog('unknown-run-xyz')
    assert.deepEqual(entries, [])
  })

  it('returns empty array for known run but unknown phase', async () => {
    const client = createKBClient(vectorConfig)
    await vectorSearch(client, { run_id: 'run-phase-miss', phase: 'discovery', query: 'q', top_k: 5 })

    const entries = getQueryLog('run-phase-miss', 'nonexistent-phase')
    assert.deepEqual(entries, [])
  })

  it('each log entry has required fields', async () => {
    const client = createKBClient(vectorConfig)
    await vectorSearch(client, {
      run_id: 'run-fields',
      phase: 'discovery',
      query: 'field check query',
      top_k: 5,
    })

    const entries = getQueryLog('run-fields', 'discovery')
    const entry = entries[0]

    assert.ok(typeof entry.timestamp === 'string')
    assert.ok(entry.timestamp.length > 0)
    assert.ok(typeof entry.source === 'string')
    assert.ok(typeof entry.query_type === 'string')
    assert.ok(typeof entry.query === 'string')
    assert.ok(typeof entry.results_count === 'number')
    assert.ok(Array.isArray(entry.results_used))
  })
})

// ── clearQueryLog ─────────────────────────────────────────────

describe('clearQueryLog', () => {
  it('removes all entries across all runs', async () => {
    const client = createKBClient(vectorConfig)
    await vectorSearch(client, { run_id: 'run-a', phase: 'discovery', query: 'q1', top_k: 5 })
    await vectorSearch(client, { run_id: 'run-b', phase: 'synthesis', query: 'q2', top_k: 5 })

    clearQueryLog()

    assert.deepEqual(getQueryLog('run-a'), [])
    assert.deepEqual(getQueryLog('run-b'), [])
  })

  it('is safe to call on an already-empty log', () => {
    clearQueryLog()
    assert.doesNotThrow(() => clearQueryLog())
    assert.deepEqual(getQueryLog('any-run'), [])
  })
})

// ── exportQueryLog ────────────────────────────────────────────

describe('exportQueryLog', () => {
  beforeEach(() => { clearQueryLog() })

  it('exports in kb-query-log schema format', async () => {
    const client = createKBClient(vectorConfig)
    await vectorSearch(client, {
      run_id: 'run-export',
      phase: 'discovery',
      query: 'kalshi volume trends',
      top_k: 5,
    })

    const log = exportQueryLog('run-export', 'discovery')
    assert.equal(log.run_id, 'run-export')
    assert.equal(log.phase, 'discovery')
    assert.ok(Array.isArray(log.queries))
    assert.equal(log.queries.length, 1)
  })

  it('includes all queries for the phase', async () => {
    const client = createKBClient(vectorConfig)
    const sqlClient = createKBClient(sqlConfig)

    await vectorSearch(client, { run_id: 'run-exp2', phase: 'synthesis', query: 'q1', top_k: 5 })
    await sqlQuery(sqlClient, { run_id: 'run-exp2', phase: 'synthesis', template_name: 'get_contract_history', parameters: { ticker: 'X' } })
    await vectorSearch(client, { run_id: 'run-exp2', phase: 'discovery', query: 'q3', top_k: 3 })

    const log = exportQueryLog('run-exp2', 'synthesis')
    assert.equal(log.queries.length, 2)
  })

  it('exported queries have required schema fields', async () => {
    const client = createKBClient(vectorConfig)
    await vectorSearch(client, {
      run_id: 'run-exp3',
      phase: 'curation',
      query: 'test query for export',
      top_k: 5,
    })

    const log = exportQueryLog('run-exp3', 'curation')
    const q = log.queries[0]

    assert.ok(typeof q.timestamp === 'string')
    assert.ok(typeof q.source === 'string')
    assert.ok(typeof q.query_type === 'string')
    assert.ok(Array.isArray(q.results_used))
  })

  it('includes debate_transcript_id when provided', async () => {
    const client = createKBClient(vectorConfig)
    await vectorSearch(client, {
      run_id: 'run-debate-exp',
      phase: 'debate',
      query: 'debate query',
      top_k: 5,
      debate_transcript_id: 'debate-xyz-999',
    })

    const log = exportQueryLog('run-debate-exp', 'debate', 'debate-xyz-999')
    assert.equal(log.debate_transcript_id, 'debate-xyz-999')
    assert.equal(log.queries.length, 1)
  })

  it('returns empty queries array for unknown run', () => {
    const log = exportQueryLog('no-such-run', 'discovery')
    assert.equal(log.run_id, 'no-such-run')
    assert.equal(log.phase, 'discovery')
    assert.deepEqual(log.queries, [])
  })
})
