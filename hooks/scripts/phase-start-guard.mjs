#!/usr/bin/env node
// phase-start-guard.mjs — PreToolUse hook for pipeline_start_phase
// Tracks phases started per session via TMPDIR temp files, warns or denies repeat starts

import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { tmpdir } from 'node:os'

const __dirname = dirname(fileURLToPath(import.meta.url))
const CONFIG_PATH = join(__dirname, '..', 'runtime-config.json')

let input = ''
for await (const chunk of process.stdin) {
  input += chunk
}

try {
  let config = { phase_session_isolation: { warn_only: true } }
  if (existsSync(CONFIG_PATH)) {
    config = JSON.parse(readFileSync(CONFIG_PATH, 'utf-8'))
  }

  const guardConfig = config.phase_session_isolation || {}
  const warnOnly = guardConfig.warn_only !== false

  const hookInput = JSON.parse(input || '{}')
  const sessionId = hookInput.session_id || 'unknown'
  const toolInput = hookInput.tool_input || {}
  const phaseName = toolInput.phase || toolInput.phase_name || 'unknown'

  // Track phases per session in TMPDIR
  const trackDir = join(tmpdir(), 'pipeline-hooks-sessions')
  if (!existsSync(trackDir)) mkdirSync(trackDir, { recursive: true })

  const sessionFile = join(trackDir, `session-${sessionId}.json`)
  let sessionData = { phases_started: [] }

  if (existsSync(sessionFile)) {
    try {
      sessionData = JSON.parse(readFileSync(sessionFile, 'utf-8'))
    } catch {
      sessionData = { phases_started: [] }
    }
  }

  const alreadyStarted = sessionData.phases_started.includes(phaseName)

  if (alreadyStarted) {
    if (warnOnly) {
      process.stdout.write(JSON.stringify({
        permissionDecision: 'allow',
        systemMessage: `Phase "${phaseName}" was already started in this session. Consider using /clear between phases to reset context and reduce token costs.`,
      }))
    } else {
      process.stdout.write(JSON.stringify({
        permissionDecision: 'deny',
        deny_reason: `Phase "${phaseName}" was already started in this session. Use /clear to reset context before starting a new phase.`,
      }))
    }
  } else {
    // Record this phase start
    sessionData.phases_started.push(phaseName)
    writeFileSync(sessionFile, JSON.stringify(sessionData, null, 2), 'utf-8')

    process.stdout.write(JSON.stringify({ permissionDecision: 'allow' }))
  }
} catch {
  process.stdout.write(JSON.stringify({ permissionDecision: 'allow' }))
}
