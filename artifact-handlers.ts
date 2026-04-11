// artifact-handlers.ts — artifact tool handler logic extracted from server.ts

import { existsSync, readFileSync, statSync } from 'node:fs'
import { basename, isAbsolute, join, normalize, resolve } from 'node:path'
import { PipelineError } from './types.ts'
import type { RunState, StorageConfig, ResponseEnvelope, ArtifactRef, HandlerResponse } from './types.ts'
import type { SchemaMap, ValidationResult } from './validator.ts'
import type { ArtifactSummary } from './storage.ts'
import { appendEvent } from './lifecycle-handlers.ts'

// ── Helper: safe file size ────────────────────────────────────

/**
 * Read file size in bytes. If statSync throws (file was moved/deleted between
 * write and event emission), log a warning and return 0 rather than swallowing
 * the exception. This keeps the event log authoritative even under races.
 */
function safeSizeBytes(path: string): number {
  try {
    return statSync(path).size
  } catch (err) {
    console.warn(
      `[artifact-handlers] unable to stat artifact for size_bytes: ${path} — ${(err as Error).message}`,
    )
    return 0
  }
}

// ── Context provided by server.ts ────────────────────────────

export interface ArtifactContext {
  getSchemas(): SchemaMap
  validateArtifact(schemas: SchemaMap, schemaFile: string, artifact: unknown): ValidationResult
  storeArtifact(config: StorageConfig, key: string, name: string, artifact: unknown, force?: boolean): string
  loadArtifact(config: StorageConfig, key: string, name: string): unknown | null
  listArtifacts(config: StorageConfig, key: string): string[]
  buildArtifactSummary(artifact: unknown, storageKey: string, fileName: string): ArtifactSummary
  getStorageConfig(baseOverride?: string): StorageConfig
  baseDirFromRunDir(runDir: string): string
  getProjectRoot(): string
  getConfigStorageBaseDir(): string
  getActiveRun(): RunState | null
  setActiveRun(state: RunState): void
  getActiveRunDir(): string
  requireRun(): RunState
  addArtifact(state: RunState, ref: ArtifactRef, stateDir: string): RunState
}

// Re-export HandlerResponse for backward compatibility
export type { HandlerResponse } from './types.ts'

// ── Helpers ──────────────────────────────────────────────────

function resolveFilePath(filePath: string, baseDir: string): string {
  if (isAbsolute(filePath)) return filePath
  return resolve(baseDir, filePath)
}

/**
 * Compute the next version number for an artifact of a given type in a given phase.
 * Scans `available_artifacts` for existing artifacts matching both `type` and `phase`,
 * then returns max(existing versions) + 1. Returns 1 if no duplicates exist.
 */
export function computeNextVersion(
  availableArtifacts: ArtifactRef[],
  artifactType: string,
  phase: string,
): number {
  const matches = availableArtifacts.filter(
    a => a.type === artifactType && a.phase === phase,
  )
  if (matches.length === 0) return 1
  const maxVersion = Math.max(...matches.map(a => a.version ?? 1))
  return maxVersion + 1
}

/**
 * Enforce response size limits. If the response exceeds maxChars,
 * truncate it and add a continuation_ref pointing to the full artifact.
 */
export function enforceResponseSize(
  responseJson: string,
  maxChars: number,
  continuationRef?: { storage_key: string; file_name: string },
): string {
  if (responseJson.length <= maxChars) return responseJson

  if (continuationRef) {
    const truncated = {
      truncated: true,
      preview_length: maxChars,
      full_length: responseJson.length,
      message: `Response truncated to ${maxChars} chars. Use pipeline_load_artifact with inline=true to retrieve full content.`,
      continuation_ref: continuationRef,
    }
    return JSON.stringify(truncated, null, 2)
  }

  // No continuation ref — just truncate with a message
  return responseJson.slice(0, maxChars) + '\n... (truncated)'
}

// ── Handlers ─────────────────────────────────────────────────

export function handleValidateArtifact(args: Record<string, unknown>, ctx: ArtifactContext): HandlerResponse {
  const schemaFile = args.schema as string
  const artifact = args.artifact as unknown
  const filePath = args.file_path as string | undefined

  if (!schemaFile) {
    throw new PipelineError('schema is required', 'validation_error', {
      recovery_action: 'Provide the schema parameter with the name of a JSON Schema file.',
    })
  }
  if (!artifact && !filePath) {
    throw new PipelineError('Either artifact (inline JSON) or file_path must be provided', 'validation_error', {
      recovery_action: 'Provide either an inline artifact object or a file_path to a JSON file.',
    })
  }

  let resolvedArtifact: unknown
  if (filePath) {
    const activeRunDir = ctx.getActiveRunDir()
    const baseDir = ctx.getActiveRun()
      ? ctx.baseDirFromRunDir(activeRunDir)
      : resolve(ctx.getProjectRoot(), ctx.getConfigStorageBaseDir())
    const resolved = resolveFilePath(filePath, baseDir)
    if (!existsSync(resolved)) {
      throw new PipelineError(`File not found: ${resolved}`, 'artifact_not_found', {
        recovery_action: 'Verify the file_path exists relative to the run directory.',
        details: { path: resolved },
      })
    }
    resolvedArtifact = JSON.parse(readFileSync(resolved, 'utf-8')) as unknown
  } else {
    resolvedArtifact = artifact
  }

  const result = ctx.validateArtifact(ctx.getSchemas(), schemaFile, resolvedArtifact)

  if (result.valid) {
    const envelope: ResponseEnvelope = {
      status: 'ok',
      data: { schema: schemaFile, valid: true },
      next_step: 'Call pipeline_store_artifact to persist the validated artifact.',
    }
    return { json: JSON.stringify(envelope, null, 2) }
  }
  return { json: `Validation FAILED against ${schemaFile}:\n${result.errors.join('\n')}`, isError: true }
}

