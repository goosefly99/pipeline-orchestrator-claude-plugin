// hooks.ts — Phase lifecycle hook execution via execFileSync

import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { resolve, normalize, isAbsolute, join } from 'node:path'
import type { AgentDirective, HookConfig, HookTrigger, PipelineRunParameters } from './types.ts'
import { PipelineError } from './types.ts'

/** Context passed to hook commands via PIPELINE_HOOK_CONTEXT env var. */
export interface HookContext {
  run_id: string
  phase: string
  trigger: HookTrigger
  project_root: string
  run_dir: string
  [key: string]: unknown
}

/**
 * Context passed to `pre_pipeline_init` hooks via PIPELINE_HOOK_CONTEXT env var.
 * Distinct from `HookContext` because there is no phase or run yet — this hook
 * fires BEFORE `pipeline_init_run` mutates state. The hook can inspect the
 * raw tool arguments and emit parameters or user prompts.
 */
export interface PreInitHookContext {
  trigger: 'pre_pipeline_init'
  project_root: string
  /** Raw `pipeline_init_run` tool arguments (e.g. run_id, skip_phases, etc.). */
  requested_args: Record<string, unknown>
}

/** Result of running all `pre_pipeline_init` hooks: merged parameters plus any user prompts. */
export interface PreInitHookResult {
  parameters: PipelineRunParameters
  userPrompts: string[]
}

/** Result of running a single hook. */
export interface HookResult {
  command: string
  trigger: HookTrigger
  phase: string
  success: boolean
  stdout?: string
  stderr?: string
  error?: string
  /**
   * Feature B directive parsed from the hook's JSON stdout. Present only
   * when the hook emitted a JSON object containing a well-formed
   * `agent_directive` with all four required string fields
   * (`subagent_type`, `model`, `description`, `prompt`). Feature B's
   * `handleStartPhase` forwards this into the `pipeline_start_phase` MCP
   * tool response so the client can spawn the phase subagent.
   */
  agent_directive?: AgentDirective
}

/**
 * Parse an `AgentDirective` from a hook's raw stdout string.
 *
 * The stdout must be a JSON object literal containing an `agent_directive`
 * field whose value has all four required string fields (`subagent_type`,
 * `model`, `description`, `prompt`). An optional `isolation: 'worktree'`
 * is preserved when present; any other `isolation` value is dropped.
 *
 * Returns `undefined` when the stdout is absent, empty, not JSON, missing
 * the `agent_directive` field, or the directive fails shape validation.
 * This helper is intentionally lenient so existing non-directive hooks
 * (plain text output, JSON without a directive) pass through unchanged —
 * only Feature B `pre_start` hooks that opt in by emitting the correctly
 * shaped JSON attach a directive to the `HookResult`.
 */
export function parseAgentDirective(stdout: string | undefined): AgentDirective | undefined {
  if (!stdout) return undefined
  const trimmed = stdout.trim()
  if (trimmed.length === 0) return undefined

  let parsed: unknown
  try {
    parsed = JSON.parse(trimmed)
  } catch {
    return undefined
  }

  if (!parsed || typeof parsed !== 'object') return undefined
  const root = parsed as Record<string, unknown>
  const raw = root.agent_directive
  if (!raw || typeof raw !== 'object') return undefined

  const d = raw as Record<string, unknown>
  const { subagent_type, model, description, prompt } = d
  if (
    typeof subagent_type !== 'string' ||
    typeof model !== 'string' ||
    typeof description !== 'string' ||
    typeof prompt !== 'string'
  ) {
    return undefined
  }

  const directive: AgentDirective = { subagent_type, model, description, prompt }
  if (d.isolation === 'worktree') {
    directive.isolation = 'worktree'
  }
  return directive
}

const MAX_CONTEXT_BYTES = 65536 // 64KB cap on env var context

/**
 * Validate that a hook command path is safe:
 * - Resolve against project root
 * - Reject any path containing '..' after normalization
 * - Must resolve to a path under project root
 */
export function validateHookPath(command: string, projectRoot: string): string {
  const normalized = normalize(command)

  // Reject paths with '..' traversal after normalization
  if (normalized.includes('..')) {
    throw new PipelineError(
      `Hook command path "${command}" contains directory traversal (..)`,
      'configuration_error',
      {
        recovery_action: 'Use a path relative to the project root without ".." components.',
        details: { command, normalized },
      },
    )
  }

  // Resolve against project root
  const resolved = isAbsolute(normalized) ? normalized : resolve(projectRoot, normalized)

  // Verify resolved path is under project root
  const normalizedRoot = normalize(projectRoot)
  if (!resolved.startsWith(normalizedRoot)) {
    throw new PipelineError(
      `Hook command path "${command}" resolves outside project root`,
      'configuration_error',
      {
        recovery_action: 'Ensure hook command path is within the project directory.',
        details: { command, resolved, projectRoot },
      },
    )
  }

  return resolved
}

