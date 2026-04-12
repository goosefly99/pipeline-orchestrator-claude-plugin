// quality-gates.ts — Pure quality gate check functions and orchestrator

import { readFileSync, existsSync } from 'node:fs'
import type { QualityCheck, QualityGate, CheckResult, GateResult, ArtifactRef } from './types.ts'

// ── Pure check functions ────────────────────────────────────

/**
 * Check that a required field is present and non-empty in an artifact.
 * params: { field: string, artifact_type?: string }
 */
export function checkFieldPresent(
  artifact: Record<string, unknown>,
  params: Record<string, unknown>,
): CheckResult {
  const field = params.field as string
  if (!field) {
    return { check_type: 'field_present', passed: false, message: 'Check misconfigured: missing "field" param' }
  }

  // Support dot-notation paths like "architecture.components"
  const value = field.includes('.')
    ? field.split('.').reduce<unknown>((obj, key) => {
        if (obj == null || typeof obj !== 'object') return undefined
        return (obj as Record<string, unknown>)[key]
      }, artifact)
    : artifact[field]
  if (value === undefined || value === null) {
    return { check_type: 'field_present', passed: false, message: `Required field "${field}" is missing` }
  }
  if (typeof value === 'string' && value.trim().length === 0) {
    return { check_type: 'field_present', passed: false, message: `Required field "${field}" is empty` }
  }
  if (Array.isArray(value) && value.length === 0) {
    return { check_type: 'field_present', passed: false, message: `Required field "${field}" is an empty array` }
  }

  return { check_type: 'field_present', passed: true, message: `Field "${field}" is present` }
}

/**
 * Check that an array field has at least N items.
 * params: { field: string, min: number }
 */
export function checkMinItems(
  artifact: Record<string, unknown>,
  params: Record<string, unknown>,
): CheckResult {
  const field = params.field as string
  const min = params.min as number

  if (!field || min === undefined) {
    return { check_type: 'min_items', passed: false, message: 'Check misconfigured: missing "field" or "min" param' }
  }

  const value = artifact[field]
  if (!Array.isArray(value)) {
    return { check_type: 'min_items', passed: false, message: `Field "${field}" is not an array` }
  }

  if (value.length < min) {
    return {
      check_type: 'min_items',
      passed: false,
      message: `Field "${field}" has ${value.length} items (minimum: ${min})`,
    }
  }

  return { check_type: 'min_items', passed: true, message: `Field "${field}" has ${value.length} items (minimum: ${min})` }
}

/**
 * Check that cross-references between artifacts are valid.
 * Validates that values in a source field exist as keys in a target artifact.
 * params: { source_field: string, target_artifact_type: string, target_field: string }
 */
export function checkCrossRefValid(
  artifact: Record<string, unknown>,
  params: Record<string, unknown>,
  allArtifacts: Map<string, Record<string, unknown>>,
): CheckResult {
  const sourceField = params.source_field as string
  const targetType = params.target_artifact_type as string
  const targetField = params.target_field as string

  if (!sourceField || !targetType || !targetField) {
    return {
      check_type: 'cross_ref_valid',
      passed: false,
      message: 'Check misconfigured: missing source_field, target_artifact_type, or target_field param',
    }
  }

  const sourceValues = artifact[sourceField]
  if (!Array.isArray(sourceValues)) {
    return { check_type: 'cross_ref_valid', passed: false, message: `Source field "${sourceField}" is not an array` }
  }

  // Find target artifact by type
  let targetArtifact: Record<string, unknown> | undefined
  for (const [type, art] of allArtifacts) {
    if (type === targetType) {
      targetArtifact = art
      break
    }
  }

  if (!targetArtifact) {
    return {
      check_type: 'cross_ref_valid',
      passed: false,
      message: `Target artifact of type "${targetType}" not found`,
    }
  }

  const targetValues = targetArtifact[targetField]
  if (!Array.isArray(targetValues) && typeof targetValues !== 'object') {
    return {
      check_type: 'cross_ref_valid',
      passed: false,
      message: `Target field "${targetField}" in "${targetType}" is not iterable`,
    }
  }

  // For object targets, check keys; for array targets, check values
  const validRefs = Array.isArray(targetValues)
    ? new Set(targetValues.map(v => String(v)))
    : new Set(Object.keys(targetValues as Record<string, unknown>))

  const invalid = sourceValues.filter(v => !validRefs.has(String(v)))
  if (invalid.length > 0) {
    return {
      check_type: 'cross_ref_valid',
      passed: false,
      message: `Cross-reference invalid: ${invalid.length} value(s) in "${sourceField}" not found in "${targetType}.${targetField}"`,
    }
  }

  return {
    check_type: 'cross_ref_valid',
    passed: true,
    message: `All ${sourceValues.length} cross-references from "${sourceField}" are valid`,
  }
}

