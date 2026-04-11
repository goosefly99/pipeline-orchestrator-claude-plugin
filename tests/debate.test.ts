// pipeline-mcp/tests/debate.test.ts

import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import {
  initDebate,
  submitArgument,
  buildTranscript,
  generateAgentPrompts,
  generateSynthesisPrompt,
  resolveProfile,
  FINANCE_PROFILE,
  SOFTWARE_PROFILE,
  RESEARCH_PROFILE,
  DEFAULT_PROFILE,
  type DebateState,
  type AgentArgument,
  type DomainProfile,
} from '../debate.ts'
import { handleInitDebate } from '../debate-handlers.ts'
import type { DebateContext } from '../debate-handlers.ts'

// ── initDebate ────────────────────────────────────────────────

describe('initDebate', () => {
  it('initialises state for a knowledge-overview input', () => {
    const state = initDebate({
      inputType: 'knowledge-overview',
      inputId: 'ov-abc12345',
      inputVersion: '1.0.0',
      artifactContent: { overview_id: 'ov-abc12345', title: 'Kalshi Edge Strategies' },
    })

    assert.equal(state.inputType, 'knowledge-overview')
    assert.equal(state.inputId, 'ov-abc12345')
    assert.equal(state.inputVersion, '1.0.0')
    assert.ok(state.transcriptId.startsWith('dt-'))
    assert.equal(state.transcriptId.length, 11) // dt- + 8 hex chars
    assert.deepEqual(state.round1Arguments, [])
    assert.equal(state.synthesis, null)
    assert.ok(state.createdDate)
  })

  it('initialises state for a design-spec input', () => {
    const state = initDebate({
      inputType: 'design-spec',
      inputId: 'spec-def67890',
      inputVersion: '2.1.0',
      artifactContent: { spec_id: 'spec-def67890', title: 'Kalshi Market Maker' },
    })

    assert.equal(state.inputType, 'design-spec')
    assert.equal(state.inputId, 'spec-def67890')
  })

  it('throws on invalid input type', () => {
    assert.throws(
      () => initDebate({
        inputType: 'raw-collection' as 'knowledge-overview',
        inputId: 'x',
        inputVersion: '1.0.0',
        artifactContent: {},
      }),
      /invalid input_type/i,
    )
  })

  it('assigns a unique transcript ID each call', () => {
    const s1 = initDebate({ inputType: 'knowledge-overview', inputId: 'a', inputVersion: '1.0.0', artifactContent: {} })
    const s2 = initDebate({ inputType: 'knowledge-overview', inputId: 'a', inputVersion: '1.0.0', artifactContent: {} })
    assert.notEqual(s1.transcriptId, s2.transcriptId)
  })
})

// ── submitArgument ────────────────────────────────────────────

describe('submitArgument', () => {
  function makeState(): DebateState {
    return initDebate({
      inputType: 'design-spec',
      inputId: 'spec-test',
      inputVersion: '1.0.0',
      artifactContent: { spec_id: 'spec-test', title: 'Test' },
    })
  }

  it('records an advocate argument', () => {
    const state = submitArgument(makeState(), {
      role: 'advocate',
      position: 'The strategy has strong empirical backing from 6 months of data.',
      evidence: ['item-001', 'item-002'],
      counterpoints: [],
      proposed_changes: [],
      confidence: 0.9,
    })

    assert.equal(state.round1Arguments.length, 1)
    assert.equal(state.round1Arguments[0].role, 'advocate')
    assert.equal(state.round1Arguments[0].confidence, 0.9)
  })

  it('records multiple arguments from different agents', () => {
    let state = makeState()
    state = submitArgument(state, { role: 'advocate', position: 'Strong fundamentals.', confidence: 0.85 })
    state = submitArgument(state, { role: 'critic', position: 'Overfitting risk.', confidence: 0.7 })
    state = submitArgument(state, { role: 'risk_specialist', position: 'Drawdown inadequately modelled.', confidence: 0.8 })
    state = submitArgument(state, { role: 'domain_specialist', position: 'Missing regulatory constraints.', confidence: 0.75 })

    assert.equal(state.round1Arguments.length, 4)
  })

  it('throws on unknown role', () => {
    assert.throws(
      () => submitArgument(makeState(), { role: 'unknown_agent' as AgentArgument['role'], position: 'x', confidence: 0.5 }),
      /invalid role/i,
    )
  })

  it('throws if synthesis role submitted to round 1', () => {
    assert.throws(
      () => submitArgument(makeState(), { role: 'synthesizer', position: 'x', confidence: 0.5 }),
      /synthesizer.*pipeline_synthesize_debate/i,
    )
  })

  it('throws if confidence is out of range', () => {
    assert.throws(
      () => submitArgument(makeState(), { role: 'critic', position: 'x', confidence: 1.5 }),
      /confidence must be between 0 and 1/i,
    )
  })
})