/**
 * Check if a hook's phase_filter matches the given phase name.
 * Supports '*' (all phases) or exact match.
 */
export function matchesPhase(filter: string | undefined, phaseName: string): boolean {
  if (!filter || filter === '*') return true
  return filter === phaseName
}

/**
 * Check whether events.jsonl in the given run directory already contains a
 * `phase_completed` event for the target phase. Returns true if at least one
 * matching event is present, false otherwise (including when the file does not
 * exist or cannot be read).
 *
 * Used by runHooks to enforce idempotency for post_complete hooks: if the
 * event is already recorded, we know the hook has already fired once and
 * should not fire again.
 */
function hasPhaseCompletedEvent(runDir: string, phase: string): boolean {
  try {
    const eventsPath = join(runDir, 'events.jsonl')
    const content = readFileSync(eventsPath, 'utf-8')
    const lines = content.split('\n')
    for (const line of lines) {
      const trimmed = line.trim()
      if (!trimmed) continue
      try {
        const parsed = JSON.parse(trimmed) as { event?: unknown; phase?: unknown }
        if (parsed.event === 'phase_completed' && parsed.phase === phase) {
          return true
        }
      } catch {
        // Malformed line — skip defensively.
      }
    }
    return false
  } catch {
    // File missing or unreadable — treat as "no prior event".
    return false
  }
}

/**
 * Run hooks matching a specific trigger and phase.
 *
 * Pre-start hooks are blocking: if any hook fails, a PipelineError is thrown.
 * Post-complete and on_fail hooks are non-blocking: failures are returned as warnings.
 *
 * @param hooks - Array of hook configurations
 * @param trigger - Which trigger point to run
 * @param context - Hook context (passed as PIPELINE_HOOK_CONTEXT env var)
 * @param projectRoot - Project root for path resolution
 * @returns Array of hook results
 */
export function runHooks(
  hooks: HookConfig[],
  trigger: HookTrigger,
  context: HookContext,
  projectRoot: string,
): HookResult[] {
  const matching = hooks.filter(
    h => h.trigger === trigger && matchesPhase(h.phase_filter, context.phase),
  )

  if (matching.length === 0) return []

  // Phase idempotency for post_complete: if a phase_completed event already
  // exists in events.jsonl for this phase, skip all post_complete hooks for
  // this call and return a synthetic skipped result per matching hook.
  if (trigger === 'post_complete' && hasPhaseCompletedEvent(context.run_dir, context.phase)) {
    const timestamp = new Date().toISOString()
    // Stderr debug line for post-mortem investigation of hook-fire sequences.
    console.warn(
      `[hooks] SKIPPED post_complete for phase="${context.phase}" at ${timestamp} ` +
      `(events.jsonl already contains phase_completed for this phase)`,
    )
    return matching.map(hook => ({
      command: hook.command,
      trigger,
      phase: context.phase,
      success: false,
      error: `skipped: phase_completed already recorded for phase "${context.phase}" (idempotency guard)`,
    }))
  }

  // Debug logging keyed on trigger + phase + timestamp for post-mortem traces.
  console.warn(
    `[hooks] firing trigger="${trigger}" phase="${context.phase}" ` +
    `matched=${matching.length} at ${new Date().toISOString()}`,
  )

  const results: HookResult[] = []
  const contextJson = JSON.stringify(context)

  // Cap context size for env var safety
  const envContext = contextJson.length <= MAX_CONTEXT_BYTES
    ? contextJson
    : JSON.stringify({ error: 'context_too_large', run_id: context.run_id, phase: context.phase })

  for (const hook of matching) {
    let resolvedCommand: string
    try {
      resolvedCommand = validateHookPath(hook.command, projectRoot)
    } catch (err) {
      const result: HookResult = {
        command: hook.command,
        trigger,
        phase: context.phase,
        success: false,
        error: err instanceof Error ? err.message : String(err),
      }

      if (trigger === 'pre_start') {
        throw err // Re-throw for blocking hooks
      }

      results.push(result)
      continue
    }

    try {
      const stdout = execFileSync(resolvedCommand, hook.args ?? [], {
        timeout: hook.timeout_ms ?? 5000,
        env: { ...process.env, PIPELINE_HOOK_CONTEXT: envContext },
        encoding: 'utf-8',
        stdio: ['pipe', 'pipe', 'pipe'],
      })

      const trimmedStdout = typeof stdout === 'string' ? stdout.trim() : undefined
      const directive = parseAgentDirective(trimmedStdout)
      const result: HookResult = {
        command: hook.command,
        trigger,
        phase: context.phase,
        success: true,
        stdout: trimmedStdout,
      }
      if (directive) result.agent_directive = directive
      results.push(result)
    } catch (err: unknown) {
      const error = err instanceof Error ? err : new Error(String(err))
      const result: HookResult = {
        command: hook.command,
        trigger,
        phase: context.phase,
        success: false,
        stderr: (error as { stderr?: string }).stderr,
        error: error.message,
      }

      if (trigger === 'pre_start') {
        // Blocking: throw PipelineError
        throw new PipelineError(
          `Pre-start hook failed for phase "${context.phase}": ${hook.command} — ${error.message}`,
          'configuration_error',
          {
            recovery_action: 'Fix the hook script or remove it from hooks configuration.',
            details: {
              command: hook.command,
              phase: context.phase,
              error: error.message,
            },
          },
        )
      }

      // Non-blocking: record and continue
      results.push(result)
    }
  }

  return results
}

