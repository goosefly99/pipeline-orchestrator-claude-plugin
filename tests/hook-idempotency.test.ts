// Tests for hook idempotency and appendPhaseCompletedEvent wrapper (Fix 3.2)
//
// Covers:
//   1. runHooks(post_complete) skips when events.jsonl already has
//      phase_completed for the target phase, and does not add a duplicate.
//   2. runHooks(post_complete) actually fires when no prior phase_completed
//      event exists.
//   3. appendPhaseCompletedEvent validates phase existence + status and
//      suppresses duplicate writes.

import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, readFileSync, existsSync, writeFileSync } from 'node:fs'
import { dirname, basename, join } from 'node:path'
import { tmpdir } from 'node:os'
import { runHooks } from '../hooks.ts'
import type { HookContext } from '../hooks.ts'
import { appendEvent, appendPhaseCompletedEvent } from '../lifecycle-handlers.ts'
import { PipelineError } from '../types.ts'
import type { HookConfig, LifecycleEvent, RunState, PhaseState } from '../types.ts'

let tempDir: string

beforeEach(() => {
  tempDir = mkdtempSync(join(tmpdir(), 'hook-idempotency-test-'))
})

afterEach(() => {
  rmSync(tempDir, { recursive: true, force: true })
})

// ── Helpers ──────────────────────────────────────────────────

function makePhase(name: string, status: PhaseState['status']): PhaseState {
  return {
    phase_name: name,
    status,
    input_artifacts: [],
    output_artifacts: [],
    retry_count: 0,
    started_at: status !== 'pending' ? new Date().toISOString() : undefined,
    completed_at: status === 'completed' ? new Date().toISOString() : undefined,
  }
}

function makeRunState(phases: Record<string, PhaseState['status']>): RunState {
  const phaseMap: Record<string, PhaseState> = {}
  for (const [name, status] of Object.entries(phases)) {
    phaseMap[name] = makePhase(name, status)
  }
  return {
    run_id: 'test-run',
    pipeline_version: '1.0.0',
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    status: 'running',
    phases: phaseMap,
    available_artifacts: [],
    config_path: tempDir,
  }
}

function countPhaseCompletedEvents(runDir: string, phase: string): number {
  const eventsPath = join(runDir, 'events.jsonl')
  if (!existsSync(eventsPath)) return 0
  const content = readFileSync(eventsPath, 'utf-8')
  let count = 0
  for (const line of content.split('\n')) {
    const trimmed = line.trim()
    if (!trimmed) continue
    try {
      const parsed = JSON.parse(trimmed) as { event?: unknown; phase?: unknown }
      if (parsed.event === 'phase_completed' && parsed.phase === phase) count++
    } catch {
      // ignore
    }
  }
  return count
}

/**
 * Return the projectRoot/command pair to use for executing node as a hook.
 *
 * `validateHookPath` requires the resolved command to sit under projectRoot,
 * so we set projectRoot to the directory containing the currently running
 * node binary and pass its basename as the command. This works on Windows
 * (where hard-linking across protected dirs is denied) and on POSIX.
 *
 * Note: projectRoot used here is JUST for path validation; the run_dir in
 * the HookContext is separately set to tempDir where events.jsonl lives.
 */
function nodeHookCommand(): { projectRoot: string; command: string } {
  return {
    projectRoot: dirname(process.execPath),
    command: basename(process.execPath),
  }
}

// ── Test case 1: idempotency guard suppresses duplicate post_complete ──

