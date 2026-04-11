#!/usr/bin/env node
// session-init.mjs — SessionStart hook
// Scans for active run state, injects context via systemMessage

import { readFileSync, readdirSync, existsSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const RUNS_DIR = join(__dirname, '..', '..', 'pipeline_mcp_data', 'runs')

// Read hook input from stdin
let input = ''
for await (const chunk of process.stdin) {
  input += chunk
}

try {
  // Find active run state
  if (!existsSync(RUNS_DIR)) {
    // No runs directory — no-op
    process.exit(0)
  }

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

  if (!latestRun) {
    process.exit(0)
  }

  // Build context summary
  const phaseStatuses = Object.entries(latestRun.phases)
    .map(([name, p]) => `  ${name}: ${p.status}`)
    .join('\n')

  const nextPhases = Object.entries(latestRun.phases)
    .filter(([, p]) => p.status === 'pending')
    .map(([name]) => name)

  const message = [
    `Active pipeline run: ${latestRun.run_id}`,
    `Status: ${latestRun.status}`,
    `Phases:\n${phaseStatuses}`,
    nextPhases.length > 0 ? `Next available: ${nextPhases.join(', ')}` : '',
    '',
    'Call pipeline_run_status to get full state before making changes.',
    'Consider using /clear between phases to reset context.',
  ].filter(Boolean).join('\n')

  const response = { systemMessage: message }
  process.stdout.write(JSON.stringify(response))
} catch (err) {
  // Non-blocking — exit cleanly on error
  process.exit(0)
}
