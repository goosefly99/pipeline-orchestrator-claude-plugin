// pipeline-mcp/cross-ref-validator.ts

import { existsSync, readFileSync } from 'node:fs'
import { join, basename } from 'node:path'
import type { RunState, StorageConfig, ArtifactRef } from './types.ts'

// ── Result types ─────────────────────────────────────────────────────────

export interface CrossRefIssue {
  source_artifact: string    // e.g. "debate-transcript:dt-aabb1122"
  field: string              // e.g. "input_id"
  referenced_id: string      // e.g. "ov-12345678"
  expected_type: string      // e.g. "knowledge-overview"
  message: string
}

export interface SemanticIssue {
  artifact: string           // e.g. "debate-transcript:dt-aabb1122"
  rule: string               // e.g. "debate_min_round1_agents"
  message: string
}

export interface CompletenessIssue {
  phase: string
  missing_type: string
  message: string
}

export interface RunValidationReport {
  valid: boolean
  cross_ref_issues: CrossRefIssue[]
  semantic_issues: SemanticIssue[]
  completeness_issues: CompletenessIssue[]
  summary: string
  artifacts_checked: number
}

// ── Internal helpers ─────────────────────────────────────────────────────

/**
 * Build a map of artifact type -> array of loaded artifact objects.
 * Only loads artifacts that are registered in the run state.
 */
function loadArtifactsByType(
  state: RunState,
  config: StorageConfig,
  parseErrors?: CrossRefIssue[],
): Map<string, Array<{ ref: ArtifactRef; data: Record<string, unknown> }>> {
  const map = new Map<string, Array<{ ref: ArtifactRef; data: Record<string, unknown> }>>()

  // Map artifact types to storage keys
  const typeToStorageKey: Record<string, string> = {
    'raw-collection': 'raw_collections',
    'curated-collection': 'curated_collections',
    'knowledge-overview': 'overviews',
    'design-spec': 'specs',
    'debate-transcript': 'debates',
    'codebase-requirements': 'codebase',
  }

  for (const ref of state.available_artifacts) {
    const storageKey = typeToStorageKey[ref.type]
    if (!storageKey || !config.paths[storageKey]) continue

    const dir = join(config.base_dir, config.paths[storageKey])
    const fileName = basename(ref.path)
    const filePath = join(dir, fileName)

    if (!existsSync(filePath)) continue

    try {
      const data = JSON.parse(readFileSync(filePath, 'utf-8')) as Record<string, unknown>
      const entries = map.get(ref.type) ?? []
      entries.push({ ref, data })
      map.set(ref.type, entries)
    } catch (err) {
      parseErrors?.push({
        source_artifact: `${ref.type}:${basename(ref.path)}`,
        field: '(file)',
        referenced_id: '',
        expected_type: ref.type,
        message: `Failed to parse artifact at "${filePath}": ${err instanceof Error ? err.message : String(err)}`,
      })
    }
  }

  return map
}

/**
 * Get the primary ID of an artifact based on its type.
 */
function getArtifactId(type: string, data: Record<string, unknown>): string {
  switch (type) {
    case 'raw-collection':
    case 'curated-collection':
      return (data.collection_id as string) ?? ''
    case 'knowledge-overview':
      return (data.overview_id as string) ?? ''
    case 'design-spec':
      return (data.spec_id as string) ?? ''
    case 'debate-transcript':
      return (data.transcript_id as string) ?? ''
    case 'codebase-requirements':
      return (data.requirements_id as string) ?? ''
    default:
      return ''
  }
}

/**
 * Check whether a given ID exists among loaded artifacts of specified types.
 */
function idExists(
  artifactMap: Map<string, Array<{ ref: ArtifactRef; data: Record<string, unknown> }>>,
  targetTypes: string[],
  targetId: string,
): boolean {
  for (const type of targetTypes) {
    const entries = artifactMap.get(type)
    if (!entries) continue
    for (const entry of entries) {
      if (getArtifactId(type, entry.data) === targetId) return true
    }
  }
  return false
}