// ── Check dispatcher ────────────────────────────────────────

const CHECK_FUNCTIONS: Record<string, (
  artifact: Record<string, unknown>,
  params: Record<string, unknown>,
  allArtifacts: Map<string, Record<string, unknown>>,
) => CheckResult> = {
  field_present: (art, params) => checkFieldPresent(art, params),
  min_items: (art, params) => checkMinItems(art, params),
  cross_ref_valid: checkCrossRefValid,
}

/**
 * Run a single check against an artifact.
 */
export function runCheck(
  check: QualityCheck,
  artifact: Record<string, unknown>,
  allArtifacts: Map<string, Record<string, unknown>>,
): CheckResult {
  const fn = CHECK_FUNCTIONS[check.check_type]
  if (!fn) {
    return { check_type: check.check_type, passed: false, message: `Unknown check type: "${check.check_type}"` }
  }
  return fn(artifact, check.params, allArtifacts)
}

// ── Gate orchestrator ───────────────────────────────────────

/**
 * Load an artifact from disk and parse as JSON.
 * Returns null if the file does not exist or cannot be parsed.
 */
export function loadArtifactForGate(path: string): Record<string, unknown> | null {
  if (!existsSync(path)) return null
  try {
    return JSON.parse(readFileSync(path, 'utf-8')) as Record<string, unknown>
  } catch {
    return null
  }
}

/**
 * Run all gate checks for a phase.
 * Loads artifacts from disk using the artifactRefs, then runs each check
 * against the appropriate artifact.
 *
 * @param gate - The quality gate configuration for the phase
 * @param artifactRefs - Artifact references registered for this phase
 * @returns GateResult with per-check results and overall pass/fail
 */
export function runGateChecks(
  gate: QualityGate,
  artifactRefs: ArtifactRef[],
): GateResult {
  // Load all artifacts into a map by type
  const allArtifacts = new Map<string, Record<string, unknown>>()
  for (const ref of artifactRefs) {
    const artifact = loadArtifactForGate(ref.path)
    if (artifact) {
      allArtifacts.set(ref.type, artifact)
    }
  }

  const results: CheckResult[] = []
  for (const check of gate.checks) {
    // Determine which artifact to check against
    const artifactType = check.params.artifact_type as string | undefined

    let targetArtifact: Record<string, unknown> | undefined
    if (artifactType) {
      targetArtifact = allArtifacts.get(artifactType)
    } else {
      // Default to first artifact if no type specified
      const first = allArtifacts.values().next()
      targetArtifact = first.done ? undefined : first.value
    }

    if (!targetArtifact) {
      results.push({
        check_type: check.check_type,
        passed: false,
        message: artifactType
          ? `Artifact of type "${artifactType}" not found for check`
          : 'No artifacts available for check',
      })
      continue
    }

    results.push(runCheck(check, targetArtifact, allArtifacts))
  }

  const passed = results.every(r => r.passed)

  return {
    phase: gate.phase,
    passed,
    on_failure: gate.on_failure,
    results,
  }
}
