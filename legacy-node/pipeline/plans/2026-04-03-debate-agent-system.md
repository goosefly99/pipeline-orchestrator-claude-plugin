# Debate Agent System — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Phase D debate infrastructure to the pipeline-mcp server. The module manages debate state, generates role-specific prompts, records a structured transcript, and validates the result — but does NOT dispatch agents itself. Claude orchestrates agent dispatching via the Agent tool and feeds results back through the provided tools.

**Architecture:** A new `debate.ts` module handles all state for a single active debate: initializing from an artifact, accumulating arguments, and assembling the final transcript JSON. Four new MCP tools expose these operations (`pipeline_init_debate`, `pipeline_submit_argument`, `pipeline_synthesize_debate`, `pipeline_save_debate`). The debate transcript is validated against `debate-transcript.json` before saving, using the existing `validateArtifact` infrastructure.

**Tech Stack:** Node.js v24 (native TypeScript), `@modelcontextprotocol/sdk`, `node:test` (testing), `node:crypto` (transcript ID generation), existing `validator.ts` and `storage.ts` modules

---

## File Structure

```
pipeline-mcp/
  debate.ts               — Debate state management and prompt generation
  tests/debate.test.ts    — Unit tests for debate module
  server.ts               — Modified to add 4 new debate tools
```

---

### Task 1: Debate State Manager

**Files:**
- Create: `pipeline-mcp/tests/debate.test.ts`
- Create: `pipeline-mcp/debate.ts`

- [ ] **Step 1: Write failing tests**

```typescript
// pipeline-mcp/tests/debate.test.ts

import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import {
  initDebate,
  submitArgument,
  buildTranscript,
  generateAgentPrompts,
  generateSynthesisPrompt,
  type DebateState,
  type AgentArgument,
} from '../debate.ts'

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
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd pipeline-mcp && node --test tests/debate.test.ts`
Expected: FAIL — `debate.ts` does not exist

- [ ] **Step 3: Implement debate.ts**

