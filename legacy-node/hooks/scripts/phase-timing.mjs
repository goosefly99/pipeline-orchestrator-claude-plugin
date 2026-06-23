#!/usr/bin/env node
// phase-timing.mjs — PostToolUse hook for pipeline_(start|complete)_phase
// Appends timing entries to timing.jsonl for telemetry

import { appendFileSync, mkdirSync, existsSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { readStdin } from '../lib/common.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const LOGS_DIR = join(__dirname, '..', 'hook-logs')
const TIMING_PATH = join(LOGS_DIR, 'timing.jsonl')

const input = await readStdin()

try {
  const hookInput = JSON.parse(input || '{}')
  const toolName = hookInput.tool_name || ''
  const toolInput = hookInput.tool_input || {}
  const sessionId = hookInput.session_id || 'unknown'

  const phaseName = toolInput.phase || toolInput.phase_name || 'unknown'

  // Determine event type from tool name
  let event = 'unknown'
  if (toolName.includes('start_phase')) {
    event = 'phase_started'
  } else if (toolName.includes('complete_phase')) {
    event = 'phase_completed'
  }

  const entry = {
    phase: phaseName,
    event,
    timestamp: new Date().toISOString(),
    session_id: sessionId,
  }

  if (!existsSync(LOGS_DIR)) mkdirSync(LOGS_DIR, { recursive: true })

  appendFileSync(TIMING_PATH, JSON.stringify(entry) + '\n', 'utf-8')

  // No output needed for PostToolUse timing — silent telemetry
  process.exit(0)
} catch {
  process.exit(0)
}