// ── buildTranscript ───────────────────────────────────────────

describe('buildTranscript', () => {
  function makeFullState(): DebateState {
    let state = initDebate({
      inputType: 'design-spec',
      inputId: 'spec-full',
      inputVersion: '1.0.0',
      artifactContent: { spec_id: 'spec-full', title: 'Full' },
    })
    state = submitArgument(state, {
      role: 'advocate',
      position: 'Solid approach.',
      evidence: ['src-001'],
      counterpoints: [],
      proposed_changes: [],
      confidence: 0.9,
    })
    state = submitArgument(state, {
      role: 'critic',
      position: 'Execution risk is understated.',
      evidence: [],
      counterpoints: ['Slippage not modelled'],
      proposed_changes: ['Add slippage model'],
      confidence: 0.7,
    })
    state.synthesis = {
      changes_accepted: ['Add slippage model'],
      changes_rejected: [],
      open_questions: ['What is realistic slippage for Kalshi limits?'],
    }
    state.outputVersion = '1.1.0'
    return state
  }

  it('builds a structurally complete transcript', () => {
    const transcript = buildTranscript(makeFullState())

    assert.ok(transcript.transcript_id.startsWith('dt-'))
    assert.equal(transcript.input_type, 'design-spec')
    assert.equal(transcript.input_id, 'spec-full')
    assert.equal(transcript.input_version, '1.0.0')
    assert.equal(transcript.output_version, '1.1.0')
    assert.equal(transcript.rounds.length, 2) // round 1 divergent + round 2 convergent
    assert.equal(transcript.rounds[0].type, 'divergent')
    assert.equal(transcript.rounds[1].type, 'convergent')
    assert.equal(transcript.rounds[0].agents.length, 2) // advocate + critic
    assert.equal(transcript.rounds[1].agents.length, 1) // synthesizer
    assert.ok(transcript.synthesis.changes_accepted.includes('Add slippage model'))
  })

  it('throws if synthesis is not yet recorded', () => {
    const state = initDebate({
      inputType: 'design-spec',
      inputId: 'spec-incomplete',
      inputVersion: '1.0.0',
      artifactContent: {},
    })
    assert.throws(() => buildTranscript(state), /synthesis not yet recorded/i)
  })
})

// ── generateAgentPrompts ──────────────────────────────────────

