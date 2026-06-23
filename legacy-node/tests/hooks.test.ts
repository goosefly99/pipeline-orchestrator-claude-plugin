import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, writeFileSync, mkdirSync, readFileSync, existsSync } from 'node:fs'
import { dirname, basename, join } from 'node:path'
import { tmpdir } from 'node:os'
import { validateHookPath, matchesPhase, runHooks, parseAgentDirective } from '../hooks.ts'
import { appendEvent } from '../lifecycle-handlers.ts'
import { loadHooksConfig } from '../toml-loader.ts'
import { PipelineError } from '../types.ts'
import type { HookConfig, LifecycleEvent } from '../types.ts'
import type { HookContext } from '../hooks.ts'

let tempDir: string

beforeEach(() => {
  tempDir = mkdtempSync(join(tmpdir(), 'hooks-test-'))
})

afterEach(() => {
  rmSync(tempDir, { recursive: true, force: true })
})

// ── validateHookPath ─────────────────────────────────────────

describe('validateHookPath', () => {
  it('resolves a relative path against project root', () => {
    const projectRoot = tempDir
    const scriptDir = join(projectRoot, 'scripts')
    mkdirSync(scriptDir, { recursive: true })

    const resolved = validateHookPath('scripts/test.sh', projectRoot)
    assert.equal(resolved, join(projectRoot, 'scripts', 'test.sh'))
  })

  it('accepts an absolute path within project root', () => {
    const projectRoot = tempDir
    const scriptPath = join(projectRoot, 'test.sh')

    const resolved = validateHookPath(scriptPath, projectRoot)
    assert.equal(resolved, scriptPath)
  })

  it('rejects paths with .. traversal', () => {
    const projectRoot = tempDir

    assert.throws(
      () => validateHookPath('../evil.sh', projectRoot),
      (err: unknown) => err instanceof PipelineError && err.error_class === 'configuration_error',
    )
  })

  it('rejects paths with embedded .. after normalization', () => {
    const projectRoot = tempDir

    assert.throws(
      () => validateHookPath('scripts/../../evil.sh', projectRoot),
      (err: unknown) => err instanceof PipelineError && err.error_class === 'configuration_error',
    )
  })
})

// ── matchesPhase ─────────────────────────────────────────────

describe('matchesPhase', () => {
  it('returns true for wildcard filter', () => {
    assert.equal(matchesPhase('*', 'discovery'), true)
  })

  it('returns true for undefined filter', () => {
    assert.equal(matchesPhase(undefined, 'discovery'), true)
  })

  it('returns true for exact match', () => {
    assert.equal(matchesPhase('discovery', 'discovery'), true)
  })

  it('returns false for non-matching filter', () => {
    assert.equal(matchesPhase('curation', 'discovery'), false)
  })
})

// ── runHooks ─────────────────────────────────────────────────

describe('runHooks', () => {
  it('returns empty array when no hooks match', () => {
    const hooks: HookConfig[] = [
      { trigger: 'post_complete', command: 'echo', args: ['hi'], phase_filter: '*' },
    ]
    const ctx: HookContext = { run_id: 'r1', phase: 'discovery', trigger: 'pre_start', project_root: tempDir, run_dir: tempDir }

    const results = runHooks(hooks, 'pre_start', ctx, tempDir)
    assert.deepEqual(results, [])
  })

  it('returns empty array when no hooks are configured', () => {
    const ctx: HookContext = { run_id: 'r1', phase: 'discovery', trigger: 'pre_start', project_root: tempDir, run_dir: tempDir }
    const results = runHooks([], 'pre_start', ctx, tempDir)
    assert.deepEqual(results, [])
  })

  it('filters hooks by phase_filter', () => {
    const hooks: HookConfig[] = [
      { trigger: 'post_complete', command: 'echo', args: ['hi'], phase_filter: 'curation' },
    ]
    const ctx: HookContext = { run_id: 'r1', phase: 'discovery', trigger: 'post_complete', project_root: tempDir, run_dir: tempDir }

    const results = runHooks(hooks, 'post_complete', ctx, tempDir)
    assert.deepEqual(results, [])
  })

  it('throws PipelineError for pre_start hook path validation failure', () => {
    const hooks: HookConfig[] = [
      { trigger: 'pre_start', command: '../evil.sh', phase_filter: '*' },
    ]
    const ctx: HookContext = { run_id: 'r1', phase: 'discovery', trigger: 'pre_start', project_root: tempDir, run_dir: tempDir }

    assert.throws(
      () => runHooks(hooks, 'pre_start', ctx, tempDir),
      (err: unknown) => err instanceof PipelineError && err.error_class === 'configuration_error',
    )
  })

  it('records failure for non-blocking hook path validation failure', () => {
    const hooks: HookConfig[] = [
      { trigger: 'post_complete', command: '../evil.sh', phase_filter: '*' },
    ]
    const ctx: HookContext = { run_id: 'r1', phase: 'discovery', trigger: 'post_complete', project_root: tempDir, run_dir: tempDir }

    const results = runHooks(hooks, 'post_complete', ctx, tempDir)
    assert.equal(results.length, 1)
    assert.equal(results[0].success, false)
    assert.ok(results[0].error!.includes('traversal'))
  })
})

