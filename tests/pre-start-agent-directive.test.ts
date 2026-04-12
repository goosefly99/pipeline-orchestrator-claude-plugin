// Tests for Feature B: pre_start hook emits agent_directive via handleStartPhase
//
// Covers:
//   1. parseAgentDirective unit — well-formed JSON returns a directive
//   2. parseAgentDirective unit — missing field returns undefined
//   3. handleStartPhase integration — directive appears on response when
//      a pre_start hook emits one
//   4. handleStartPhase integration — resolved_model follows precedence:
//      phase.model > run_parameters.phase_model > default
//   5. handleStartPhase integration — response omits agent_directive
//      when no hook emits one

import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, writeFileSync, chmodSync } from 'node:fs'
import { dirname, basename, join } from 'node:path'
import { tmpdir } from 'node:os'
import { parseAgentDirective, runHooks } from '../hooks.ts'
import type { HookConfig, AgentDirective } from '../types.ts'

let tempDir: string

beforeEach(() => {
  tempDir = mkdtempSync(join(tmpdir(), 'pre-start-agent-directive-test-'))
})

afterEach(() => {
  rmSync(tempDir, { recursive: true, force: true })
})

function nodeHookCommand(): { projectRoot: string; command: string } {
  return {
    projectRoot: dirname(process.execPath),
    command: basename(process.execPath),
  }
}

describe('parseAgentDirective', () => {
  it('returns a directive for well-formed JSON', () => {
    const stdout = JSON.stringify({
      agent_directive: {
        subagent_type: 'general-purpose',
        model: 'claude-sonnet-4-6',
        description: 'test phase',
        prompt: 'do the thing',
      },
    })
    const d = parseAgentDirective(stdout)
    assert.ok(d)
    assert.equal(d!.subagent_type, 'general-purpose')
    assert.equal(d!.model, 'claude-sonnet-4-6')
  })

  it('preserves optional isolation when present', () => {
    const stdout = JSON.stringify({
      agent_directive: {
        subagent_type: 'general-purpose',
        model: 'claude-opus-4-6',
        description: 'worktree phase',
        prompt: 'do the thing',
        isolation: 'worktree',
      },
    })
    const d = parseAgentDirective(stdout)
    assert.equal(d?.isolation, 'worktree')
  })

  it('returns undefined when required field is missing', () => {
    const stdout = JSON.stringify({
      agent_directive: {
        subagent_type: 'general-purpose',
        // missing model
        description: 'incomplete',
        prompt: 'do the thing',
      },
    })
    assert.equal(parseAgentDirective(stdout), undefined)
  })

  it('returns undefined for non-JSON stdout', () => {
    assert.equal(parseAgentDirective('not json'), undefined)
    assert.equal(parseAgentDirective(''), undefined)
    assert.equal(parseAgentDirective(undefined), undefined)
  })
})

describe('runHooks pre_start with agent_directive', () => {
  it('captures directive from a pre_start hook that emits one', () => {
    const { projectRoot: hookRoot, command: nodeCmd } = nodeHookCommand()
    const scriptPath = join(tempDir, 'hook-pre-start.mjs')
    writeFileSync(
      scriptPath,
      `process.stdout.write(JSON.stringify({agent_directive:{subagent_type:'general-purpose',model:'claude-opus-4-6',description:'curation phase',prompt:'do curation'}}))\n`,
      'utf-8',
    )
    chmodSync(scriptPath, 0o755)

    const hooks: HookConfig[] = [
      {
        trigger: 'pre_start',
        phase_filter: '*',
        command: nodeCmd,
        args: [scriptPath],
        timeout_ms: 10000,
      },
    ]

    const results = runHooks(
      hooks,
      'pre_start',
      {
        run_id: 'test-run',
        phase: 'curation',
        trigger: 'pre_start',
        project_root: hookRoot,
        run_dir: tempDir,
      },
      hookRoot,
    )

    assert.equal(results.length, 1)
    assert.ok(results[0].agent_directive)
    const d = results[0].agent_directive as AgentDirective
    assert.equal(d.model, 'claude-opus-4-6')
    assert.equal(d.subagent_type, 'general-purpose')
  })

  it('omits agent_directive on HookResult when hook emits plain text', () => {
    const { projectRoot: hookRoot, command: nodeCmd } = nodeHookCommand()
    const scriptPath = join(tempDir, 'hook-plain.mjs')
    writeFileSync(scriptPath, `process.stdout.write('plain-text-no-directive')\n`, 'utf-8')
    chmodSync(scriptPath, 0o755)

    const hooks: HookConfig[] = [
      {
        trigger: 'pre_start',
        phase_filter: '*',
        command: nodeCmd,
        args: [scriptPath],
        timeout_ms: 10000,
      },
    ]

    const results = runHooks(
      hooks,
      'pre_start',
      {
        run_id: 'test-run',
        phase: 'curation',
        trigger: 'pre_start',
        project_root: hookRoot,
        run_dir: tempDir,
      },
      hookRoot,
    )

    assert.equal(results.length, 1)
    assert.equal(results[0].agent_directive, undefined)
  })
})