describe('generateAgentPrompts', () => {
  it('returns prompts for all 4 round-1 agents', () => {
    const state = initDebate({
      inputType: 'knowledge-overview',
      inputId: 'ov-test',
      inputVersion: '1.0.0',
      artifactContent: { overview_id: 'ov-test', title: 'Test Overview' },
    })
    const prompts = generateAgentPrompts(state)

    assert.equal(prompts.length, 4)
    const roles = prompts.map(p => p.role)
    assert.ok(roles.includes('advocate'))
    assert.ok(roles.includes('critic'))
    assert.ok(roles.includes('risk_specialist'))
    assert.ok(roles.includes('domain_specialist'))
  })

  it('each prompt contains the artifact ID', () => {
    const state = initDebate({
      inputType: 'design-spec',
      inputId: 'spec-prompt-test',
      inputVersion: '1.0.0',
      artifactContent: { spec_id: 'spec-prompt-test', strategy_name: 'Event Arb' },
    })
    const prompts = generateAgentPrompts(state)
    for (const p of prompts) {
      assert.ok(p.prompt.includes('spec-prompt-test'), `${p.role} prompt missing artifact ID`)
    }
  })

  it('prompts do NOT contain the full artifact JSON (artifact-once pattern)', () => {
    const artifactContent = { spec_id: 'spec-no-embed', strategy_name: 'Event Arb', unique_marker: 'SHOULD_NOT_APPEAR_IN_PROMPT' }
    const state = initDebate({
      inputType: 'design-spec',
      inputId: 'spec-no-embed',
      inputVersion: '1.0.0',
      artifactContent,
    })
    const prompts = generateAgentPrompts(state)
    const artifactJson = JSON.stringify(artifactContent, null, 2)
    for (const p of prompts) {
      assert.ok(!p.prompt.includes('unique_marker'), `${p.role} prompt must not embed artifact JSON fields`)
      assert.ok(!p.prompt.includes('SHOULD_NOT_APPEAR_IN_PROMPT'), `${p.role} prompt must not embed artifact JSON values`)
      assert.ok(!p.prompt.includes(artifactJson), `${p.role} prompt must not embed the full artifact JSON`)
    }
  })

  it('critic prompt emphasises weaknesses and alternatives', () => {
    const state = initDebate({
      inputType: 'design-spec',
      inputId: 'spec-critic',
      inputVersion: '1.0.0',
      artifactContent: {},
    })
    const prompts = generateAgentPrompts(state)
    const criticPrompt = prompts.find(p => p.role === 'critic')!
    assert.ok(criticPrompt.prompt.toLowerCase().includes('weakn') || criticPrompt.prompt.toLowerCase().includes('challeng'))
  })

  it('risk_specialist prompt interpolates risk_vocabulary from profile', () => {
    const state = initDebate({
      inputType: 'design-spec',
      inputId: 'spec-finance',
      inputVersion: '1.0.0',
      artifactContent: {},
      domainProfile: 'finance',
    })
    const prompts = generateAgentPrompts(state)
    const riskPrompt = prompts.find(p => p.role === 'risk_specialist')!
    // FINANCE_PROFILE has 'liquidity risk' and 'execution risk' in its vocabulary
    assert.ok(riskPrompt.prompt.includes('liquidity risk'), 'risk prompt should include liquidity risk')
    assert.ok(riskPrompt.prompt.includes('execution risk'), 'risk prompt should include execution risk')
  })

  it('domain_specialist prompt interpolates specialist_focus from profile', () => {
    const state = initDebate({
      inputType: 'design-spec',
      inputId: 'spec-finance',
      inputVersion: '1.0.0',
      artifactContent: {},
      domainProfile: 'finance',
    })
    const prompts = generateAgentPrompts(state)
    const domainPrompt = prompts.find(p => p.role === 'domain_specialist')!
    // FINANCE_PROFILE specialist_focus mentions market microstructure
    assert.ok(domainPrompt.prompt.includes('market microstructure'), 'domain prompt should include specialist_focus content')
  })

  it('FINANCE_PROFILE preserves original finance-specific terms in prompts', () => {
    const state = initDebate({
      inputType: 'design-spec',
      inputId: 'spec-finance-eq',
      inputVersion: '1.0.0',
      artifactContent: {},
      domainProfile: 'finance',
    })
    const prompts = generateAgentPrompts(state)
    const riskPrompt = prompts.find(p => p.role === 'risk_specialist')!
    const domainPrompt = prompts.find(p => p.role === 'domain_specialist')!
    // Verify the terms from the pre-refactor finance-hardcoded prompts survive
    assert.ok(riskPrompt.prompt.includes('liquidity risk'))
    assert.ok(riskPrompt.prompt.includes('operational risk'))
    assert.ok(riskPrompt.prompt.includes('tail risk'))
    assert.ok(domainPrompt.prompt.includes('market microstructure'))
    assert.ok(domainPrompt.prompt.includes('settlement timing'))
    assert.ok(domainPrompt.prompt.includes('regulatory requirements'))
  })

  it('SOFTWARE_PROFILE produces software-specific prompts', () => {
    const state = initDebate({
      inputType: 'design-spec',
      inputId: 'spec-software',
      inputVersion: '1.0.0',
      artifactContent: {},
      domainProfile: 'software',
    })
    const prompts = generateAgentPrompts(state)
    const riskPrompt = prompts.find(p => p.role === 'risk_specialist')!
    const domainPrompt = prompts.find(p => p.role === 'domain_specialist')!
    assert.ok(riskPrompt.prompt.includes('technical debt'))
    assert.ok(riskPrompt.prompt.includes('API stability'))
    assert.ok(domainPrompt.prompt.includes('software architecture'))
    // Must NOT contain finance terms
    assert.ok(!riskPrompt.prompt.includes('liquidity risk'))
    assert.ok(!domainPrompt.prompt.includes('market microstructure'))
  })

  it('RESEARCH_PROFILE produces research-specific prompts', () => {
    const state = initDebate({
      inputType: 'design-spec',
      inputId: 'spec-research',
      inputVersion: '1.0.0',
      artifactContent: {},
      domainProfile: 'research',
    })
    const prompts = generateAgentPrompts(state)
    const riskPrompt = prompts.find(p => p.role === 'risk_specialist')!
    const domainPrompt = prompts.find(p => p.role === 'domain_specialist')!
    assert.ok(riskPrompt.prompt.includes('reproducibility'))
    assert.ok(riskPrompt.prompt.includes('statistical validity'))
    assert.ok(domainPrompt.prompt.includes('research methodology'))
  })

  it('DEFAULT_PROFILE (no domainProfile passed) uses generic vocabulary', () => {
    const state = initDebate({
      inputType: 'design-spec',
      inputId: 'spec-generic',
      inputVersion: '1.0.0',
      artifactContent: {},
    })
    const prompts = generateAgentPrompts(state)
    const riskPrompt = prompts.find(p => p.role === 'risk_specialist')!
    const domainPrompt = prompts.find(p => p.role === 'domain_specialist')!
    // DEFAULT_PROFILE uses generic terms, not finance-specific ones
    assert.ok(riskPrompt.prompt.includes('feasibility risk'))
    assert.ok(!riskPrompt.prompt.includes('liquidity risk'), 'default should not include finance terms')
    assert.ok(!riskPrompt.prompt.includes('market microstructure'), 'default should not include finance terms')
    assert.ok(domainPrompt.prompt.includes('feasibility analysis') || domainPrompt.prompt.includes('trade-off'))
  })

  it('custom DomainProfile object interpolates correctly', () => {
    const custom: DomainProfile = {
      name: 'aerospace',
      risk_vocabulary: ['mission-critical failure', 'thermal risk'],
      domain_constraints: ['FAA regulations'],
      specialist_focus: 'Apply aerospace engineering expertise.',
    }
    const state = initDebate({
      inputType: 'design-spec',
      inputId: 'spec-aero',
      inputVersion: '1.0.0',
      artifactContent: {},
      domainProfile: custom,
    })
    const prompts = generateAgentPrompts(state)
    const riskPrompt = prompts.find(p => p.role === 'risk_specialist')!
    const domainPrompt = prompts.find(p => p.role === 'domain_specialist')!
    assert.ok(riskPrompt.prompt.includes('mission-critical failure'))
    assert.ok(riskPrompt.prompt.includes('thermal risk'))
    assert.ok(domainPrompt.prompt.includes('aerospace engineering'))
  })
})

