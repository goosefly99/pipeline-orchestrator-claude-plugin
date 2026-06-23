// Tests for runPrePipelineInitHooks and pre_pipeline_init TOML loader entry.
//
// The runPrePipelineInitHooks helper is blocking: any hook failure (path
// traversal, malformed JSON stdout, non-zero exit) must surface as a
// PipelineError with error_class === 'configuration_error'. Successful hooks
// merge their emitted `parameters` (later wins) and accumulate any
// `userPrompts` (plus legacy `user_prompts` / `user_prompt` aliases).

import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, writeFileSync, existsSync, readFileSync } from 'node:fs'
import { dirname, basename, join } from 'node:path'
import { tmpdir } from 'node:os'
import { runPrePipelineInitHooks } from '../hooks.ts'
import type { PreInitHookContext, PreInitHookResult } from '../hooks.ts'
import { loadHooksConfig } from '../toml-loader.ts'
import { PipelineError } from '../types.ts'
import type {
  HookConfig,
  RunState,
  PipelineConfig,
  StorageConfig,
  PhaseDefinition,
  GateResult,
  PipelineRunParameters,
} from '../types.ts'
import {
  handleInitRun,
  type LifecycleContext,
} from '../lifecycle-handlers.ts'
import {
  initRun as initRunState,
  addArtifact as addArtifactState,
  skipPhase as skipPhaseState,
  loadRunState as loadRunStateFn,
} from '../run-state.ts'
import type { InputSatisfaction } from '../dag.ts'
import type { RecommendedAction, RecoveryResult } from '../run-state.ts'

let tempDir: string

beforeEach(() => {
  tempDir = mkdtempSync(join(tmpdir(), 'pre-init-hook-test-'))
})

afterEach(() => {
  rmSync(tempDir, { recursive: true, force: true })
})

/**
 * Return the projectRoot/command pair that lets `validateHookPath` accept the
 * running node binary. Mirrors the nodeHookCommand helper in
 * hook-idempotency.test.ts: projectRoot is the directory containing the node
 * binary, command is the binary's basename. Script files live in tempDir and
 * are passed as args[0].
 */
function nodeHookCommand(): { projectRoot: string; command: string } {
  return {
    projectRoot: dirname(process.execPath),
    command: basename(process.execPath),
  }
}

function makeContext(): PreInitHookContext {
  return {
    trigger: 'pre_pipeline_init',
    project_root: tempDir,
    requested_args: { run_id: 'test-run', skip_phases: [] },
  }
}

// ── runPrePipelineInitHooks ─────────────────────────────────

