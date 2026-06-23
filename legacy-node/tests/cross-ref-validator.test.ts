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
      transcript_id: 'dt-thin',
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

// ── Fix 3.7 — validation phase completeness with validation-report ──────

describe('Fix 3.7 — validation phase completeness with validation-report', () => {
  it('passes completeness when a validation-report artifact is registered for a completed validation phase', () => {
    const runDir = join(tempDir, 'runs', 'val-report-ok-run')
    let state = initRun('val-report-ok-run', '1.0.0', ['validation'], runDir)
    state = startPhase(state, 'validation', runDir)
    state = addArtifact(state, {
      type: 'validation-report',
      path: 'validation-report-ok.json',
      phase: 'validation',
      created_at: '2026-04-10T10:00:00Z',
    }, runDir)
    state = completePhase(state, 'validation', runDir)

    const report = validateRun(state, storageConfig, { validation: ['validation-report'] })

    assert.equal(report.completeness_issues.length, 0)
    assert.ok(
      !report.completeness_issues.some(i => i.phase === 'validation'),
      'Expected no completeness issue mentioning the validation phase',
    )
  })

  it('flags completeness when the validation phase completes with only a design-spec and no validation-report', () => {
    const runDir = join(tempDir, 'runs', 'val-report-missing-run')
    let state = initRun('val-report-missing-run', '1.0.0', ['validation'], runDir)
    state = startPhase(state, 'validation', runDir)
    state = addArtifact(state, {
      type: 'design-spec',
      path: 'updated-spec.json',
      phase: 'validation',
      created_at: '2026-04-10T10:00:00Z',
    }, runDir)
    state = completePhase(state, 'validation', runDir)

    const report = validateRun(state, storageConfig, { validation: ['validation-report'] })

    assert.equal(report.completeness_issues.length, 1)
    assert.equal(report.completeness_issues[0].phase, 'validation')
    assert.equal(report.completeness_issues[0].missing_type, 'validation-report')
  })
})
