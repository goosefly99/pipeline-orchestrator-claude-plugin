#!/usr/bin/env node
// post-phase-complete.mjs — PostToolUse hook for pipeline_complete_phase
// Appends completion event to events.jsonl, emits next available phases

import { readFileSync, existsSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parse } from 'smol-toml'
import { readStdin, findLatestRunState } from '../lib/common.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const RUNS_DIR = join(__dirname, '..', '..', 'pipeline_mcp_data', 'runs')
const PIPELINE_TOML = join(__dirname, '..', '..', 'pipeline', 'pipeline.toml')

function getNextPhases(runState) {
  if (!existsSync(PIPELINE_TOML) || !runState) return []

  try {
    const toml = parse(readFileSync(PIPELINE_TOML, 'utf-8'))
    const edges = toml.edges || []
    const phases = toml.phases || {}

    // Find phases that are still pending and whose dependencies are met
    const nextPhases = []
    for (const [phaseName, phaseDef] of Object.entries(phases)) {
      const phaseState = runState.phases[phaseName]
      if (phaseState && phaseState.status !== 'pending') continue

      // Get required dependencies for this phase
      const requiredDeps = edges
        .filter(e => e.to === phaseName && !e.optional)
        .map(e => e.from)

      // If entry_point and no required deps, it's always available
      if (phaseDef.entry_point && requiredDeps.length === 0) {
        nextPhases.push({ name: phaseName, model_tier: phaseDef.model_tier })
        continue
      }

      // DAG semantics match dag.ts:59 — a required predecessor is
      // satisfied by EITHER 'completed' OR 'skipped'.
      const allMet = requiredDeps.every(dep => {
        const depState = runState.phases[dep]
        return !!depState && (depState.status === 'completed' || depState.status === 'skipped')
      })

      if (allMet && requiredDeps.length > 0) {
        nextPhases.push({ name: phaseName, model_tier: phaseDef.model_tier })
      }
    }

    return nextPhases
  } catch {
    return []
  }
}

const input = await readStdin()

try {
  const hookInput = JSON.parse(input || '{}')
  const toolInput = hookInput.tool_input || {}
  const phaseName = toolInput.phase || toolInput.phase_name || 'unknown'

  const runState = findLatestRunState(RUNS_DIR)

  // Find next available phases
  const nextPhases = getNextPhases(runState)

  if (nextPhases.length > 0) {
    const phaseList = nextPhases
      .map(p => p.model_tier ? `  ${p.name} (recommended: ${p.model_tier})` : `  ${p.name}`)
      .join('\n')

    process.stdout.write(JSON.stringify({
      systemMessage: `Phase "${phaseName}" completed. Next available phases:\n${phaseList}\n\nConsider using /clear to reset context before starting the next phase.`,
    }))
  } else {
    process.stdout.write(JSON.stringify({
      systemMessage: `Phase "${phaseName}" completed. No more phases are available — pipeline may be complete.`,
    }))
  }
} catch {
  // Non-blocking — exit cleanly on error
  process.exit(0)
}