describe('runPrePipelineInitHooks', () => {
  it('returns empty parameters and userPrompts when no hooks match', () => {
    const result = runPrePipelineInitHooks([], makeContext(), tempDir)
    assert.deepEqual(result, { parameters: {}, userPrompts: [] })
  })

  it('also returns empty when hooks exist but none match pre_pipeline_init', () => {
    const hooks: HookConfig[] = [
      { trigger: 'pre_start', command: 'scripts/x.sh', phase_filter: '*' },
    ]
    const result = runPrePipelineInitHooks(hooks, makeContext(), tempDir)
    assert.deepEqual(result, { parameters: {}, userPrompts: [] })
  })

  it('flows parameters from a hook through to the merged result', () => {
    const { projectRoot: hookRoot, command: nodeCommand } = nodeHookCommand()
    const scriptPath = join(tempDir, 'hook-params.mjs')
    writeFileSync(
      scriptPath,
      'process.stdout.write(JSON.stringify({parameters:{run_name:"test-run",phase_model:"claude-opus-4-6"}}))\n',
      'utf-8',
    )

    const hooks: HookConfig[] = [
      {
        trigger: 'pre_pipeline_init',
        command: nodeCommand,
        args: [scriptPath],
        timeout_ms: 10000,
      },
    ]

    const result = runPrePipelineInitHooks(hooks, makeContext(), hookRoot)
    assert.equal(result.parameters.run_name, 'test-run')
    assert.equal(result.parameters.phase_model, 'claude-opus-4-6')
    assert.deepEqual(result.userPrompts, [])
  })

  it('flows userPrompts from a hook through to the merged result', () => {
    const { projectRoot: hookRoot, command: nodeCommand } = nodeHookCommand()
    const scriptPath = join(tempDir, 'hook-prompts.mjs')
    writeFileSync(
      scriptPath,
      'process.stdout.write(JSON.stringify({userPrompts:["What is run_name?"]}))\n',
      'utf-8',
    )

    const hooks: HookConfig[] = [
      {
        trigger: 'pre_pipeline_init',
        command: nodeCommand,
        args: [scriptPath],
        timeout_ms: 10000,
      },
    ]

    const result = runPrePipelineInitHooks(hooks, makeContext(), hookRoot)
    assert.deepEqual(result.userPrompts, ['What is run_name?'])
    assert.deepEqual(result.parameters, {})
  })

  it('flows both parameters and userPrompts from a single hook', () => {
    const { projectRoot: hookRoot, command: nodeCommand } = nodeHookCommand()
    const scriptPath = join(tempDir, 'hook-both.mjs')
    writeFileSync(
      scriptPath,
      'process.stdout.write(JSON.stringify({parameters:{phase_model:"x"},userPrompts:["confirm?"]}))\n',
      'utf-8',
    )

    const hooks: HookConfig[] = [
      {
        trigger: 'pre_pipeline_init',
        command: nodeCommand,
        args: [scriptPath],
        timeout_ms: 10000,
      },
    ]

    const result = runPrePipelineInitHooks(hooks, makeContext(), hookRoot)
    assert.equal(result.parameters.phase_model, 'x')
    assert.deepEqual(result.userPrompts, ['confirm?'])
  })

  it('accepts legacy singular user_prompt alongside the plural userPrompts', () => {
    const { projectRoot: hookRoot, command: nodeCommand } = nodeHookCommand()
    const scriptPath = join(tempDir, 'hook-legacy.mjs')
    writeFileSync(
      scriptPath,
      'process.stdout.write(JSON.stringify({user_prompt:"legacy"}))\n',
      'utf-8',
    )

    const hooks: HookConfig[] = [
      {
        trigger: 'pre_pipeline_init',
        command: nodeCommand,
        args: [scriptPath],
        timeout_ms: 10000,
      },
    ]

    const result = runPrePipelineInitHooks(hooks, makeContext(), hookRoot)
    assert.deepEqual(result.userPrompts, ['legacy'])
  })

  it('throws PipelineError configuration_error on non-JSON stdout', () => {
    const { projectRoot: hookRoot, command: nodeCommand } = nodeHookCommand()
    const scriptPath = join(tempDir, 'hook-bad-json.mjs')
    writeFileSync(
      scriptPath,
      'process.stdout.write("not json here")\n',
      'utf-8',
    )

    const hooks: HookConfig[] = [
      {
        trigger: 'pre_pipeline_init',
        command: nodeCommand,
        args: [scriptPath],
        timeout_ms: 10000,
      },
    ]

    assert.throws(
      () => runPrePipelineInitHooks(hooks, makeContext(), hookRoot),
      (err: unknown) =>
        err instanceof PipelineError && err.error_class === 'configuration_error',
    )
  })

  it('throws PipelineError configuration_error on path traversal', () => {
    const hooks: HookConfig[] = [
      { trigger: 'pre_pipeline_init', command: '../evil.js', phase_filter: '*' },
    ]

    assert.throws(
      () => runPrePipelineInitHooks(hooks, makeContext(), tempDir),
      (err: unknown) =>
        err instanceof PipelineError && err.error_class === 'configuration_error',
    )
  })

  it('throws PipelineError configuration_error on non-zero exit', () => {
    const { projectRoot: hookRoot, command: nodeCommand } = nodeHookCommand()
    const scriptPath = join(tempDir, 'hook-exit1.mjs')
    writeFileSync(scriptPath, 'process.exit(1)\n', 'utf-8')

    const hooks: HookConfig[] = [
      {
        trigger: 'pre_pipeline_init',
        command: nodeCommand,
        args: [scriptPath],
        timeout_ms: 10000,
      },
    ]

    assert.throws(
      () => runPrePipelineInitHooks(hooks, makeContext(), hookRoot),
      (err: unknown) =>
        err instanceof PipelineError && err.error_class === 'configuration_error',
    )
  })

  it('merges parameters across multiple hooks — later hook wins', () => {
    const { projectRoot: hookRoot, command: nodeCommand } = nodeHookCommand()
    const scriptA = join(tempDir, 'hook-a.mjs')
    const scriptB = join(tempDir, 'hook-b.mjs')
    writeFileSync(
      scriptA,
      'process.stdout.write(JSON.stringify({parameters:{phase_model:"a"}}))\n',
      'utf-8',
    )
    writeFileSync(
      scriptB,
      'process.stdout.write(JSON.stringify({parameters:{phase_model:"b"}}))\n',
      'utf-8',
    )

    const hooks: HookConfig[] = [
      {
        trigger: 'pre_pipeline_init',
        command: nodeCommand,
        args: [scriptA],
        timeout_ms: 10000,
      },
      {
        trigger: 'pre_pipeline_init',
        command: nodeCommand,
        args: [scriptB],
        timeout_ms: 10000,
      },
    ]

    const result = runPrePipelineInitHooks(hooks, makeContext(), hookRoot)
    assert.equal(result.parameters.phase_model, 'b')
  })
})