// ── parseAgentDirective (Feature B B2) ──────────────────────

describe('parseAgentDirective', () => {
  const valid = {
    agent_directive: {
      subagent_type: 'general-purpose',
      model: 'claude-sonnet-4-6',
      description: 'discovery execution',
      prompt: 'Run phase discovery and call pipeline_complete_phase when done.',
    },
  }

  it('returns undefined for undefined stdout', () => {
    assert.equal(parseAgentDirective(undefined), undefined)
  })

  it('returns undefined for empty string', () => {
    assert.equal(parseAgentDirective(''), undefined)
  })

  it('returns undefined for whitespace-only stdout', () => {
    assert.equal(parseAgentDirective('   \n  \t '), undefined)
  })

  it('returns undefined for non-JSON stdout', () => {
    assert.equal(parseAgentDirective('hello world'), undefined)
  })

  it('returns undefined for JSON that is not an object', () => {
    assert.equal(parseAgentDirective('"just a string"'), undefined)
    assert.equal(parseAgentDirective('42'), undefined)
    assert.equal(parseAgentDirective('null'), undefined)
    assert.equal(parseAgentDirective('[1, 2, 3]'), undefined)
  })

  it('returns undefined when agent_directive key is missing', () => {
    assert.equal(parseAgentDirective('{"parameters": {}}'), undefined)
  })

  it('returns undefined when agent_directive is null', () => {
    assert.equal(parseAgentDirective('{"agent_directive": null}'), undefined)
  })

  it('returns undefined when agent_directive is not an object', () => {
    assert.equal(parseAgentDirective('{"agent_directive": "nope"}'), undefined)
    assert.equal(parseAgentDirective('{"agent_directive": 7}'), undefined)
  })

  it('returns undefined when any required field is missing', () => {
    for (const field of ['subagent_type', 'model', 'description', 'prompt']) {
      const clone = JSON.parse(JSON.stringify(valid))
      delete clone.agent_directive[field]
      assert.equal(parseAgentDirective(JSON.stringify(clone)), undefined, `missing ${field} should fail`)
    }
  })

  it('returns undefined when any required field is not a string', () => {
    for (const field of ['subagent_type', 'model', 'description', 'prompt']) {
      const clone = JSON.parse(JSON.stringify(valid))
      clone.agent_directive[field] = 42
      assert.equal(parseAgentDirective(JSON.stringify(clone)), undefined, `numeric ${field} should fail`)
    }
  })

  it('parses a valid directive with no isolation', () => {
    const result = parseAgentDirective(JSON.stringify(valid))
    assert.deepEqual(result, {
      subagent_type: 'general-purpose',
      model: 'claude-sonnet-4-6',
      description: 'discovery execution',
      prompt: 'Run phase discovery and call pipeline_complete_phase when done.',
    })
  })

  it('preserves isolation: "worktree" when present', () => {
    const withWorktree = JSON.parse(JSON.stringify(valid))
    withWorktree.agent_directive.isolation = 'worktree'
    const result = parseAgentDirective(JSON.stringify(withWorktree))
    assert.equal(result?.isolation, 'worktree')
  })

  it('drops non-worktree isolation values', () => {
    const withOther = JSON.parse(JSON.stringify(valid))
    withOther.agent_directive.isolation = 'sandbox'
    const result = parseAgentDirective(JSON.stringify(withOther))
    assert.equal(result?.isolation, undefined)
  })

  it('tolerates surrounding whitespace around the JSON', () => {
    const padded = `\n\n  ${JSON.stringify(valid)}  \n`
    const result = parseAgentDirective(padded)
    assert.equal(result?.subagent_type, 'general-purpose')
  })

  it('ignores extra unknown fields on the directive', () => {
    const withExtra = JSON.parse(JSON.stringify(valid))
    withExtra.agent_directive.debug = true
    withExtra.agent_directive.extra_note = 'ignored'
    const result = parseAgentDirective(JSON.stringify(withExtra))
    // Only the four required fields should be forwarded — extras are dropped.
    assert.deepEqual(result, {
      subagent_type: 'general-purpose',
      model: 'claude-sonnet-4-6',
      description: 'discovery execution',
      prompt: 'Run phase discovery and call pipeline_complete_phase when done.',
    })
  })
})