// ── generateSynthesisPrompt ───────────────────────────────────

describe('generateSynthesisPrompt', () => {
  it('includes all round-1 arguments in the synthesis prompt', () => {
    let state = initDebate({
      inputType: 'design-spec',
      inputId: 'spec-synth',
      inputVersion: '1.0.0',
      artifactContent: { title: 'Synth Test' },
    })
    state = submitArgument(state, { role: 'advocate', position: 'Strong data support.', confidence: 0.85 })
    state = submitArgument(state, { role: 'critic', position: 'Parameter instability.', confidence: 0.7 })

    const prompt = generateSynthesisPrompt(state)

    assert.ok(prompt.includes('Strong data support.'))
    assert.ok(prompt.includes('Parameter instability.'))
    assert.ok(prompt.toLowerCase().includes('synthes'))
  })

  it('throws if no round-1 arguments have been submitted', () => {
    const state = initDebate({
      inputType: 'design-spec',
      inputId: 'spec-empty',
      inputVersion: '1.0.0',
      artifactContent: {},
    })
    assert.throws(() => generateSynthesisPrompt(state), /no round.1 arguments/i)
  })

  it('includes domain name and constraints for non-default profiles', () => {
    let state = initDebate({
      inputType: 'design-spec',
      inputId: 'spec-domain-synth',
      inputVersion: '1.0.0',
      artifactContent: {},
      domainProfile: 'finance',
    })
    state = submitArgument(state, { role: 'advocate', position: 'x', confidence: 0.5 })
    const prompt = generateSynthesisPrompt(state)
    assert.ok(prompt.includes('Domain: finance'))
    assert.ok(prompt.includes('market microstructure'))
  })

  it('omits domain note for DEFAULT_PROFILE', () => {
    let state = initDebate({
      inputType: 'design-spec',
      inputId: 'spec-default-synth',
      inputVersion: '1.0.0',
      artifactContent: {},
    })
    state = submitArgument(state, { role: 'advocate', position: 'x', confidence: 0.5 })
    const prompt = generateSynthesisPrompt(state)
    assert.ok(!prompt.includes('Domain: default'))
    assert.ok(!prompt.includes('Key constraints'))
  })

  it('does NOT embed the full artifact JSON in the synthesis prompt (5.2 path-reference pattern)', () => {
    // Create a large artifact with a deeply nested structure and a big array —
    // the old behaviour was to JSON.stringify the full artifact into the prompt.
    // The new behaviour (5.2) uses buildArtifactStructureSummary which shows
    // only key names and shape metadata (counts for arrays/objects), NOT full content.
    const bigArray = Array.from({ length: 20 }, (_, i) => ({
      id: `component-${i}`,
      weight: i * 0.05,
      config: { mode: 'active', priority: i },
    }))
    const artifactContent = {
      spec_id: 'spec-synth-no-embed',
      strategy_name: 'Market Maker Strategy with Chunked Components',
      components: bigArray,
      execution_rules: { max_position: 1000, slippage_model: 'linear', timeout_ms: 5000 },
    }
    let state = initDebate({
      inputType: 'design-spec',
      inputId: 'spec-synth-no-embed',
      inputVersion: '1.0.0',
      artifactContent,
      artifactPath: '/tmp/artifacts/spec-synth-no-embed.json',
    })
    state = submitArgument(state, { role: 'advocate', position: 'Well tested.', confidence: 0.8 })
    state = submitArgument(state, { role: 'critic', position: 'Lacks edge cases.', confidence: 0.65 })

    const prompt = generateSynthesisPrompt(state)
    const fullJson = JSON.stringify(artifactContent, null, 2)

    // Must NOT embed the full JSON serialisation (old pattern — embedding everything)
    assert.ok(!prompt.includes(fullJson), 'synthesis prompt must not embed full artifact JSON')
    // The array of 20 items should appear as a count ("20 items"), not as serialised JSON
    assert.ok(!prompt.includes('"component-0"'), 'synthesis prompt must not embed array element IDs from the artifact')
    assert.ok(!prompt.includes('"component-19"'), 'synthesis prompt must not embed last array element from the artifact')
    assert.ok(prompt.includes('20 items'), 'synthesis prompt summary should show component count as "20 items"')
    // Must include the artifact path reference instead of the full content
    assert.ok(prompt.includes('/tmp/artifacts/spec-synth-no-embed.json'), 'synthesis prompt should reference artifact path')
    // Must include artifact ID for traceability
    assert.ok(prompt.includes('spec-synth-no-embed'), 'synthesis prompt should include artifact ID')
  })
})

