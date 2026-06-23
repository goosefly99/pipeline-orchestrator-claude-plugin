#!/usr/bin/env node
// phase-start-guard.mjs — PreToolUse hook for pipeline_start_phase
// Tracks phases started per session via TMPDIR temp files, warns or denies repeat starts

import { readFileSync, writeFileSync, existsSync, mkdirSync, readdirSync, statSync, unlinkSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { tmpdir } from 'node:os'
import { readStdin, loadConfig } from '../lib/common.mjs'

const MAX_SANITIZED_SESSION_ID_LEN = 128

/**
 * Sanitize a session_id string for safe use as a filename component.
 * Any character outside [A-Za-z0-9_-] is considered unsafe. The sanitizer
 * fails closed (caller should deny) if:
 *   - the input is not a string,
 *   - the input contains any char outside [A-Za-z0-9_-] (path traversal,
 *     null bytes, separators, etc.),
 *   - the (sanitized) result is empty — we never want to write to
 *     `session-.json`,
 *   - the result exceeds MAX_SANITIZED_SESSION_ID_LEN characters.
 * On success, the returned `sanitized` value is identical to the input
 * (since any unsafe character would have already triggered a failure).
 */
function sanitizeSessionId(raw) {
  if (typeof raw !== 'string') return { ok: false, reason: 'session_id is not a string' }
  const sanitized = raw.replace(/[^a-zA-Z0-9_-]/g, '_')
  if (sanitized.length === 0) return { ok: false, reason: 'session_id is empty after sanitization' }
  if (sanitized.length > MAX_SANITIZED_SESSION_ID_LEN) {
    return { ok: false, reason: `session_id exceeds ${MAX_SANITIZED_SESSION_ID_LEN} characters after sanitization` }
  }
  if (sanitized !== raw) {
    return { ok: false, reason: 'session_id contains disallowed characters (only [A-Za-z0-9_-] permitted)' }
  }
  return { ok: true, sanitized }
}

const SESSION_FILE_TTL_MS = 24 * 60 * 60 * 1000  // 24 hours
const SESSION_CLEANUP_MAX_FILES = 100

/**
 * Remove stale session-tracking files in trackDir whose mtime is older than
 * SESSION_FILE_TTL_MS. Caps work at SESSION_CLEANUP_MAX_FILES per invocation
 * so a huge $TMPDIR never makes this hook slow. Unlink errors are swallowed
 * — this is best-effort housekeeping, not a correctness barrier.
 */
function cleanupStaleSessionFiles(trackDir) {
  if (!existsSync(trackDir)) return
  let entries
  try {
    entries = readdirSync(trackDir)
  } catch {
    return
  }
  const cutoff = Date.now() - SESSION_FILE_TTL_MS
  let processed = 0
  for (const name of entries) {
    if (processed >= SESSION_CLEANUP_MAX_FILES) break
    if (!name.startsWith('session-') || !name.endsWith('.json')) continue
    const full = join(trackDir, name)
    let mtimeMs
    try {
      mtimeMs = statSync(full).mtimeMs
    } catch {
      continue
    }
    if (mtimeMs < cutoff) {
      try {
        unlinkSync(full)
      } catch {
        // Ignore — another process may have just deleted it, or permissions.
      }
    }
    processed++
  }
}

const __dirname = dirname(fileURLToPath(import.meta.url))
const CONFIG_PATH = join(__dirname, '..', 'runtime-config.json')

const input = await readStdin()

try {
  const config = { phase_session_isolation: { warn_only: true }, ...loadConfig(CONFIG_PATH) }

  const guardConfig = config.phase_session_isolation || {}
  const warnOnly = guardConfig.warn_only !== false

  const hookInput = JSON.parse(input || '{}')
  const rawSessionId = hookInput.session_id || 'unknown'
  const toolInput = hookInput.tool_input || {}
  const phaseName = toolInput.phase || toolInput.phase_name || 'unknown'

  // Fail-closed on any session_id that can't be safely used as a file
  // component. This guards against path traversal (../../etc/passwd), null
  // bytes, absolute paths, and unbounded-length inputs.
  const san = sanitizeSessionId(rawSessionId)
  if (!san.ok) {
    process.stdout.write(JSON.stringify({
      permissionDecision: 'deny',
      deny_reason: `phase-start-guard refused unsafe session_id: ${san.reason}`,
    }))
    process.exit(0)
  }
  const sessionId = san.sanitized

  // Track phases per session in TMPDIR
  const trackDir = join(tmpdir(), 'pipeline-hooks-sessions')
  if (!existsSync(trackDir)) mkdirSync(trackDir, { recursive: true })

  // Best-effort cleanup of session-tracking files older than 24h so
  // $TMPDIR/pipeline-hooks-sessions/ doesn't grow unbounded across runs.
  // Scoped, rate-limited, and swallows errors — correctness never depends
  // on this firing.
  cleanupStaleSessionFiles(trackDir)

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