```typescript
// pipeline-mcp/debate.ts

import { randomBytes } from 'node:crypto'

// ── Types ─────────────────────────────────────────────────────

export type DebateInputType = 'knowledge-overview' | 'design-spec'

export type AgentRole = 'advocate' | 'critic' | 'risk_specialist' | 'domain_specialist' | 'synthesizer'

export interface AgentArgument {
  role: AgentRole
  position: string
  evidence?: string[]
  counterpoints?: string[]
  proposed_changes?: string[]
  confidence: number
}

export interface DebateSynthesis {
  changes_accepted: string[]
  changes_rejected: Array<{ proposed: string; reason: string }>
  open_questions?: string[]
}

export interface DebateState {
  transcriptId: string
  inputType: DebateInputType
  inputId: string
  inputVersion: string
  outputVersion: string | null
  createdDate: string
  artifactContent: unknown
  round1Arguments: AgentArgument[]
  synthesis: DebateSynthesis | null
  codebbaseRequirementsId?: string
  kbQueriesUsed: boolean
}

// ── Transcript shape (matches debate-transcript.json schema) ──

export interface DebateTranscript {
  transcript_id: string
  input_type: DebateInputType
  input_id: string
  input_version?: string
  output_version?: string
  created_date: string
  codebase_requirements_id?: string
  kb_queries_used: boolean
  rounds: DebateRound[]
  synthesis: {
    changes_accepted: string[]
    changes_rejected: Array<{ proposed: string; reason: string }>
    open_questions?: string[]
  }
}

interface DebateRound {
  round: number
  type: 'divergent' | 'convergent'
  agents: DebateRoundAgent[]
}

interface DebateRoundAgent {
  role: AgentRole
  position: string
  evidence?: string[]
  counterpoints?: string[]
  proposed_changes?: string[]
  confidence?: number
}

// ── Agent prompt descriptors ──────────────────────────────────

export interface AgentPrompt {
  role: AgentRole
  prompt: string
}

// ── Constants ─────────────────────────────────────────────────

const VALID_INPUT_TYPES: DebateInputType[] = ['knowledge-overview', 'design-spec']

const ROUND_1_ROLES: AgentRole[] = ['advocate', 'critic', 'risk_specialist', 'domain_specialist']

const ALL_ROLES: AgentRole[] = [...ROUND_1_ROLES, 'synthesizer']

// ── initDebate ────────────────────────────────────────────────

export function initDebate(params: {
  inputType: DebateInputType
  inputId: string
  inputVersion: string
  artifactContent: unknown
  codebbaseRequirementsId?: string
}): DebateState {
  if (!VALID_INPUT_TYPES.includes(params.inputType)) {
    throw new Error(
      `Invalid input_type: "${params.inputType}". Must be one of: ${VALID_INPUT_TYPES.join(', ')}`,
    )
  }

  return {
    transcriptId: `dt-${randomBytes(4).toString('hex')}`,
    inputType: params.inputType,
    inputId: params.inputId,
    inputVersion: params.inputVersion,
    outputVersion: null,
    createdDate: new Date().toISOString(),
    artifactContent: params.artifactContent,
    round1Arguments: [],
    synthesis: null,
    codebbaseRequirementsId: params.codebbaseRequirementsId,
    kbQueriesUsed: false,
  }
}

// ── submitArgument ────────────────────────────────────────────

export function submitArgument(state: DebateState, arg: AgentArgument): DebateState {
  if (!ALL_ROLES.includes(arg.role)) {
    throw new Error(
      `Invalid role: "${arg.role}". Must be one of: ${ALL_ROLES.join(', ')}`,
    )
  }

  if (arg.role === 'synthesizer') {
    throw new Error(
      'Synthesizer arguments are recorded via pipeline_synthesize_debate, not pipeline_submit_argument.',
    )
  }

  if (arg.confidence < 0 || arg.confidence > 1) {
    throw new Error('confidence must be between 0 and 1')
  }

  return {
    ...state,
    round1Arguments: [
      ...state.round1Arguments,
      {
        role: arg.role,
        position: arg.position,
        evidence: arg.evidence ?? [],
        counterpoints: arg.counterpoints ?? [],
        proposed_changes: arg.proposed_changes ?? [],
        confidence: arg.confidence,
      },
    ],
  }
}

// ── buildTranscript ───────────────────────────────────────────

export function buildTranscript(state: DebateState): DebateTranscript {
  if (state.synthesis === null) {
    throw new Error('synthesis not yet recorded — call pipeline_synthesize_debate first')
  }

  const round1Agents: DebateRoundAgent[] = state.round1Arguments.map(arg => ({
    role: arg.role,
    position: arg.position,
    evidence: arg.evidence,
    counterpoints: arg.counterpoints,
    proposed_changes: arg.proposed_changes,
    confidence: arg.confidence,
  }))

  const round2Agents: DebateRoundAgent[] = [
    {
      role: 'synthesizer',
      position: [
        'Accepted changes: ' + (state.synthesis.changes_accepted.join('; ') || 'none'),
        'Rejected changes: ' + (state.synthesis.changes_rejected.map(r => r.proposed).join('; ') || 'none'),
        ...(state.synthesis.open_questions?.length
          ? ['Open questions: ' + state.synthesis.open_questions.join('; ')]
          : []),
      ].join('. '),
      evidence: [],
      counterpoints: [],
      proposed_changes: [],
      confidence: 1,
    },
  ]

  const transcript: DebateTranscript = {
    transcript_id: state.transcriptId,
    input_type: state.inputType,
    input_id: state.inputId,
    created_date: state.createdDate,
    kb_queries_used: state.kbQueriesUsed,
    rounds: [
      { round: 1, type: 'divergent', agents: round1Agents },
      { round: 2, type: 'convergent', agents: round2Agents },
    ],
    synthesis: {
      changes_accepted: state.synthesis.changes_accepted,
      changes_rejected: state.synthesis.changes_rejected,
      open_questions: state.synthesis.open_questions,
    },
  }

  if (state.inputVersion) transcript.input_version = state.inputVersion
  if (state.outputVersion) transcript.output_version = state.outputVersion
  if (state.codebbaseRequirementsId) transcript.codebase_requirements_id = state.codebbaseRequirementsId

  return transcript
}

// ── generateAgentPrompts ──────────────────────────────────────

export function generateAgentPrompts(state: DebateState): AgentPrompt[] {
  const artifactJson = JSON.stringify(state.artifactContent, null, 2)
  const inputLabel = state.inputType === 'knowledge-overview' ? 'knowledge overview' : 'design spec'

  const roleInstructions: Record<Exclude<AgentRole, 'synthesizer'>, string> = {
    advocate: `Your role is ADVOCATE. Defend the current artifact's decisions with evidence from the source material and domain knowledge. Identify what is strong, well-reasoned, and supported. Propose only changes that strengthen the existing approach rather than redirecting it.`,

    critic: `Your role is CRITIC. Challenge the artifact's assumptions, identify weaknesses, gaps in reasoning, and under-explored alternatives. Your goal is to find what is missing, overstated, or fragile. Propose concrete alternative approaches or changes where the current direction is flawed.`,

    risk_specialist: `Your role is RISK SPECIALIST. Evaluate risk models, failure modes, worst-case scenarios, and the adequacy of mitigation strategies in this artifact. Assess execution risk, liquidity risk, model risk, operational risk, and tail risk. Identify specific gaps in the artifact's risk treatment and propose concrete mitigations.`,

    domain_specialist: `Your role is DOMAIN SPECIALIST. Apply deep domain knowledge: market microstructure, platform API constraints (rate limits, settlement timing, resolution rules), regulatory requirements, and operational realities. Identify where the artifact makes assumptions that conflict with real-world constraints or misses domain-specific best practices.`,
  }

  return ROUND_1_ROLES.map(role => {
    const instruction = roleInstructions[role as Exclude<AgentRole, 'synthesizer'>]
    const prompt = [
      `You are participating in an adversarial debate reviewing a ${inputLabel}.`,
      `Artifact ID: ${state.inputId} (version ${state.inputVersion})`,
      '',
      instruction,
      '',
      'After your analysis, respond with a JSON object matching this structure:',
      '{',
      '  "position": "your primary argument or assessment (1-3 sentences)",',
      '  "evidence": ["source_id or reference supporting your position"],',
      '  "counterpoints": ["specific counterarguments to the artifact or anticipated opposing views"],',
      '  "proposed_changes": ["concrete, actionable change you recommend to the artifact"],',
      '  "confidence": 0.0-1.0',
      '}',
      '',
      `--- ARTIFACT (${state.inputId}) ---`,
      artifactJson,
    ].join('\n')

    return { role, prompt }
  })
}