// ── loadHooksConfig — pre_pipeline_init trigger ──────────────

describe('loadHooksConfig — pre_pipeline_init trigger', () => {
  it('accepts pre_pipeline_init as a valid trigger value', () => {
    const toml = `
[[hooks]]
trigger = "pre_pipeline_init"
command = "hooks/x.js"
`
    const tomlPath = join(tempDir, 'hooks.toml')
    writeFileSync(tomlPath, toml, 'utf-8')

    const hooks = loadHooksConfig(tomlPath)
    assert.equal(hooks.length, 1)
    assert.equal(hooks[0].trigger, 'pre_pipeline_init')
    assert.equal(hooks[0].command, 'hooks/x.js')
  })
})

// ── handleInitRun — pre_pipeline_init integration ───────────
//
// These tests exercise the handler end-to-end with a mocked LifecycleContext.
// `runPrePipelineInitHooks` is stubbed on the ctx so we can control its
// return value without spawning real hook scripts — the underlying runner
// already has unit tests above. We DO use the real `initRun` from run-state.ts
// so we can verify that run_parameters are persisted to disk.

function makeIntegrationConfig(): PipelineConfig {
  const phases: Record<string, PhaseDefinition> = {
    discovery: {
      id: 0, description: '', inputs: [], outputs: [], tools: [],
      entry_point: true, optional: false,
    },
  }
  return {
    pipeline: { id: 'test', version: '1.0.0', description: '' },
    phases,
    edges: [],
    debate: {
      agents: {},
      rounds: {},
      output: { includes_transcript: true, includes_refined_artifact: true, artifact_version_bump: 'minor' },
    },
    knowledge_bases: { available_in: [], max_queries_per_phase: 20, query_mode: 'proactive' },
    schemas: {},
    storage: { base_dir: 'test', paths: {} },
  }
}

interface IntegrationCtxOptions {
  hooks?: HookConfig[]
  preInitResult?: PreInitHookResult
  preInitThrows?: boolean
}

function makeIntegrationCtx(
  baseDir: string,
  options: IntegrationCtxOptions = {},
): LifecycleContext & { activeRunRef: { current: RunState | null } } {
  const activeRunRef: { current: RunState | null } = { current: null }
  let activeRunDir = ''

  const ctx: LifecycleContext = {
    getActiveRun: () => activeRunRef.current,
    setActiveRun: (s) => { activeRunRef.current = s },
    getActiveRunDir: () => activeRunDir,
    setActiveRunDir: (dir) => { activeRunDir = dir },
    getConfig: () => makeIntegrationConfig(),
    setProjectRoot: () => {},
    getStorageConfig: (): StorageConfig => ({ base_dir: baseDir, paths: {} }),
    initRun: (runId, pipelineVersion, phaseNames, stateDir, runParameters) =>
      initRunState(runId, pipelineVersion, phaseNames, stateDir, runParameters),
    skipPhase: (s, phase, dir) => skipPhaseState(s, phase, dir),
    addArtifact: (s, ref, dir) => addArtifactState(s, ref, dir),
    startPhase: (s) => s,
    completePhase: (s) => s,
    failPhase: (s) => s,
    retryPhase: (s) => s,
    resolveNextPhases: () => [],
    getPhaseInputSatisfaction: (phase): InputSatisfaction => ({
      satisfied: [],
      missing: phase.inputs,
      can_run: false,
    }),
    loadRunState: () => null,
    recoverRun: (): RecoveryResult => ({
      state: activeRunRef.current as RunState,
      recovered_phases: [],
      warnings: [],
    }),
    removePhaseArtifacts: () => [],
    getQualityGates: () => [],
    runGateChecks: (): GateResult => ({ phase: '', passed: true, on_failure: 'warn', results: [] }),
    getHooksConfig: () => options.hooks ?? [],
    runHooks: () => [],
    runPrePipelineInitHooks: (): PreInitHookResult => {
      if (options.preInitThrows) {
        throw new PipelineError('hook failed', 'configuration_error')
      }
      return options.preInitResult ?? { parameters: {}, userPrompts: [] }
    },
    getProjectRoot: () => null,
    computeRecommendedAction: (): RecommendedAction => ({ action: 'review', reason: '' }),
    computeRunWarnings: () => [],
  }

  // Attach the mutable ref so tests can inspect the persisted state.
  return Object.assign(ctx, { activeRunRef })
}

