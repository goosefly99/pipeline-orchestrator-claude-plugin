#!/usr/bin/env node
// artifact-gate.mjs — PreToolUse hook for pipeline_complete_phase
// Verifies at least one artifact exists for the target phase before allowing completion

import { readFileSync, readdirSync, existsSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const RUNS_DIR = join(__dirname, '..', '..', 'pipeline_mcp_data', 'runs')

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

  const runState = findLatestRunState()

  if (!runState) {
    process.stdout.write(JSON.stringify({ permissionDecision: 'allow' }))
    process.exit(0)
  }

  // Check available_artifacts for this phase
  const phaseArtifacts = (runState.available_artifacts || [])
    .filter(a => a.phase === phaseName)

  // Also check phase output_artifacts
  const phaseState = runState.phases[phaseName]
  const outputArtifacts = phaseState?.output_artifacts || []

  if (phaseArtifacts.length === 0 && outputArtifacts.length === 0) {
    process.stdout.write(JSON.stringify({
      permissionDecision: 'deny',
      deny_reason: `Cannot complete phase "${phaseName}" — no artifacts have been stored. Use pipeline_store_artifact to save at least one output before completing.`,
    }))
  } else {
    process.stdout.write(JSON.stringify({ permissionDecision: 'allow' }))
  }
} catch {
  process.stdout.write(JSON.stringify({ permissionDecision: 'allow' }))
}