// ── generateSynthesisPrompt ───────────────────────────────────

export function generateSynthesisPrompt(state: DebateState): string {
  if (state.round1Arguments.length === 0) {
    throw new Error('No round-1 arguments submitted yet — run all 4 debate agents first')
  }

  const inputLabel = state.inputType === 'knowledge-overview' ? 'knowledge overview' : 'design spec'
  const artifactJson = JSON.stringify(state.artifactContent, null, 2)

  const argumentSections = state.round1Arguments.map(arg => {
    const lines = [
      `### ${arg.role.toUpperCase()} (confidence: ${arg.confidence})`,
      `**Position:** ${arg.position}`,
    ]
    if (arg.evidence?.length) lines.push(`**Evidence:** ${arg.evidence.join(', ')}`)
    if (arg.counterpoints?.length) lines.push(`**Counterpoints:** ${arg.counterpoints.join('; ')}`)
    if (arg.proposed_changes?.length) lines.push(`**Proposed changes:** ${arg.proposed_changes.join('; ')}`)
    return lines.join('\n')
  }).join('\n\n')

  return [
    `You are SYNTHESIZER in an adversarial debate review of a ${inputLabel}.`,
    `Artifact ID: ${state.inputId} (version ${state.inputVersion})`,
    '',
    'Your task: evaluate all round-1 arguments, resolve conflicts, and produce a final synthesis decision.',
    '',
    '## Round 1 Arguments',
    '',
    argumentSections,
    '',
    '## Original Artifact',
    '',
    artifactJson,
    '',
    'Respond with a JSON object matching this structure:',
    '{',
    '  "changes_accepted": ["change description — rationale"],',
    '  "changes_rejected": [{ "proposed": "change text", "reason": "why rejected" }],',
    '  "open_questions": ["unresolved question requiring further research or human decision"]',
    '}',
    '',
    'Be rigorous: accept changes that are well-evidenced and improve the artifact. Reject changes that are speculative, contradicted by evidence, or outside scope. Flag genuinely unresolved issues as open questions.',
  ].join('\n')
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd pipeline-mcp && node --test tests/debate.test.ts`
Expected: All 16 tests PASS

- [ ] **Step 5: Type-check**

Run: `cd pipeline-mcp && npx tsc --noEmit debate.ts`
Expected: No errors

- [ ] **Step 6: Commit**

```bash
cd pipeline-mcp && git add debate.ts tests/debate.test.ts
git commit -m "feat(pipeline-mcp): debate state manager and prompt generator"
```

---

### Task 2: Add 4 Debate Tools to server.ts

**Files:**
- Modify: `pipeline-mcp/server.ts`

- [ ] **Step 1: Add the import**

In `server.ts`, add the following to the imports block (after the existing `run-state.ts` import):

```typescript
import {
  initDebate,
  submitArgument,
  buildTranscript,
  generateAgentPrompts,
  generateSynthesisPrompt,
  type DebateState,
  type DebateSynthesis,
} from './debate.ts'
```

- [ ] **Step 2: Add in-memory debate state variable**

After the `let activeRunDir = ''` line, add:

```typescript
let activeDebate: DebateState | null = null
```

- [ ] **Step 3: Add the 4 tool definitions to the ListToolsRequestSchema handler**

In the `tools: [` array inside the `ListToolsRequestSchema` handler, append these four tool definitions after the existing `pipeline_run_status` entry:

```typescript
    {
      name: 'pipeline_init_debate',
      description: [
        'Start an adversarial debate on an artifact (knowledge-overview or design-spec).',
        'Returns structured prompts for all 4 round-1 agents (advocate, critic, risk_specialist, domain_specialist).',
        'Dispatch each agent in parallel using the Agent tool, then submit each result via pipeline_submit_argument.',
      ].join(' '),
      inputSchema: {
        type: 'object' as const,
        properties: {
          input_type: {
            type: 'string',
            enum: ['knowledge-overview', 'design-spec'],
            description: 'The type of artifact being debated',
          },
          input_id: {
            type: 'string',
            description: 'ID of the artifact (overview_id or spec_id)',
          },
          input_version: {
            type: 'string',
            description: 'Version of the artifact before debate',
          },
          artifact_content: {
            type: 'object',
            description: 'Full artifact JSON to be debated',
          },
          codebase_requirements_id: {
            type: 'string',
            description: 'Optional: ID of codebase-requirements artifact for context',
          },
        },
        required: ['input_type', 'input_id', 'input_version', 'artifact_content'],
      },
    },
    {
      name: 'pipeline_submit_argument',
      description: [
        'Record one round-1 agent argument into the active debate transcript.',
        'Call this once per agent (advocate, critic, risk_specialist, domain_specialist) after each Agent tool dispatch returns.',
        'Do not use this for the synthesizer — use pipeline_synthesize_debate instead.',
      ].join(' '),
      inputSchema: {
        type: 'object' as const,
        properties: {
          role: {
            type: 'string',
            enum: ['advocate', 'critic', 'risk_specialist', 'domain_specialist'],
            description: 'The debating agent role',
          },
          position: {
            type: 'string',
            description: "The agent's primary argument or assessment",
          },
          evidence: {
            type: 'array',
            items: { type: 'string' },
            description: 'Source IDs or references supporting the position',
          },
          counterpoints: {
            type: 'array',
            items: { type: 'string' },
            description: 'Specific counterarguments to other positions or to the artifact',
          },
          proposed_changes: {
            type: 'array',
            items: { type: 'string' },
            description: 'Concrete changes recommended to the artifact',
          },
          confidence: {
            type: 'number',
            minimum: 0,
            maximum: 1,
            description: "Agent's confidence in their position (0-1)",
          },
        },
        required: ['role', 'position', 'confidence'],
      },
    },
    {
      name: 'pipeline_synthesize_debate',
      description: [
        'After all 4 round-1 arguments are submitted, returns a synthesis prompt for the Synthesizer agent.',
        'Dispatch the synthesizer using the Agent tool, parse the JSON response, then record the synthesis result here.',
        'This completes the debate and prepares the transcript for saving.',
      ].join(' '),
      inputSchema: {
        type: 'object' as const,
        properties: {
          changes_accepted: {
            type: 'array',
            items: { type: 'string' },
            description: 'Changes accepted by the synthesizer (with brief rationale)',
          },
          changes_rejected: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                proposed: { type: 'string' },
                reason: { type: 'string' },
              },
              required: ['proposed', 'reason'],
            },
            description: 'Changes rejected by the synthesizer with reasons',
          },
          open_questions: {
            type: 'array',
            items: { type: 'string' },
            description: 'Unresolved questions flagged for further research or human decision',
          },
          output_version: {
            type: 'string',
            description: 'Version of the refined artifact after debate (e.g. "1.1.0")',
          },
          kb_queries_used: {
            type: 'boolean',
            description: 'Whether knowledge base queries were made during this debate',
          },
        },
        required: ['changes_accepted', 'changes_rejected'],
      },
    },
    {
      name: 'pipeline_save_debate',
      description: [
        'Validate and save the completed debate transcript.',
        'Validates against debate-transcript.json schema, stores to the debates storage path,',
        'and registers the artifact in the active run state.',
        'Returns the stored file path and transcript ID.',
      ].join(' '),
      inputSchema: {
        type: 'object' as const,
        properties: {
          file_name: {
            type: 'string',
            description: 'Filename for the transcript (e.g. "dt-abc12345.json")',
          },
          phase: {
            type: 'string',
            description: 'Pipeline phase name to register this artifact under (usually "debate")',
          },
        },
        required: ['file_name', 'phase'],
      },
    },
