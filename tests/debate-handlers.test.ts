// pipeline-mcp/tests/debate-handlers.test.ts

import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { handleSaveDebate } from '../debate-handlers.ts'
import type { SaveDebateContext } from '../debate-handlers.ts'
import { initDebate, submitArgument } from '../debate.ts'
import type { DebateState } from '../debate.ts'
import type { RunState, StorageConfig, ArtifactRef } from '../types.ts'
import type { SchemaMap, ValidationResult } from '../validator.ts'

// ── Helpers ───────────────────────────────────────────────────

/**
 * Build a DebateState that is ready to be saved (round-1 complete + synthesis
 * recorded), so buildTranscript inside handleSaveDebate succeeds.
 */
function makeSaveReadyDebate(): DebateState {
  let state = initDebate({
    inputType: 'design-spec',
    inputId: 'spec-save-test',
    inputVersion: '1.0.0',
    artifactContent: { spec_id: 'spec-save-test', title: 'Save-ready spec' },
  })
  state = submitArgument(state, { role: 'advocate', position: 'Solid approach.', confidence: 0.9 })
  state = submitArgument(state, { role: 'critic', position: 'Has some gaps.', confidence: 0.7 })
  state = submitArgument(state, { role: 'risk_specialist', position: 'Drawdown OK.', confidence: 0.8 })
  state = submitArgument(state, { role: 'domain_specialist', position: 'Domain constraints noted.', confidence: 0.75 })
  state.synthesis = {
    changes_accepted: ['Add slippage model'],
    changes_rejected: [],
    open_questions: [],
  }
  state.outputVersion = '1.1.0'
  return state
}

interface StubCallLog {
  storeArtifactCalls: Array<{
    config: StorageConfig
    key: string
    name: string
    artifact: unknown
    force?: boolean
  }>
  persistDebateCalls: Array<{
    transcript: unknown
    fileName: string
    force?: boolean
  }>
  addArtifactCalls: Array<{ ref: ArtifactRef; stateDir: string }>
  activeDebate: DebateState | null
  activeRun: RunState | null
}

function makeValidSchemas(): SchemaMap {
  // handleSaveDebate only passes the SchemaMap through to validateArtifact,
  // which we stub out to always-valid. An empty map is fine for the stub.
  return {}
}

function makeStubCtx(
  opts: {
    initialDebate: DebateState | null
    activeRun?: RunState | null
    validationResult?: ValidationResult
    includePersistDebate?: boolean
    storedPath?: string
    persistDebatePath?: string
  },
): SaveDebateContext & { log: StubCallLog } {
  const log: StubCallLog = {
    storeArtifactCalls: [],
    persistDebateCalls: [],
    addArtifactCalls: [],
    activeDebate: opts.initialDebate,
    activeRun: opts.activeRun ?? null,
  }

  const storedPath = opts.storedPath ?? '/tmp/legacy/debates/dt-fallback.json'
  const persistDebatePath = opts.persistDebatePath ?? '/tmp/runs/abc/debates/dt-persisted.json'
  const validation: ValidationResult = opts.validationResult ?? { valid: true, errors: [] }

  const ctx: SaveDebateContext & { log: StubCallLog } = {
    log,
    getActiveDebate: () => log.activeDebate,
    setActiveDebate: (s) => { log.activeDebate = s },
    validateArtifact: (_schemas, _file, _artifact) => validation,
    getSchemas: () => makeValidSchemas(),
    storeArtifact: (config, key, name, artifact, force) => {
      log.storeArtifactCalls.push({ config, key, name, artifact, force })
      return storedPath
    },
    getStorageConfig: (baseDir) => ({ base_dir: baseDir, paths: { debates: 'debates' } }),
    baseDirFromRunDir: (runDir) => `${runDir}/..`,
    getProjectRoot: () => '/tmp/project',
    getConfigStorageBaseDir: () => 'pipeline_mcp_data',
    getActiveRun: () => log.activeRun,
    getActiveRunDir: () => '/tmp/project/pipeline_mcp_data/runs/test-run',
    addArtifact: (state, ref, stateDir) => {
      log.addArtifactCalls.push({ ref, stateDir })
      return state
    },
    setActiveRun: (s) => { log.activeRun = s },
  }

  if (opts.includePersistDebate) {
    ctx.persistDebate = (transcript, fileName, force) => {
      log.persistDebateCalls.push({ transcript, fileName, force })
      return persistDebatePath
    }
  }

  return ctx
}

// ── handleSaveDebate — persistDebate routing (Feature C) ───────