describe('runHooks post_complete idempotency guard', () => {
  it('skips hooks and does not add a duplicate event when phase_completed already recorded', () => {
    // Pre-seed events.jsonl with a phase_completed event for phase1
    const preSeed: LifecycleEvent = {
      timestamp: new Date().toISOString(),
      event: 'phase_completed',
      phase: 'phase1',
      run_id: 'test-run',
    }
    appendEvent(tempDir, preSeed)

    assert.equal(countPhaseCompletedEvents(tempDir, 'phase1'), 1, 'pre-seed should place exactly one event')

    // Construct a hook that would, if run, add a second phase_completed event.
    // Because the idempotency guard should fire first, the hook command is
    // never actually invoked — but for safety we point it at a valid command
    // that would succeed anyway.
    const { projectRoot: hookRoot, command: nodeCommand } = nodeHookCommand()
    const scriptPath = join(tempDir, 'hook-script.mjs')
    writeFileSync(scriptPath, 'console.log("should_not_run")\n', 'utf-8')

    const hooks: HookConfig[] = [
      {
        trigger: 'post_complete',
        phase_filter: '*',
        command: nodeCommand,
        args: [scriptPath],
      },
    ]

    const ctx: HookContext = {
      run_id: 'test-run',
      phase: 'phase1',
      trigger: 'post_complete',
      project_root: hookRoot,
      run_dir: tempDir,
    }

    const results = runHooks(hooks, 'post_complete', ctx, hookRoot)

    assert.equal(results.length, 1, 'one hook should have matched')
    assert.equal(results[0].success, false, 'hook should be reported as not-successful')
    assert.ok(
      results[0].error !== undefined,
      'error message should be populated',
    )
    assert.ok(
      results[0].error!.includes('idempotency') || results[0].error!.includes('already recorded'),
      `error must mention idempotency or already recorded — got: "${results[0].error}"`,
    )

    // Ensure no duplicate event was written.
    assert.equal(
      countPhaseCompletedEvents(tempDir, 'phase1'),
      1,
      'exactly one phase_completed event should remain for phase1',
    )
  })

  it('actually runs post_complete hooks when no prior phase_completed event exists', () => {
    // Phase3 has no phase_completed event in events.jsonl (file may not even exist).
    // Install a small .mjs script that writes a marker file so we can verify
    // the hook actually ran.
    const { projectRoot: hookRoot, command: nodeCommand } = nodeHookCommand()
    const markerPath = join(tempDir, 'hook-marker.txt')
    const scriptPath = join(tempDir, 'phase3-hook.mjs')
    // Use JSON.stringify on the marker path so Windows backslashes survive.
    writeFileSync(
      scriptPath,
      `import { writeFileSync } from 'node:fs'\nwriteFileSync(${JSON.stringify(markerPath)}, 'ran', 'utf-8')\n`,
      'utf-8',
    )

    const hooks: HookConfig[] = [
      {
        trigger: 'post_complete',
        phase_filter: '*',
        command: nodeCommand,
        args: [scriptPath],
        timeout_ms: 10000,
      },
    ]

    const ctx: HookContext = {
      run_id: 'test-run',
      phase: 'phase3',
      trigger: 'post_complete',
      project_root: hookRoot,
      run_dir: tempDir,
    }

    // Pre-seed an UNRELATED phase_completed event for phase1 so the events
    // file exists but does not contain phase3.
    appendEvent(tempDir, {
      timestamp: new Date().toISOString(),
      event: 'phase_completed',
      phase: 'phase1',
      run_id: 'test-run',
    })

    const results = runHooks(hooks, 'post_complete', ctx, hookRoot)

    assert.equal(results.length, 1, 'one hook should have matched')
    assert.equal(
      results[0].success,
      true,
      `hook should have succeeded — got error: "${results[0].error ?? ''}", stderr: "${results[0].stderr ?? ''}"`,
    )
    assert.ok(existsSync(markerPath), 'hook script should have written marker file')
    assert.equal(readFileSync(markerPath, 'utf-8'), 'ran')
  })
})

// ── Test case 3: appendPhaseCompletedEvent validation + idempotency ──

describe('appendPhaseCompletedEvent', () => {
  it('writes once then suppresses duplicate writes for the same phase', () => {
    const state = makeRunState({ phaseA: 'completed' })

    const firstWrite = appendPhaseCompletedEvent(tempDir, state, 'phaseA')
    assert.equal(firstWrite, true, 'first call should return true')
    assert.equal(countPhaseCompletedEvents(tempDir, 'phaseA'), 1)

    const secondWrite = appendPhaseCompletedEvent(tempDir, state, 'phaseA')
    assert.equal(secondWrite, false, 'second call should return false')
    assert.equal(
      countPhaseCompletedEvents(tempDir, 'phaseA'),
      1,
      'events.jsonl should still contain exactly one phase_completed for phaseA',
    )
  })

  it('throws PipelineError state_transition_error when phase is not in state', () => {
    const state = makeRunState({ phaseA: 'completed' })

    assert.throws(
      () => appendPhaseCompletedEvent(tempDir, state, 'phaseB'),
      (err: unknown) =>
        err instanceof PipelineError &&
        err.error_class === 'state_transition_error' &&
        /phaseB/.test(err.message),
    )
  })

  it('throws PipelineError state_transition_error when phase status is not completed', () => {
    const state = makeRunState({ phaseC: 'in_progress' })

    assert.throws(
      () => appendPhaseCompletedEvent(tempDir, state, 'phaseC'),
      (err: unknown) =>
        err instanceof PipelineError &&
        err.error_class === 'state_transition_error' &&
        /in_progress/.test(err.message),
    )
  })
})
