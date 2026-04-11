import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, existsSync, writeFileSync, readFileSync } from 'node:fs'
import { join, sep } from 'node:path'
import { tmpdir } from 'node:os'
import {
  initRun, startPhase, completePhase, failPhase, skipPhase, retryPhase, loadRunState, addArtifact,
  computeRecommendedAction, computeRunWarnings, STALE_PHASE_THRESHOLD_MS, atomicRename,
  validateRunStateIntegrity, recoverRun,
  getCompletedPhases, getSkippedPhases, getInProgressPhases, getArtifactTypes,
} from '../run-state.ts'
import { PipelineError } from '../types.ts'

let tempDir: string

beforeEach(() => {
  tempDir = mkdtempSync(join(tmpdir(), 'pipeline-run-test-'))
})

afterEach(() => {
  rmSync(tempDir, { recursive: true, force: true })
})

describe('initRun', () => {
  it('creates a new run state with all phases pending', () => {
    const phaseNames = ['discovery', 'curation', 'synthesis']
    const state = initRun('test-run-1', '1.0.0', phaseNames, tempDir)

    assert.equal(state.run_id, 'test-run-1')
    assert.equal(state.status, 'initialized')
    assert.equal(Object.keys(state.phases).length, 3)
    assert.equal(state.phases.discovery.status, 'pending')
    assert.equal(state.phases.curation.status, 'pending')
  })

  it('persists state to disk', () => {
    initRun('test-run-2', '1.0.0', ['discovery'], tempDir)
    assert.ok(existsSync(join(tempDir, 'run-state.json')))
  })

  it('sets run_data_dir when run_parameters.run_name and run_directory_timestamp are present', () => {
    const baseDir = join(tempDir, 'test-base')
    const stateDir = join(baseDir, 'runs', 'run-001')
    const state = initRun('run-001', 'v1', ['phase-a'], stateDir, {
      run_name: 'My Test Run',
      run_directory_timestamp: '2026-04-10T12:34:56.789Z',
    })

    assert.ok(state.run_data_dir, 'run_data_dir should be defined')
    // Must live under {baseDir}/runs/... (cross-platform assertion via path.sep)
    const runsPrefix = join(baseDir, 'runs') + sep
    assert.ok(
      state.run_data_dir!.startsWith(runsPrefix),
      `expected run_data_dir to start with ${runsPrefix}, got ${state.run_data_dir}`,
    )
    // Sanitized run name and sanitized timestamp should appear in the final segment.
    assert.ok(
      state.run_data_dir!.includes('my-test-run'),
      `expected run_data_dir to include sanitized name 'my-test-run', got ${state.run_data_dir}`,
    )
    assert.ok(
      state.run_data_dir!.includes('2026-04-10T12-34-56-789Z'),
      `expected run_data_dir to include sanitized timestamp '2026-04-10T12-34-56-789Z', got ${state.run_data_dir}`,
    )
  })

  it('leaves run_data_dir undefined and warns when parameters absent', () => {
    const originalWarn = console.warn
    const warnings: string[] = []
    console.warn = (...args: unknown[]) => {
      warnings.push(args.map(a => String(a)).join(' '))
    }
    try {
      const stateDir = join(tempDir, 'test-base', 'runs', 'run-002')
      const state = initRun('run-002', 'v1', ['phase-a'], stateDir)
      assert.equal(state.run_data_dir, undefined)
      assert.ok(
        warnings.some(w => w.includes('run-002') && w.includes('parameterization inactive')),
        `expected warn containing 'run-002' and 'parameterization inactive', got ${JSON.stringify(warnings)}`,
      )
    } finally {
      console.warn = originalWarn
    }
  })
})

