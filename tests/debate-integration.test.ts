// pipeline-mcp/tests/debate-integration.test.ts

import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

import { loadSchemas, validateArtifact } from '../validator.ts'
import { storeArtifact, loadArtifact } from '../storage.ts'
import {
  initDebate,
  submitArgument,
  generateAgentPrompts,
  generateSynthesisPrompt,
  buildTranscript,
} from '../debate.ts'
import type { StorageConfig } from '../types.ts'

const __dirname = dirname(fileURLToPath(import.meta.url))
const PIPELINE_DIR = resolve(__dirname, '../pipeline')

let tempDir: string

beforeEach(() => {
  tempDir = mkdtempSync(join(tmpdir(), 'pipeline-debate-integration-'))
})

afterEach(() => {
  rmSync(tempDir, { recursive: true, force: true })
})

describe('integration: full debate flow on a design-spec', () => {
  it('init → prompts → submit 4 arguments → synthesize → build transcript → validate → store → load', () => {
    const schemas = loadSchemas(join(PIPELINE_DIR, 'schemas'))

    const designSpec = {
      spec_id: 'spec-integration-test',
      strategy_name: 'Kalshi Event Arb',
      version: '1.0.0',
      created_date: '2026-04-03T14:00:00Z',
      updated_date: '2026-04-03T14:00:00Z',
      status: 'draft',
      overview_ids: ['ov-abc12345'],
      trading_objective: 'Capture mispricing in correlated Kalshi event markets',
      market_selection: {
        platforms: ['kalshi'],
        criteria: 'High-volume event markets with correlated outcomes',
        target_markets: ['FOMC-2026-Q2', 'FOMC-2026-Q3'],
      },
      edge_hypothesis: 'Correlated resolution events are mispriced relative to implied joint probability',
      signal_inputs: [{ name: 'market_price', source: 'kalshi_api', type: 'real_time' }],
      entry_rules: [{ condition: 'spread > threshold', action: 'enter_long' }],
      exit_rules: [{ condition: 'spread normalises', action: 'exit' }],
      risk_controls: { max_position_size: 0.02, max_daily_loss: 0.05, stop_loss_pct: 0.1 },
      position_sizing: { method: 'fixed_fraction', base_fraction: 0.02 },
      implementation_notes: 'Requires Kalshi API v2 with WebSocket feed',
    }

    let state = initDebate({
      inputType: 'design-spec',
      inputId: 'spec-integration-test',
      inputVersion: '1.0.0',
      artifactContent: designSpec,
    })

    assert.ok(state.transcriptId.startsWith('dt-'))
    assert.equal(state.round1Arguments.length, 0)

    const prompts = generateAgentPrompts(state)
    assert.equal(prompts.length, 4)
    assert.ok(prompts.every(p => p.prompt.includes('spec-integration-test')))

    state = submitArgument(state, {
      role: 'advocate',
      position: 'The edge hypothesis is grounded in established correlation mispricing literature.',
      evidence: ['item-001', 'item-002'],
      counterpoints: [],
      proposed_changes: [],
      confidence: 0.85,
    })
    state = submitArgument(state, {
      role: 'critic',
      position: 'The entry rule threshold is undefined — this is not a deployable spec.',
      evidence: [],
      counterpoints: ['No specific spread threshold value given'],
      proposed_changes: ['Define threshold as a number or formula'],
      confidence: 0.9,
    })
    state = submitArgument(state, {
      role: 'risk_specialist',
      position: 'Max daily loss 5% is appropriate but stop-loss 10% is inconsistent — stop should be tighter.',
      evidence: [],
      counterpoints: ['stop_loss_pct 0.1 implies losses larger than max_daily_loss in some scenarios'],
      proposed_changes: ['Set stop_loss_pct to 0.03 or less to stay within max_daily_loss constraint'],
      confidence: 0.8,
    })
    state = submitArgument(state, {
      role: 'domain_specialist',
      position: 'Kalshi API v2 WebSocket is not publicly documented — verify feed availability before implementation.',
      evidence: [],
      counterpoints: [],
      proposed_changes: ['Validate WebSocket endpoint availability and rate limits before committing to implementation'],
      confidence: 0.75,
    })

    assert.equal(state.round1Arguments.length, 4)

    const synthPrompt = generateSynthesisPrompt(state)
    assert.ok(synthPrompt.includes('edge hypothesis is grounded'))
    assert.ok(synthPrompt.includes('threshold is undefined'))
    assert.ok(synthPrompt.includes('stop-loss'))
    assert.ok(synthPrompt.includes('WebSocket'))

    state.synthesis = {
      changes_accepted: [
        'Define threshold as a specific spread value — accepted: core spec gap',
        'Validate WebSocket API availability before implementation — accepted: de-risks build phase',
        'Set stop_loss_pct to 0.03 — accepted: resolves logical inconsistency with max_daily_loss',
      ],
      changes_rejected: [
        {
          proposed: 'Reject the edge hypothesis entirely',
          reason: 'Advocate provided strong empirical backing; insufficient evidence to reject',
        },
      ],
      open_questions: [
        'What is the exact Kalshi API WebSocket endpoint and rate limit for live trading?',
        'What spread threshold is appropriate given Kalshi fee structure?',
      ],
    }
    state.outputVersion = '1.1.0'

    const transcript = buildTranscript(state)

    assert.equal(transcript.rounds.length, 2)
    assert.equal(transcript.rounds[0].type, 'divergent')
    assert.equal(transcript.rounds[0].agents.length, 4)
    assert.equal(transcript.rounds[1].type, 'convergent')
    assert.equal(transcript.rounds[1].agents[0].role, 'synthesizer')
    assert.equal(transcript.synthesis.changes_accepted.length, 3)
    assert.equal(transcript.synthesis.changes_rejected.length, 1)
    assert.equal(transcript.synthesis.open_questions?.length, 2)

    const result = validateArtifact(schemas, 'debate-transcript.json', transcript)
    assert.equal(result.valid, true, `Schema validation failed: ${result.errors.join(', ')}`)

    const sc: StorageConfig = {
      base_dir: tempDir,
      paths: { debates: 'debates' },
    }
    const fileName = `${transcript.transcript_id}.json`
    const storedPath = storeArtifact(sc, 'debates', fileName, transcript)
    assert.ok(storedPath.endsWith(fileName))

    const loaded = loadArtifact(sc, 'debates', fileName) as typeof transcript
    assert.equal(loaded.transcript_id, transcript.transcript_id)
    assert.equal(loaded.input_id, 'spec-integration-test')
    assert.equal(loaded.input_version, '1.0.0')
    assert.equal(loaded.output_version, '1.1.0')
    assert.equal(loaded.rounds[0].agents.length, 4)
    assert.equal(loaded.synthesis.changes_accepted.length, 3)
  })
})

