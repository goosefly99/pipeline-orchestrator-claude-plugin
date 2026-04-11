#!/usr/bin/env node
// stop-guard.mjs — Stop hook
// Blocks stop when pipeline phases are in-progress

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

  // Prevent infinite loops: if stop_hook_active is set, allow stop
  if (hookInput.stop_hook_active) {
    process.exit(0)
  }

  const runState = findLatestRunState()

  if (!runState || runState.status === 'completed' || runState.status === 'failed') {
    // No active run — allow stop
    process.exit(0)
  }

  // Check for in-progress phases
  const activePhases = Object.entries(runState.phases)
    .filter(([, p]) => p.status === 'in_progress')
    .map(([name]) => name)

  if (activePhases.length > 0) {
    const phaseList = activePhases.join(', ')
    process.stdout.write(JSON.stringify({
      systemMessage: `Warning: Pipeline run "${runState.run_id}" has active phases: ${phaseList}. Consider completing or failing these phases before ending the session. Use pipeline_fail_phase to mark incomplete work, or pipeline_complete_phase if work is done.`,
    }))
  }
} catch {
  // On error, allow stop
  process.exit(0)
}
