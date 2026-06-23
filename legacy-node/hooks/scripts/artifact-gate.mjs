#!/usr/bin/env node
// artifact-gate.mjs — PreToolUse hook for pipeline_complete_phase
// Verifies at least one artifact exists for the target phase before allowing completion

import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { readStdin, findLatestRunState } from '../lib/common.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const RUNS_DIR = join(__dirname, '..', '..', 'pipeline_mcp_data', 'runs')

const input = await readStdin()

try {
  const hookInput = JSON.parse(input || '{}')
  const toolInput = hookInput.tool_input || {}
  const phaseName = toolInput.phase || toolInput.phase_name

  if (!phaseName) {
    process.stdout.write(JSON.stringify({ permissionDecision: 'allow' }))
    process.exit(0)
  }

  const runState = findLatestRunState(RUNS_DIR)

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
