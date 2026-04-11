import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import {
  mkdtempSync,
  rmSync,
  writeFileSync,
  mkdirSync,
  readFileSync,
  existsSync,
} from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import {
  handleRegisterScaffoldOutputs,
  type RegisterScaffoldOutputsContext,
} from '../misc-handlers.ts'
import { PipelineError } from '../types.ts'
import type { RunState } from '../types.ts'
import { initRun, addArtifact } from '../run-state.ts'

/**
 * Build a minimal RegisterScaffoldOutputsContext backed by real run-state
 * helpers. Mirrors the `makeCtx` pattern used in artifact-handlers.test.ts.
 */
function makeCtx(
  tempDir: string,
  runState: { current: RunState },
  runDir: string,
): RegisterScaffoldOutputsContext {
  return {
    requireRun: () => runState.current,
    setActiveRun: (state) => {
      runState.current = state
    },
    getActiveRunDir: () => runDir,
    baseDirFromRunDir: () => tempDir,
    addArtifact: (state, ref, dir) => addArtifact(state, ref, dir),
  }
}

function readEvents(runDir: string): Array<Record<string, unknown>> {
  const eventsPath = join(runDir, 'events.jsonl')
  if (!existsSync(eventsPath)) return []
  const content = readFileSync(eventsPath, 'utf-8')
  const events: Array<Record<string, unknown>> = []
  for (const line of content.split('\n')) {
    const trimmed = line.trim()
    if (!trimmed) continue
    try {
      events.push(JSON.parse(trimmed) as Record<string, unknown>)
    } catch {
      // ignore malformed
    }
  }
  return events
}