describe('integration: debate flow on a knowledge-overview', () => {
  it('init → prompts → submit → synthesize → validate', () => {
    const schemas = loadSchemas(join(PIPELINE_DIR, 'schemas'))

    const overview = {
      overview_id: 'ov-integration-test',
      strategy_name: 'Kalshi Rate Markets',
      version: '1.0.0',
      created_date: '2026-04-03T14:00:00Z',
      updated_date: '2026-04-03T14:00:00Z',
      status: 'draft',
      collection_ids: ['col-abc12345'],
      key_concepts: [{ concept: 'rate prediction', confidence: 0.8, supporting_items: ['item-001'] }],
      market_landscape: 'Kalshi offers FOMC rate decision markets with binary resolution',
      edge_opportunities: ['Anchoring bias in rate predictions', 'Post-meeting drift'],
      data_requirements: ['FOMC statements', 'Fed funds futures'],
      risks_and_constraints: ['Low liquidity post-resolution'],
      research_gaps: ['Historical resolution data limited'],
    }

    let state = initDebate({
      inputType: 'knowledge-overview',
      inputId: 'ov-integration-test',
      inputVersion: '1.0.0',
      artifactContent: overview,
    })

    state = submitArgument(state, { role: 'advocate', position: 'Anchoring bias is well-documented.', confidence: 0.8 })
    state = submitArgument(state, { role: 'critic', position: 'Edge opportunities lack quantification.', proposed_changes: ['Add expected edge size estimates'], confidence: 0.7 })
    state = submitArgument(state, { role: 'risk_specialist', position: 'Liquidity risk is correctly identified.', confidence: 0.75 })
    state = submitArgument(state, { role: 'domain_specialist', position: 'FOMC binary markets have strict 24h resolution windows — factor into hold timing.', confidence: 0.85 })

    state.synthesis = {
      changes_accepted: ['Add expected edge size estimates — accepted: improves specificity'],
      changes_rejected: [],
      open_questions: ['Is 24h resolution window enforced strictly by Kalshi or is there grace period?'],
    }
    state.outputVersion = '1.1.0'

    const transcript = buildTranscript(state)
    const result = validateArtifact(schemas, 'debate-transcript.json', transcript)
    assert.equal(result.valid, true, `Schema validation failed: ${result.errors.join(', ')}`)
    assert.equal(transcript.input_type, 'knowledge-overview')
    assert.equal(transcript.rounds[0].agents.length, 4)
  })
})
