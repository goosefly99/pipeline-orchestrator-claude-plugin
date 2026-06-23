import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'

import { createSecondBrainAdapter, type RunCli, type RunCliResult } from '../second-brain-adapter.ts'
import type { KBClient, VectorSearchParams } from '../kb-client.ts'

// ── Fixtures ──────────────────────────────────────────────────

const FIXTURE_INDEX_PATH = '/data/vault/index'
const FIXTURE_EMBEDDER = 'sentence-transformers/all-MiniLM-L6-v2@hf-fp32'

const client: KBClient = {
  name: 'second_brain',
  type: 'vector',
  provider: 'second_brain',
  config: {},
}

const params: VectorSearchParams = {
  run_id: 'run-sb-001',
  phase: 'discovery',
  query: 'how is config resolved',
  top_k: 5,
}

function jsonLine(obj: unknown): string {
  return JSON.stringify(obj) + '\n'
}

/**
 * Build a fake runCli that records every invocation's args and returns a
 * scripted RunCliResult keyed by subcommand (`status` / `query`). No real
 * process is ever spawned — there is NO second_brain venv on this host.
 */
function makeFakeRunCli(scripted: {
  status?: RunCliResult
  query?: RunCliResult
}): { runCli: RunCli; calls: string[][] } {
  const calls: string[][] = []
  const runCli: RunCli = async (args) => {
    calls.push(args)
    const sub = args[0]
    if (sub === 'status' && scripted.status) return scripted.status
    if (sub === 'query' && scripted.query) return scripted.query
    // Any other subcommand (e.g. build/rebuild) is a hard test failure —
    // the adapter must NEVER issue one.
    throw new Error(`unexpected CLI subcommand in fake: ${sub}`)
  }
  return { runCli, calls }
}

// Snapshot + restore the env vars the adapter reads so tests don't leak state.
let savedIndexPath: string | undefined
let savedEmbedder: string | undefined

beforeEach(() => {
  savedIndexPath = process.env.SECOND_BRAIN_INDEX_PATH
  savedEmbedder = process.env.SECOND_BRAIN_EMBEDDER_VERSION
  delete process.env.SECOND_BRAIN_INDEX_PATH
  delete process.env.SECOND_BRAIN_EMBEDDER_VERSION
})

afterEach(() => {
  if (savedIndexPath === undefined) delete process.env.SECOND_BRAIN_INDEX_PATH
  else process.env.SECOND_BRAIN_INDEX_PATH = savedIndexPath
  if (savedEmbedder === undefined) delete process.env.SECOND_BRAIN_EMBEDDER_VERSION
  else process.env.SECOND_BRAIN_EMBEDDER_VERSION = savedEmbedder
})

// ── Adapter shape ─────────────────────────────────────────────

describe('createSecondBrainAdapter', () => {
  it('declares the vector:second_brain provider key', () => {
    const adapter = createSecondBrainAdapter(async () => ({ code: 0, stdout: '', stderr: '' }))
    assert.equal(adapter.type, 'vector')
    assert.equal(adapter.provider, 'second_brain')
    assert.equal(typeof adapter.vectorSearch, 'function')
  })
})

// ── vectorSearch contract ─────────────────────────────────────