// ── resolveProfile ────────────────────────────────────────────

describe('resolveProfile', () => {
  it('returns DEFAULT_PROFILE when no argument provided', () => {
    const profile = resolveProfile()
    assert.equal(profile.name, 'default')
    assert.equal(profile, DEFAULT_PROFILE)
  })

  it('returns DEFAULT_PROFILE when argument is undefined', () => {
    const profile = resolveProfile(undefined)
    assert.equal(profile.name, 'default')
  })

  it('resolves "finance" string to FINANCE_PROFILE', () => {
    const profile = resolveProfile('finance')
    assert.equal(profile.name, 'finance')
    assert.equal(profile, FINANCE_PROFILE)
  })

  it('resolves "software" string to SOFTWARE_PROFILE', () => {
    const profile = resolveProfile('software')
    assert.equal(profile.name, 'software')
    assert.equal(profile, SOFTWARE_PROFILE)
  })

  it('resolves "research" string to RESEARCH_PROFILE', () => {
    const profile = resolveProfile('research')
    assert.equal(profile.name, 'research')
    assert.equal(profile, RESEARCH_PROFILE)
  })

  it('resolves "default" string to DEFAULT_PROFILE', () => {
    const profile = resolveProfile('default')
    assert.equal(profile.name, 'default')
    assert.equal(profile, DEFAULT_PROFILE)
  })

  it('falls back to DEFAULT_PROFILE for unknown name', () => {
    const profile = resolveProfile('made-up-domain')
    assert.equal(profile.name, 'default')
    assert.equal(profile, DEFAULT_PROFILE)
  })

  it('passes DomainProfile object through unchanged', () => {
    const custom: DomainProfile = {
      name: 'healthcare',
      risk_vocabulary: ['clinical risk', 'regulatory compliance'],
      domain_constraints: ['HIPAA', 'FDA approval'],
      specialist_focus: 'Apply healthcare domain expertise.',
    }
    const profile = resolveProfile(custom)
    assert.equal(profile, custom)
    assert.equal(profile.name, 'healthcare')
  })
})