describe('handleInitRun — pre_pipeline_init integration', () => {
  it('no pre-init hooks and no user params → baseline behavior, run_parameters undefined', () => {
    const ctx = makeIntegrationCtx(tempDir)

    const result = handleInitRun(
      { run_id: 'base-run', project_root: tempDir },
      ctx,
    )
    const parsed = JSON.parse(result.json) as {
      run_id: string
      run_parameters: Record<string, unknown>
    }

    assert.equal(parsed.run_id, 'base-run')
    assert.deepEqual(parsed.run_parameters, {})

    // Persisted state must NOT include run_parameters when nothing was collected.
    const disk = loadRunStateFn(join(tempDir, 'runs', 'base-run'))
    assert.ok(disk)
    assert.equal(disk!.run_parameters, undefined)
  })

  it('no pre-init hooks with explicit run_parameters → persists caller values', () => {
    const ctx = makeIntegrationCtx(tempDir)

    const result = handleInitRun(
      {
        run_id: 'explicit-run',
        project_root: tempDir,
        run_parameters: { run_name: 'explicit-test', phase_model: 'sonnet-4-6' },
      },
      ctx,
    )
    const parsed = JSON.parse(result.json) as {
      run_parameters: PipelineRunParameters
    }

    assert.equal(parsed.run_parameters.run_name, 'explicit-test')
    assert.equal(parsed.run_parameters.phase_model, 'sonnet-4-6')

    const disk = loadRunStateFn(join(tempDir, 'runs', 'explicit-run'))
    assert.ok(disk)
    assert.deepEqual(disk!.run_parameters, {
      run_name: 'explicit-test',
      phase_model: 'sonnet-4-6',
    })
  })

  it('pre-init hook emits parameters and no user override → hook values persisted', () => {
    const ctx = makeIntegrationCtx(tempDir, {
      hooks: [{ trigger: 'pre_pipeline_init', command: 'hooks/anything.js' }],
      preInitResult: {
        parameters: { run_name: 'hook-default', phase_model: 'opus' },
        userPrompts: [],
      },
    })

    const result = handleInitRun(
      { run_id: 'hook-run', project_root: tempDir },
      ctx,
    )
    const parsed = JSON.parse(result.json) as { run_parameters: PipelineRunParameters }

    assert.equal(parsed.run_parameters.run_name, 'hook-default')
    assert.equal(parsed.run_parameters.phase_model, 'opus')

    const disk = loadRunStateFn(join(tempDir, 'runs', 'hook-run'))
    assert.ok(disk)
    assert.equal(disk!.run_parameters?.run_name, 'hook-default')
    assert.equal(disk!.run_parameters?.phase_model, 'opus')
  })

  it('pre-init hook parameters merged with explicit args → user wins', () => {
    const ctx = makeIntegrationCtx(tempDir, {
      hooks: [{ trigger: 'pre_pipeline_init', command: 'hooks/anything.js' }],
      preInitResult: {
        parameters: { phase_model: 'opus', target_feature: 'from-hook' },
        userPrompts: [],
      },
    })

    const result = handleInitRun(
      {
        run_id: 'merge-run',
        project_root: tempDir,
        run_parameters: { phase_model: 'sonnet' },
      },
      ctx,
    )
    const parsed = JSON.parse(result.json) as { run_parameters: PipelineRunParameters }

    // User's phase_model wins; hook's target_feature is preserved.
    assert.equal(parsed.run_parameters.phase_model, 'sonnet')
    assert.equal(parsed.run_parameters.target_feature, 'from-hook')

    const disk = loadRunStateFn(join(tempDir, 'runs', 'merge-run'))
    assert.ok(disk)
    assert.equal(disk!.run_parameters?.phase_model, 'sonnet')
    assert.equal(disk!.run_parameters?.target_feature, 'from-hook')
  })

  it('pre-init hook requests user input with no params → short-circuits; no state written', () => {
    const ctx = makeIntegrationCtx(tempDir, {
      hooks: [{ trigger: 'pre_pipeline_init', command: 'hooks/anything.js' }],
      preInitResult: {
        parameters: {},
        userPrompts: ['What is run_name?'],
      },
    })

    const result = handleInitRun(
      { run_id: 'short-circuit-run', project_root: tempDir },
      ctx,
    )
    const envelope = JSON.parse(result.json) as {
      status: string
      data: {
        status: string
        prompts: string[]
        partial_parameters: Record<string, unknown>
      }
      next_step: string
    }

    assert.equal(envelope.status, 'ok')
    assert.equal(envelope.data.status, 'user_input_required')
    assert.deepEqual(envelope.data.prompts, ['What is run_name?'])
    assert.deepEqual(envelope.data.partial_parameters, {})
    assert.ok(envelope.next_step.includes('run_parameters'))

    // Critical assertion: NO run state was written to disk.
    const diskPath = join(tempDir, 'runs', 'short-circuit-run', 'run-state.json')
    assert.equal(existsSync(diskPath), false, 'run state must not be written on short-circuit')

    // And the active run on the ctx must still be null.
    assert.equal(ctx.activeRunRef.current, null)
  })

  it('pre-init hook requests user input but params provided → proceeds normally', () => {
    const ctx = makeIntegrationCtx(tempDir, {
      hooks: [{ trigger: 'pre_pipeline_init', command: 'hooks/anything.js' }],
      preInitResult: {
        parameters: {},
        userPrompts: ['What is run_name?'],
      },
    })

    const result = handleInitRun(
      {
        run_id: 'supplied-run',
        project_root: tempDir,
        run_parameters: { run_name: 'supplied' },
      },
      ctx,
    )
    const parsed = JSON.parse(result.json) as {
      run_id: string
      run_parameters: PipelineRunParameters
    }

    assert.equal(parsed.run_id, 'supplied-run')
    assert.equal(parsed.run_parameters.run_name, 'supplied')

    // State IS written this time.
    const disk = loadRunStateFn(join(tempDir, 'runs', 'supplied-run'))
    assert.ok(disk)
    assert.equal(disk!.run_parameters?.run_name, 'supplied')
  })
})

