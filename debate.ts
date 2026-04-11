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
  artifactPath?: string
  round1Arguments: AgentArgument[]
  synthesis: DebateSynthesis | null
  codebaseRequirementsId?: string
  kbQueriesUsed: boolean
  domainProfile: DomainProfile
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

// ── Domain profiles ──────────────────────────────────────────

export interface DomainProfile {
  name: string
  risk_vocabulary: string[]
  domain_constraints: string[]
  specialist_focus: string
}

export const FINANCE_PROFILE: DomainProfile = {
  name: 'finance',
  risk_vocabulary: ['execution risk', 'liquidity risk', 'model risk', 'operational risk', 'tail risk', 'counterparty risk'],
  domain_constraints: ['market microstructure', 'platform API constraints (rate limits, settlement timing, resolution rules)', 'regulatory requirements'],
  specialist_focus: 'Apply deep domain knowledge: market microstructure, platform API constraints (rate limits, settlement timing, resolution rules), regulatory requirements, and operational realities.',
}

export const SOFTWARE_PROFILE: DomainProfile = {
  name: 'software',
  risk_vocabulary: ['technical debt', 'API stability', 'performance regression', 'backward compatibility', 'security vulnerability'],
  domain_constraints: ['dependency management', 'deployment constraints', 'backward compatibility requirements'],
  specialist_focus: 'Apply deep domain knowledge: software architecture patterns, dependency management, API design, deployment and operational constraints, and backward compatibility requirements.',
}

export const RESEARCH_PROFILE: DomainProfile = {
  name: 'research',
  risk_vocabulary: ['methodological rigor', 'reproducibility', 'statistical validity', 'domain coverage', 'publication bias'],
  domain_constraints: ['peer review standards', 'reproducibility requirements', 'ethical considerations'],
  specialist_focus: 'Apply deep domain knowledge: research methodology, statistical rigor, reproducibility standards, peer review requirements, and domain-specific best practices.',
}

export const DEFAULT_PROFILE: DomainProfile = {
  name: 'default',
  risk_vocabulary: ['feasibility risk', 'scope risk', 'resource risk', 'integration risk', 'quality risk'],
  domain_constraints: ['resource constraints', 'timeline constraints', 'integration requirements'],
  specialist_focus: 'Apply deep domain knowledge: feasibility analysis, trade-off assessment, assumption validation, evidence quality, and real-world operational constraints.',
}

const BUILT_IN_PROFILES: Record<string, DomainProfile> = {
  finance: FINANCE_PROFILE,
  software: SOFTWARE_PROFILE,
  research: RESEARCH_PROFILE,
  default: DEFAULT_PROFILE,
}

export function resolveProfile(profileOrName?: DomainProfile | string): DomainProfile {
  if (!profileOrName) return DEFAULT_PROFILE
  if (typeof profileOrName === 'string') return BUILT_IN_PROFILES[profileOrName] ?? DEFAULT_PROFILE
  return profileOrName
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
  artifactPath?: string
  codebaseRequirementsId?: string
  domainProfile?: DomainProfile | string
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
    artifactPath: params.artifactPath,
    round1Arguments: [],
    synthesis: null,
    codebaseRequirementsId: params.codebaseRequirementsId,
    kbQueriesUsed: false,
    domainProfile: resolveProfile(params.domainProfile),
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
  if (state.codebaseRequirementsId) transcript.codebase_requirements_id = state.codebaseRequirementsId
  if (state.domainProfile.name !== 'default') {
    (transcript as unknown as Record<string, unknown>).domain_profile = state.domainProfile.name
  }

  return transcript
}

// ── generateAgentPrompts ──────────────────────────────────────

export function generateAgentPrompts(state: DebateState): AgentPrompt[] {
  const inputLabel = state.inputType === 'knowledge-overview' ? 'knowledge overview' : 'design spec'
  const profile = state.domainProfile

  const roleInstructions: Record<Exclude<AgentRole, 'synthesizer'>, string> = {
    advocate: `Your role is ADVOCATE. Defend the current artifact's decisions with evidence from the source material and domain knowledge. Identify what is strong, well-reasoned, and supported. Propose only changes that strengthen the existing approach rather than redirecting it.`,

    critic: `Your role is CRITIC. Challenge the artifact's assumptions, identify weaknesses, gaps in reasoning, and under-explored alternatives. Your goal is to find what is missing, overstated, or fragile. Propose concrete alternative approaches or changes where the current direction is flawed.`,

    risk_specialist: `Your role is RISK SPECIALIST. Evaluate risk models, failure modes, worst-case scenarios, and the adequacy of mitigation strategies in this artifact. Assess ${profile.risk_vocabulary.join(', ')}. Identify specific gaps in the artifact's risk treatment and propose concrete mitigations.`,

    domain_specialist: `Your role is DOMAIN SPECIALIST. ${profile.specialist_focus} Identify where the artifact makes assumptions that conflict with real-world constraints or misses domain-specific best practices.`,
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
    ].join('\n')

    return { role, prompt }
  })
}

// ── buildArtifactStructureSummary ─────────────────────────────

export function buildArtifactStructureSummary(content: unknown): string {
  if (content === null || typeof content !== 'object') {
    return `(non-object artifact, ${typeof content})`
  }
  const obj = content as Record<string, unknown>
  const lines = Object.entries(obj).map(([key, value]) => {
    if (typeof value === 'string') {
      if (value.length > 60) {
        return `  - ${key}: "${value.slice(0, 60)}..."`
      }
      return `  - ${key}: "${value}"`
    }
    if (Array.isArray(value)) {
      return `  - ${key}: ${value.length} items`
    }
    if (value !== null && typeof value === 'object') {
      return `  - ${key}: {${Object.keys(value as object).length} keys}`
    }
    return `  - ${key}: ${String(value)}`
  })
  return lines.join('\n')
}

// ── generateSynthesisPrompt ───────────────────────────────────

export function generateSynthesisPrompt(state: DebateState): string {
  if (state.round1Arguments.length === 0) {
    throw new Error('No round-1 arguments submitted yet — run all 4 debate agents first')
  }

  const inputLabel = state.inputType === 'knowledge-overview' ? 'knowledge overview' : 'design spec'

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

  const profile = state.domainProfile
  const domainNote = profile.name !== 'default' ? ` Domain: ${profile.name}. Key constraints: ${profile.domain_constraints.join(', ')}.` : ''

  return [
    `You are SYNTHESIZER in an adversarial debate review of a ${inputLabel}.${domainNote}`,
    '',
    'Your task: evaluate all round-1 arguments, resolve conflicts, and produce a final synthesis decision.',
    '',
    '## Round 1 Arguments',
    '',
    argumentSections,
    '',
    '## Artifact Reference',
    '',
    ...(state.artifactPath !== undefined
      ? [`Path: ${state.artifactPath} (read this file for full content)`]
      : []),
    `Artifact ID: ${state.inputId} (version ${state.inputVersion})`,
    '',
    '## Artifact Structure (key summary)',
    '',
    buildArtifactStructureSummary(state.artifactContent),
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