// ── Built-in profiles ─────────────────────────────────────────

describe('built-in domain profiles', () => {
  it('FINANCE_PROFILE contains expected finance vocabulary', () => {
    assert.equal(FINANCE_PROFILE.name, 'finance')
    assert.ok(FINANCE_PROFILE.risk_vocabulary.includes('liquidity risk'))
    assert.ok(FINANCE_PROFILE.risk_vocabulary.includes('execution risk'))
    assert.ok(FINANCE_PROFILE.domain_constraints.some(c => c.includes('market microstructure')))
  })

  it('SOFTWARE_PROFILE contains expected software vocabulary', () => {
    assert.equal(SOFTWARE_PROFILE.name, 'software')
    assert.ok(SOFTWARE_PROFILE.risk_vocabulary.includes('technical debt'))
    assert.ok(SOFTWARE_PROFILE.risk_vocabulary.includes('API stability'))
  })

  it('RESEARCH_PROFILE contains expected research vocabulary', () => {
    assert.equal(RESEARCH_PROFILE.name, 'research')
    assert.ok(RESEARCH_PROFILE.risk_vocabulary.includes('reproducibility'))
    assert.ok(RESEARCH_PROFILE.risk_vocabulary.includes('statistical validity'))
  })

  it('DEFAULT_PROFILE is generic with no domain-specific terms', () => {
    assert.equal(DEFAULT_PROFILE.name, 'default')
    assert.ok(DEFAULT_PROFILE.risk_vocabulary.length > 0)
    assert.ok(DEFAULT_PROFILE.specialist_focus.length > 0)
  })

  it('all built-in profiles have the required DomainProfile shape', () => {
    for (const profile of [FINANCE_PROFILE, SOFTWARE_PROFILE, RESEARCH_PROFILE, DEFAULT_PROFILE]) {
      assert.ok(typeof profile.name === 'string' && profile.name.length > 0)
      assert.ok(Array.isArray(profile.risk_vocabulary) && profile.risk_vocabulary.length > 0)
      assert.ok(Array.isArray(profile.domain_constraints) && profile.domain_constraints.length > 0)
      assert.ok(typeof profile.specialist_focus === 'string' && profile.specialist_focus.length > 0)
    }
  })
})

// ── initDebate with domainProfile ─────────────────────────────