```

- [ ] **Step 4: Add the 4 case dispatches to the CallToolRequestSchema switch**

In the `switch (req.params.name)` block, add these cases before the `default:` case:

```typescript
      case 'pipeline_init_debate':
        return handleInitDebate(args)
      case 'pipeline_submit_argument':
        return handleSubmitArgument(args)
      case 'pipeline_synthesize_debate':
        return handleSynthesizeDebate(args)
      case 'pipeline_save_debate':
        return handleSaveDebate(args)
```

- [ ] **Step 5: Add the 4 handler functions**

Append these functions to `server.ts` before the `// ── Transport & lifecycle` section:

```typescript
// ── Debate handlers ───────────────────────────────────────────

function handleInitDebate(args: Record<string, unknown>) {
  const inputType = args.input_type as 'knowledge-overview' | 'design-spec'
  const inputId = args.input_id as string
  const inputVersion = args.input_version as string
  const artifactContent = args.artifact_content as unknown
  const codebbaseRequirementsId = args.codebase_requirements_id as string | undefined

  if (!inputType || !inputId || !inputVersion || !artifactContent) {
    throw new Error('input_type, input_id, input_version, and artifact_content are required')
  }

  activeDebate = initDebate({ inputType, inputId, inputVersion, artifactContent, codebbaseRequirementsId })
  const agentPrompts = generateAgentPrompts(activeDebate)

  return text(JSON.stringify({
    transcript_id: activeDebate.transcriptId,
    input_type: activeDebate.inputType,
    input_id: activeDebate.inputId,
    instruction: [
      'Dispatch all 4 agents in parallel using the Agent tool.',
      'Feed each agent its prompt. Parse the JSON response from each agent.',
      'Call pipeline_submit_argument once per agent with the parsed result.',
      'Then call pipeline_synthesize_debate to get the synthesis prompt.',
    ].join(' '),
    agents: agentPrompts,
  }, null, 2))
}

function handleSubmitArgument(args: Record<string, unknown>) {
  if (!activeDebate) throw new Error('No active debate. Call pipeline_init_debate first.')

  const role = args.role as 'advocate' | 'critic' | 'risk_specialist' | 'domain_specialist'
  const position = args.position as string
  const confidence = args.confidence as number
  const evidence = (args.evidence as string[]) ?? []
  const counterpoints = (args.counterpoints as string[]) ?? []
  const proposed_changes = (args.proposed_changes as string[]) ?? []

  if (!role || !position || confidence === undefined) {
    throw new Error('role, position, and confidence are required')
  }

  activeDebate = submitArgument(activeDebate, { role, position, confidence, evidence, counterpoints, proposed_changes })

  const submittedRoles = activeDebate.round1Arguments.map(a => a.role)
  const remaining = (['advocate', 'critic', 'risk_specialist', 'domain_specialist'] as const)
    .filter(r => !submittedRoles.includes(r))

  return text(JSON.stringify({
    recorded: role,
    submitted_so_far: submittedRoles,
    remaining,
    ready_for_synthesis: remaining.length === 0,
    next_step: remaining.length === 0
      ? 'All round-1 arguments recorded. Call pipeline_synthesize_debate to get the synthesizer prompt.'
      : `Still waiting for: ${remaining.join(', ')}`,
  }, null, 2))
}

function handleSynthesizeDebate(args: Record<string, unknown>) {
  if (!activeDebate) throw new Error('No active debate. Call pipeline_init_debate first.')

  // If no synthesis data provided yet, return the synthesis prompt for the agent
  if (!args.changes_accepted) {
    const prompt = generateSynthesisPrompt(activeDebate)
    return text(JSON.stringify({
      instruction: 'Dispatch the Synthesizer agent using the Agent tool with this prompt. Parse the JSON response, then call pipeline_synthesize_debate again with the parsed changes_accepted, changes_rejected, and open_questions fields.',
      synthesis_prompt: prompt,
    }, null, 2))
  }

  // Record the synthesis result
  const synthesis: DebateSynthesis = {
    changes_accepted: (args.changes_accepted as string[]) ?? [],
    changes_rejected: (args.changes_rejected as Array<{ proposed: string; reason: string }>) ?? [],
    open_questions: (args.open_questions as string[]) ?? [],
  }
  const outputVersion = args.output_version as string | undefined
  const kbQueriesUsed = (args.kb_queries_used as boolean) ?? false

  activeDebate = {
    ...activeDebate,
    synthesis,
    outputVersion: outputVersion ?? null,
    kbQueriesUsed,
  }

  return text(JSON.stringify({
    status: 'synthesis_recorded',
    changes_accepted: synthesis.changes_accepted.length,
    changes_rejected: synthesis.changes_rejected.length,
    open_questions: synthesis.open_questions?.length ?? 0,
    next_step: 'Call pipeline_save_debate to validate and persist the transcript.',
  }, null, 2))
}

function handleSaveDebate(args: Record<string, unknown>) {
  if (!activeDebate) throw new Error('No active debate. Call pipeline_init_debate first.')

  const fileName = args.file_name as string
  const phase = args.phase as string
  if (!fileName || !phase) throw new Error('file_name and phase are required')

  const transcript = buildTranscript(activeDebate)

  // Validate against schema
  const validationResult = validateArtifact(schemas, 'debate-transcript.json', transcript)
  if (!validationResult.valid) {
    return text(
      `Debate transcript validation FAILED:\n${validationResult.errors.join('\n')}`,
      true,
    )
  }

  // Store to debates path
  const baseDir = activeRun
    ? baseDirFromRunDir(activeRunDir)
    : resolve(__dirname, '..', config.storage.base_dir)
  const sc = getStorageConfig(baseDir)
  const storedPath = storeArtifact(sc, 'debates', fileName, transcript)

  // Register in run state if a run is active
  if (activeRun) {
    activeRun = addArtifact(activeRun, {
      type: 'debate-transcript',
      path: storedPath,
      phase,
      created_at: new Date().toISOString(),
    }, activeRunDir)
  }

  const savedId = activeDebate.transcriptId
  activeDebate = null

  return text(JSON.stringify({
    transcript_id: savedId,
    stored_path: storedPath,
    status: 'saved',
  }, null, 2))
}
```