/**
 * Check whether a collection_id exists in either raw or curated collections.
 */
function collectionExists(
  artifactMap: Map<string, Array<{ ref: ArtifactRef; data: Record<string, unknown> }>>,
  collectionId: string,
): boolean {
  return idExists(artifactMap, ['raw-collection', 'curated-collection'], collectionId)
}

// ── Cross-reference validation ───────────────────────────────────────────

export function validateCrossReferences(
  state: RunState,
  config: StorageConfig,
): CrossRefIssue[] {
  const issues: CrossRefIssue[] = []
  const artifactMap = loadArtifactsByType(state, config, issues)

  // Check knowledge-overview sources[].collection
  for (const entry of artifactMap.get('knowledge-overview') ?? []) {
    const id = getArtifactId('knowledge-overview', entry.data)
    const label = `knowledge-overview:${id}`
    const sources = (entry.data.sources as Array<{ collection: string }>) ?? []

    for (const source of sources) {
      if (source.collection && !collectionExists(artifactMap, source.collection)) {
        issues.push({
          source_artifact: label,
          field: 'sources[].collection',
          referenced_id: source.collection,
          expected_type: 'curated-collection or raw-collection',
          message: `Overview "${id}" references collection "${source.collection}" which is not in the run's artifacts`,
        })
      }
    }
  }

  // Check design-spec sources[].collection
  for (const entry of artifactMap.get('design-spec') ?? []) {
    const id = getArtifactId('design-spec', entry.data)
    const label = `design-spec:${id}`
    const sources = (entry.data.sources as Array<{ collection: string }>) ?? []

    for (const source of sources) {
      if (source.collection && !collectionExists(artifactMap, source.collection)) {
        issues.push({
          source_artifact: label,
          field: 'sources[].collection',
          referenced_id: source.collection,
          expected_type: 'curated-collection or raw-collection',
          message: `Spec "${id}" references collection "${source.collection}" which is not in the run's artifacts`,
        })
      }
    }
  }

  // Check debate-transcript input_id
  for (const entry of artifactMap.get('debate-transcript') ?? []) {
    const id = getArtifactId('debate-transcript', entry.data)
    const label = `debate-transcript:${id}`
    const inputId = entry.data.input_id as string | undefined
    const inputType = entry.data.input_type as string | undefined

    if (inputId && inputType) {
      const targetType = inputType === 'knowledge-overview' ? 'knowledge-overview' : 'design-spec'
      if (!idExists(artifactMap, [targetType], inputId)) {
        issues.push({
          source_artifact: label,
          field: 'input_id',
          referenced_id: inputId,
          expected_type: targetType,
          message: `Debate "${id}" references ${targetType} "${inputId}" which is not in the run's artifacts`,
        })
      }
    }

    // Check codebase_requirements_id
    const crId = entry.data.codebase_requirements_id as string | undefined
    if (crId) {
      if (!idExists(artifactMap, ['codebase-requirements'], crId)) {
        issues.push({
          source_artifact: label,
          field: 'codebase_requirements_id',
          referenced_id: crId,
          expected_type: 'codebase-requirements',
          message: `Debate "${id}" references codebase-requirements "${crId}" which is not in the run's artifacts`,
        })
      }
    }
  }

  return issues
}

// ── Semantic validation ──────────────────────────────────────────────────

