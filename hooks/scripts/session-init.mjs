#!/usr/bin/env node
// session-init.mjs — SessionStart hook
// Scans for active run state, injects context via systemMessage

import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { readStdin, findLatestRunState } from '../lib/common.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const RUNS_DIR = join(__dirname, '..', '..', 'pipeline_mcp_data', 'runs')

// Read hook input from stdin (drain it even though session-init doesn't consume the body)
await readStdin()

try {
  const latestRun = findLatestRunState(RUNS_DIR)

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