- [ ] **Step 6: Type-check**

Run: `cd pipeline-mcp && npx tsc --noEmit server.ts`
Expected: No errors

- [ ] **Step 7: Run full test suite**

Run: `cd pipeline-mcp && node --test`
Expected: 39 pre-existing tests still PASS, 0 failures (debate.test.ts is tested separately in Task 1)

- [ ] **Step 8: Commit**

```bash
cd pipeline-mcp && git add server.ts
git commit -m "feat(pipeline-mcp): add 4 debate tools to server (init, submit, synthesize, save)"
```

---

### Task 3: Integration Test

**Files:**
- Create: `pipeline-mcp/tests/debate-integration.test.ts`

- [ ] **Step 1: Write the integration test**

```typescript
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
const PIPELINE_DIR = resolve(__dirname, '../../strategies/pipeline')

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

    // 1. Minimal design-spec artifact (enough to satisfy schema required fields)
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

    // 2. Init debate
    let state = initDebate({
      inputType: 'design-spec',
      inputId: 'spec-integration-test',
      inputVersion: '1.0.0',
      artifactContent: designSpec,
    })

    assert.ok(state.transcriptId.startsWith('dt-'))
    assert.equal(state.round1Arguments.length, 0)

    // 3. Generate prompts — all 4 agents should be present
    const prompts = generateAgentPrompts(state)
    assert.equal(prompts.length, 4)
    assert.ok(prompts.every(p => p.prompt.includes('spec-integration-test')))

    // 4. Submit 4 round-1 arguments (simulating agent responses)
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

    // 5. Generate synthesis prompt — must contain all 4 positions
    const synthPrompt = generateSynthesisPrompt(state)
    assert.ok(synthPrompt.includes('edge hypothesis is grounded'))
    assert.ok(synthPrompt.includes('threshold is undefined'))
    assert.ok(synthPrompt.includes('stop-loss'))
    assert.ok(synthPrompt.includes('WebSocket'))

    // 6. Record synthesis (simulating synthesizer agent response)
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

    // 7. Build transcript
    const transcript = buildTranscript(state)

    assert.equal(transcript.rounds.length, 2)
    assert.equal(transcript.rounds[0].type, 'divergent')
    assert.equal(transcript.rounds[0].agents.length, 4)
    assert.equal(transcript.rounds[1].type, 'convergent')
    assert.equal(transcript.rounds[1].agents[0].role, 'synthesizer')
    assert.equal(transcript.synthesis.changes_accepted.length, 3)
    assert.equal(transcript.synthesis.changes_rejected.length, 1)
    assert.equal(transcript.synthesis.open_questions?.length, 2)

    // 8. Validate against schema
    const result = validateArtifact(schemas, 'debate-transcript.json', transcript)
    assert.equal(result.valid, true, `Schema validation failed: ${result.errors.join(', ')}`)

    // 9. Store and reload
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
```

