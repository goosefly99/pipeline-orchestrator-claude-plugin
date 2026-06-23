# Cross-Referencing & Automated Validation --- Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a cross-reference validator module that checks ID-based references between pipeline artifacts resolve correctly, enforces semantic consistency rules, verifies run completeness, and expose all of this through a new `pipeline_validate_run` MCP tool.

**Architecture:** One new module (`cross-ref-validator.ts`) that imports `storage.ts` for loading artifacts and uses `run-state.ts` types for traversing the active run. The validator walks `available_artifacts` in the run state, loads each artifact from storage, and checks that every ID reference points to another stored artifact. Semantic rules are hardcoded checks (not schema-driven) that validate internal consistency within and across artifacts. The `pipeline_validate_run` tool in `server.ts` delegates entirely to the validator module and returns a structured report.

**Tech Stack:** Node.js v24 (native TypeScript), `node:fs`, `node:path`, `node:test` (testing). No new npm dependencies.

---

## File Structure

```
pipeline-mcp/
  cross-ref-validator.ts              -- Cross-reference checks, semantic rules, completeness checks
  tests/
    cross-ref-validator.test.ts       -- Unit tests (TDD: written first)
```

Modifications to existing files:
```
pipeline-mcp/server.ts               -- Add pipeline_validate_run tool definition + handler
```

---

## Cross-Reference Map

These are the ID-based references between artifact types, derived from the schemas:

| Source Artifact | Field | References | Target Artifact |
|---|---|---|---|
| `knowledge-overview` | `sources[].collection` | `collection_id` | `curated-collection` or `raw-collection` |
| `knowledge-overview` | `concepts[].source_items[]` | `items[].id` | items within the source collections |
| `design-spec` | `sources[].collection` | `collection_id` | `curated-collection` or `raw-collection` |
| `design-spec` | `sources[].item_id` | `items[].id` | items within the source collections |
| `debate-transcript` | `input_id` | `overview_id` or `spec_id` | `knowledge-overview` or `design-spec` |
| `debate-transcript` | `codebase_requirements_id` | `requirements_id` | `codebase-requirements` |
| `kb-query-log` | `debate_transcript_id` | `transcript_id` | `debate-transcript` |

---

### Task 1: Cross-Reference Validator Module (Tests + Implementation)

**Files:**
- Create: `pipeline-mcp/tests/cross-ref-validator.test.ts`
- Create: `pipeline-mcp/cross-ref-validator.ts`

- [ ] **Step 1: Write failing tests for the cross-reference validator**

