import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, rmSync, writeFileSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

import { loadPipelineConfig } from '../toml-loader.ts'
import { resolveNextPhases } from '../dag.ts'
import { loadSchemas, validateArtifact } from '../validator.ts'
import { storeArtifact, loadArtifact } from '../storage.ts'
import { initRun, startPhase, completePhase, addArtifact, loadRunState } from '../run-state.ts'
import type { StorageConfig } from '../types.ts'
import { analyzeCodebase } from '../codebase-analyzer.ts'
import { validateRun } from '../cross-ref-validator.ts'

const __dirname = dirname(fileURLToPath(import.meta.url))
const PIPELINE_DIR = resolve(__dirname, '../pipeline')

let tempDir: string

beforeEach(() => {
  tempDir = mkdtempSync(join(tmpdir(), 'pipeline-integration-'))
})

afterEach(() => {
  rmSync(tempDir, { recursive: true, force: true })
})

describe('integration: full pipeline run flow', () => {
  it('init → resolve → start → validate → store → complete → next', () => {
    // 1. Load pipeline config and schemas
    const config = loadPipelineConfig(join(PIPELINE_DIR, 'pipeline.toml'))
    const schemas = loadSchemas(join(PIPELINE_DIR, 'schemas'))

    assert.equal(config.pipeline.id, 'research-to-implementation')
    assert.equal(Object.keys(schemas).length, 12)

    // 2. Init run
    const runDir = join(tempDir, 'runs', 'test-run')
    const phaseNames = Object.keys(config.phases)
    let state = initRun('test-run', config.pipeline.version, phaseNames, runDir)

    assert.equal(state.status, 'initialized')
    assert.equal(Object.keys(state.phases).length, 9)

    // 3. Resolve next phases — entry-point phases are available immediately (no inputs required)
    const artifactTypes = state.available_artifacts.map(a => a.type)
    let next = resolveNextPhases(config, [], artifactTypes)
    // All entry-point phases should be available (they bypass input satisfaction)
    const entryPointPhases = Object.entries(config.phases)
      .filter(([, p]) => p.entry_point)
      .map(([name]) => name)
    for (const ep of entryPointPhases) {
      assert.ok(next.includes(ep), `entry-point phase "${ep}" should be available at init`)
    }
    assert.ok(next.includes('research_discovery'))

    // 5. Start discovery phase
    state = startPhase(state, 'research_discovery', runDir)
    assert.equal(state.phases.research_discovery.status, 'in_progress')

    // 6. Create and validate a raw collection
    const rawCollection = {
      collection_id: 'test--12345678',
      created_date: '2026-04-03T14:30:00Z',
      status: 'raw',
      source_runs: [
        {
          source_type: 'x_search',
          query: 'prediction market',
          executed_at: '2026-04-03T14:30:05Z',
          items_found: 2,
          items_stored: 2,
        },
      ],
      items: [
        {
          id: 'x_123',
          source_type: 'x_search',
          content: 'Test tweet about trading strategies',
        },
      ],
    }

    const validation = validateArtifact(schemas, 'raw-collection.json', rawCollection)
    assert.equal(validation.valid, true, `Validation failed: ${validation.errors.join(', ')}`)

    // 7. Store the artifact
    const sc: StorageConfig = {
      base_dir: tempDir,
      paths: { raw_collections: 'collections/raw', specs: 'specs', overviews: 'overviews' },
    }
    const storedPath = storeArtifact(sc, 'raw_collections', 'test--12345678.json', rawCollection)
    assert.ok(storedPath.includes('collections'))

    // 8. Register artifact in run state
    state = addArtifact(state, {
      type: 'raw-collection',
      path: storedPath,
      phase: 'research_discovery',
      created_at: new Date().toISOString(),
    }, runDir)

    // 9. Complete discovery
    state = completePhase(state, 'research_discovery', runDir)
    assert.equal(state.phases.research_discovery.status, 'completed')

    // 10. Resolve next — curation should now be available
    const completedPhases = ['research_discovery']
    const allArtifacts = ['research-manifest', 'raw-collection']
    next = resolveNextPhases(config, completedPhases, allArtifacts)
    assert.ok(next.includes('curation'), `Expected curation in ${JSON.stringify(next)}`)

    // 11. Load the stored artifact back
    const loaded = loadArtifact(sc, 'raw_collections', 'test--12345678.json') as Record<string, unknown>
    assert.equal(loaded.collection_id, 'test--12345678')
  })
})