- [ ] **Step 2: Run integration test**

Run: `cd pipeline-mcp && node --test tests/debate-integration.test.ts`
Expected: Both integration tests PASS

- [ ] **Step 3: Run full test suite to confirm no regressions**

Run: `cd pipeline-mcp && node --test`
Expected: All 55 tests PASS (39 original + 16 unit + 2 integration = 57 total; note: unit tests from Task 1 already counted)

- [ ] **Step 4: Commit**

```bash
cd pipeline-mcp && git add tests/debate-integration.test.ts
git commit -m "test(pipeline-mcp): debate integration tests — full flow from init to schema-validated save"
```

---

## Design Notes

**Why the module does not dispatch agents:** The debate module is state management infrastructure, not an orchestration layer. Agent dispatching requires access to Claude's Agent tool, which only exists in the orchestrator context (i.e. Claude itself). Separating these concerns keeps `debate.ts` purely functional and fully testable without spawning subprocesses.

**Debate state lifecycle:** `activeDebate` in `server.ts` is in-memory and single-tenanted (one debate at a time per server instance). This matches the pattern of `activeRun` — the pipeline is designed for sequential orchestration by a single Claude session.

**Two-step `pipeline_synthesize_debate`:** The tool handles both phases of the synthesizer workflow: called with no `changes_accepted` argument it returns the synthesis prompt; called with the parsed agent output it records the result. This avoids requiring a separate "get synthesis prompt" tool.

**Transcript rounds always has 2:** Even if fewer than 4 round-1 agents participated, `buildTranscript` always emits round 1 (divergent) and round 2 (convergent). Round 1 contains only the arguments that were actually submitted.
