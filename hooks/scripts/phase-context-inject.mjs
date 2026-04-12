#!/usr/bin/env node
// phase-context-inject.mjs — PostToolUse hook for pipeline_start_phase
// Injects phase description, available tools, output types, and quality gate criteria

import { readFileSync, existsSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parse } from 'smol-toml'
import { readStdin } from '../lib/common.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const PIPELINE_TOML = join(__dirname, '..', '..', 'pipeline', 'pipeline.toml')
const QUALITY_GATES_TOML = join(__dirname, '..', '..', 'pipeline', 'quality-gates.toml')

function loadPhaseDefinition(phaseName) {
  if (!existsSync(PIPELINE_TOML)) return null

  try {
    const toml = parse(readFileSync(PIPELINE_TOML, 'utf-8'))
    return toml.phases?.[phaseName] || null
  } catch {
    return null
  }
}

function loadQualityGate(phaseName) {
  if (!existsSync(QUALITY_GATES_TOML)) return null

  try {
    const toml = parse(readFileSync(QUALITY_GATES_TOML, 'utf-8'))
    const gates = toml.gates || []
    return gates.find(g => g.phase === phaseName) || null
  } catch {
    return null
  }
}

const input = await readStdin()

try {
  const hookInput = JSON.parse(input || '{}')
  const toolInput = hookInput.tool_input || {}
  const phaseName = toolInput.phase || toolInput.phase_name

  if (!phaseName) {
    process.exit(0)
  }

  const phaseDef = loadPhaseDefinition(phaseName)

  if (!phaseDef) {
    process.exit(0)
  }

  const lines = [
    `Phase: ${phaseName}`,
    `Description: ${phaseDef.description || 'No description'}`,
  ]

  if (phaseDef.outputs && phaseDef.outputs.length > 0) {
    lines.push(`Expected outputs: ${phaseDef.outputs.join(', ')}`)
  }

  if (phaseDef.tools && phaseDef.tools.length > 0) {
    lines.push(`Available tools: ${phaseDef.tools.join(', ')}`)
  }

  if (phaseDef.model_tier) {
    lines.push(`Recommended model: ${phaseDef.model_tier}`)
  }

  // Add quality gate criteria
  const gate = loadQualityGate(phaseName)
  if (gate) {
    lines.push('')
    lines.push(`Quality gate (on_failure: ${gate.on_failure || 'warn'}):`)
    const checks = gate.checks || []
    for (const check of checks) {
      lines.push(`  - ${check.check_type}: ${check.description || JSON.stringify(check.params || {})}`)
    }
  }

  lines.push('')
  lines.push('Store artifacts with pipeline_store_artifact before calling pipeline_complete_phase.')

  process.stdout.write(JSON.stringify({
    systemMessage: lines.join('\n'),
  }))
} catch {
  process.exit(0)
}