describe('handleRegisterScaffoldOutputs', () => {
  let tempDir: string
  let runDir: string
  let scaffoldDir: string

  beforeEach(() => {
    tempDir = mkdtempSync(join(tmpdir(), 'scaffold-register-test-'))
    runDir = join(tempDir, 'runs', 'scaffold-run')
    scaffoldDir = join(tempDir, 'scaffold')
  })

  afterEach(() => {
    rmSync(tempDir, { recursive: true, force: true })
  })

  it('registers four scaffold files with correct types and phase', () => {
    const runState = {
      current: initRun(
        'scaffold-run-1',
        '1.0.0',
        ['implementation_scaffold'],
        runDir,
      ),
    }
    const ctx = makeCtx(tempDir, runState, runDir)

    // Seed the scaffold dir with the four canonical files.
    mkdirSync(scaffoldDir, { recursive: true })
    writeFileSync(
      join(scaffoldDir, 'IMPLEMENTATION-PLAN.md'),
      '# Implementation Plan\n',
      'utf-8',
    )
    writeFileSync(
      join(scaffoldDir, 'SCAFFOLD-INDEX.md'),
      '# Scaffold Index\n',
      'utf-8',
    )
    writeFileSync(
      join(scaffoldDir, 'AGENTS.md.proposed-diff.md'),
      '# AGENTS.md proposed diff\n',
      'utf-8',
    )
    writeFileSync(
      join(scaffoldDir, 'scaffold-manifest.json'),
      JSON.stringify({ version: 1, files: [] }, null, 2),
      'utf-8',
    )

    const result = handleRegisterScaffoldOutputs({}, ctx)
    assert.ok(!result.isError, 'response should not be an error')

    const parsed = JSON.parse(result.json) as {
      status: string
      data: {
        scaffold_dir: string
        registered: Array<{
          filename: string
          artifact_type: string
          path: string
          version: number
        }>
        skipped_existing: string[]
      }
    }

    assert.equal(parsed.status, 'ok')
    assert.equal(parsed.data.registered.length, 4)
    assert.equal(parsed.data.skipped_existing.length, 0)

    // Run state should now have all 4 artifacts.
    assert.equal(
      runState.current.available_artifacts.length,
      4,
      'expected four registered artifacts in run state',
    )

    // Exactly one scaffold-manifest, three scaffold-documents.
    const manifestRefs = runState.current.available_artifacts.filter(
      (a) => a.type === 'scaffold-manifest',
    )
    const docRefs = runState.current.available_artifacts.filter(
      (a) => a.type === 'scaffold-document',
    )
    assert.equal(manifestRefs.length, 1, 'expected exactly one scaffold-manifest')
    assert.equal(docRefs.length, 3, 'expected exactly three scaffold-document entries')

    // The manifest ref must come from scaffold-manifest.json.
    assert.ok(
      manifestRefs[0].path.endsWith('scaffold-manifest.json'),
      'manifest ref should point at scaffold-manifest.json',
    )

    // Phase state should list all four output artifacts.
    const phaseState = runState.current.phases['implementation_scaffold']
    assert.ok(phaseState, 'implementation_scaffold phase should exist')
    assert.equal(
      phaseState.output_artifacts.length,
      4,
      'phase.output_artifacts should contain all four scaffold files',
    )

    // The single manifest gets version 1; successive scaffold-document entries
    // in the same phase auto-increment because they share (type, phase).
    const manifestEntry = parsed.data.registered.find(
      (e) => e.artifact_type === 'scaffold-manifest',
    )
    assert.ok(manifestEntry, 'manifest should appear in registered list')
    assert.equal(manifestEntry.version, 1)

    const docEntries = parsed.data.registered.filter(
      (e) => e.artifact_type === 'scaffold-document',
    )
    assert.equal(docEntries.length, 3)
    // Versions assigned to the three scaffold-documents should be the set {1, 2, 3}.
    const docVersions = docEntries.map((e) => e.version).sort()
    assert.deepEqual(docVersions, [1, 2, 3])

    // One artifact_stored event per file should have been appended.
    const events = readEvents(runDir)
    const storedEvents = events.filter((e) => e.event === 'artifact_stored')
    assert.equal(
      storedEvents.length,
      4,
      'expected one artifact_stored event per registered file',
    )
    for (const evt of storedEvents) {
      assert.equal(evt.phase, 'implementation_scaffold')
      assert.equal(evt.run_id, 'scaffold-run-1')
      const details = evt.details as Record<string, unknown>
      assert.ok(
        details.artifact_type === 'scaffold-manifest' ||
          details.artifact_type === 'scaffold-document',
        'event details.artifact_type should be scaffold-manifest or scaffold-document',
      )
      assert.equal(typeof details.size_bytes, 'number')
    }
  })

  it('is idempotent on a second call — files already registered are skipped', () => {
    const runState = {
      current: initRun(
        'scaffold-run-2',
        '1.0.0',
        ['implementation_scaffold'],
        runDir,
      ),
    }
    const ctx = makeCtx(tempDir, runState, runDir)

    mkdirSync(scaffoldDir, { recursive: true })
    writeFileSync(
      join(scaffoldDir, 'IMPLEMENTATION-PLAN.md'),
      '# Plan\n',
      'utf-8',
    )
    writeFileSync(
      join(scaffoldDir, 'SCAFFOLD-INDEX.md'),
      '# Index\n',
      'utf-8',
    )
    writeFileSync(
      join(scaffoldDir, 'AGENTS.md.proposed-diff.md'),
      '# Diff\n',
      'utf-8',
    )
    writeFileSync(
      join(scaffoldDir, 'scaffold-manifest.json'),
      JSON.stringify({ version: 1 }),
      'utf-8',
    )

    // First call: register all four.
    const first = handleRegisterScaffoldOutputs({}, ctx)
    const firstParsed = JSON.parse(first.json) as {
      data: { registered: unknown[]; skipped_existing: string[] }
    }
    assert.equal(firstParsed.data.registered.length, 4)
    assert.equal(firstParsed.data.skipped_existing.length, 0)
    assert.equal(runState.current.available_artifacts.length, 4)

    // Second call: all four should be skipped, no new registrations.
    const second = handleRegisterScaffoldOutputs({}, ctx)
    const secondParsed = JSON.parse(second.json) as {
      data: { registered: unknown[]; skipped_existing: string[] }
    }
    assert.equal(secondParsed.data.registered.length, 0)
    assert.equal(
      secondParsed.data.skipped_existing.length,
      4,
      'second call should report all four files as skipped',
    )

    // Run state should still have exactly 4 artifacts (no duplicates).
    assert.equal(
      runState.current.available_artifacts.length,
      4,
      'idempotent call should not create duplicate entries',
    )

    // Phase output_artifacts should also remain at 4.
    const phaseState = runState.current.phases['implementation_scaffold']
    assert.equal(phaseState.output_artifacts.length, 4)

    // No additional artifact_stored events for the skipped files.
    const events = readEvents(runDir)
    const storedEvents = events.filter((e) => e.event === 'artifact_stored')
    assert.equal(
      storedEvents.length,
      4,
      'idempotent call should not emit new artifact_stored events',
    )
  })

  it('throws PipelineError(artifact_not_found) when scaffold dir is missing', () => {
    const runState = {
      current: initRun(
        'scaffold-run-3',
        '1.0.0',
        ['implementation_scaffold'],
        runDir,
      ),
    }
    const ctx = makeCtx(tempDir, runState, runDir)

    const missingPath = join(tempDir, 'does-not-exist', 'scaffold')
    assert.throws(
      () => handleRegisterScaffoldOutputs({ scaffold_dir: missingPath }, ctx),
      (err: unknown) => {
        assert.ok(
          err instanceof PipelineError,
          'should throw a PipelineError',
        )
        assert.equal(
          (err as PipelineError).error_class,
          'artifact_not_found',
          'error_class should be artifact_not_found',
        )
        return true
      },
    )

    // No artifacts should have been registered.
    assert.equal(runState.current.available_artifacts.length, 0)
  })
})