describe('initDebate with domainProfile', () => {
  it('defaults to DEFAULT_PROFILE when no domainProfile passed', () => {
    const state = initDebate({
      inputType: 'design-spec',
      inputId: 'spec-def',
      inputVersion: '1.0.0',
      artifactContent: {},
    })
    assert.equal(state.domainProfile.name, 'default')
  })

  it('uses named profile when string passed', () => {
    const state = initDebate({
      inputType: 'design-spec',
      inputId: 'spec-fin',
      inputVersion: '1.0.0',
      artifactContent: {},
      domainProfile: 'finance',
    })
    assert.equal(state.domainProfile.name, 'finance')
    assert.equal(state.domainProfile, FINANCE_PROFILE)
  })

  it('uses custom profile when object passed', () => {
    const custom: DomainProfile = {
      name: 'biotech',
      risk_vocabulary: ['regulatory risk', 'clinical risk'],
      domain_constraints: ['FDA', 'IRB'],
      specialist_focus: 'biotech expertise',
    }
    const state = initDebate({
      inputType: 'design-spec',
      inputId: 'spec-bio',
      inputVersion: '1.0.0',
      artifactContent: {},
      domainProfile: custom,
    })
    assert.equal(state.domainProfile.name, 'biotech')
    assert.equal(state.domainProfile, custom)
  })
})

// ── handleInitDebate (artifact-once pattern) ──────────────────

describe('handleInitDebate', () => {
  function makeCtx(): DebateContext & { stored: DebateState | null } {
    return {
      stored: null,
      getActiveDebate() { return this.stored },
      setActiveDebate(s) { this.stored = s },
    }
  }

  it('response includes artifact_path when provided', () => {
    const ctx = makeCtx()
    const result = handleInitDebate({
      input_type: 'design-spec',
      input_id: 'spec-handler-test',
      input_version: '1.0.0',
      artifact_content: { spec_id: 'spec-handler-test' },
      artifact_path: '/tmp/artifacts/spec-handler-test.json',
    }, ctx)

    const response = JSON.parse(result.json)
    assert.equal(response.artifact_path, '/tmp/artifacts/spec-handler-test.json')
    assert.ok(Array.isArray(response.agents), 'response should have agents array')
    assert.equal(response.agents.length, 4)
  })

  it('response omits artifact_path when not provided', () => {
    const ctx = makeCtx()
    const result = handleInitDebate({
      input_type: 'design-spec',
      input_id: 'spec-no-path',
      input_version: '1.0.0',
      artifact_content: { spec_id: 'spec-no-path' },
    }, ctx)

    const response = JSON.parse(result.json)
    assert.ok(!Object.prototype.hasOwnProperty.call(response, 'artifact_path'), 'artifact_path should not be present when not passed')
  })

  it('agent prompts in response do not contain the artifact JSON', () => {
    const ctx = makeCtx()
    const artifactContent = { spec_id: 'spec-no-embed-handler', unique_value: 'EMBED_MARKER' }
    const result = handleInitDebate({
      input_type: 'design-spec',
      input_id: 'spec-no-embed-handler',
      input_version: '1.0.0',
      artifact_content: artifactContent,
      artifact_path: '/tmp/spec-no-embed-handler.json',
    }, ctx)

    const response = JSON.parse(result.json)
    for (const agent of response.agents as Array<{ role: string; prompt: string }>) {
      assert.ok(!agent.prompt.includes('EMBED_MARKER'), `${agent.role} prompt must not embed artifact JSON values`)
      assert.ok(!agent.prompt.includes('unique_value'), `${agent.role} prompt must not embed artifact JSON fields`)
    }
  })

  it('response includes transcript_id, input_type, input_id, and agents', () => {
    const ctx = makeCtx()
    const result = handleInitDebate({
      input_type: 'knowledge-overview',
      input_id: 'ov-handler-check',
      input_version: '1.0.0',
      artifact_content: { overview_id: 'ov-handler-check' },
    }, ctx)

    const response = JSON.parse(result.json)
    assert.ok(typeof response.transcript_id === 'string' && response.transcript_id.startsWith('dt-'))
    assert.equal(response.input_type, 'knowledge-overview')
    assert.equal(response.input_id, 'ov-handler-check')
    assert.ok(Array.isArray(response.agents))
    assert.equal(response.agents.length, 4)
  })
})