describe('second_brain adapter vectorSearch', () => {
  it('handshake match → ok: maps results, calls status then query, never build/rebuild', async () => {
    process.env.SECOND_BRAIN_INDEX_PATH = FIXTURE_INDEX_PATH
    const hits = [
      { path: '/data/vault/wiki/a.md', title: 'A', score: 0.91, snippet: 'alpha' },
      { path: '/data/vault/wiki/b.md', title: 'B', score: 0.77, snippet: 'beta' },
    ]
    const { runCli, calls } = makeFakeRunCli({
      status: {
        code: 0,
        stdout: jsonLine({
          backend: 'sentence-transformers',
          enabled: true,
          embedder_version: FIXTURE_EMBEDDER,
          dimension: 384,
          count: 42,
          index_path: FIXTURE_INDEX_PATH,
          last_build: '2026-06-20T00:00:00Z',
          stale: false,
        }),
        stderr: 'loading model...\n',
      },
      query: {
        code: 0,
        stdout: jsonLine({ status: 'ok', results: hits, embedder_version: FIXTURE_EMBEDDER }),
        stderr: '',
      },
    })
    const adapter = createSecondBrainAdapter(runCli)
    const result = await adapter.vectorSearch!(client, params)

    assert.equal(result.status, 'ok')
    assert.equal(result.results_count, 2)
    assert.deepEqual(result.results, hits)

    // status issued first, then query — and NOTHING else.
    assert.equal(calls.length, 2)
    assert.equal(calls[0][0], 'status')
    assert.equal(calls[1][0], 'query')
    assert.equal(calls[1][1], params.query) // positional query text
    assert.ok(calls[1].includes('--top-k'))
    assert.ok(calls[1].includes('5'))
    const allArgs = calls.flat()
    assert.ok(!allArgs.includes('build'), 'must never issue build')
    assert.ok(!allArgs.includes('rebuild'), 'must never issue rebuild')
  })

  it("status:'empty' → ok with zero results", async () => {
    process.env.SECOND_BRAIN_INDEX_PATH = FIXTURE_INDEX_PATH
    const { runCli } = makeFakeRunCli({
      status: {
        code: 0,
        stdout: jsonLine({ enabled: true, index_path: FIXTURE_INDEX_PATH, embedder_version: FIXTURE_EMBEDDER }),
        stderr: '',
      },
      query: { code: 0, stdout: jsonLine({ status: 'empty', results: [], embedder_version: FIXTURE_EMBEDDER }), stderr: '' },
    })
    const result = await createSecondBrainAdapter(runCli).vectorSearch!(client, params)
    assert.equal(result.status, 'ok')
    assert.equal(result.results_count, 0)
    assert.deepEqual(result.results, [])
  })

  it('index_path mismatch → provider_not_configured, no query issued', async () => {
    process.env.SECOND_BRAIN_INDEX_PATH = FIXTURE_INDEX_PATH
    const { runCli, calls } = makeFakeRunCli({
      status: {
        code: 0,
        stdout: jsonLine({ enabled: true, index_path: '/some/other/path', embedder_version: FIXTURE_EMBEDDER }),
        stderr: '',
      },
    })
    const result = await createSecondBrainAdapter(runCli).vectorSearch!(client, params)
    assert.equal(result.status, 'provider_not_configured')
    assert.equal(result.results_count, 0)
    assert.ok(result.error && result.error.includes('mismatch'))
    // Only status was issued — no query after a failed handshake.
    assert.equal(calls.length, 1)
    assert.equal(calls[0][0], 'status')
  })

  it('embedder_version mismatch (env set) → provider_not_configured, no query issued', async () => {
    process.env.SECOND_BRAIN_INDEX_PATH = FIXTURE_INDEX_PATH
    process.env.SECOND_BRAIN_EMBEDDER_VERSION = FIXTURE_EMBEDDER
    const { runCli, calls } = makeFakeRunCli({
      status: {
        code: 0,
        stdout: jsonLine({ enabled: true, index_path: FIXTURE_INDEX_PATH, embedder_version: 'some-other-embedder@v2' }),
        stderr: '',
      },
    })
    const result = await createSecondBrainAdapter(runCli).vectorSearch!(client, params)
    assert.equal(result.status, 'provider_not_configured')
    assert.ok(result.error && result.error.includes('embedder_version'))
    assert.equal(calls.length, 1)
    assert.equal(calls[0][0], 'status')
  })

  it("query status:'unavailable' → provider_not_configured", async () => {
    process.env.SECOND_BRAIN_INDEX_PATH = FIXTURE_INDEX_PATH
    const { runCli } = makeFakeRunCli({
      status: {
        code: 0,
        stdout: jsonLine({ enabled: true, index_path: FIXTURE_INDEX_PATH, embedder_version: FIXTURE_EMBEDDER }),
        stderr: '',
      },
      query: { code: 0, stdout: jsonLine({ status: 'unavailable', reason: 'index not built', results: [] }), stderr: '' },
    })
    const result = await createSecondBrainAdapter(runCli).vectorSearch!(client, params)
    assert.equal(result.status, 'provider_not_configured')
    assert.equal(result.results_count, 0)
    assert.ok(result.error && result.error.includes('unavailable'))
  })

  it('enabled !== true → provider_not_configured, no query issued', async () => {
    process.env.SECOND_BRAIN_INDEX_PATH = FIXTURE_INDEX_PATH
    const { runCli, calls } = makeFakeRunCli({
      status: { code: 0, stdout: jsonLine({ enabled: false, index_path: FIXTURE_INDEX_PATH }), stderr: '' },
    })
    const result = await createSecondBrainAdapter(runCli).vectorSearch!(client, params)
    assert.equal(result.status, 'provider_not_configured')
    assert.equal(calls.length, 1)
  })

  it('non-zero exit on status → provider_not_configured (no throw)', async () => {
    process.env.SECOND_BRAIN_INDEX_PATH = FIXTURE_INDEX_PATH
    const { runCli, calls } = makeFakeRunCli({
      status: { code: 2, stdout: '', stderr: 'Traceback...\n' },
    })
    const result = await createSecondBrainAdapter(runCli).vectorSearch!(client, params)
    assert.equal(result.status, 'provider_not_configured')
    assert.equal(result.results_count, 0)
    assert.equal(calls.length, 1)
  })

  it('garbage stdout on status → provider_not_configured (no throw)', async () => {
    process.env.SECOND_BRAIN_INDEX_PATH = FIXTURE_INDEX_PATH
    const { runCli } = makeFakeRunCli({
      status: { code: 0, stdout: 'not json at all <<<\n', stderr: '' },
    })
    const result = await createSecondBrainAdapter(runCli).vectorSearch!(client, params)
    assert.equal(result.status, 'provider_not_configured')
    assert.equal(result.results_count, 0)
  })

  it('garbage stdout on query → provider_not_configured (no throw)', async () => {
    process.env.SECOND_BRAIN_INDEX_PATH = FIXTURE_INDEX_PATH
    const { runCli } = makeFakeRunCli({
      status: {
        code: 0,
        stdout: jsonLine({ enabled: true, index_path: FIXTURE_INDEX_PATH, embedder_version: FIXTURE_EMBEDDER }),
        stderr: '',
      },
      query: { code: 0, stdout: '!!! totally broken', stderr: '' },
    })
    const result = await createSecondBrainAdapter(runCli).vectorSearch!(client, params)
    assert.equal(result.status, 'provider_not_configured')
  })

  it('runCli that throws → provider_not_configured (never throws out)', async () => {
    process.env.SECOND_BRAIN_INDEX_PATH = FIXTURE_INDEX_PATH
    const runCli: RunCli = async () => {
      throw new Error('spawn ENOENT')
    }
    const result = await createSecondBrainAdapter(runCli).vectorSearch!(client, params)
    assert.equal(result.status, 'provider_not_configured')
    assert.ok(result.error && result.error.includes('adapter error'))
  })

  it('env unset → provider_not_configured without spawning anything', async () => {
    // SECOND_BRAIN_INDEX_PATH is deleted in beforeEach.
    let spawned = false
    const runCli: RunCli = async () => {
      spawned = true
      return { code: 0, stdout: '', stderr: '' }
    }
    const result = await createSecondBrainAdapter(runCli).vectorSearch!(client, params)
    assert.equal(result.status, 'provider_not_configured')
    assert.equal(result.error, 'SECOND_BRAIN_INDEX_PATH unset')
    assert.equal(spawned, false, 'must not spawn when env is unset')
  })
})