// ── run-state.initRun — run_parameters persistence ──────────

describe('run-state.initRun — run_parameters persistence', () => {
  it('persists run_parameters when non-empty object is provided', () => {
    const stateDir = join(tempDir, 'run-params-set')
    const state = initRunState('p1', '1.0.0', ['discovery'], stateDir, { run_name: 'test' })

    assert.deepEqual(state.run_parameters, { run_name: 'test' })

    // Verify the persisted JSON on disk has run_parameters too.
    const raw = readFileSync(join(stateDir, 'run-state.json'), 'utf-8')
    const parsed = JSON.parse(raw) as { run_parameters?: Record<string, unknown> }
    assert.deepEqual(parsed.run_parameters, { run_name: 'test' })
  })

  it('omits run_parameters from state when empty object is provided', () => {
    const stateDir = join(tempDir, 'run-params-empty')
    const state = initRunState('p2', '1.0.0', ['discovery'], stateDir, {})

    assert.equal(state.run_parameters, undefined)

    // The persisted JSON on disk must NOT have a run_parameters key either.
    const raw = readFileSync(join(stateDir, 'run-state.json'), 'utf-8')
    const parsed = JSON.parse(raw) as { run_parameters?: Record<string, unknown> }
    assert.equal(parsed.run_parameters, undefined)
    assert.equal('run_parameters' in parsed, false)
  })

  it('omits run_parameters from state when argument is undefined', () => {
    const stateDir = join(tempDir, 'run-params-undef')
    const state = initRunState('p3', '1.0.0', ['discovery'], stateDir)

    assert.equal(state.run_parameters, undefined)

    const raw = readFileSync(join(stateDir, 'run-state.json'), 'utf-8')
    const parsed = JSON.parse(raw) as { run_parameters?: Record<string, unknown> }
    assert.equal('run_parameters' in parsed, false)
  })
})
