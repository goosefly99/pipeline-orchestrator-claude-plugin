// debate-handlers.ts — debate tool handler logic extracted from server.ts

import { join } from 'node:path'
import {
  initDebate,
  submitArgument,
  buildTranscript,
  generateAgentPrompts,
  generateSynthesisPrompt,
  type DebateState,
  type DebateSynthesis,
  type DomainProfile,
} from './debate.ts'
import type { RunState, StorageConfig, ArtifactRef, HandlerResponse } from './types.ts'
import type { SchemaMap, ValidationResult } from './validator.ts'

// ── Context provided by server.ts ────────────────────────────

export interface DebateContext {
  getActiveDebate(): DebateState | null
  setActiveDebate(state: DebateState | null): void
}

export interface SaveDebateContext extends DebateContext {
  validateArtifact(schemas: SchemaMap, schemaFile: string, artifact: unknown): ValidationResult
  getSchemas(): SchemaMap
  storeArtifact(config: StorageConfig, key: string, name: string, artifact: unknown, force?: boolean): string
  getStorageConfig(baseDir: string): StorageConfig
  baseDirFromRunDir(runDir: string): string
  getProjectRoot(): string
  getConfigStorageBaseDir(): string
  getActiveRun(): RunState | null
  getActiveRunDir(): string
  addArtifact(state: RunState, ref: ArtifactRef, stateDir: string): RunState
  setActiveRun(state: RunState): void

  /**
   * Per-run debate transcript persistence (Feature C). When present,
   * takes precedence over `storeArtifact` in `handleSaveDebate`. Server.ts
   * wires this to `persistDebate(runDataDir, legacyBaseDir, transcript,
   * fileName, force)` so callers get automatic run-scoped routing without
   * plumbing context through the handler.
   *
   * Optional so older test fixtures that mock the context without this
   * field continue to fall back to `storeArtifact`. Throws on pre-existing
   * files unless `force === true`, matching `storage.ts::storeArtifact`.
   */
  persistDebate?(transcript: unknown, fileName: string, force?: boolean): string
}

// Re-export HandlerResponse for backward compatibility
export type { HandlerResponse } from './types.ts'

// ── Handlers ─────────────────────────────────────────────────

export function handleInitDebate(args: Record<string, unknown>, ctx: DebateContext): HandlerResponse {
  const inputType = args.input_type as 'knowledge-overview' | 'design-spec'
  const inputId = args.input_id as string
  const inputVersion = args.input_version as string
  const artifactContent = args.artifact_content as unknown
  const artifactPath = args.artifact_path as string | undefined
  const codebaseRequirementsId = args.codebase_requirements_id as string | undefined
  const domainProfile = args.domain_profile as DomainProfile | string | undefined

  if (!inputType || !inputId || !inputVersion || !artifactContent) {
    throw new Error('input_type, input_id, input_version, and artifact_content are required')
  }

  const state = initDebate({ inputType, inputId, inputVersion, artifactContent, artifactPath, codebaseRequirementsId, domainProfile })
  ctx.setActiveDebate(state)
  const agentPrompts = generateAgentPrompts(state)

  const response: Record<string, unknown> = {
    transcript_id: state.transcriptId,
    input_type: state.inputType,
    input_id: state.inputId,
    instruction: [
      'Dispatch all 4 agents in parallel using the Agent tool.',
      'Combine artifact_path with each agent role prompt when dispatching.',
      'Feed each agent its role prompt plus the artifact at artifact_path. Parse the JSON response from each agent.',
      'Call pipeline_submit_argument once per agent with the parsed result.',
      'Then call pipeline_synthesize_debate to get the synthesis prompt.',
    ].join(' '),
    agents: agentPrompts,
  }

  if (artifactPath !== undefined) {
    response.artifact_path = artifactPath
  }

  return {
    json: JSON.stringify(response, null, 2),
  }
}

export function handleSubmitArgument(args: Record<string, unknown>, ctx: DebateContext): HandlerResponse {
  const activeDebate = ctx.getActiveDebate()
  if (!activeDebate) throw new Error('No active debate. Call pipeline_init_debate first.')

  const role = args.role as 'advocate' | 'critic' | 'risk_specialist' | 'domain_specialist'
  const position = args.position as string
  const confidence = args.confidence as number
  const evidence = Array.isArray(args.evidence) ? args.evidence as string[] : []
  const counterpoints = Array.isArray(args.counterpoints) ? args.counterpoints as string[] : []
  const proposed_changes = Array.isArray(args.proposed_changes) ? args.proposed_changes as string[] : []

  if (!role || !position || confidence === undefined) {
    throw new Error('role, position, and confidence are required')
  }

  const updated = submitArgument(activeDebate, { role, position, confidence, evidence, counterpoints, proposed_changes })
  ctx.setActiveDebate(updated)

  const submittedRoles = updated.round1Arguments.map(a => a.role)
  const remaining = (['advocate', 'critic', 'risk_specialist', 'domain_specialist'] as const)
    .filter(r => !submittedRoles.includes(r))

  return {
    json: JSON.stringify({
      recorded: role,
      submitted_so_far: submittedRoles,
      remaining,
      ready_for_synthesis: remaining.length === 0,
      next_step: remaining.length === 0
        ? 'All round-1 arguments recorded. Call pipeline_synthesize_debate to get the synthesizer prompt.'
        : `Still waiting for: ${remaining.join(', ')}`,
    }, null, 2),
  }
}

