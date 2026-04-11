#!/usr/bin/env node
// token-budget-guard.mjs — PreToolUse hook for mcp__pipeline__pipeline_.*
// Tracks transcript-based token cost and enforces budget limits

import { readFileSync, existsSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createReadStream } from 'node:fs'
import { createInterface } from 'node:readline'

const __dirname = dirname(fileURLToPath(import.meta.url))
const CONFIG_PATH = join(__dirname, '..', 'runtime-config.json')

// Model pricing per million tokens (USD)
const MODEL_PRICING = {
  'claude-sonnet-4-20250514': { input: 3.0, output: 15.0, cache_write: 3.75, cache_read: 0.30 },
  'claude-opus-4-20250514': { input: 15.0, output: 75.0, cache_write: 18.75, cache_read: 1.50 },
  'claude-haiku-3-20250307': { input: 0.80, output: 4.0, cache_write: 1.0, cache_read: 0.08 },
  // Fallback pricing (sonnet-level)
  default: { input: 3.0, output: 15.0, cache_write: 3.75, cache_read: 0.30 },
}

function getPricing(model) {
  if (!model) return MODEL_PRICING.default
  for (const [key, pricing] of Object.entries(MODEL_PRICING)) {
    if (key === 'default') continue
    if (model.includes(key) || model.startsWith(key.split('-')[0] + '-' + key.split('-')[1])) {
      return pricing
    }
  }
  // Try partial matching
  if (model.includes('opus')) return MODEL_PRICING['claude-opus-4-20250514']
  if (model.includes('haiku')) return MODEL_PRICING['claude-haiku-3-20250307']
  if (model.includes('sonnet')) return MODEL_PRICING['claude-sonnet-4-20250514']
  return MODEL_PRICING.default
}

function calculateCost(usage, model) {
  const pricing = getPricing(model)
  const inputTokens = usage.input_tokens || 0
  const outputTokens = usage.output_tokens || 0
  const cacheWrite = usage.cache_creation_input_tokens || 0
  const cacheRead = usage.cache_read_input_tokens || 0

  return (
    (inputTokens * pricing.input) / 1_000_000 +
    (outputTokens * pricing.output) / 1_000_000 +
    (cacheWrite * pricing.cache_write) / 1_000_000 +
    (cacheRead * pricing.cache_read) / 1_000_000
  )
}

async function sumTranscriptCost(transcriptPath) {
  if (!transcriptPath || !existsSync(transcriptPath)) return 0

  let totalCost = 0
  const rl = createInterface({
    input: createReadStream(transcriptPath, 'utf-8'),
    crlfDelay: Infinity,
  })

  for await (const line of rl) {
    if (!line.trim()) continue
    try {
      const entry = JSON.parse(line)
      if (entry.usage) {
        totalCost += calculateCost(entry.usage, entry.model)
      }
    } catch {
      // Skip malformed lines
    }
  }

  return totalCost
}

// Read hook input from stdin
let input = ''
for await (const chunk of process.stdin) {
  input += chunk
}

try {
  let config = { token_budget_guard: { max_extra_usage_usd: 5.0, warn_at_pct: 80 } }
  if (existsSync(CONFIG_PATH)) {
    config = JSON.parse(readFileSync(CONFIG_PATH, 'utf-8'))
  }

  const guardConfig = config.token_budget_guard || {}
  const maxBudget = guardConfig.max_extra_usage_usd || 5.0
  const warnPct = guardConfig.warn_at_pct || 80

  const hookInput = JSON.parse(input || '{}')
  const transcriptPath = hookInput.transcript_path

  const totalCost = await sumTranscriptCost(transcriptPath)
  const usagePct = (totalCost / maxBudget) * 100

  if (totalCost >= maxBudget) {
    process.stdout.write(JSON.stringify({
      permissionDecision: 'deny',
      deny_reason: `Token budget exceeded: $${totalCost.toFixed(2)} spent of $${maxBudget.toFixed(2)} limit (${usagePct.toFixed(0)}%). Stop and review before continuing.`,
    }))
  } else if (usagePct >= warnPct) {
    process.stdout.write(JSON.stringify({
      permissionDecision: 'allow',
      systemMessage: `Token budget warning: $${totalCost.toFixed(2)} of $${maxBudget.toFixed(2)} used (${usagePct.toFixed(0)}%). Approaching limit.`,
    }))
  } else {
    process.stdout.write(JSON.stringify({ permissionDecision: 'allow' }))
  }
} catch {
  // On error, allow to avoid blocking pipeline
  process.stdout.write(JSON.stringify({ permissionDecision: 'allow' }))
}
