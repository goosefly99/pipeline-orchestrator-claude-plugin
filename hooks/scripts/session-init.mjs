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

  // Build context summary. The "next available phases" line was removed
  // in Fix 6.6 because the prior implementation listed ALL pending phases
  // without DAG dependency checks — producing misleading "next up"
  // suggestions. Users should call pipeline_next_phases for an accurate,
  // DAG-gated list.
  const phaseStatuses = Object.entries(latestRun.phases)
    .map(([name, p]) => `  ${name}: ${p.status}`)
    .join('\n')

  const message = [
    `Active pipeline run: ${latestRun.run_id}`,
    `Status: ${latestRun.status}`,
    `Phases:\n${phaseStatuses}`,
    '',
    'Call pipeline_next_phases to see which phases are actually available (DAG-gated).',
    'Call pipeline_run_status to get full state before making changes.',
    'Consider using /clear between phases to reset context.',
  ].join('\n')

  const response = { systemMessage: message }
  process.stdout.write(JSON.stringify(response))
} catch (err) {
  // Non-blocking — exit cleanly on error
  process.exit(0)
}