export function handleStoreArtifact(args: Record<string, unknown>, ctx: ArtifactContext): HandlerResponse {
  const state = ctx.requireRun()
  const storageKey = args.storage_key as string
  const fileName = args.file_name as string
  const artifact = args.artifact as unknown
  const filePath = args.file_path as string | undefined
  const artifactType = args.artifact_type as string
  const phase = args.phase as string
  const force = args.force as boolean | undefined
  const parentArtifact = args.parent_artifact as string | undefined

  if (!storageKey || !fileName || !artifactType || !phase) {
    throw new PipelineError('storage_key, file_name, artifact_type, and phase are all required', 'validation_error', {
      recovery_action: 'Provide all required parameters: storage_key, file_name, artifact_type, and phase.',
    })
  }
  if (!artifact && !filePath) {
    throw new PipelineError('Either artifact (inline JSON) or file_path must be provided', 'validation_error', {
      recovery_action: 'Provide either an inline artifact object or a file_path to a JSON file.',
    })
  }

  const activeRunDir = ctx.getActiveRunDir()
  const baseDir = ctx.baseDirFromRunDir(activeRunDir)
  const sc = ctx.getStorageConfig(baseDir)

  let storedPath: string
  if (filePath) {
    // Load artifact from disk instead of inline
    const resolved = resolveFilePath(filePath, baseDir)
    if (!existsSync(resolved)) {
      throw new PipelineError(`File not found: ${resolved}`, 'artifact_not_found', {
        recovery_action: 'Verify the file_path exists relative to the run directory.',
        details: { path: resolved },
      })
    }
    const content = JSON.parse(readFileSync(resolved, 'utf-8')) as unknown
    storedPath = ctx.storeArtifact(sc, storageKey, fileName, content, force)
  } else {
    storedPath = ctx.storeArtifact(sc, storageKey, fileName, artifact, force)
  }

  const version = computeNextVersion(state.available_artifacts, artifactType, phase)
  const ref: ArtifactRef = {
    type: artifactType,
    path: storedPath,
    phase,
    created_at: new Date().toISOString(),
    version,
  }
  if (parentArtifact !== undefined) ref.parent_artifact = parentArtifact
  const updated = ctx.addArtifact(state, ref, activeRunDir)
  ctx.setActiveRun(updated)

  // Emit artifact_stored audit event after the successful addArtifact call.
  appendEvent(activeRunDir, {
    timestamp: new Date().toISOString(),
    event: 'artifact_stored',
    phase,
    run_id: updated.run_id,
    details: {
      artifact_type: artifactType,
      path: storedPath,
      size_bytes: safeSizeBytes(storedPath),
    },
  })

  const envelope: ResponseEnvelope = {
    status: 'ok',
    data: { path: storedPath, artifact_type: artifactType, phase, version, ...(parentArtifact !== undefined ? { parent_artifact: parentArtifact } : {}) },
    next_step: 'Call pipeline_complete_phase when done, or pipeline_store_artifact for more artifacts.',
  }
  return { json: JSON.stringify(envelope, null, 2) }
}

