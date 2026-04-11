#!/usr/bin/env node
// dag-guard.mjs — PreToolUse hook for pipeline_start_phase
// Validates DAG dependencies are satisfied before allowing phase start

import { readFileSync, readdirSync, existsSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parse } from 'smol-toml'

const __dirname = dirname(fileURLToPath(import.meta.url))
const CONFIG_PATH = join(__dirname, '..', 'runtime-config.json')
const RUNS_DIR = join(__dirname, '..', '..', 'pipeline_mcp_data', 'runs')
const PIPELINE_TOML = join(__dirname, '..', '..', 'pipeline', 'pipeline.toml')

function findLatestRunState() {
  if (!existsSync(RUNS_DIR)) return null

  const runDirs = readdirSync(RUNS_DIR, { withFileTypes: true })
    .filter(d => d.isDirectory())
    .map(d => d.name)

  let latestRun = null
  let latestTime = 0

  for (const dir of runDirs) {
    const statePath = join(RUNS_DIR, dir, 'run-state.json')
    if (!existsSync(statePath)) continue
    try {
      const state = JSON.parse(readFileSync(statePath, 'utf-8'))
      const updatedAt = new Date(state.updated_at).getTime()
      if (updatedAt > latestTime) {
        latestTime = updatedAt
        latestRun = state
      }
    } catch {
      continue
    }
  }

  return latestRun
}

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

let input = ''
for await (const chunk of process.stdin) {
  input += chunk
}

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

  const runState = findLatestRunState()

  if (!runState) {
    // No run state found — allow (the MCP server will handle validation)
    process.stdout.write(JSON.stringify({ permissionDecision: 'allow' }))
    process.exit(0)
  }

  const unmetDeps = requiredDeps.filter(dep => {
    const phase = runState.phases[dep]
    return !phase || phase.status !== 'completed'
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
