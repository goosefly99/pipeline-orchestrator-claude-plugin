// second-brain-adapter.ts — Live vector adapter for the OS-owned `second_brain` COLD store.
//
// Shells out (read-only) to the second_brain Python CLI to surface semantic recall.
// Hard invariants (SP3 program design):
//   #4 COLD never blocks — any failure/mismatch/unavailable degrades to
//      `provider_not_configured`; `vectorSearch` NEVER throws.
//   #5 ONE store, verifiable — before any query the adapter calls `status --json`
//      and asserts byte-equality of `index_path` (and `embedder_version` when the
//      env var is set) against the configured store. The adapter issues ONLY
//      read-only `status` / `query` — NEVER `build`/`rebuild`, and never creates
//      any directory or collection.

import { execFile } from 'node:child_process'
import type {
  KBProviderAdapter,
  KBClient,
  VectorSearchParams,
  VectorSearchResult,
} from './kb-client.ts'

// ── Frozen SP1 CLI contract (verbatim shapes) ─────────────────

/** `<PY> -m second_brain.cli status --json` payload. */
interface SecondBrainStatus {
  backend?: string
  enabled?: boolean
  embedder_version?: string | null
  dimension?: number
  count?: number
  index_path?: string
  last_build?: string | null
  stale?: boolean
}

/** `<PY> -m second_brain.cli query "<text>" --top-k <N> --json` payload. */
interface SecondBrainQuery {
  status?: 'ok' | 'empty' | 'unavailable'
  results?: unknown[]
  embedder_version?: string | null
  reason?: string
}

/** Result of a single read-only CLI invocation. */
export interface RunCliResult {
  code: number
  stdout: string
  stderr: string
}

/**
 * Spawns `<PY> -m second_brain.cli <args>` and captures its output.
 * Dependency-injectable so tests can pass a fake (no real venv needed).
 */
export type RunCli = (args: string[], timeoutMs: number) => Promise<RunCliResult>

// Subprocess env for stdout hygiene — the CLI emits exactly ONE JSON line on
// stdout; these silence library chatter that would otherwise contaminate it.
const HYGIENE_ENV: Record<string, string> = {
  HF_HUB_DISABLE_PROGRESS_BARS: '1',
  TRANSFORMERS_VERBOSITY: 'error',
  TOKENIZERS_PARALLELISM: 'false',
}

const STATUS_TIMEOUT_MS = 20_000
const QUERY_TIMEOUT_MS = 60_000

/**
 * Default `runCli`: async `execFile` of the second_brain venv python with the
 * `-m second_brain.cli` module entrypoint. Mirrors the capture pattern at
 * hooks.ts:272 (env merge, encoding, timeout) but async + non-throwing on
 * non-zero exit so the adapter can branch on `code`.
 */
export const defaultRunCli: RunCli = (args, timeoutMs) =>
  new Promise<RunCliResult>((resolvePromise) => {
    const py = process.env.SECOND_BRAIN_PYTHON || '/opt/second-brain/bin/python'
    execFile(
      py,
      ['-m', 'second_brain.cli', ...args],
      {
        timeout: timeoutMs,
        env: { ...process.env, ...HYGIENE_ENV },
        encoding: 'utf-8',
        maxBuffer: 8 * 1024 * 1024,
      },
      (err, stdout, stderr) => {
        // execFile only sets err on non-zero exit, signal, or timeout. Never
        // reject — surface the exit code so vectorSearch can degrade cleanly.
        const code =
          err && typeof (err as { code?: unknown }).code === 'number'
            ? (err as { code: number }).code
            : err
              ? 1
              : 0
        resolvePromise({ code, stdout: stdout ?? '', stderr: stderr ?? '' })
      },
    )
  })

// ── Internal helpers ──────────────────────────────────────────