export function handleSynthesizeDebate(args: Record<string, unknown>, ctx: DebateContext): HandlerResponse {
  const activeDebate = ctx.getActiveDebate()
  if (!activeDebate) throw new Error('No active debate. Call pipeline_init_debate first.')

  if (!args.changes_accepted) {
    const prompt = generateSynthesisPrompt(activeDebate)
    const responseObj: Record<string, unknown> = {
      instruction: 'Dispatch the Synthesizer agent using the Agent tool with this prompt. Parse the JSON response, then call pipeline_synthesize_debate again with the parsed changes_accepted, changes_rejected, and open_questions fields.',
      synthesis_prompt: prompt,
    }
    if (activeDebate.artifactPath !== undefined) {
      responseObj.artifact_path = activeDebate.artifactPath
    }
    return { json: JSON.stringify(responseObj, null, 2) }
  }

  const synthesis: DebateSynthesis = {
    changes_accepted: Array.isArray(args.changes_accepted) ? args.changes_accepted as string[] : [],
    changes_rejected: Array.isArray(args.changes_rejected) ? args.changes_rejected as Array<{ proposed: string; reason: string }> : [],
    open_questions: Array.isArray(args.open_questions) ? args.open_questions as string[] : [],
  }
  const outputVersion = args.output_version as string | undefined
  const kbQueriesUsed = (args.kb_queries_used as boolean) ?? false

  ctx.setActiveDebate({
    ...activeDebate,
    synthesis,
    outputVersion: outputVersion ?? null,
    kbQueriesUsed,
  })

  return {
    json: JSON.stringify({
      status: 'synthesis_recorded',
      changes_accepted: synthesis.changes_accepted.length,
      changes_rejected: synthesis.changes_rejected.length,
      open_questions: synthesis.open_questions?.length ?? 0,
      next_step: 'Call pipeline_save_debate to validate and persist the transcript.',
    }, null, 2),
  }
}

export function handleSaveDebate(args: Record<string, unknown>, ctx: SaveDebateContext): HandlerResponse {
  const activeDebate = ctx.getActiveDebate()
  if (!activeDebate) throw new Error('No active debate. Call pipeline_init_debate first.')

  const fileName = args.file_name as string
  const phase = args.phase as string
  if (!fileName || !phase) throw new Error('file_name and phase are required')

  const transcript = buildTranscript(activeDebate)

  const validationResult = ctx.validateArtifact(ctx.getSchemas(), 'debate-transcript.json', transcript)
  if (!validationResult.valid) {
    return {
      json: `Debate transcript validation FAILED:\n${validationResult.errors.join('\n')}`,
      isError: true,
    }
  }

  const activeRun = ctx.getActiveRun()
  const activeRunDir = ctx.getActiveRunDir()

  // Feature C: prefer persistDebate (run-scoped) when the context supplies
  // it; fall back to the legacy storeArtifact path so older test fixtures
  // and any callers without the new wiring keep working. Both branches
  // preserve the throw-on-collision-unless-force semantics of storeArtifact.
  let storedPath: string
  if (ctx.persistDebate) {
    storedPath = ctx.persistDebate(transcript, fileName)
  } else {
    const baseDir = activeRun
      ? ctx.baseDirFromRunDir(activeRunDir)
      : join(ctx.getProjectRoot(), ctx.getConfigStorageBaseDir())
    const sc = ctx.getStorageConfig(baseDir)
    storedPath = ctx.storeArtifact(sc, 'debates', fileName, transcript)
  }

  if (activeRun) {
    const updated = ctx.addArtifact(activeRun, {
      type: 'debate-transcript',
      path: storedPath,
      phase,
      created_at: new Date().toISOString(),
      version: 1,
    }, activeRunDir)
    ctx.setActiveRun(updated)
  }

  const savedId = activeDebate.transcriptId
  ctx.setActiveDebate(null)

  return {
    json: JSON.stringify({
      transcript_id: savedId,
      stored_path: storedPath,
      status: 'saved',
    }, null, 2),
  }
}
