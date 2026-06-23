#!/usr/bin/env node
// send-message-guard.mjs — PreToolUse hook for SendMessage
// Controls whether SendMessage is allowed, denied, or requires approval

import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { readStdin, loadConfig } from '../lib/common.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const CONFIG_PATH = join(__dirname, '..', 'runtime-config.json')

const input = await readStdin()

try {
  const config = { send_message_guard: { mode: 'ask', deny_reason: 'SendMessage requires approval.' }, ...loadConfig(CONFIG_PATH) }

  const guardConfig = config.send_message_guard || {}
  const mode = guardConfig.mode || 'ask'
  const denyReason = guardConfig.deny_reason || 'SendMessage requires approval during pipeline execution.'

  let response
  switch (mode) {
    case 'allow':
      response = { permissionDecision: 'allow' }
      break
    case 'deny':
      response = { permissionDecision: 'deny', deny_reason: denyReason }
      break
    case 'ask':
    default:
      response = { permissionDecision: 'ask' }
      break
  }

  process.stdout.write(JSON.stringify(response))
} catch {
  // On error, default to ask
  process.stdout.write(JSON.stringify({ permissionDecision: 'ask' }))
}