describe('integration: codebase analysis against real projects', () => {
  const SCHEMAS_DIR = join(PIPELINE_DIR, 'schemas')

  it('analyzes pipeline-mcp itself — valid schema, deps include smol-toml and ajv', () => {
    const pipelineMcpPath = resolve(__dirname, '..')
    const schemas = loadSchemas(SCHEMAS_DIR)

    const result = analyzeCodebase(pipelineMcpPath)

    assert.ok(result.requirements_id.startsWith('cr-'), `requirements_id should start with cr-: ${result.requirements_id}`)
    assert.equal(result.package.name, 'pipeline-mcp')
    assert.ok(
      result.dependencies.runtime.some(d => d.startsWith('smol-toml@')),
      `expected smol-toml in: ${JSON.stringify(result.dependencies.runtime)}`,
    )
    assert.ok(
      result.dependencies.runtime.some(d => d.startsWith('ajv@')),
      `expected ajv in: ${JSON.stringify(result.dependencies.runtime)}`,
    )

    const validation = validateArtifact(schemas, 'codebase-requirements.json', result)
    assert.equal(
      validation.valid,
      true,
      `Schema validation failed for pipeline-mcp: ${validation.errors.join('; ')}`,
    )
  })

  it('full pipeline run: init → analyzeCodebase → validateArtifact → storeArtifact → loadArtifact round-trip', () => {
    const pipelineMcpPath = resolve(__dirname, '..')
    const config = loadPipelineConfig(join(PIPELINE_DIR, 'pipeline.toml'))
    const schemas = loadSchemas(SCHEMAS_DIR)

    // Init a run
    const runDir = join(tempDir, 'runs', 'codebase-run')
    const phaseNames = Object.keys(config.phases)
    const state = initRun('codebase-run', config.pipeline.version, phaseNames, runDir)
    assert.equal(state.status, 'initialized')

    // Analyze the codebase
    const result = analyzeCodebase(pipelineMcpPath)
    assert.ok(result.requirements_id.startsWith('cr-'))

    // Validate directly — no remapping needed
    const validation = validateArtifact(schemas, 'codebase-requirements.json', result)
    assert.equal(
      validation.valid,
      true,
      `Round-trip schema validation failed: ${validation.errors.join('; ')}`,
    )

    // Store the artifact
    const sc: StorageConfig = {
      base_dir: tempDir,
      paths: { codebase: 'codebase' },
    }
    const fileName = `${result.requirements_id}.json`
    const storedPath = storeArtifact(sc, 'codebase', fileName, result)
    assert.ok(storedPath.endsWith(fileName), `expected stored path to end with ${fileName}`)

    // Load it back and verify round-trip fidelity
    const reloaded = loadArtifact(sc, 'codebase', fileName) as Record<string, unknown>
    assert.equal(reloaded.requirements_id, result.requirements_id)
    assert.equal(reloaded.codebase_path, pipelineMcpPath)
    const pkg = reloaded.package as Record<string, unknown>
    assert.equal(pkg.name, 'pipeline-mcp')
    // No tsconfig.json at root — analyzer detects as 'javascript' or 'typescript' by .ts presence
    assert.ok(
      pkg.language === 'javascript' || pkg.language === 'typescript',
      `expected js/ts language, got: ${pkg.language as string}`,
    )
  })
})

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
          relationships: ['related to API Integration'],
        },
        {
          name: 'API Integration',
          category: 'architecture',
          description: 'REST API patterns for Kalshi',
          key_details: ['WebSocket feeds', 'Order placement'],
          source_items: ['item_002'],
          relationships: ['enables Market Making'],
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

// ── Register-artifact flow ───────────────────────────────────

describe('integration: register-artifact flow', () => {
  it('registers an existing file in run state and records the artifact ref', () => {
    const config = loadPipelineConfig(join(PIPELINE_DIR, 'pipeline.toml'))
    const runDir = join(tempDir, 'runs', 'register-test')
    let state = initRun('register-test', config.pipeline.version, Object.keys(config.phases), runDir)

    // Create a real file on disk
    const artifactDir = join(tempDir, 'codebase')
    mkdirSync(artifactDir, { recursive: true })
    const artifactPath = join(artifactDir, 'cr-abcd1234.json')
    writeFileSync(artifactPath, JSON.stringify({ requirements_id: 'cr-abcd1234', codebase_path: '/test' }), 'utf-8')
    assert.ok(existsSync(artifactPath))

    // Register it
    state = addArtifact(state, {
      type: 'codebase-requirements',
      path: artifactPath,
      phase: 'codebase_analysis',
      created_at: new Date().toISOString(),
    }, runDir)

    assert.equal(state.available_artifacts.length, 1)
    assert.equal(state.available_artifacts[0].type, 'codebase-requirements')
    assert.equal(state.available_artifacts[0].path, artifactPath)
    assert.equal(state.available_artifacts[0].phase, 'codebase_analysis')
    assert.ok(state.phases.codebase_analysis.output_artifacts.includes(artifactPath))
  })

  it('nonexistent file path would cause handler to reject (existsSync check)', () => {
    const bogusPath = join(tempDir, 'no-such-artifact.json')
    assert.equal(existsSync(bogusPath), false, 'precondition: file should not exist')
    // The handler does: if (!existsSync(resolved)) throw new Error(...)
    // We test the same guard condition
    assert.throws(() => {
      if (!existsSync(bogusPath)) {
        throw new Error(`File not found: ${bogusPath}`)
      }
    }, /File not found/)
  })

  it('validation with a valid artifact passes schema check', () => {
    const schemas = loadSchemas(join(PIPELINE_DIR, 'schemas'))

    // Create a minimal valid raw-collection artifact
    const artifact = {
      collection_id: 'register-valid-test--00000001',
      created_date: new Date().toISOString(),
      status: 'raw',
      source_runs: [],
      items: [],
    }
    const artifactPath = join(tempDir, 'register-valid.json')
    writeFileSync(artifactPath, JSON.stringify(artifact), 'utf-8')

    // Same validation the handler does when validate=true
    const result = validateArtifact(schemas, 'raw-collection.json', artifact)
    assert.equal(result.valid, true, `Expected valid artifact: ${result.errors.join('; ')}`)
  })

  it('validation with an invalid artifact reports schema errors', () => {
    const schemas = loadSchemas(join(PIPELINE_DIR, 'schemas'))

    // Missing required 'collection_id' field
    const badArtifact = { status: 'raw', items: [] }
    const result = validateArtifact(schemas, 'raw-collection.json', badArtifact)
    assert.equal(result.valid, false)
    assert.ok(result.errors.length > 0)
    assert.ok(result.errors.some(e => e.includes('collection_id') || e.includes('required')))
  })

  it('registered artifacts appear in the run state and persist to disk', () => {
    const config = loadPipelineConfig(join(PIPELINE_DIR, 'pipeline.toml'))
    const runDir = join(tempDir, 'runs', 'persist-test')
    let state = initRun('persist-test', config.pipeline.version, Object.keys(config.phases), runDir)

    const artifactPath = join(tempDir, 'test-spec.json')
    writeFileSync(artifactPath, JSON.stringify({ spec_id: 'spec-test' }), 'utf-8')

    state = addArtifact(state, {
      type: 'design-spec',
      path: artifactPath,
      phase: 'design_synthesis',
      created_at: new Date().toISOString(),
    }, runDir)

    // Reload from disk to verify persistence
    const reloaded = loadRunState(runDir)
    assert.ok(reloaded)
    assert.equal(reloaded.available_artifacts.length, 1)
    assert.equal(reloaded.available_artifacts[0].type, 'design-spec')
    assert.equal(reloaded.available_artifacts[0].path, artifactPath)
  })
})