```typescript
// pipeline-mcp/tests/cross-ref-validator.test.ts

import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'

import { storeArtifact } from '../storage.ts'
import { initRun, startPhase, completePhase, addArtifact } from '../run-state.ts'
import type { RunState, StorageConfig } from '../types.ts'
import {
  validateCrossReferences,
  validateSemantics,
  validateRunCompleteness,
  validateRun,
  type CrossRefIssue,
  type SemanticIssue,
  type CompletenessIssue,
  type RunValidationReport,
} from '../cross-ref-validator.ts'

// ── Fixtures ────────────────────────────────────────────────────────────

let tempDir: string
let storageConfig: StorageConfig

beforeEach(() => {
  tempDir = mkdtempSync(join(tmpdir(), 'cross-ref-test-'))
  storageConfig = {
    base_dir: tempDir,
    paths: {
      runs: 'runs',
      manifests: 'manifests',
      raw_collections: 'collections/raw',
      curated_collections: 'collections/curated',
      overviews: 'overviews',
      specs: 'specs',
      debates: 'debates',
      codebase: 'codebase',
    },
  }
})

afterEach(() => {
  rmSync(tempDir, { recursive: true, force: true })
})

function makeRunState(overrides?: Partial<RunState>): RunState {
  return {
    run_id: 'test-run',
    pipeline_version: '1.0.0',
    created_at: '2026-04-03T10:00:00Z',
    updated_at: '2026-04-03T10:00:00Z',
    status: 'running',
    phases: {},
    available_artifacts: [],
    config_path: tempDir,
    ...overrides,
  }
}

// ── Sample artifacts ────────────────────────────────────────────────────

const sampleRawCollection = {
  collection_id: 'test--aabbccdd',
  created_date: '2026-04-03T10:00:00Z',
  status: 'raw',
  items: [
    { id: 'x_001', source_type: 'x_search', content: 'Some tweet content' },
    { id: 'x_002', source_type: 'x_search', content: 'Another tweet' },
  ],
}

const sampleCuratedCollection = {
  collection_id: 'test--aabbccdd',
  created_date: '2026-04-03T10:00:00Z',
  curated_date: '2026-04-03T11:00:00Z',
  status: 'curated',
  items: [
    { id: 'x_001', content: 'Some tweet content', summary: 'Tweet about X', tags: ['trading'] },
    { id: 'x_002', content: 'Another tweet', summary: 'Tweet about Y', tags: ['market'] },
  ],
}

const sampleOverview = {
  overview_id: 'ov-12345678',
  title: 'Test Overview',
  created_date: '2026-04-03T12:00:00Z',
  status: 'complete',
  sources: [
    { collection: 'test--aabbccdd', item_id: 'x_001', title: 'Source tweet' },
  ],
  summary: 'A test summary',
  concepts: [
    {
      name: 'Test Concept',
      category: 'strategy',
      description: 'A test concept',
      key_details: ['detail 1'],
      source_items: ['x_001'],
      relationships: [],
    },
  ],
  themes: [
    { name: 'Test Theme', description: 'A grouping', concept_names: ['Test Concept'] },
  ],
}

const sampleSpec = {
  spec_id: 'a1b2c3d4-e5f6-7890-abcd-ef1234567890',
  title: 'Test Spec',
  created_date: '2026-04-03',
  updated_date: '2026-04-03',
  version: '1.0.0',
  status: 'draft',
  spec_type: 'implementation',
  sources: [
    { collection: 'test--aabbccdd', item_id: 'x_001', title: 'Source', relevance: 'Key input' },
  ],
  overview: {
    description: 'Test spec description',
    objectives: ['obj1'],
    constraints: ['con1'],
    assumptions: ['asm1'],
  },
  architecture: {
    components: [{ name: 'Core' }],
    data_flow: 'A to B',
    integration_points: ['API X'],
  },
  implementation: {
    phases: [{ phase: 1, name: 'Phase 1', tasks: ['task1'], deliverables: ['d1'] }],
    tech_stack: ['TypeScript'],
    complexity: 'medium',
  },
  risks: [{ description: 'Risk 1', severity: 'medium', mitigation: 'Mitigate it' }],
  success_criteria: ['criterion 1'],
}

const sampleDebateTranscript = {
  transcript_id: 'dt-aabb1122',
  input_type: 'knowledge-overview',
  input_id: 'ov-12345678',
  input_version: '1.0.0',
  created_date: '2026-04-03T14:00:00Z',
  rounds: [
    {
      round: 1,
      type: 'divergent',
      agents: [
        { role: 'advocate', position: 'Support it', confidence: 0.8 },
        { role: 'critic', position: 'Challenge it', confidence: 0.7 },
      ],
    },
  ],
  synthesis: {
    changes_accepted: ['Change 1'],
    changes_rejected: [{ proposed: 'Bad idea', reason: 'Too risky' }],
  },
}

// ── validateCrossReferences ─────────────────────────────────────────────

describe('validateCrossReferences', () => {
  it('returns no issues when all references resolve', () => {
    // Store artifacts that reference each other correctly
    storeArtifact(storageConfig, 'curated_collections', 'test--aabbccdd.json', sampleCuratedCollection)
    storeArtifact(storageConfig, 'overviews', 'ov-12345678.json', sampleOverview)
    storeArtifact(storageConfig, 'specs', 'test-spec.json', sampleSpec)
    storeArtifact(storageConfig, 'debates', 'dt-aabb1122.json', sampleDebateTranscript)

    const state = makeRunState({
      available_artifacts: [
        { type: 'curated-collection', path: 'test--aabbccdd.json', phase: 'curation', created_at: '2026-04-03T10:00:00Z' },
        { type: 'knowledge-overview', path: 'ov-12345678.json', phase: 'concept_extraction', created_at: '2026-04-03T12:00:00Z' },
        { type: 'design-spec', path: 'test-spec.json', phase: 'design_synthesis', created_at: '2026-04-03T13:00:00Z' },
        { type: 'debate-transcript', path: 'dt-aabb1122.json', phase: 'debate', created_at: '2026-04-03T14:00:00Z' },
      ],
    })

    const issues = validateCrossReferences(state, storageConfig)
    assert.deepEqual(issues, [])
  })

  it('reports broken collection reference in knowledge-overview sources', () => {
    const badOverview = {
      ...sampleOverview,
      sources: [
        { collection: 'nonexistent--collection', item_id: 'x_001', title: 'Source' },
      ],
    }
    storeArtifact(storageConfig, 'overviews', 'ov-12345678.json', badOverview)

    const state = makeRunState({
      available_artifacts: [
        { type: 'knowledge-overview', path: 'ov-12345678.json', phase: 'concept_extraction', created_at: '2026-04-03T12:00:00Z' },
      ],
    })

    const issues = validateCrossReferences(state, storageConfig)
    assert.ok(issues.length > 0)
    assert.ok(issues.some(i => i.source_artifact === 'knowledge-overview:ov-12345678'))
    assert.ok(issues.some(i => i.referenced_id === 'nonexistent--collection'))
  })

  it('reports broken input_id reference in debate-transcript', () => {
    // Debate references overview_id that does not exist in run
    storeArtifact(storageConfig, 'debates', 'dt-aabb1122.json', sampleDebateTranscript)

    const state = makeRunState({
      available_artifacts: [
        { type: 'debate-transcript', path: 'dt-aabb1122.json', phase: 'debate', created_at: '2026-04-03T14:00:00Z' },
        // No knowledge-overview artifact -- the input_id 'ov-12345678' is dangling
      ],
    })

    const issues = validateCrossReferences(state, storageConfig)
    assert.ok(issues.length > 0)
    assert.ok(issues.some(i =>
      i.source_artifact === 'debate-transcript:dt-aabb1122' &&
      i.referenced_id === 'ov-12345678'
    ))
  })

  it('reports broken codebase_requirements_id in debate-transcript', () => {
    const debateWithCR = {
      ...sampleDebateTranscript,
      codebase_requirements_id: 'cr-nonexist',
    }
    storeArtifact(storageConfig, 'debates', 'dt-aabb1122.json', debateWithCR)

    const state = makeRunState({
      available_artifacts: [
        { type: 'knowledge-overview', path: 'ov-12345678.json', phase: 'concept_extraction', created_at: '2026-04-03T12:00:00Z' },
        { type: 'debate-transcript', path: 'dt-aabb1122.json', phase: 'debate', created_at: '2026-04-03T14:00:00Z' },
      ],
    })

    // Store the overview so input_id resolves, but no codebase-requirements
    storeArtifact(storageConfig, 'overviews', 'ov-12345678.json', sampleOverview)

    const issues = validateCrossReferences(state, storageConfig)
    assert.ok(issues.some(i =>
      i.field === 'codebase_requirements_id' &&
      i.referenced_id === 'cr-nonexist'
    ))
  })

  it('reports broken collection reference in design-spec sources', () => {
    const badSpec = {
      ...sampleSpec,
      sources: [
        { collection: 'missing--collection', item_id: 'x_999', title: 'Ghost', relevance: 'n/a' },
      ],
    }
    storeArtifact(storageConfig, 'specs', 'bad-spec.json', badSpec)

    const state = makeRunState({
      available_artifacts: [
        { type: 'design-spec', path: 'bad-spec.json', phase: 'design_synthesis', created_at: '2026-04-03T13:00:00Z' },
      ],
    })

    const issues = validateCrossReferences(state, storageConfig)
    assert.ok(issues.some(i => i.referenced_id === 'missing--collection'))
  })
})

// ── validateSemantics ───────────────────────────────────────────────────

describe('validateSemantics', () => {
  it('returns no issues for well-formed artifacts', () => {
    storeArtifact(storageConfig, 'overviews', 'ov-12345678.json', sampleOverview)
    storeArtifact(storageConfig, 'specs', 'test-spec.json', sampleSpec)
    storeArtifact(storageConfig, 'debates', 'dt-aabb1122.json', sampleDebateTranscript)

    const state = makeRunState({
      available_artifacts: [
        { type: 'knowledge-overview', path: 'ov-12345678.json', phase: 'concept_extraction', created_at: '2026-04-03T12:00:00Z' },
        { type: 'design-spec', path: 'test-spec.json', phase: 'design_synthesis', created_at: '2026-04-03T13:00:00Z' },
        { type: 'debate-transcript', path: 'dt-aabb1122.json', phase: 'debate', created_at: '2026-04-03T14:00:00Z' },
      ],
    })

    const issues = validateSemantics(state, storageConfig)
    assert.deepEqual(issues, [])
  })

  it('flags debate transcript with fewer than 2 round-1 arguments', () => {
    const thinDebate = {
      ...sampleDebateTranscript,
      rounds: [
        {
          round: 1,
          type: 'divergent',
          agents: [
            { role: 'advocate', position: 'Only one voice', confidence: 0.8 },
          ],
        },
      ],
    }
    storeArtifact(storageConfig, 'debates', 'dt-thin.json', thinDebate)

    const state = makeRunState({
      available_artifacts: [
        { type: 'debate-transcript', path: 'dt-thin.json', phase: 'debate', created_at: '2026-04-03T14:00:00Z' },
      ],
    })

    const issues = validateSemantics(state, storageConfig)
    assert.ok(issues.some(i =>
      i.artifact === 'debate-transcript:dt-thin' &&
      i.rule === 'debate_min_round1_agents'
    ))
  })

  it('flags design spec with no sources', () => {
    const emptySourcesSpec = { ...sampleSpec, sources: [] }
    storeArtifact(storageConfig, 'specs', 'empty-sources.json', emptySourcesSpec)

    const state = makeRunState({
      available_artifacts: [
        { type: 'design-spec', path: 'empty-sources.json', phase: 'design_synthesis', created_at: '2026-04-03T13:00:00Z' },
      ],
    })

    const issues = validateSemantics(state, storageConfig)
    assert.ok(issues.some(i =>
      i.rule === 'spec_has_sources'
    ))
  })

  it('flags knowledge overview with no concepts', () => {
    const emptyConcepts = { ...sampleOverview, concepts: [] }
    storeArtifact(storageConfig, 'overviews', 'empty-concepts.json', emptyConcepts)

    const state = makeRunState({
      available_artifacts: [
        { type: 'knowledge-overview', path: 'empty-concepts.json', phase: 'concept_extraction', created_at: '2026-04-03T12:00:00Z' },
      ],
    })

    const issues = validateSemantics(state, storageConfig)
    assert.ok(issues.some(i =>
      i.rule === 'overview_has_concepts'
    ))
  })

  it('flags knowledge overview theme referencing nonexistent concept name', () => {
    const badThemes = {
      ...sampleOverview,
      themes: [
        { name: 'Orphan Theme', description: 'References nothing real', concept_names: ['Nonexistent Concept'] },
      ],
    }
    storeArtifact(storageConfig, 'overviews', 'bad-themes.json', badThemes)

    const state = makeRunState({
      available_artifacts: [
        { type: 'knowledge-overview', path: 'bad-themes.json', phase: 'concept_extraction', created_at: '2026-04-03T12:00:00Z' },
      ],
    })

    const issues = validateSemantics(state, storageConfig)
    assert.ok(issues.some(i =>
      i.rule === 'overview_theme_concepts_exist'
    ))
  })
})

// ── validateRunCompleteness ─────────────────────────────────────────────

describe('validateRunCompleteness', () => {
  it('returns no issues when all completed phases have output artifacts', () => {
    const runDir = join(tempDir, 'runs', 'complete-run')
    let state = initRun('complete-run', '1.0.0', ['curation', 'concept_extraction'], runDir)
    state = startPhase(state, 'curation', runDir)
    state = addArtifact(state, {
      type: 'curated-collection',
      path: 'test.json',
      phase: 'curation',
      created_at: '2026-04-03T10:00:00Z',
    }, runDir)
    state = completePhase(state, 'curation', runDir)

    const phaseOutputMap = {
      curation: ['curated-collection'],
      concept_extraction: ['knowledge-overview'],
    }

    const issues = validateRunCompleteness(state, phaseOutputMap)
    assert.deepEqual(issues, [])
  })

  it('flags completed phase missing expected output artifacts', () => {
    const runDir = join(tempDir, 'runs', 'incomplete-run')
    let state = initRun('incomplete-run', '1.0.0', ['curation', 'concept_extraction'], runDir)

    // Complete curation without storing any artifact
    state = startPhase(state, 'curation', runDir)
    state = completePhase(state, 'curation', runDir)

    const phaseOutputMap = {
      curation: ['curated-collection'],
      concept_extraction: ['knowledge-overview'],
    }

    const issues = validateRunCompleteness(state, phaseOutputMap)
    assert.ok(issues.length > 0)
    assert.ok(issues.some(i =>
      i.phase === 'curation' &&
      i.missing_type === 'curated-collection'
    ))
  })

  it('ignores skipped and pending phases', () => {
    const runDir = join(tempDir, 'runs', 'partial-run')
    let state = initRun('partial-run', '1.0.0', ['curation', 'concept_extraction', 'design_synthesis'], runDir)

    // Only complete curation with its artifact
    state = startPhase(state, 'curation', runDir)
    state = addArtifact(state, {
      type: 'curated-collection',
      path: 'test.json',
      phase: 'curation',
      created_at: '2026-04-03T10:00:00Z',
    }, runDir)
    state = completePhase(state, 'curation', runDir)

    // concept_extraction is still pending, design_synthesis is still pending

    const phaseOutputMap = {
      curation: ['curated-collection'],
      concept_extraction: ['knowledge-overview'],
      design_synthesis: ['design-spec'],
    }

    const issues = validateRunCompleteness(state, phaseOutputMap)
    assert.deepEqual(issues, [])
  })
})

// ── validateRun (full orchestrator) ─────────────────────────────────────

describe('validateRun', () => {
  it('returns a clean report when everything is consistent', () => {
    storeArtifact(storageConfig, 'curated_collections', 'test--aabbccdd.json', sampleCuratedCollection)
    storeArtifact(storageConfig, 'overviews', 'ov-12345678.json', sampleOverview)
    storeArtifact(storageConfig, 'specs', 'test-spec.json', sampleSpec)
    storeArtifact(storageConfig, 'debates', 'dt-aabb1122.json', sampleDebateTranscript)

    const runDir = join(tempDir, 'runs', 'full-run')
    let state = initRun('full-run', '1.0.0', ['curation', 'concept_extraction', 'design_synthesis', 'debate'], runDir)

    // Register artifacts in run state
    for (const art of [
      { type: 'curated-collection', path: 'test--aabbccdd.json', phase: 'curation' },
      { type: 'knowledge-overview', path: 'ov-12345678.json', phase: 'concept_extraction' },
      { type: 'design-spec', path: 'test-spec.json', phase: 'design_synthesis' },
      { type: 'debate-transcript', path: 'dt-aabb1122.json', phase: 'debate' },
    ]) {
      state = addArtifact(state, { ...art, created_at: '2026-04-03T10:00:00Z' }, runDir)
    }

    // Complete all phases
    for (const phase of ['curation', 'concept_extraction', 'design_synthesis', 'debate']) {
      state = startPhase(state, phase, runDir)
      state = completePhase(state, phase, runDir)
    }

    const phaseOutputMap = {
      curation: ['curated-collection'],
      concept_extraction: ['knowledge-overview'],
      design_synthesis: ['design-spec'],
      debate: ['debate-transcript'],
    }

    const report = validateRun(state, storageConfig, phaseOutputMap)

    assert.equal(report.valid, true)
    assert.equal(report.cross_ref_issues.length, 0)
    assert.equal(report.semantic_issues.length, 0)
    assert.equal(report.completeness_issues.length, 0)
  })

  it('returns invalid report when there are cross-ref issues', () => {
    // Debate references nonexistent overview
    storeArtifact(storageConfig, 'debates', 'dt-aabb1122.json', sampleDebateTranscript)

    const runDir = join(tempDir, 'runs', 'bad-run')
    let state = initRun('bad-run', '1.0.0', ['debate'], runDir)
    state = addArtifact(state, {
      type: 'debate-transcript',
      path: 'dt-aabb1122.json',
      phase: 'debate',
      created_at: '2026-04-03T14:00:00Z',
    }, runDir)
    state = startPhase(state, 'debate', runDir)
    state = completePhase(state, 'debate', runDir)

    const report = validateRun(state, storageConfig, { debate: ['debate-transcript'] })

    assert.equal(report.valid, false)
    assert.ok(report.cross_ref_issues.length > 0)
  })

  it('aggregates issues from all three validators', () => {
    // Thin debate (semantic) + missing overview (cross-ref) + no curation artifact (completeness)
    const thinDebate = {
      ...sampleDebateTranscript,
      rounds: [
        { round: 1, type: 'divergent', agents: [{ role: 'advocate', position: 'Solo', confidence: 0.5 }] },
      ],
    }
    storeArtifact(storageConfig, 'debates', 'dt-aabb1122.json', thinDebate)

    const runDir = join(tempDir, 'runs', 'multi-issue-run')
    let state = initRun('multi-issue-run', '1.0.0', ['curation', 'debate'], runDir)

    state = addArtifact(state, {
      type: 'debate-transcript',
      path: 'dt-aabb1122.json',
      phase: 'debate',
      created_at: '2026-04-03T14:00:00Z',
    }, runDir)

    // Complete both phases but curation has no artifact
    state = startPhase(state, 'curation', runDir)
    state = completePhase(state, 'curation', runDir)
    state = startPhase(state, 'debate', runDir)
    state = completePhase(state, 'debate', runDir)

    const report = validateRun(state, storageConfig, {
      curation: ['curated-collection'],
      debate: ['debate-transcript'],
    })

    assert.equal(report.valid, false)
    // Should have cross-ref (dangling input_id), semantic (thin debate), and completeness (no curation artifact)
    assert.ok(report.cross_ref_issues.length > 0, 'Expected cross-ref issues')
    assert.ok(report.semantic_issues.length > 0, 'Expected semantic issues')
    assert.ok(report.completeness_issues.length > 0, 'Expected completeness issues')
    assert.ok(report.summary.length > 0, 'Expected a summary string')
  })
})
```