/** Parse the single JSON line the CLI emits on stdout. Returns null on failure. */
function parseJsonLine<T>(stdout: string): T | null {
  const trimmed = stdout.trim()
  if (!trimmed) return null
  // The CLI emits exactly one JSON line; if logs leaked, take the last
  // non-empty line that parses as an object.
  const lines = trimmed.split(/\r?\n/).filter((l) => l.trim().length > 0)
  for (let i = lines.length - 1; i >= 0; i--) {
    try {
      const parsed = JSON.parse(lines[i])
      if (parsed && typeof parsed === 'object') return parsed as T
    } catch {
      // try the next line up
    }
  }
  return null
}

function notConfigured(error: string): VectorSearchResult {
  return { status: 'provider_not_configured', results: [], results_count: 0, error }
}

// ── Adapter factory ───────────────────────────────────────────

/**
 * Build the `vector:second_brain` provider adapter.
 *
 * @param runCli optional injected CLI runner (tests pass a fake; the default
 *   spawns the real venv). The adapter only ever asks `runCli` for `status` and
 *   `query` — it has no code path that runs `build`/`rebuild`.
 */
export function createSecondBrainAdapter(runCli: RunCli = defaultRunCli): KBProviderAdapter {
  return {
    type: 'vector',
    provider: 'second_brain',
    async vectorSearch(_client: KBClient, params: VectorSearchParams): Promise<VectorSearchResult> {
      try {
        // (a) Env gate — no store configured → no spawn at all.
        const indexPath = process.env.SECOND_BRAIN_INDEX_PATH
        if (!indexPath) {
          return notConfigured('SECOND_BRAIN_INDEX_PATH unset')
        }

        // (b) Read-only status probe.
        const statusRun = await runCli(['status', '--json'], STATUS_TIMEOUT_MS)
        if (statusRun.code !== 0) {
          return notConfigured(`second_brain status exited ${statusRun.code}`)
        }
        const status = parseJsonLine<SecondBrainStatus>(statusRun.stdout)
        if (!status) {
          return notConfigured('second_brain status: unparseable stdout')
        }
        if (status.enabled !== true) {
          return notConfigured('second_brain store not enabled')
        }

        // (c) Handshake (invariant #5) — byte-equality before any query.
        if (status.index_path !== indexPath) {
          return notConfigured(
            `second_brain store mismatch: index_path "${status.index_path}" !== SECOND_BRAIN_INDEX_PATH "${indexPath}"`,
          )
        }
        const expectedEmbedder = process.env.SECOND_BRAIN_EMBEDDER_VERSION
        if (expectedEmbedder && status.embedder_version !== expectedEmbedder) {
          return notConfigured(
            `second_brain store mismatch: embedder_version "${status.embedder_version}" !== SECOND_BRAIN_EMBEDDER_VERSION "${expectedEmbedder}"`,
          )
        }

        // (d) Read-only query (positional text arg, not stdin).
        const topK = params.top_k
        const queryRun = await runCli(
          ['query', params.query, '--top-k', String(topK), '--json'],
          QUERY_TIMEOUT_MS,
        )
        if (queryRun.code !== 0) {
          return notConfigured(`second_brain query exited ${queryRun.code}`)
        }
        const queryResult = parseJsonLine<SecondBrainQuery>(queryRun.stdout)
        if (!queryResult) {
          return notConfigured('second_brain query: unparseable stdout')
        }

        if (queryResult.status === 'ok') {
          const results = Array.isArray(queryResult.results) ? queryResult.results : []
          return { status: 'ok', results, results_count: results.length }
        }
        if (queryResult.status === 'empty') {
          return { status: 'ok', results: [], results_count: 0 }
        }
        // 'unavailable' or anything unexpected → degrade.
        return notConfigured(
          `second_brain query unavailable${queryResult.reason ? `: ${queryResult.reason}` : ''}`,
        )
      } catch (err) {
        // (e) Invariant #4 — never throw out of the adapter.
        return notConfigured(
          `second_brain adapter error: ${err instanceof Error ? err.message : String(err)}`,
        )
      }
    },
  }
}
