#!/usr/bin/env node
// dag-guard.mjs — PreToolUse hook for pipeline_start_phase
// Validates DAG dependencies are satisfied before allowing phase start

import { readFileSync, existsSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parse } from 'smol-toml'
import { readStdin, findLatestRunState } from '../lib/common.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const RUNS_DIR = join(__dirname, '..', '..', 'pipeline_mcp_data', 'runs')
const PIPELINE_TOML = join(__dirname, '..', '..', 'pipeline', 'pipeline.toml')

function getPhaseInfo(phaseName) {
  if (!existsSync(PIPELINE_TOML)) return { isEntryPoint: false, requiredDeps: [] }

  try {
    const toml = parse(readFileSync(PIPELINE_TOML, 'utf-8'))
    const phaseDef = toml.phases?.[phaseName]
    const isEntryPoint = phaseDef?.entry_point === true

    const edges = toml.edges || []
    const requiredDeps = edges
      .filter(e => e.to === phaseName && !e.optional)
      .map(e => e.from)

    return { isEntryPoint, requiredDeps }
  } catch {
    return { isEntryPoint: false, requiredDeps: [] }
  }
}

const input = await readStdin()

try {
  const hookInput = JSON.parse(input || '{}')
  const toolInput = hookInput.tool_input || {}
  const phaseName = toolInput.phase || toolInput.phase_name

  if (!phaseName) {
    process.stdout.write(JSON.stringify({ permissionDecision: 'allow' }))
    process.exit(0)
  }

  const { isEntryPoint, requiredDeps } = getPhaseInfo(phaseName)

  if (isEntryPoint || requiredDeps.length === 0) {
    // Entry point phase or no required dependencies — allow without DAG check
    process.stdout.write(JSON.stringify({ permissionDecision: 'allow' }))
    process.exit(0)
  }

  const runState = findLatestRunState(RUNS_DIR)

  if (!runState) {
    // No run state found — allow (the MCP server will handle validation)
    process.stdout.write(JSON.stringify({ permissionDecision: 'allow' }))
    process.exit(0)
  }

  // DAG semantics match dag.ts:59 — a required predecessor is satisfied
  // by EITHER status 'completed' OR 'skipped'. A missing phase or any
  // other status (pending, in_progress, failed) is unmet.
  const unmetDeps = requiredDeps.filter(dep => {
    const phase = runState.phases[dep]
    if (!phase) return true
    return phase.status !== 'completed' && phase.status !== 'skipped'
  })

  if (unmetDeps.length > 0) {
    const depStatuses = unmetDeps.map(dep => {
      const phase = runState.phases[dep]
      return `  ${dep}: ${phase ? phase.status : 'not found'}`
    }).join('\n')

    process.stdout.write(JSON.stringify({
      permissionDecision: 'deny',
      deny_reason: `Cannot start phase "${phaseName}" — required dependencies not completed:\n${depStatuses}\n\nComplete these phases first.`,
    }))
  } else {
    process.stdout.write(JSON.stringify({ permissionDecision: 'allow' }))
  }
} catch {
  process.stdout.write(JSON.stringify({ permissionDecision: 'allow' }))
}