export function validateSemantics(
  state: RunState,
  config: StorageConfig,
): SemanticIssue[] {
  const issues: SemanticIssue[] = []
  const artifactMap = loadArtifactsByType(state, config)

  // Rule: debate transcripts should have at least 2 round-1 arguments
  for (const entry of artifactMap.get('debate-transcript') ?? []) {
    const id = getArtifactId('debate-transcript', entry.data)
    const label = `debate-transcript:${id}`
    const rounds = (entry.data.rounds as Array<{ round: number; agents: unknown[] }>) ?? []
    const round1 = rounds.find(r => r.round === 1)

    if (!round1 || !round1.agents || round1.agents.length < 2) {
      issues.push({
        artifact: label,
        rule: 'debate_min_round1_agents',
        message: `Debate "${id}" has fewer than 2 round-1 arguments (found ${round1?.agents?.length ?? 0}). Effective debate requires at least advocate and critic.`,
      })
    }
  }

  // Rule: design specs should have at least one source
  for (const entry of artifactMap.get('design-spec') ?? []) {
    const id = getArtifactId('design-spec', entry.data)
    const label = `design-spec:${id}`
    const sources = (entry.data.sources as unknown[]) ?? []

    if (sources.length === 0) {
      issues.push({
        artifact: label,
        rule: 'spec_has_sources',
        message: `Spec "${id}" has no sources. Design specs should reference at least one research source.`,
      })
    }
  }

  // Rule: knowledge overviews should have at least one concept
  for (const entry of artifactMap.get('knowledge-overview') ?? []) {
    const id = getArtifactId('knowledge-overview', entry.data)
    const label = `knowledge-overview:${id}`
    const concepts = (entry.data.concepts as Array<{ name: string }>) ?? []

    if (concepts.length === 0) {
      issues.push({
        artifact: label,
        rule: 'overview_has_concepts',
        message: `Overview "${id}" has no concepts. Concept extraction should produce at least one concept.`,
      })
    }

    // Rule: theme concept_names should reference actual concept names
    const conceptNames = new Set(concepts.map(c => c.name))
    const themes = (entry.data.themes as Array<{ name: string; concept_names: string[] }>) ?? []

    for (const theme of themes) {
      for (const cName of theme.concept_names ?? []) {
        if (!conceptNames.has(cName)) {
          issues.push({
            artifact: label,
            rule: 'overview_theme_concepts_exist',
            message: `Overview "${id}" theme "${theme.name}" references concept "${cName}" which is not defined in the overview's concepts list.`,
          })
        }
      }
    }
  }

  return issues
}

// ── Run completeness validation ──────────────────────────────────────────

export function validateRunCompleteness(
  state: RunState,
  phaseOutputMap: Record<string, string[]>,
): CompletenessIssue[] {
  const issues: CompletenessIssue[] = []
  const availableTypes = new Set(state.available_artifacts.map(a => a.type))

  for (const [phaseName, phaseState] of Object.entries(state.phases)) {
    // Only check completed phases
    if (phaseState.status !== 'completed') continue

    const expectedOutputs = phaseOutputMap[phaseName]
    if (!expectedOutputs) continue

    for (const expectedType of expectedOutputs) {
      if (!availableTypes.has(expectedType)) {
        issues.push({
          phase: phaseName,
          missing_type: expectedType,
          message: `Phase "${phaseName}" completed but no "${expectedType}" artifact was produced.`,
        })
      }
    }
  }

  return issues
}

// ── Full run validation ──────────────────────────────────────────────────

export function validateRun(
  state: RunState,
  config: StorageConfig,
  phaseOutputMap: Record<string, string[]>,
): RunValidationReport {
  const crossRefIssues = validateCrossReferences(state, config)
  const semanticIssues = validateSemantics(state, config)
  const completenessIssues = validateRunCompleteness(state, phaseOutputMap)

  const totalIssues = crossRefIssues.length + semanticIssues.length + completenessIssues.length
  const valid = totalIssues === 0

  const parts: string[] = []
  if (crossRefIssues.length > 0) parts.push(`${crossRefIssues.length} cross-reference issue(s)`)
  if (semanticIssues.length > 0) parts.push(`${semanticIssues.length} semantic issue(s)`)
  if (completenessIssues.length > 0) parts.push(`${completenessIssues.length} completeness issue(s)`)

  const summary = valid
    ? `All checks passed. ${state.available_artifacts.length} artifacts validated.`
    : `Found ${totalIssues} issue(s): ${parts.join(', ')}.`

  return {
    valid,
    cross_ref_issues: crossRefIssues,
    semantic_issues: semanticIssues,
    completeness_issues: completenessIssues,
    summary,
    artifacts_checked: state.available_artifacts.length,
  }
}