// ── runHooks attaches agent_directive from stdout JSON ─────

describe('runHooks agent_directive passthrough', () => {
  /**
   * Return a {projectRoot, command} pair that lets `validateHookPath` accept
   * the currently-running `node` binary as a hook command. Mirrors the
   * `nodeHookCommand` helper used in hook-idempotency.test.ts and
   * pre-init-hook.test.ts — projectRoot is the node binary's directory,
   * command is its basename, and the real hook script is passed via args[0].
   */
  function nodeHookCommand(): { projectRoot: string; command: string } {
    return {
      projectRoot: dirname(process.execPath),
      command: basename(process.execPath),
    }
  }

  it('attaches agent_directive when pre_start hook emits a valid JSON payload', () => {
    const { projectRoot: hookRoot, command: nodeCommand } = nodeHookCommand()
    const scriptPath = join(tempDir, 'pre-start-directive.mjs')
    const payload = JSON.stringify({
      agent_directive: {
        subagent_type: 'general-purpose',
        model: 'claude-opus-4-6',
        description: 'debate execution',
        prompt: 'Run debate and call pipeline_complete_phase on finish.',
      },
    })
    writeFileSync(scriptPath, `console.log(${JSON.stringify(payload)})\n`, 'utf-8')

    const hooks: HookConfig[] = [
      {
        trigger: 'pre_start',
        phase_filter: '*',
        command: nodeCommand,
        args: [scriptPath],
        timeout_ms: 10000,
      },
    ]
    const ctx: HookContext = {
      run_id: 'r1',
      phase: 'debate',
      trigger: 'pre_start',
      project_root: hookRoot,
      run_dir: tempDir,
    }

    const results = runHooks(hooks, 'pre_start', ctx, hookRoot)
    assert.equal(results.length, 1)
    assert.equal(results[0].success, true)
    assert.deepEqual(results[0].agent_directive, {
      subagent_type: 'general-purpose',
      model: 'claude-opus-4-6',
      description: 'debate execution',
      prompt: 'Run debate and call pipeline_complete_phase on finish.',
    })
  })

  it('leaves agent_directive undefined when a hook emits plain-text stdout', () => {
    const { projectRoot: hookRoot, command: nodeCommand } = nodeHookCommand()
    const scriptPath = join(tempDir, 'plain-stdout.mjs')
    writeFileSync(scriptPath, 'console.log("hello from hook")\n', 'utf-8')

    const hooks: HookConfig[] = [
      {
        trigger: 'pre_start',
        phase_filter: '*',
        command: nodeCommand,
        args: [scriptPath],
        timeout_ms: 10000,
      },
    ]
    const ctx: HookContext = {
      run_id: 'r1',
      phase: 'discovery',
      trigger: 'pre_start',
      project_root: hookRoot,
      run_dir: tempDir,
    }

    const results = runHooks(hooks, 'pre_start', ctx, hookRoot)
    assert.equal(results.length, 1)
    assert.equal(results[0].success, true)
    assert.equal(results[0].agent_directive, undefined)
    assert.equal(results[0].stdout, 'hello from hook')
  })
})

// ── appendEvent ──────────────────────────────────────────────

