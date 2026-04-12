// hooks/lib/common.mjs — Shared helpers for Claude Code harness hook scripts.
// All 11 scripts under hooks/scripts/ import from this module. Do not add
// side-effects at import time — every export must be pure/lazy so scripts
// can import only what they need without paying startup cost for unused
// helpers.

import { readFileSync, readdirSync, existsSync } from 'node:fs'
import { join } from 'node:path'

/**
 * Read all of stdin as a UTF-8 string. Simple unbounded reader used by
 * every hook script's top-of-file `for await (const chunk of process.stdin)`
 * pattern. Returns '' when stdin is empty or already closed.
 *
 * NOTE: a size cap is added in Fix 6.11; keep this function minimal here
 * so the 6.11 diff is scoped to cap + test only.
 */
export async function readStdin() {
  let input = ''
  for await (const chunk of process.stdin) {
    input += chunk
  }
  return input
}

/**
 * Load runtime-config.json from the given absolute path. Returns an empty
 * object on any read or parse error so callers can apply per-hook defaults
 * via `const cfg = { ...defaults, ...loadConfig(CONFIG_PATH) }`.
 */
export function loadConfig(configPath) {
  if (!existsSync(configPath)) return {}
  try {
    return JSON.parse(readFileSync(configPath, 'utf-8'))
  } catch {
    return {}
  }
}

/**
 * Scan `runsDir` for run-state.json files and return the most-recently-
 * updated run state. When `options.includeDir === true`, returns
 * `{ state, dir }` (both null when no state found) instead of the bare
 * state object. The dir-variant matches post-phase-complete.mjs's historical
 * need for the containing run directory to append events to.
 *
 * Returns `null` (or `{ state: null, dir: null }`) when no run state is
 * found or `runsDir` does not exist.
 */
export function findLatestRunState(runsDir, options = {}) {
  const includeDir = options.includeDir === true
  const nullResult = includeDir ? { state: null, dir: null } : null

  if (!existsSync(runsDir)) return nullResult

  let runDirs
  try {
    runDirs = readdirSync(runsDir, { withFileTypes: true })
      .filter(d => d.isDirectory())
      .map(d => d.name)
  } catch {
    return nullResult
  }

  let latestState = null
  let latestDir = null
  let latestTime = 0

  for (const dir of runDirs) {
    const statePath = join(runsDir, dir, 'run-state.json')
    if (!existsSync(statePath)) continue
    try {
      const state = JSON.parse(readFileSync(statePath, 'utf-8'))
      const updatedAt = new Date(state.updated_at).getTime()
      if (Number.isFinite(updatedAt) && updatedAt > latestTime) {
        latestTime = updatedAt
        latestState = state
        latestDir = join(runsDir, dir)
      }
    } catch {
      continue
    }
  }

  return includeDir ? { state: latestState, dir: latestDir } : latestState
}