- [ ] **Step 2: Run the tests to confirm they fail (module not found)**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && node --test tests/cross-ref-validator.test.ts 2>&1
```

Expected: Fails with `Cannot find module '../cross-ref-validator.ts'` or similar import error.

- [ ] **Step 3: Implement the cross-reference validator module**

```typescript
// pipeline-mcp/cross-ref-validator.ts

import { existsSync, readFileSync, readdirSync } from 'node:fs'
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
    } catch {
      // Skip artifacts that can't be parsed
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
  const artifactMap = loadArtifactsByType(state, config)

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
```

- [ ] **Step 4: Run the tests to confirm they pass**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && node --test tests/cross-ref-validator.test.ts 2>&1
```

Expected: All tests pass (0 failures).

- [ ] **Step 5: Verify TypeScript on the new module**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && npx tsc --noEmit cross-ref-validator.ts 2>&1
```

Expected: No errors.

- [ ] **Step 6: Commit**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && git add cross-ref-validator.ts tests/cross-ref-validator.test.ts && git commit -m "feat(pipeline-mcp): add cross-reference validator with TDD tests

Validates ID references between artifacts (overview->collection,
debate->overview/spec, spec->collection), enforces semantic rules
(min debate agents, spec sources, overview concepts), and checks
run completeness (completed phases have expected artifacts)."
```

---

### Task 2: Add `pipeline_validate_run` Tool to server.ts

**Files:**
- Modify: `pipeline-mcp/server.ts`

- [ ] **Step 1: Add the import for the cross-reference validator**

In `server.ts`, after the existing `kb-client.ts` import block, add:

```typescript
import {
  validateRun,
  type RunValidationReport,
} from './cross-ref-validator.ts'
```

- [ ] **Step 2: Add the tool definition to the `ListToolsRequestSchema` handler**

Add the following entry to the `tools` array, after the `pipeline_kb_export_query_log` tool definition (before the closing `]`):

```typescript
    {
      name: 'pipeline_validate_run',
      description: 'Run cross-reference, semantic, and completeness validation on the active pipeline run. Checks that artifact ID references resolve, enforces consistency rules, and verifies completed phases produced expected outputs. Returns a structured report.',
      inputSchema: {
        type: 'object' as const,
        properties: {
          phase_output_overrides: {
            type: 'object',
            description: 'Optional overrides for expected phase outputs. Keys are phase names, values are arrays of expected artifact type strings. Defaults to the standard pipeline phase outputs.',
            additionalProperties: {
              type: 'array',
              items: { type: 'string' },
            },
          },
        },
      },
    },
```

- [ ] **Step 3: Add the case to the `CallToolRequestSchema` handler switch**

In the `switch (req.params.name)` block, after the `case 'pipeline_kb_export_query_log':` line, add:

```typescript
      case 'pipeline_validate_run':
        return handleValidateRun(args)
```

- [ ] **Step 4: Add the handler function**

Add the following handler function before the `// ── Transport & lifecycle` section:

```typescript
function handleValidateRun(args: Record<string, unknown>) {
  const state = requireRun()
  const baseDir = baseDirFromRunDir(activeRunDir)
  const sc = getStorageConfig(baseDir)

  // Default phase -> expected output types, derived from pipeline.toml
  const defaultPhaseOutputs: Record<string, string[]> = {
    document_ingestion: ['raw-collection'],
    research_discovery: ['raw-collection'],
    curation: ['curated-collection'],
    concept_extraction: ['knowledge-overview'],
    codebase_analysis: ['codebase-requirements'],
    design_synthesis: ['design-spec'],
    debate: ['debate-transcript'],
    validation: ['design-spec'],
  }

  const overrides = (args.phase_output_overrides as Record<string, string[]>) ?? {}
  const phaseOutputMap = { ...defaultPhaseOutputs, ...overrides }

  const report: RunValidationReport = validateRun(state, sc, phaseOutputMap)

  return text(JSON.stringify({
    run_id: state.run_id,
    ...report,
  }, null, 2), !report.valid)
}
```

- [ ] **Step 5: Verify TypeScript on server.ts**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && npx tsc --noEmit server.ts 2>&1
```

Expected: No errors.

- [ ] **Step 6: Run the full test suite**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && node --test tests/*.test.ts 2>&1
```

Expected: All tests pass.

- [ ] **Step 7: Commit**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && git add server.ts && git commit -m "feat(pipeline-mcp): add pipeline_validate_run MCP tool

Exposes cross-reference, semantic, and completeness validation
through the MCP tool interface. Returns structured report with
issues categorized by type. Tool 22 of the pipeline server."
```

---

### Task 3: Integration Test with Multi-Artifact Run

**Files:**
- Modify: `pipeline-mcp/tests/integration.test.ts`

- [ ] **Step 1: Add the cross-reference validator integration test**

Add the following test to the existing `integration.test.ts` file, after the existing test cases:

```typescript
import {
  validateRun,
  validateCrossReferences,
  validateSemantics,
  validateRunCompleteness,
} from '../cross-ref-validator.ts'
```

Add this `describe` block after the existing integration tests:

```typescript
describe('integration: cross-reference validation on a multi-phase run', () => {
  it('validates a run with curated collection, overview, spec, and debate', () => {
    const config = loadPipelineConfig(join(PIPELINE_DIR, 'pipeline.toml'))
    const schemas = loadSchemas(join(PIPELINE_DIR, 'schemas'))

    // Setup storage config rooted at tempDir
    const sc: StorageConfig = {
      base_dir: tempDir,
      paths: config.storage.paths,
    }

    // 1. Store a curated collection
    const curatedCollection = {
      collection_id: 'integ--11223344',
      created_date: '2026-04-03T10:00:00Z',
      curated_date: '2026-04-03T11:00:00Z',
      status: 'curated',
      items: [
        { id: 'item_001', content: 'Research on prediction markets', summary: 'PM research', tags: ['trading'] },
        { id: 'item_002', content: 'Kalshi API documentation', summary: 'API docs', tags: ['api'] },
      ],
    }
    storeArtifact(sc, 'curated_collections', 'integ--11223344.json', curatedCollection)

    // 2. Store a knowledge overview referencing the collection
    const overview = {
      overview_id: 'ov-integ001',
      title: 'Prediction Market Overview',
      created_date: '2026-04-03T12:00:00Z',
      status: 'complete',
      sources: [
        { collection: 'integ--11223344', item_id: 'item_001', title: 'PM research' },
        { collection: 'integ--11223344', item_id: 'item_002', title: 'API docs' },
      ],
      summary: 'Overview of prediction market landscape',
      concepts: [
        {
          name: 'Market Making',
          category: 'strategy',
          description: 'Providing liquidity by quoting bid/ask',
          key_details: ['Spread capture', 'Inventory risk'],
          source_items: ['item_001'],
          relationships: [],
        },
        {
          name: 'API Integration',
          category: 'architecture',
          description: 'REST API patterns for Kalshi',
          key_details: ['WebSocket feeds', 'Order placement'],
          source_items: ['item_002'],
          relationships: [{ concept: 'Market Making', relationship: 'enables' }],
        },
      ],
      themes: [
        { name: 'Trading Infrastructure', description: 'Core trading components', concept_names: ['Market Making', 'API Integration'] },
      ],
    }
    storeArtifact(sc, 'overviews', 'ov-integ001.json', overview)

    // Validate overview against schema
    const ovResult = validateArtifact(schemas, 'knowledge-overview.json', overview)
    assert.equal(ovResult.valid, true, `Overview validation failed: ${ovResult.errors.join(', ')}`)

    // 3. Store a design spec referencing the same collection
    const spec = {
      spec_id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
      title: 'Prediction Market Trading Bot',
      created_date: '2026-04-03',
      updated_date: '2026-04-03',
      version: '1.0.0',
      status: 'draft',
      spec_type: 'implementation',
      sources: [
        { collection: 'integ--11223344', item_id: 'item_001', title: 'PM research', relevance: 'Core strategy' },
      ],
      overview: {
        description: 'An automated trading bot for prediction markets',
        objectives: ['Automate market making on Kalshi'],
        constraints: ['US residency required'],
        assumptions: ['Kalshi API remains stable'],
      },
      architecture: {
        components: [{ name: 'OrderManager', description: 'Manages order lifecycle' }],
        data_flow: 'Market data -> Signal -> Order',
        integration_points: ['Kalshi REST API', 'Kalshi WebSocket'],
      },
      implementation: {
        phases: [{ phase: 1, name: 'Core', tasks: ['API client'], deliverables: ['kalshi-client module'] }],
        tech_stack: ['Python', 'asyncio'],
        complexity: 'high',
      },
      risks: [{ description: 'API rate limits', severity: 'medium', mitigation: 'Backoff with jitter' }],
      success_criteria: ['Profitable over 30-day window'],
    }
    storeArtifact(sc, 'specs', 'integ-spec.json', spec)

    const specResult = validateArtifact(schemas, 'design-spec.json', spec)
    assert.equal(specResult.valid, true, `Spec validation failed: ${specResult.errors.join(', ')}`)

    // 4. Store a debate transcript referencing the overview
    const debate = {
      transcript_id: 'dt-integ001',
      input_type: 'knowledge-overview',
      input_id: 'ov-integ001',
      input_version: '1.0.0',
      created_date: '2026-04-03T14:00:00Z',
      rounds: [
        {
          round: 1,
          type: 'divergent',
          agents: [
            { role: 'advocate', position: 'Market making is viable', confidence: 0.85 },
            { role: 'critic', position: 'Liquidity risk is underestimated', confidence: 0.7 },
            { role: 'risk_specialist', position: 'Need tighter stop-losses', confidence: 0.75 },
            { role: 'domain_specialist', position: 'Kalshi market structure supports this', confidence: 0.8 },
          ],
        },
      ],
      synthesis: {
        changes_accepted: ['Add inventory limit parameter', 'Specify stop-loss thresholds'],
        changes_rejected: [{ proposed: 'Remove market making entirely', reason: 'Core strategy, well-supported by evidence' }],
      },
    }
    storeArtifact(sc, 'debates', 'dt-integ001.json', debate)

    const debateResult = validateArtifact(schemas, 'debate-transcript.json', debate)
    assert.equal(debateResult.valid, true, `Debate validation failed: ${debateResult.errors.join(', ')}`)

    // 5. Build run state with all artifacts
    const runDir = join(tempDir, 'runs', 'integ-run')
    let state = initRun('integ-run', config.pipeline.version, [
      'curation', 'concept_extraction', 'design_synthesis', 'debate',
    ], runDir)

    const artifacts = [
      { type: 'curated-collection', path: 'integ--11223344.json', phase: 'curation' },
      { type: 'knowledge-overview', path: 'ov-integ001.json', phase: 'concept_extraction' },
      { type: 'design-spec', path: 'integ-spec.json', phase: 'design_synthesis' },
      { type: 'debate-transcript', path: 'dt-integ001.json', phase: 'debate' },
    ]
    for (const art of artifacts) {
      state = addArtifact(state, { ...art, created_at: '2026-04-03T10:00:00Z' }, runDir)
    }
    for (const phase of ['curation', 'concept_extraction', 'design_synthesis', 'debate']) {
      state = startPhase(state, phase, runDir)
      state = completePhase(state, phase, runDir)
    }

    // 6. Run full validation
    const phaseOutputMap = {
      curation: ['curated-collection'],
      concept_extraction: ['knowledge-overview'],
      design_synthesis: ['design-spec'],
      debate: ['debate-transcript'],
    }

    const report = validateRun(state, sc, phaseOutputMap)

    assert.equal(report.valid, true, `Expected clean report but got: ${report.summary}`)
    assert.equal(report.cross_ref_issues.length, 0)
    assert.equal(report.semantic_issues.length, 0)
    assert.equal(report.completeness_issues.length, 0)
    assert.equal(report.artifacts_checked, 4)
  })

  it('detects issues when artifacts have broken cross-references', () => {
    const config = loadPipelineConfig(join(PIPELINE_DIR, 'pipeline.toml'))

    const sc: StorageConfig = {
      base_dir: tempDir,
      paths: config.storage.paths,
    }

    // Store a debate that references a nonexistent overview
    const brokenDebate = {
      transcript_id: 'dt-broken01',
      input_type: 'knowledge-overview',
      input_id: 'ov-doesnotexist',
      input_version: '1.0.0',
      created_date: '2026-04-03T14:00:00Z',
      rounds: [
        {
          round: 1,
          type: 'divergent',
          agents: [
            { role: 'advocate', position: 'Yes', confidence: 0.9 },
            { role: 'critic', position: 'No', confidence: 0.8 },
          ],
        },
      ],
      synthesis: { changes_accepted: [], changes_rejected: [] },
    }
    storeArtifact(sc, 'debates', 'dt-broken01.json', brokenDebate)

    // Store an overview that references a nonexistent collection
    const brokenOverview = {
      overview_id: 'ov-broken01',
      title: 'Broken Overview',
      created_date: '2026-04-03T12:00:00Z',
      status: 'complete',
      sources: [{ collection: 'ghost--collection', item_id: 'ghost_1', title: 'Ghost source' }],
      summary: 'This overview has broken refs',
      concepts: [],
      themes: [],
    }
    storeArtifact(sc, 'overviews', 'ov-broken01.json', brokenOverview)

    const runDir = join(tempDir, 'runs', 'broken-run')
    let state = initRun('broken-run', config.pipeline.version, ['concept_extraction', 'debate'], runDir)

    state = addArtifact(state, { type: 'knowledge-overview', path: 'ov-broken01.json', phase: 'concept_extraction', created_at: '2026-04-03T12:00:00Z' }, runDir)
    state = addArtifact(state, { type: 'debate-transcript', path: 'dt-broken01.json', phase: 'debate', created_at: '2026-04-03T14:00:00Z' }, runDir)

    for (const phase of ['concept_extraction', 'debate']) {
      state = startPhase(state, phase, runDir)
      state = completePhase(state, phase, runDir)
    }

    const report = validateRun(state, sc, {
      concept_extraction: ['knowledge-overview'],
      debate: ['debate-transcript'],
    })

    assert.equal(report.valid, false)
    // Cross-ref: debate input_id -> ov-doesnotexist, overview collection -> ghost--collection
    assert.ok(report.cross_ref_issues.length >= 2, `Expected at least 2 cross-ref issues, got ${report.cross_ref_issues.length}`)
    // Semantic: overview has no concepts
    assert.ok(report.semantic_issues.length >= 1, `Expected at least 1 semantic issue, got ${report.semantic_issues.length}`)
  })
})
```

- [ ] **Step 2: Add the import at the top of the file**

At the top of `integration.test.ts`, alongside the existing imports, add:

```typescript
import {
  validateRun,
} from '../cross-ref-validator.ts'
```

- [ ] **Step 3: Run the integration tests**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && node --test tests/integration.test.ts 2>&1
```

Expected: All integration tests pass, including the 2 new cross-reference validation tests.

- [ ] **Step 4: Run the full test suite**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && node --test tests/*.test.ts 2>&1
```

Expected: All tests pass. Record the final test count.

- [ ] **Step 5: Verify TypeScript on all modified files**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && npx tsc --noEmit cross-ref-validator.ts server.ts tests/cross-ref-validator.test.ts tests/integration.test.ts 2>&1
```

Expected: No errors.

- [ ] **Step 6: Commit**

```bash
cd /c/Users/olive/Documents/ai_trading/pipeline-mcp && git add tests/integration.test.ts && git commit -m "test(pipeline-mcp): integration tests for cross-reference validation against multi-artifact pipeline runs"
```

---

## Appendix: Key Design Decisions

### Why a Separate Module Instead of Extending validator.ts

The existing `validator.ts` handles JSON Schema validation (structural conformance). Cross-reference validation is fundamentally different: it checks relationships between artifacts, not the shape of individual artifacts. Keeping them separate follows single-responsibility and avoids coupling the AJV-based schema validator with the run-state-aware cross-reference logic.

### Why Cross-References Are Checked Against Run State, Not the Filesystem

The validator checks artifact IDs against what is registered in `state.available_artifacts`, not by scanning storage directories. This is intentional: artifacts from other runs may exist on disk but should not satisfy references within the current run. The run state is the source of truth for what artifacts belong to a given pipeline execution.

### Why phaseOutputMap Is a Parameter Instead of Derived from pipeline.toml

The `validateRunCompleteness` function takes a `phaseOutputMap` parameter rather than reading pipeline.toml directly. This keeps the function pure and testable without filesystem access to the pipeline config. The MCP tool handler in server.ts provides sensible defaults derived from the pipeline config and allows overrides through the tool's input schema.

### Why No Item-Level Cross-Reference Checks

The schemas show that `sources[].item_id` in overviews and specs reference items within collections, and `concepts[].source_items[]` does the same. While we could load every collection and check that each item ID exists, this adds significant I/O for marginal value: item IDs are typically generated by the pipeline itself and rarely become stale. The collection-level reference check catches the most common and most impactful broken reference (wrong or missing collection entirely). Item-level checks can be added later if needed.