describe('handleSaveDebate', () => {
  it('prefers ctx.persistDebate when present and does not call storeArtifact', () => {
    const debate = makeSaveReadyDebate()
    const ctx = makeStubCtx({
      initialDebate: debate,
      includePersistDebate: true,
      persistDebatePath: '/tmp/runs/xyz/debates/dt-from-persist.json',
    })

    const result = handleSaveDebate(
      { file_name: 'dt-from-persist.json', phase: 'knowledge-overview-debate' },
      ctx,
    )

    // persistDebate called exactly once with the transcript and filename; storeArtifact untouched.
    assert.equal(ctx.log.persistDebateCalls.length, 1, 'persistDebate should be called exactly once')
    assert.equal(ctx.log.storeArtifactCalls.length, 0, 'storeArtifact should not be called when persistDebate is present')
    assert.equal(ctx.log.persistDebateCalls[0].fileName, 'dt-from-persist.json')

    // Handler passes the built transcript, not the raw debate state.
    const passedTranscript = ctx.log.persistDebateCalls[0].transcript as Record<string, unknown>
    assert.equal(passedTranscript.transcript_id, debate.transcriptId)
    assert.equal(passedTranscript.input_type, 'design-spec')
    assert.equal(passedTranscript.input_id, 'spec-save-test')

    // Response JSON carries the path returned by persistDebate.
    const response = JSON.parse(result.json)
    assert.equal(response.stored_path, '/tmp/runs/xyz/debates/dt-from-persist.json')
    assert.equal(response.status, 'saved')
    assert.equal(response.transcript_id, debate.transcriptId)

    // Active debate cleared on success.
    assert.equal(ctx.log.activeDebate, null)
  })

  it('falls back to ctx.storeArtifact when ctx.persistDebate is absent', () => {
    const debate = makeSaveReadyDebate()
    const ctx = makeStubCtx({
      initialDebate: debate,
      includePersistDebate: false,
      storedPath: '/tmp/legacy/debates/dt-from-store.json',
    })

    const result = handleSaveDebate(
      { file_name: 'dt-from-store.json', phase: 'design-debate' },
      ctx,
    )

    assert.equal(ctx.log.storeArtifactCalls.length, 1, 'storeArtifact should be called exactly once in the fallback branch')
    assert.equal(ctx.log.persistDebateCalls.length, 0, 'persistDebate should not be called when it is absent on ctx')
    const call = ctx.log.storeArtifactCalls[0]
    assert.equal(call.key, 'debates')
    assert.equal(call.name, 'dt-from-store.json')

    // Handler passes undefined for force (preserving pre-C5 behaviour).
    assert.equal(call.force, undefined, 'storeArtifact should be called without force to preserve throw-on-collision semantics')

    // Response includes the path from the stub storeArtifact.
    const response = JSON.parse(result.json)
    assert.equal(response.stored_path, '/tmp/legacy/debates/dt-from-store.json')
    assert.equal(response.status, 'saved')
  })

  it('throws when there is no active debate', () => {
    const ctx = makeStubCtx({ initialDebate: null, includePersistDebate: true })
    assert.throws(
      () => handleSaveDebate({ file_name: 'dt-xyz.json', phase: 'p' }, ctx),
      /No active debate/i,
    )
  })

  it('throws when file_name or phase is missing', () => {
    const debate = makeSaveReadyDebate()

    const ctxNoFile = makeStubCtx({ initialDebate: debate, includePersistDebate: true })
    assert.throws(
      () => handleSaveDebate({ phase: 'p' }, ctxNoFile),
      /file_name and phase are required/,
    )

    const debate2 = makeSaveReadyDebate()
    const ctxNoPhase = makeStubCtx({ initialDebate: debate2, includePersistDebate: true })
    assert.throws(
      () => handleSaveDebate({ file_name: 'dt-y.json' }, ctxNoPhase),
      /file_name and phase are required/,
    )
  })

  it('returns isError response (does not throw) when validation fails', () => {
    const debate = makeSaveReadyDebate()
    const ctx = makeStubCtx({
      initialDebate: debate,
      includePersistDebate: true,
      validationResult: { valid: false, errors: ['missing required field "transcript_id"'] },
    })

    const result = handleSaveDebate({ file_name: 'dt-bad.json', phase: 'p' }, ctx)

    assert.equal(result.isError, true)
    assert.ok(result.json.includes('Debate transcript validation FAILED'))
    assert.ok(result.json.includes('missing required field'))
    // Must NOT have attempted to persist on validation failure.
    assert.equal(ctx.log.persistDebateCalls.length, 0)
    assert.equal(ctx.log.storeArtifactCalls.length, 0)
  })
})
