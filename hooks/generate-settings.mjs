#!/usr/bin/env node
// generate-settings.mjs — Read hooks-config.toml, write runtime-config.json
// and merge hook entries into .claude/settings.local.json

import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parse } from 'smol-toml'

const __dirname = dirname(fileURLToPath(import.meta.url))
const CONFIG_PATH = join(__dirname, 'hooks-config.toml')
const RUNTIME_CONFIG_PATH = join(__dirname, 'runtime-config.json')
const PROJECT_ROOT = join(__dirname, '..')
const SETTINGS_PATH = join(PROJECT_ROOT, '.claude', 'settings.local.json')

// ── Load and parse TOML config ──────────────────────────────

const tomlContent = readFileSync(CONFIG_PATH, 'utf-8')
const config = parse(tomlContent)

// ── Write flattened runtime-config.json ──────────────────────

writeFileSync(RUNTIME_CONFIG_PATH, JSON.stringify(config, null, 2), 'utf-8')
console.log(`Wrote runtime-config.json`)

// ── Build hook entries for settings.local.json ──────────────

if (!config.master?.enabled) {
  console.log('Master switch is disabled — no hooks will be registered.')
  process.exit(0)
}

const scriptsDir = join(__dirname, 'scripts')

// Hook definitions: [configKey, event, matcher, scriptFile]
const hookDefs = [
  ['session_start_clear', 'SessionStart', undefined, 'session-init.mjs'],
  ['send_message_guard', 'PreToolUse', 'SendMessage', 'send-message-guard.mjs'],
  ['token_budget_guard', 'PreToolUse', 'mcp__pipeline__pipeline_.*', 'token-budget-guard.mjs'],
  ['phase_session_isolation', 'PreToolUse', 'mcp__pipeline__pipeline_start_phase', 'phase-start-guard.mjs'],
  ['dag_guard', 'PreToolUse', 'mcp__pipeline__pipeline_start_phase', 'dag-guard.mjs'],
  ['artifact_gate', 'PreToolUse', 'mcp__pipeline__pipeline_complete_phase', 'artifact-gate.mjs'],
  ['file_read_version_guard', 'PreToolUse', 'Read', 'file-read-version-guard.mjs'],
  ['post_phase_complete', 'PostToolUse', 'mcp__pipeline__pipeline_complete_phase', 'post-phase-complete.mjs'],
  ['phase_context_inject', 'PostToolUse', 'mcp__pipeline__pipeline_start_phase', 'phase-context-inject.mjs'],
  ['phase_timing', 'PostToolUse', 'mcp__pipeline__pipeline_(start|complete)_phase', 'phase-timing.mjs'],
  ['stop_guard', 'Stop', undefined, 'stop-guard.mjs'],
]

const hooks = []
for (const [configKey, event, matcher, scriptFile] of hookDefs) {
  const hookConfig = config[configKey]
  if (!hookConfig?.enabled) continue

  const entry = {
    type: 'command',
    event,
    command: `node ${join(scriptsDir, scriptFile).replace(/\\/g, '/')}`,
  }
  if (matcher) {
    entry.matcher = matcher
  }
  hooks.push(entry)
}

// ── Merge into settings.local.json ──────────────────────────

const claudeDir = dirname(SETTINGS_PATH)
if (!existsSync(claudeDir)) mkdirSync(claudeDir, { recursive: true })

let existingSettings = {}
if (existsSync(SETTINGS_PATH)) {
  try {
    existingSettings = JSON.parse(readFileSync(SETTINGS_PATH, 'utf-8'))
  } catch {
    existingSettings = {}
  }
}

// Preserve non-hook keys, replace hooks array
existingSettings.hooks = hooks

writeFileSync(SETTINGS_PATH, JSON.stringify(existingSettings, null, 2), 'utf-8')
console.log(`Wrote ${hooks.length} hooks to ${SETTINGS_PATH}`)