describe('startPhase', () => {
  it('marks phase as in_progress', () => {
    const state = initRun('test', '1.0.0', ['discovery'], tempDir)
    const updated = startPhase(state, 'discovery', tempDir)

    assert.equal(updated.phases.discovery.status, 'in_progress')
    assert.ok(updated.phases.discovery.started_at)
    assert.equal(updated.status, 'running')
  })

  it('throws if phase does not exist', () => {
    const state = initRun('test', '1.0.0', ['discovery'], tempDir)
    assert.throws(
      () => startPhase(state, 'nonexistent', tempDir),
      /not found/i,
    )
  })

  it('rejects starting an already in_progress phase', () => {
    let state = initRun('test', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    assert.throws(
      () => startPhase(state, 'discovery', tempDir),
      /Cannot start phase "discovery": status is "in_progress" \(expected "pending"\)/,
    )
  })

  it('rejects starting a completed phase', () => {
    let state = initRun('test', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    state = completePhase(state, 'discovery', tempDir)
    assert.throws(
      () => startPhase(state, 'discovery', tempDir),
      /Cannot start phase "discovery": status is "completed" \(expected "pending"\)/,
    )
  })
})

describe('completePhase', () => {
  it('marks phase as completed', () => {
    let state = initRun('test', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    const updated = completePhase(state, 'discovery', tempDir)

    assert.equal(updated.phases.discovery.status, 'completed')
    assert.ok(updated.phases.discovery.completed_at)
  })

  it('rejects completing a pending phase', () => {
    const state = initRun('test', '1.0.0', ['discovery'], tempDir)
    assert.throws(
      () => completePhase(state, 'discovery', tempDir),
      /Cannot complete phase "discovery": status is "pending" \(expected "in_progress"\)/,
    )
  })
})

describe('failPhase', () => {
  it('marks phase as failed with error', () => {
    let state = initRun('test', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    const updated = failPhase(state, 'discovery', 'API timeout', tempDir)

    assert.equal(updated.phases.discovery.status, 'failed')
    assert.equal(updated.phases.discovery.error, 'API timeout')
    assert.equal(updated.status, 'failed')
  })

  it('rejects failing a pending phase', () => {
    const state = initRun('test', '1.0.0', ['discovery'], tempDir)
    assert.throws(
      () => failPhase(state, 'discovery', 'error', tempDir),
      /Cannot fail phase "discovery": status is "pending" \(expected "in_progress"\)/,
    )
  })
})

describe('retryPhase', () => {
  it('transitions a failed phase back to pending and increments retry_count', () => {
    let state = initRun('test', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    state = failPhase(state, 'discovery', 'API timeout', tempDir)

    assert.equal(state.phases.discovery.status, 'failed')
    assert.equal(state.phases.discovery.retry_count, 0)

    const updated = retryPhase(state, 'discovery', tempDir)
    assert.equal(updated.phases.discovery.status, 'pending')
    assert.equal(updated.phases.discovery.error, undefined)
    assert.equal(updated.phases.discovery.retry_count, 1)
  })

  it('increments retry_count on each successive retry', () => {
    let state = initRun('test', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    state = failPhase(state, 'discovery', 'first failure', tempDir)
    state = retryPhase(state, 'discovery', tempDir)
    state = startPhase(state, 'discovery', tempDir)
    state = failPhase(state, 'discovery', 'second failure', tempDir)
    state = retryPhase(state, 'discovery', tempDir)

    assert.equal(state.phases.discovery.retry_count, 2)
    assert.equal(state.phases.discovery.status, 'pending')
  })

  it('persists state to disk after retry', () => {
    let state = initRun('test', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    state = failPhase(state, 'discovery', 'oops', tempDir)
    retryPhase(state, 'discovery', tempDir)

    const loaded = loadRunState(tempDir)
    assert.ok(loaded)
    assert.equal(loaded.phases.discovery.status, 'pending')
    assert.equal(loaded.phases.discovery.retry_count, 1)
    assert.equal(loaded.phases.discovery.error, undefined)
  })

  it('throws if phase does not exist', () => {
    const state = initRun('test', '1.0.0', ['discovery'], tempDir)
    assert.throws(
      () => retryPhase(state, 'nonexistent', tempDir),
      /not found/i,
    )
  })

  it('rejects retrying a pending phase', () => {
    const state = initRun('test', '1.0.0', ['discovery'], tempDir)
    assert.throws(
      () => retryPhase(state, 'discovery', tempDir),
      /Cannot retry phase "discovery": status is "pending" \(expected "failed"\)/,
    )
  })

  it('rejects retrying an in_progress phase', () => {
    let state = initRun('test', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    assert.throws(
      () => retryPhase(state, 'discovery', tempDir),
      /Cannot retry phase "discovery": status is "in_progress" \(expected "failed"\)/,
    )
  })

  it('rejects retrying a completed phase', () => {
    let state = initRun('test', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    state = completePhase(state, 'discovery', tempDir)
    assert.throws(
      () => retryPhase(state, 'discovery', tempDir),
      /Cannot retry phase "discovery": status is "completed" \(expected "failed"\)/,
    )
  })
})

describe('skipPhase', () => {
  it('marks phase as skipped', () => {
    const state = initRun('test', '1.0.0', ['discovery'], tempDir)
    const updated = skipPhase(state, 'discovery', tempDir)
    assert.equal(updated.phases.discovery.status, 'skipped')
  })

  it('rejects skipping an in_progress phase', () => {
    let state = initRun('test', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    assert.throws(
      () => skipPhase(state, 'discovery', tempDir),
      /Cannot skip phase "discovery": status is "in_progress" \(expected "pending"\)/,
    )
  })
})

describe('addArtifact', () => {
  it('adds artifact ref to run state', () => {
    let state = initRun('test', '1.0.0', ['discovery'], tempDir)
    state = addArtifact(state, {
      type: 'raw-collection',
      path: 'collections/raw/test.json',
      phase: 'discovery',
      created_at: new Date().toISOString(),
    }, tempDir)

    assert.equal(state.available_artifacts.length, 1)
    assert.equal(state.available_artifacts[0].type, 'raw-collection')
  })

  it('stores version and parent_artifact when provided', () => {
    let state = initRun('test', '1.0.0', ['discovery'], tempDir)
    state = addArtifact(state, {
      type: 'design-spec',
      path: 'specs/v2.json',
      phase: 'discovery',
      created_at: new Date().toISOString(),
      version: 2,
      parent_artifact: 'specs/v1.json',
    }, tempDir)

    const ref = state.available_artifacts[0]
    assert.equal(ref.version, 2)
    assert.equal(ref.parent_artifact, 'specs/v1.json')
  })
})

describe('loadRunState', () => {
  it('loads persisted state from disk', () => {
    initRun('persisted-run', '1.0.0', ['discovery', 'curation'], tempDir)
    const loaded = loadRunState(tempDir)

    assert.ok(loaded)
    assert.equal(loaded.run_id, 'persisted-run')
    assert.equal(Object.keys(loaded.phases).length, 2)
  })

  it('returns null if no state file', () => {
    const loaded = loadRunState(join(tempDir, 'nonexistent'))
    assert.equal(loaded, null)
  })

  it('normalizes version to 1 for artifacts missing the field on disk', () => {
    const legacyState = {
      run_id: 'legacy-run',
      pipeline_version: '1.0.0',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      status: 'running',
      phases: { discovery: { phase_name: 'discovery', status: 'in_progress', input_artifacts: [], output_artifacts: [], retry_count: 0 } },
      available_artifacts: [
        { type: 'raw-collection', path: 'test.json', phase: 'discovery', created_at: '2026-01-01T00:00:00.000Z' },
      ],
      config_path: tempDir,
    }
    writeFileSync(join(tempDir, 'run-state.json'), JSON.stringify(legacyState), 'utf-8')

    const loaded = loadRunState(tempDir)
    assert.ok(loaded)
    assert.equal(loaded.available_artifacts[0].version, 1)
    assert.equal(loaded.available_artifacts[0].parent_artifact, undefined)
  })

  it('preserves existing version and parent_artifact fields on load', () => {
    const stateWithVersion = {
      run_id: 'versioned-run',
      pipeline_version: '1.0.0',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      status: 'running',
      phases: { design_synthesis: { phase_name: 'design_synthesis', status: 'in_progress', input_artifacts: [], output_artifacts: [], retry_count: 0 } },
      available_artifacts: [
        {
          type: 'design-spec', path: 'specs/v2.json', phase: 'design_synthesis',
          created_at: '2026-01-01T00:00:00.000Z', version: 2, parent_artifact: 'specs/v1.json',
        },
      ],
      config_path: tempDir,
    }
    writeFileSync(join(tempDir, 'run-state.json'), JSON.stringify(stateWithVersion), 'utf-8')

    const loaded = loadRunState(tempDir)
    assert.ok(loaded)
    assert.equal(loaded.available_artifacts[0].version, 2)
    assert.equal(loaded.available_artifacts[0].parent_artifact, 'specs/v1.json')
  })
})

describe('computeRecommendedAction', () => {
  it('returns "continue" when a phase is in_progress', () => {
    let state = initRun('r1', '1.0.0', ['discovery', 'curation'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    const action = computeRecommendedAction(state, ['curation'])
    assert.equal(action.action, 'continue')
    assert.equal(action.phase, 'discovery')
    assert.ok(action.reason.includes('discovery'))
    assert.ok(action.reason.includes('in progress'))
  })

  it('returns "start_phase" with the first next phase when none in_progress', () => {
    const state = initRun('r2', '1.0.0', ['discovery', 'curation'], tempDir)
    const action = computeRecommendedAction(state, ['discovery', 'curation'])
    assert.equal(action.action, 'start_phase')
    assert.equal(action.phase, 'discovery')
    assert.ok(action.reason.includes('discovery'))
  })

  it('returns "validate" when status is completed and no next phases', () => {
    let state = initRun('r3', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    state = completePhase(state, 'discovery', tempDir)
    assert.equal(state.status, 'completed')
    const action = computeRecommendedAction(state, [])
    assert.equal(action.action, 'validate')
    assert.ok(action.reason.toLowerCase().includes('validate'))
  })

  it('returns "review" when no next phases and status is not completed', () => {
    const state = initRun('r4', '1.0.0', ['discovery'], tempDir)
    // status is 'initialized', nothing in_progress, no next phases
    const action = computeRecommendedAction(state, [])
    assert.equal(action.action, 'review')
    assert.ok(action.reason.toLowerCase().includes('blocked') || action.reason.toLowerCase().includes('failed'))
  })

  it('prefers in_progress over available next phases', () => {
    let state = initRun('r5', '1.0.0', ['discovery', 'curation'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    // Even though curation is "available", in_progress wins
    const action = computeRecommendedAction(state, ['curation'])
    assert.equal(action.action, 'continue')
    assert.equal(action.phase, 'discovery')
  })

  it('returns "retry" when a phase has failed', () => {
    let state = initRun('r6', '1.0.0', ['discovery', 'curation'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    state = failPhase(state, 'discovery', 'API timeout', tempDir)
    const action = computeRecommendedAction(state, [])
    assert.equal(action.action, 'retry')
    assert.equal(action.phase, 'discovery')
    assert.ok(action.reason.includes('discovery'))
    assert.ok(action.reason.toLowerCase().includes('retry'))
  })

  it('does not return "retry" when no phase has failed', () => {
    const state = initRun('r7', '1.0.0', ['discovery', 'curation'], tempDir)
    // All phases are pending; no failures
    const action = computeRecommendedAction(state, ['discovery'])
    assert.notEqual(action.action, 'retry')
    assert.equal(action.action, 'start_phase')
  })

  it('prefers in_progress over failed phase', () => {
    let state = initRun('r8', '1.0.0', ['discovery', 'curation'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    // Manually mark curation as failed to simulate a state where one phase is in_progress and another failed
    state.phases.curation.status = 'failed'
    state.phases.curation.error = 'earlier failure'
    const action = computeRecommendedAction(state, [])
    assert.equal(action.action, 'continue')
    assert.equal(action.phase, 'discovery')
  })
})

describe('computeRunWarnings', () => {
  it('returns empty array when no warnings', () => {
    const state = initRun('r-clean', '1.0.0', ['discovery'], tempDir)
    const warnings = computeRunWarnings(state, new Set())
    assert.deepEqual(warnings, [])
  })

  it('flags in_progress phase that exceeds stale threshold', () => {
    let state = initRun('r-stale', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    // Inject a started_at from 2 hours ago
    const twoHoursAgo = new Date(Date.now() - 7200_000).toISOString()
    state.phases.discovery.started_at = twoHoursAgo
    const warnings = computeRunWarnings(state, new Set())
    assert.equal(warnings.length, 1)
    assert.ok(warnings[0].includes('discovery'))
    assert.ok(warnings[0].includes('1 hour'))
  })

  it('does not flag in_progress phase under stale threshold', () => {
    let state = initRun('r-fresh', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    // started_at is just now from startPhase
    const warnings = computeRunWarnings(state, new Set())
    assert.deepEqual(warnings, [])
  })

  it('flags failed optional phase', () => {
    let state = initRun('r-opt-fail', '1.0.0', ['discovery', 'curation'], tempDir)
    state = startPhase(state, 'curation', tempDir)
    state = failPhase(state, 'curation', 'API timeout', tempDir)
    const warnings = computeRunWarnings(state, new Set(['curation']))
    assert.equal(warnings.length, 1)
    assert.ok(warnings[0].includes('curation'))
    assert.ok(warnings[0].includes('API timeout'))
    assert.ok(warnings[0].toLowerCase().includes('optional'))
  })

  it('does not flag failed non-optional phase', () => {
    let state = initRun('r-req-fail', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    state = failPhase(state, 'discovery', 'hard failure', tempDir)
    const warnings = computeRunWarnings(state, new Set()) // discovery is NOT optional
    assert.deepEqual(warnings, [])
  })

  it('respects injected nowMs for deterministic stale detection', () => {
    let state = initRun('r-now', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    const startedMs = new Date(state.phases.discovery.started_at!).getTime()
    // nowMs just past threshold
    const nowMs = startedMs + STALE_PHASE_THRESHOLD_MS + 1000
    const warnings = computeRunWarnings(state, new Set(), nowMs)
    assert.equal(warnings.length, 1)
  })

  it('produces both kinds of warnings when applicable', () => {
    let state = initRun('r-both', '1.0.0', ['discovery', 'curation'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    // Make discovery stale
    state.phases.discovery.started_at = new Date(Date.now() - 7200_000).toISOString()
    // Fail curation as optional
    state = startPhase(state, 'curation', tempDir)
    state = failPhase(state, 'curation', 'flaky', tempDir)
    const warnings = computeRunWarnings(state, new Set(['curation']))
    assert.equal(warnings.length, 2)
  })
})

describe('atomicRename', () => {
  it('renames a file successfully (normal path)', () => {
    const src = join(tempDir, 'source.json')
    const dest = join(tempDir, 'dest.json')
    writeFileSync(src, '{"ok":true}', 'utf-8')

    atomicRename(src, dest)

    assert.ok(existsSync(dest))
    assert.ok(!existsSync(src))
    assert.equal(readFileSync(dest, 'utf-8'), '{"ok":true}')
  })

  it('overwrites an existing destination file', () => {
    const src = join(tempDir, 'new.json')
    const dest = join(tempDir, 'old.json')
    writeFileSync(dest, '{"version":1}', 'utf-8')
    writeFileSync(src, '{"version":2}', 'utf-8')

    atomicRename(src, dest)

    assert.ok(existsSync(dest))
    assert.ok(!existsSync(src))
    assert.equal(readFileSync(dest, 'utf-8'), '{"version":2}')
  })

  it('re-throws non-EPERM errors from renameSync', () => {
    // Attempt to rename a nonexistent file — triggers ENOENT, not EPERM
    const src = join(tempDir, 'nonexistent.json')
    const dest = join(tempDir, 'dest.json')

    assert.throws(
      () => atomicRename(src, dest),
      (err: unknown) => err instanceof Error && (err as NodeJS.ErrnoException).code === 'ENOENT',
    )
  })

  it('persist round-trip uses atomicRename (no leftover .tmp file)', () => {
    const state = initRun('atomic-test', '1.0.0', ['discovery'], tempDir)

    // Verify final file exists and tmp file is cleaned up
    assert.ok(existsSync(join(tempDir, 'run-state.json')))
    assert.ok(!existsSync(join(tempDir, 'run-state.json.tmp')))

    // Verify content integrity
    const loaded = loadRunState(tempDir)
    assert.ok(loaded)
    assert.equal(loaded.run_id, 'atomic-test')
    assert.equal(loaded.phases.discovery.status, state.phases.discovery.status)
  })
})

// ── JSON integrity validation (item 2.3) ─────────────────────

describe('validateRunStateIntegrity', () => {
  it('accepts a valid run state object', () => {
    const state = {
      run_id: 'test-run',
      pipeline_version: '1.0.0',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      status: 'initialized',
      phases: { discovery: { phase_name: 'discovery', status: 'pending' } },
      available_artifacts: [],
      config_path: '/tmp/test',
    }
    assert.equal(validateRunStateIntegrity(state), null)
  })

  it('rejects null', () => {
    const err = validateRunStateIntegrity(null)
    assert.ok(err)
    assert.ok(err.includes('not a valid object'))
  })

  it('rejects non-object (string)', () => {
    const err = validateRunStateIntegrity('not an object')
    assert.ok(err)
    assert.ok(err.includes('not a valid object'))
  })

  it('rejects missing updated_at', () => {
    const state = { run_id: 'r1', status: 'initialized', phases: { a: {} } }
    const err = validateRunStateIntegrity(state)
    assert.ok(err)
    assert.ok(err.includes('updated_at'))
  })

  it('rejects invalid ISO date in updated_at', () => {
    const state = { run_id: 'r1', updated_at: 'not-a-date', status: 'initialized', phases: { a: {} } }
    const err = validateRunStateIntegrity(state)
    assert.ok(err)
    assert.ok(err.includes('not a valid ISO date'))
  })

  it('rejects empty phases object', () => {
    const state = { run_id: 'r1', updated_at: new Date().toISOString(), status: 'initialized', phases: {} }
    const err = validateRunStateIntegrity(state)
    assert.ok(err)
    assert.ok(err.includes('empty'))
  })

  it('rejects phases that is an array', () => {
    const state = { run_id: 'r1', updated_at: new Date().toISOString(), status: 'initialized', phases: [] }
    const err = validateRunStateIntegrity(state)
    assert.ok(err)
    assert.ok(err.includes('not a valid object'))
  })

  it('rejects missing run_id', () => {
    const state = { updated_at: new Date().toISOString(), status: 'initialized', phases: { a: {} } }
    const err = validateRunStateIntegrity(state)
    assert.ok(err)
    assert.ok(err.includes('run_id'))
  })

  it('rejects empty run_id', () => {
    const state = { run_id: '', updated_at: new Date().toISOString(), status: 'initialized', phases: { a: {} } }
    const err = validateRunStateIntegrity(state)
    assert.ok(err)
    assert.ok(err.includes('run_id'))
  })

  it('rejects invalid status value', () => {
    const state = { run_id: 'r1', updated_at: new Date().toISOString(), status: 'unknown_status', phases: { a: {} } }
    const err = validateRunStateIntegrity(state)
    assert.ok(err)
    assert.ok(err.includes('invalid status'))
  })

  it('accepts all valid status values', () => {
    for (const status of ['initialized', 'running', 'completed', 'failed']) {
      const state = { run_id: 'r1', updated_at: new Date().toISOString(), status, phases: { a: {} } }
      assert.equal(validateRunStateIntegrity(state), null, `expected status "${status}" to be valid`)
    }
  })
})

describe('loadRunState integrity validation', () => {
  it('throws PipelineError for invalid JSON in state file', () => {
    writeFileSync(join(tempDir, 'run-state.json'), '{ invalid json !!!', 'utf-8')
    assert.throws(
      () => loadRunState(tempDir),
      (err: unknown) => err instanceof PipelineError && err.error_class === 'file_io_error',
    )
  })

  it('throws PipelineError for state with empty phases', () => {
    const corrupt = { run_id: 'r1', updated_at: new Date().toISOString(), status: 'running', phases: {}, available_artifacts: [], config_path: tempDir }
    writeFileSync(join(tempDir, 'run-state.json'), JSON.stringify(corrupt), 'utf-8')
    assert.throws(
      () => loadRunState(tempDir),
      (err: unknown) => err instanceof PipelineError && err.message.includes('empty'),
    )
  })

  it('throws PipelineError for state with invalid updated_at', () => {
    const corrupt = { run_id: 'r1', updated_at: 'bad-date', status: 'running', phases: { a: {} }, available_artifacts: [], config_path: tempDir }
    writeFileSync(join(tempDir, 'run-state.json'), JSON.stringify(corrupt), 'utf-8')
    assert.throws(
      () => loadRunState(tempDir),
      (err: unknown) => err instanceof PipelineError && err.message.includes('ISO date'),
    )
  })

  it('returns null for unreadable file path (no state file)', () => {
    const result = loadRunState(join(tempDir, 'nonexistent-subdir'))
    assert.equal(result, null)
  })
})

// ── recoverRun (item 2.4) ────────────────────────────────────

describe('recoverRun', () => {
  it('recovers a clean state with no in_progress phases', () => {
    const state = initRun('recover-clean', '1.0.0', ['discovery', 'curation'], tempDir)
    const result = recoverRun(tempDir)

    assert.equal(result.state.run_id, 'recover-clean')
    assert.deepEqual(result.recovered_phases, [])
    assert.deepEqual(result.warnings, [])
  })

  it('resets in_progress phases to pending and increments retry_count', () => {
    let state = initRun('recover-ip', '1.0.0', ['discovery', 'curation'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    // discovery is now in_progress — simulate crash/restart
    const result = recoverRun(tempDir)

    assert.equal(result.state.phases.discovery.status, 'pending')
    assert.equal(result.state.phases.discovery.retry_count, 1)
    assert.ok(result.recovered_phases.includes('discovery'))
    assert.equal(result.warnings.length, 1)
    assert.ok(result.warnings[0].includes('discovery'))
    assert.ok(result.warnings[0].includes('retry #1'))
  })

  it('recovers multiple in_progress phases', () => {
    let state = initRun('recover-multi', '1.0.0', ['discovery', 'curation', 'synthesis'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    state = startPhase(state, 'curation', tempDir)
    // Both in_progress
    const result = recoverRun(tempDir)

    assert.equal(result.recovered_phases.length, 2)
    assert.ok(result.recovered_phases.includes('discovery'))
    assert.ok(result.recovered_phases.includes('curation'))
    assert.equal(result.state.phases.discovery.status, 'pending')
    assert.equal(result.state.phases.curation.status, 'pending')
  })

  it('persists recovered state to disk', () => {
    let state = initRun('recover-persist', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)

    recoverRun(tempDir)

    const loaded = loadRunState(tempDir)
    assert.ok(loaded)
    assert.equal(loaded.phases.discovery.status, 'pending')
    assert.equal(loaded.phases.discovery.retry_count, 1)
  })

  it('throws PipelineError when no state file exists', () => {
    const emptyDir = mkdtempSync(join(tmpdir(), 'pipeline-recover-empty-'))
    try {
      assert.throws(
        () => recoverRun(emptyDir),
        (err: unknown) => err instanceof PipelineError && err.error_class === 'file_io_error',
      )
    } finally {
      rmSync(emptyDir, { recursive: true, force: true })
    }
  })

  it('corrects run status after recovering in_progress phases', () => {
    let state = initRun('recover-status', '1.0.0', ['discovery', 'curation'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    // status is 'running', one phase in_progress
    const result = recoverRun(tempDir)

    // After recovery: no completed phases, no in_progress → initialized
    assert.equal(result.state.status, 'initialized')
  })

  it('keeps running status when completed phases exist after recovery', () => {
    let state = initRun('recover-partial', '1.0.0', ['discovery', 'curation'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    state = completePhase(state, 'discovery', tempDir)
    state = startPhase(state, 'curation', tempDir)
    // discovery=completed, curation=in_progress
    const result = recoverRun(tempDir)

    assert.equal(result.state.phases.curation.status, 'pending')
    assert.equal(result.state.status, 'running')
  })

  it('clears started_at on recovered phases', () => {
    let state = initRun('recover-clear', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    assert.ok(state.phases.discovery.started_at)

    const result = recoverRun(tempDir)
    assert.equal(result.state.phases.discovery.started_at, undefined)
  })
})

// ── persist() is called by all state mutation functions (item 2.5) ──

describe('persist on all mutations', () => {
  it('initRun persists to disk', () => {
    initRun('persist-init', '1.0.0', ['discovery'], tempDir)
    assert.ok(existsSync(join(tempDir, 'run-state.json')))
    const loaded = loadRunState(tempDir)
    assert.ok(loaded)
    assert.equal(loaded.run_id, 'persist-init')
  })

  it('startPhase persists to disk', () => {
    let state = initRun('persist-start', '1.0.0', ['discovery'], tempDir)
    startPhase(state, 'discovery', tempDir)
    const loaded = loadRunState(tempDir)
    assert.ok(loaded)
    assert.equal(loaded.phases.discovery.status, 'in_progress')
  })

  it('completePhase persists to disk', () => {
    let state = initRun('persist-complete', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    completePhase(state, 'discovery', tempDir)
    const loaded = loadRunState(tempDir)
    assert.ok(loaded)
    assert.equal(loaded.phases.discovery.status, 'completed')
  })

  it('failPhase persists to disk', () => {
    let state = initRun('persist-fail', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    failPhase(state, 'discovery', 'test error', tempDir)
    const loaded = loadRunState(tempDir)
    assert.ok(loaded)
    assert.equal(loaded.phases.discovery.status, 'failed')
    assert.equal(loaded.phases.discovery.error, 'test error')
  })

  it('retryPhase persists to disk', () => {
    let state = initRun('persist-retry', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    state = failPhase(state, 'discovery', 'err', tempDir)
    retryPhase(state, 'discovery', tempDir)
    const loaded = loadRunState(tempDir)
    assert.ok(loaded)
    assert.equal(loaded.phases.discovery.status, 'pending')
    assert.equal(loaded.phases.discovery.retry_count, 1)
  })

  it('skipPhase persists to disk', () => {
    let state = initRun('persist-skip', '1.0.0', ['discovery'], tempDir)
    skipPhase(state, 'discovery', tempDir)
    const loaded = loadRunState(tempDir)
    assert.ok(loaded)
    assert.equal(loaded.phases.discovery.status, 'skipped')
  })

  it('addArtifact persists to disk', () => {
    let state = initRun('persist-artifact', '1.0.0', ['discovery'], tempDir)
    addArtifact(state, {
      type: 'raw-collection',
      path: 'test.json',
      phase: 'discovery',
      created_at: new Date().toISOString(),
    }, tempDir)
    const loaded = loadRunState(tempDir)
    assert.ok(loaded)
    assert.equal(loaded.available_artifacts.length, 1)
    assert.equal(loaded.available_artifacts[0].type, 'raw-collection')
  })
})

// ── Phase-status extraction helpers ──────────────────────────

describe('getCompletedPhases', () => {
  it('returns empty array when no phases are completed', () => {
    const state = initRun('test-helpers', '1.0.0', ['a', 'b', 'c'], tempDir)
    assert.deepEqual(getCompletedPhases(state), [])
  })

  it('returns names of completed phases', () => {
    let state = initRun('test-helpers', '1.0.0', ['a', 'b', 'c'], tempDir)
    state = startPhase(state, 'a', tempDir)
    state = completePhase(state, 'a', tempDir)
    state = startPhase(state, 'b', tempDir)
    state = completePhase(state, 'b', tempDir)
    assert.deepEqual(getCompletedPhases(state), ['a', 'b'])
  })
})

describe('getSkippedPhases', () => {
  it('returns empty array when no phases are skipped', () => {
    const state = initRun('test-helpers', '1.0.0', ['a', 'b'], tempDir)
    assert.deepEqual(getSkippedPhases(state), [])
  })

  it('returns names of skipped phases', () => {
    let state = initRun('test-helpers', '1.0.0', ['a', 'b', 'c'], tempDir)
    state = skipPhase(state, 'b', tempDir)
    state = skipPhase(state, 'c', tempDir)
    assert.deepEqual(getSkippedPhases(state), ['b', 'c'])
  })
})

describe('getInProgressPhases', () => {
  it('returns empty array when no phases are in progress', () => {
    const state = initRun('test-helpers', '1.0.0', ['a', 'b'], tempDir)
    assert.deepEqual(getInProgressPhases(state), [])
  })

  it('returns names of in_progress phases', () => {
    let state = initRun('test-helpers', '1.0.0', ['a', 'b', 'c'], tempDir)
    state = startPhase(state, 'a', tempDir)
    assert.deepEqual(getInProgressPhases(state), ['a'])
  })
})

describe('getArtifactTypes', () => {
  it('returns empty array when no artifacts are registered', () => {
    const state = initRun('test-helpers', '1.0.0', ['a'], tempDir)
    assert.deepEqual(getArtifactTypes(state), [])
  })

  it('returns all artifact types', () => {
    let state = initRun('test-helpers', '1.0.0', ['a'], tempDir)
    state = addArtifact(state, { type: 'raw-collection', path: 'a.json', phase: 'a', created_at: new Date().toISOString() }, tempDir)
    state = addArtifact(state, { type: 'design-spec', path: 'b.json', phase: 'a', created_at: new Date().toISOString() }, tempDir)
    assert.deepEqual(getArtifactTypes(state), ['raw-collection', 'design-spec'])
  })

  it('includes duplicate types when multiple artifacts share the same type', () => {
    let state = initRun('test-helpers', '1.0.0', ['a'], tempDir)
    state = addArtifact(state, { type: 'raw-collection', path: 'a1.json', phase: 'a', created_at: new Date().toISOString() }, tempDir)
    state = addArtifact(state, { type: 'raw-collection', path: 'a2.json', phase: 'a', created_at: new Date().toISOString() }, tempDir)
    assert.deepEqual(getArtifactTypes(state), ['raw-collection', 'raw-collection'])
  })
})