/**
 * Run all `pre_pipeline_init` hooks in order. Each hook's stdout must be a
 * JSON object shaped `{ parameters?: PipelineRunParameters, userPrompts?: string[] }`.
 * Parameters from later hooks override earlier ones; prompts accumulate.
 *
 * Unlike phase hooks, this trigger has no phase filter — every enabled hook
 * with `trigger: 'pre_pipeline_init'` fires.
 *
 * This function is blocking: if any hook script fails (non-zero exit, timeout,
 * malformed JSON, path traversal), a `PipelineError('configuration_error')` is thrown.
 * Returns `{ parameters: {}, userPrompts: [] }` when no matching hooks exist.
 *
 * @param hooks - Array of hook configurations (filtered from the loaded `[[hooks]]` table)
 * @param context - Pre-init hook context (passed as PIPELINE_HOOK_CONTEXT env var)
 * @param projectRoot - Project root for path resolution via validateHookPath
 */
export function runPrePipelineInitHooks(
  hooks: HookConfig[],
  context: PreInitHookContext,
  projectRoot: string,
): PreInitHookResult {
  const matching = hooks.filter(h => h.trigger === 'pre_pipeline_init')
  const merged: PipelineRunParameters = {}
  const allPrompts: string[] = []

  if (matching.length === 0) {
    return { parameters: merged, userPrompts: allPrompts }
  }

  console.warn(
    `[hooks] firing trigger="pre_pipeline_init" matched=${matching.length} at ${new Date().toISOString()}`,
  )

  const contextJson = JSON.stringify(context)
  const envContext = contextJson.length <= MAX_CONTEXT_BYTES
    ? contextJson
    : JSON.stringify({ error: 'context_too_large', trigger: 'pre_pipeline_init' })

  for (const hook of matching) {
    let resolvedCommand: string
    try {
      resolvedCommand = validateHookPath(hook.command, projectRoot)
    } catch (err) {
      // Re-throw path-traversal / outside-root errors as configuration_error
      throw err
    }

    try {
      const stdout = execFileSync(resolvedCommand, hook.args ?? [], {
        timeout: hook.timeout_ms ?? 5000,
        env: { ...process.env, PIPELINE_HOOK_CONTEXT: envContext },
        encoding: 'utf-8',
        stdio: ['pipe', 'pipe', 'pipe'],
      })
      const trimmed = typeof stdout === 'string' ? stdout.trim() : ''
      if (trimmed.length === 0) continue

      let parsed: { parameters?: unknown; userPrompts?: unknown; user_prompts?: unknown; user_prompt?: unknown }
      try {
        parsed = JSON.parse(trimmed)
      } catch (jsonErr) {
        throw new PipelineError(
          `pre_pipeline_init hook "${hook.command}" emitted non-JSON stdout: ${jsonErr instanceof Error ? jsonErr.message : String(jsonErr)}`,
          'configuration_error',
          {
            recovery_action: 'Ensure the hook script writes a JSON object to stdout with { parameters, userPrompts } shape.',
            details: { command: hook.command, stdout_preview: trimmed.slice(0, 200) },
          },
        )
      }

      if (parsed.parameters && typeof parsed.parameters === 'object') {
        Object.assign(merged, parsed.parameters as PipelineRunParameters)
      }
      if (Array.isArray(parsed.userPrompts)) {
        for (const p of parsed.userPrompts) {
          if (typeof p === 'string') allPrompts.push(p)
        }
      }
      if (Array.isArray(parsed.user_prompts)) {
        for (const p of parsed.user_prompts) {
          if (typeof p === 'string') allPrompts.push(p)
        }
      }
      if (typeof parsed.user_prompt === 'string') {
        allPrompts.push(parsed.user_prompt)
      }
    } catch (err: unknown) {
      if (err instanceof PipelineError) throw err
      const error = err instanceof Error ? err : new Error(String(err))
      throw new PipelineError(
        `pre_pipeline_init hook failed: ${hook.command} — ${error.message}`,
        'configuration_error',
        {
          recovery_action: 'Fix the hook script or remove it from the [[hooks]] configuration.',
          details: { command: hook.command, error: error.message },
        },
      )
    }
  }

  return { parameters: merged, userPrompts: allPrompts }
}
