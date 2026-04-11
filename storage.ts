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