export function handleRegisterArtifact(args: Record<string, unknown>, ctx: ArtifactContext): HandlerResponse {
  const state = ctx.requireRun()
  const filePath = args.file_path as string
  const artifactType = args.artifact_type as string
  const phase = args.phase as string
  const storageKey = args.storage_key as string
  const shouldValidate = (args.validate as boolean | undefined) ?? false

  if (!filePath || !artifactType || !phase || !storageKey) {
    throw new PipelineError('file_path, artifact_type, phase, and storage_key are all required', 'validation_error', {
      recovery_action: 'Provide all required parameters: file_path, artifact_type, phase, and storage_key.',
    })
  }

  const activeRunDir = ctx.getActiveRunDir()
  const baseDir = ctx.baseDirFromRunDir(activeRunDir)
  const resolved = resolveFilePath(filePath, baseDir)

  if (!existsSync(resolved)) {
    throw new PipelineError(`File not found: ${resolved}`, 'artifact_not_found', {
      recovery_action: 'Verify the file_path exists and is accessible.',
      details: { path: resolved },
    })
  }

  if (shouldValidate) {
    const schemaFile = `${artifactType}.json`
    const content = JSON.parse(readFileSync(resolved, 'utf-8')) as unknown
    const result = ctx.validateArtifact(ctx.getSchemas(), schemaFile, content)
    if (!result.valid) {
      return { json: `Validation FAILED against ${schemaFile}:\n${result.errors.join('\n')}`, isError: true }
    }
  }

  const version = computeNextVersion(state.available_artifacts, artifactType, phase)
  const updated = ctx.addArtifact(state, {
    type: artifactType,
    path: resolved,
    phase,
    created_at: new Date().toISOString(),
    version,
  }, activeRunDir)
  ctx.setActiveRun(updated)

  // Emit artifact_stored audit event after the successful addArtifact call.
  appendEvent(activeRunDir, {
    timestamp: new Date().toISOString(),
    event: 'artifact_stored',
    phase,
    run_id: updated.run_id,
    details: {
      artifact_type: artifactType,
      path: resolved,
      size_bytes: safeSizeBytes(resolved),
    },
  })

  return {
    json: JSON.stringify({
      registered: true,
      artifact_type: artifactType,
      path: resolved,
      phase,
      version,
      validated: shouldValidate,
    }, null, 2),
  }
}

export function handleLoadArtifact(args: Record<string, unknown>, ctx: ArtifactContext): HandlerResponse {
  const storageKey = args.storage_key as string
  const fileName = args.file_name as string
  const inline = args.inline as boolean | undefined
  const full = (args.full as boolean | undefined) || inline
  const fields = args.fields as string[] | undefined
  if (!storageKey || !fileName) {
    throw new PipelineError('storage_key and file_name are required', 'validation_error', {
      recovery_action: 'Provide both storage_key and file_name parameters.',
    })
  }

  const activeRun = ctx.getActiveRun()
  const activeRunDir = ctx.getActiveRunDir()
  const baseDir = activeRun
    ? ctx.baseDirFromRunDir(activeRunDir)
    : resolve(ctx.getProjectRoot(), ctx.getConfigStorageBaseDir())
  const sc = ctx.getStorageConfig(baseDir)
  const artifact = ctx.loadArtifact(sc, storageKey, fileName)

  if (artifact === null) {
    throw new PipelineError(`Artifact not found: ${storageKey}/${fileName}`, 'artifact_not_found', {
      recovery_action: 'Call pipeline_list_artifacts to see available artifacts for this storage key.',
      details: { storage_key: storageKey, file_name: fileName },
    })
  }

  if (full) {
    const fullJson = JSON.stringify(artifact, null, 2)
    const enforced = enforceResponseSize(fullJson, 50000, { storage_key: storageKey, file_name: fileName })
    return { json: enforced }
  }

  if (fields !== undefined && fields.length > 0) {
    const obj = artifact as Record<string, unknown>
    const filtered: Record<string, unknown> = {}
    for (const key of fields) {
      if (Object.prototype.hasOwnProperty.call(obj, key)) {
        filtered[key] = obj[key]
      }
    }
    return { json: JSON.stringify(filtered, null, 2) }
  }

  return { json: JSON.stringify(ctx.buildArtifactSummary(artifact, storageKey, fileName), null, 2) }
}

export function handleListArtifacts(args: Record<string, unknown>, ctx: ArtifactContext): HandlerResponse {
  const storageKey = args.storage_key as string
  if (!storageKey) {
    throw new PipelineError('storage_key is required', 'validation_error', {
      recovery_action: 'Provide the storage_key parameter.',
    })
  }

  const activeRun = ctx.getActiveRun()
  const activeRunDir = ctx.getActiveRunDir()
  const baseDir = activeRun
    ? ctx.baseDirFromRunDir(activeRunDir)
    : resolve(ctx.getProjectRoot(), ctx.getConfigStorageBaseDir())
  const sc = ctx.getStorageConfig(baseDir)
  const files = ctx.listArtifacts(sc, storageKey)

  // Build a lookup: normalized file path → ArtifactRef, using the run's available_artifacts.
  // The storage subpath for this key determines the directory each file lives in.
  const subPath = sc.paths[storageKey]
  const storageDir = subPath !== undefined ? normalize(join(sc.base_dir, subPath)) : null

  const availableArtifacts: ArtifactRef[] = activeRun?.available_artifacts ?? []
  const refByFileName = new Map<string, ArtifactRef>()
  if (storageDir !== null) {
    for (const ref of availableArtifacts) {
      const refDir = normalize(resolve(ref.path, '..'))
      if (refDir === storageDir) {
        refByFileName.set(basename(ref.path), ref)
      }
    }
  }

  const artifacts = files.map((name) => {
    const ref = refByFileName.get(name)
    const entry: Record<string, unknown> = { name }
    if (ref !== undefined) {
      if (ref.version !== undefined) entry.version = ref.version
      if (ref.parent_artifact !== undefined) entry.parent_artifact = ref.parent_artifact
    }
    return entry
  })

  return { json: JSON.stringify({ storage_key: storageKey, artifacts }, null, 2) }
}