describe('appendEvent', () => {
  it('creates events.jsonl and appends a single event', () => {
    const event: LifecycleEvent = {
      timestamp: new Date().toISOString(),
      event: 'phase_started',
      phase: 'discovery',
      run_id: 'test-run',
    }

    appendEvent(tempDir, event)

    const eventsPath = join(tempDir, 'events.jsonl')
    assert.ok(existsSync(eventsPath))

    const content = readFileSync(eventsPath, 'utf-8').trim()
    const parsed = JSON.parse(content)
    assert.equal(parsed.event, 'phase_started')
    assert.equal(parsed.phase, 'discovery')
  })

  it('appends multiple events as separate lines', () => {
    const event1: LifecycleEvent = {
      timestamp: new Date().toISOString(),
      event: 'phase_started',
      phase: 'discovery',
      run_id: 'test-run',
    }
    const event2: LifecycleEvent = {
      timestamp: new Date().toISOString(),
      event: 'phase_completed',
      phase: 'discovery',
      run_id: 'test-run',
    }

    appendEvent(tempDir, event1)
    appendEvent(tempDir, event2)

    const lines = readFileSync(join(tempDir, 'events.jsonl'), 'utf-8').trim().split('\n')
    assert.equal(lines.length, 2)

    const parsed1 = JSON.parse(lines[0])
    const parsed2 = JSON.parse(lines[1])
    assert.equal(parsed1.event, 'phase_started')
    assert.equal(parsed2.event, 'phase_completed')
  })

  it('creates run directory if it does not exist', () => {
    const subDir = join(tempDir, 'nested', 'run')
    assert.ok(!existsSync(subDir))

    appendEvent(subDir, {
      timestamp: new Date().toISOString(),
      event: 'phase_started',
      phase: 'discovery',
      run_id: 'test-run',
    })

    assert.ok(existsSync(join(subDir, 'events.jsonl')))
  })

  it('includes details when provided', () => {
    const event: LifecycleEvent = {
      timestamp: new Date().toISOString(),
      event: 'phase_failed',
      phase: 'curation',
      run_id: 'test-run',
      details: { error: 'API timeout' },
    }

    appendEvent(tempDir, event)

    const content = readFileSync(join(tempDir, 'events.jsonl'), 'utf-8').trim()
    const parsed = JSON.parse(content)
    assert.equal(parsed.details.error, 'API timeout')
  })
})

// ── loadHooksConfig (TOML parsing) ───────────────────────────

describe('loadHooksConfig', () => {
  it('returns empty array for nonexistent file', () => {
    const hooks = loadHooksConfig(join(tempDir, 'nonexistent.toml'))
    assert.deepEqual(hooks, [])
  })

  it('parses valid hooks configuration', () => {
    const toml = `
[[hooks]]
trigger = "pre_start"
phase_filter = "*"
command = "scripts/validate-env.sh"
args = ["--strict"]
timeout_ms = 3000

[[hooks]]
trigger = "post_complete"
phase_filter = "curation"
command = "scripts/notify.sh"
`
    writeFileSync(join(tempDir, 'hooks.toml'), toml, 'utf-8')

    const hooks = loadHooksConfig(join(tempDir, 'hooks.toml'))
    assert.equal(hooks.length, 2)

    assert.equal(hooks[0].trigger, 'pre_start')
    assert.equal(hooks[0].phase_filter, '*')
    assert.equal(hooks[0].command, 'scripts/validate-env.sh')
    assert.deepEqual(hooks[0].args, ['--strict'])
    assert.equal(hooks[0].timeout_ms, 3000)

    assert.equal(hooks[1].trigger, 'post_complete')
    assert.equal(hooks[1].phase_filter, 'curation')
  })

  it('defaults phase_filter to * and timeout_ms to 5000', () => {
    const toml = `
[[hooks]]
trigger = "on_fail"
command = "scripts/cleanup.sh"
`
    writeFileSync(join(tempDir, 'hooks.toml'), toml, 'utf-8')

    const hooks = loadHooksConfig(join(tempDir, 'hooks.toml'))
    assert.equal(hooks.length, 1)
    assert.equal(hooks[0].phase_filter, '*')
    assert.equal(hooks[0].timeout_ms, 5000)
  })

  it('skips hooks with invalid trigger values', () => {
    const toml = `
[[hooks]]
trigger = "invalid_trigger"
command = "scripts/test.sh"

[[hooks]]
trigger = "pre_start"
command = "scripts/valid.sh"
`
    writeFileSync(join(tempDir, 'hooks.toml'), toml, 'utf-8')

    const hooks = loadHooksConfig(join(tempDir, 'hooks.toml'))
    assert.equal(hooks.length, 1)
    assert.equal(hooks[0].trigger, 'pre_start')
  })
})

// ── model_tier parsing ───────────────────────────────────────

describe('model_tier in PhaseDefinition', () => {
  it('model_tier is surfaced in pipeline_get_config response', async () => {
    // This verifies the type includes model_tier via toml-loader
    const { loadPipelineConfig } = await import('../toml-loader.ts')
    const config = loadPipelineConfig(join(import.meta.dirname!, '..', 'pipeline', 'pipeline.toml'))

    // All phases should load (model_tier may be undefined for phases without it)
    for (const [name, phase] of Object.entries(config.phases)) {
      assert.ok(phase.id !== undefined, `Phase "${name}" should have an id`)
      // model_tier can be undefined (not yet set in pipeline.toml) — that's valid
      if (phase.model_tier !== undefined) {
        assert.ok(
          ['haiku', 'sonnet', 'opus'].includes(phase.model_tier),
          `Phase "${name}" model_tier must be haiku, sonnet, or opus`,
        )
      }
    }
  })
})
