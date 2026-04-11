import { readFileSync, writeFileSync, mkdirSync, readdirSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import type { StorageConfig } from './types.ts'

function resolveDir(config: StorageConfig, storageKey: string): string {
  const subPath = config.paths[storageKey]
  if (!subPath) {
    throw new Error(`Unknown storage key "${storageKey}". Available: ${Object.keys(config.paths).join(', ')}`)
  }
  return join(config.base_dir, subPath)
}

// ── Per-Run Hierarchical Artifact Directory Helpers (Feature C) ─────
//
// Replaces top-level per-type folders (`pipeline_mcp_data/collections/`,
// `pipeline_mcp_data/debates/`, ...) with per-run trees rooted at
// `pipeline_mcp_data/runs/{sanitized_run_name}-{sanitized_timestamp}/`.
//
// Sanitization rules are deliberately conservative for cross-platform
// (Windows + POSIX) filesystem safety.

/** Subtype path segments for per-run artifact directories. */
export type ArtifactSubtype =
  | 'collections/curated'
  | 'collections/raw'
  | 'debates'
  | 'overviews'
  | 'specs'
  | 'scaffold'

/**
 * Maximum length (in characters) of a sanitized run name. Combined with the
 * 21-character timestamp suffix this keeps the directory name well under
 * common path-length limits even when nested under deep base dirs.
 */
const MAX_SANITIZED_RUN_NAME_LENGTH = 64

/**
 * Sanitize a human-readable run name into a filesystem-safe slug.
 *
 * Rules:
 *   - Replace any character not in `[a-zA-Z0-9-]` with `-`
 *   - Lowercase the result
 *   - Collapse runs of `-` and trim leading/trailing `-`
 *   - Truncate to 64 characters
 *
 * Returns `'unnamed-run'` for inputs that sanitize to an empty string
 * (e.g. all-whitespace, all-punctuation, or non-string input).
 */
export function sanitizeRunName(name: string): string {
  if (typeof name !== 'string' || name.length === 0) {
    return 'unnamed-run'
  }
  const replaced = name.replace(/[^a-zA-Z0-9-]/g, '-').toLowerCase()
  // Collapse runs of '-' so e.g. "fix:type/mismatch" -> "fix--type-mismatch" -> "fix-type-mismatch"
  const collapsed = replaced.replace(/-+/g, '-')
  // Trim leading and trailing hyphens.
  const trimmed = collapsed.replace(/^-+/, '').replace(/-+$/, '')
  if (trimmed.length === 0) {
    return 'unnamed-run'
  }
  // Truncate to MAX_SANITIZED_RUN_NAME_LENGTH then re-trim trailing hyphens
  // in case the cut landed inside a hyphen sequence.
  const truncated = trimmed.slice(0, MAX_SANITIZED_RUN_NAME_LENGTH).replace(/-+$/, '')
  return truncated.length === 0 ? 'unnamed-run' : truncated
}

/**
 * Sanitize an ISO 8601 timestamp into a filesystem-safe suffix.
 *
 * Replaces `:` and `.` with `-` (Windows disallows both in filenames).
 * Other characters are passed through. Non-string inputs return an empty
 * string so callers can detect missing timestamps.
 *
 * Example: `"2026-04-10T09:13:58.123Z"` → `"2026-04-10T09-13-58-123Z"`.
 */
export function sanitizeRunTimestamp(iso: string): string {
  if (typeof iso !== 'string' || iso.length === 0) {
    return ''
  }
  return iso.replace(/[:.]/g, '-')
}

/**
 * Compute the per-run data directory path:
 *   `{baseDir}/runs/{sanitizedRunName}-{sanitizedTimestamp}`
 *
 * If `timestamp` is missing or sanitizes to empty, the suffix is omitted so
 * the directory is `{baseDir}/runs/{sanitizedRunName}`. Callers should always
 * pass both arguments when Feature A parameterization is active.
 */
export function getRunDataDir(baseDir: string, runName: string, timestamp: string): string {
  const safeName = sanitizeRunName(runName)
  const safeTimestamp = sanitizeRunTimestamp(timestamp)
  const dirName = safeTimestamp.length > 0 ? `${safeName}-${safeTimestamp}` : safeName
  return join(baseDir, 'runs', dirName)
}

/**
 * Compute the per-run subdirectory for a given artifact subtype.
 * `runDataDir` should be the value persisted in `RunState.run_data_dir`
 * (typically produced by `getRunDataDir`).
 *
 * Example:
 *   `getArtifactDir('pipeline_mcp_data/runs/my-run-2026', 'collections/curated')`
 *   → `'pipeline_mcp_data/runs/my-run-2026/collections/curated'`
 */
export function getArtifactDir(runDataDir: string, subtype: ArtifactSubtype): string {
  return join(runDataDir, subtype)
}

export function storeArtifact(
  config: StorageConfig,
  storageKey: string,
  fileName: string,
  artifact: unknown,
  force?: boolean,
): string {
  const dir = resolveDir(config, storageKey)
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true })

  const filePath = join(dir, fileName)
  if (existsSync(filePath) && force !== true) {
    throw new Error(`Artifact already exists at "${filePath}". Pass force=true to overwrite.`)
  }
  writeFileSync(filePath, JSON.stringify(artifact, null, 2), 'utf-8')
  return filePath
}

export function loadArtifact(
  config: StorageConfig,
  storageKey: string,
  fileName: string,
): unknown | null {
  const dir = resolveDir(config, storageKey)
  const filePath = join(dir, fileName)

  if (!existsSync(filePath)) return null

  return JSON.parse(readFileSync(filePath, 'utf-8')) as unknown
}

export function listArtifacts(
  config: StorageConfig,
  storageKey: string,
): string[] {
  const dir = resolveDir(config, storageKey)
  if (!existsSync(dir)) return []

  return readdirSync(dir).filter(f => f.endsWith('.json'))
}

export function getArtifactPath(
  config: StorageConfig,
  storageKey: string,
  fileName: string,
): string {
  return join(resolveDir(config, storageKey), fileName)
}

export function fileExists(
  config: StorageConfig,
  storageKey: string,
  fileName: string,
): boolean {
  const dir = resolveDir(config, storageKey)
  return existsSync(join(dir, fileName))
}

export interface ArtifactSummary {
  artifact_type: string
  id: string
  key_count: number
  total_size_bytes: number
  truncated: boolean
  preview_keys: string[]
}

const PREVIEW_KEY_LIMIT = 20

export function buildArtifactSummary(
  artifact: unknown,
  storageKey: string,
  fileName: string,
): ArtifactSummary {
  const json = JSON.stringify(artifact)
  const keys = typeof artifact === 'object' && artifact !== null
    ? Object.keys(artifact as Record<string, unknown>)
    : []
  return {
    artifact_type: storageKey,
    id: fileName,
    key_count: keys.length,
    total_size_bytes: Buffer.byteLength(json, 'utf-8'),
    truncated: keys.length > PREVIEW_KEY_LIMIT,
    preview_keys: keys.slice(0, PREVIEW_KEY_LIMIT),
  }
}
