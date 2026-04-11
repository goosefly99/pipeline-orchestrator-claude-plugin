#!/usr/bin/env node
// file-read-version-guard.mjs — PreToolUse hook for Read
// Prevents reading files that contain outdated version tags in their paths

import { readFileSync, existsSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const CONFIG_PATH = join(__dirname, '..', 'runtime-config.json')

/**
 * Normalize version string to comparable forms.
 * "v0.3.0" -> ["v0.3.0", "v0-3-0", "0.3.0", "0-3-0"]
 */
function normalizeVersionPatterns(version) {
  const patterns = new Set()
  const stripped = version.replace(/^v/, '')
  const dotted = stripped.replace(/-/g, '.')
  const dashed = stripped.replace(/\./g, '-')

  patterns.add(`v${dotted}`)
  patterns.add(`v${dashed}`)
  patterns.add(dotted)
  patterns.add(dashed)

  return [...patterns]
}

let input = ''
for await (const chunk of process.stdin) {
  input += chunk
}

try {
  let config = {
    file_read_version_guard: {
      present_version_in_development: '',
      previous_versions: [],
    },
  }
  if (existsSync(CONFIG_PATH)) {
    config = JSON.parse(readFileSync(CONFIG_PATH, 'utf-8'))
  }

  const guardConfig = config.file_read_version_guard || {}
  const currentVersion = guardConfig.present_version_in_development || ''
  const previousVersions = guardConfig.previous_versions || []

  if (!currentVersion || previousVersions.length === 0) {
    // No version guard configured — allow all reads
    process.stdout.write(JSON.stringify({ permissionDecision: 'allow' }))
    process.exit(0)
  }

  const hookInput = JSON.parse(input || '{}')
  const toolInput = hookInput.tool_input || {}
  const filePath = toolInput.file_path || toolInput.path || ''

  if (!filePath) {
    process.stdout.write(JSON.stringify({ permissionDecision: 'allow' }))
    process.exit(0)
  }

  // Normalize the file path for comparison
  const normalizedPath = filePath.toLowerCase().replace(/\\/g, '/')

  // Check if the file path contains any outdated version patterns
  for (const oldVersion of previousVersions) {
    const patterns = normalizeVersionPatterns(oldVersion)
    for (const pattern of patterns) {
      if (normalizedPath.includes(pattern.toLowerCase())) {
        // Check it's not actually the current version
        const currentPatterns = normalizeVersionPatterns(currentVersion)
        const isCurrentVersion = currentPatterns.some(cp =>
          normalizedPath.includes(cp.toLowerCase()),
        )

        if (!isCurrentVersion) {
          process.stdout.write(JSON.stringify({
            permissionDecision: 'deny',
            deny_reason: `File path contains outdated version "${oldVersion}". Current version in development is "${currentVersion}". Read the current version file instead.`,
          }))
          process.exit(0)
        }
      }
    }
  }

  process.stdout.write(JSON.stringify({ permissionDecision: 'allow' }))
} catch {
  process.stdout.write(JSON.stringify({ permissionDecision: 'allow' }))
}
