# Pipeline Orchestrator Core — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the pipeline orchestrator MCP server that reads `pipeline.toml`, resolves the DAG, validates artifacts against JSON Schemas, manages run state, and stores/loads artifacts — the core infrastructure all other pipeline subsystems plug into.

**Architecture:** A new MCP server (`pipeline-mcp/`) that parses the TOML pipeline definition at startup, exposes tools for initializing runs, querying available phases, validating artifacts, and managing artifact storage. The DAG resolver determines which phases can execute based on completed phases and available artifact types. Run state is persisted to disk as JSON so runs survive restarts.

**Tech Stack:** Node.js v24 (native TypeScript), `@modelcontextprotocol/sdk`, `smol-toml` (TOML parser), `ajv` + `ajv-formats` (JSON Schema validation), `node:test` (testing)

---

## File Structure

```
pipeline-mcp/
  package.json              — project manifest, dependencies
  server.ts                 — MCP server entry point, tool definitions and handlers
  types.ts                  — TypeScript types for pipeline config, phases, edges, run state
  toml-loader.ts            — Load and parse pipeline.toml into typed PipelineConfig
  dag.ts                    — DAG resolution: given state, return available next phases
  validator.ts              — JSON Schema loading and artifact validation
  storage.ts                — Artifact storage and retrieval by type and run
  run-state.ts              — Pipeline run state management (init, update, persist, load)
  tests/
    toml-loader.test.ts     — Tests for TOML parsing
    dag.test.ts             — Tests for DAG resolution
    validator.test.ts       — Tests for schema validation
    storage.test.ts         — Tests for artifact storage
    run-state.test.ts       — Tests for run state management
    integration.test.ts     — End-to-end test: init run → resolve → validate → store
```

---

### Task 1: Scaffold Project

**Files:**
- Create: `pipeline-mcp/package.json`
- Create: `pipeline-mcp/types.ts`

- [ ] **Step 1: Create package.json**

```json
{
  "name": "pipeline-mcp",
  "version": "0.1.0",
  "description": "Pipeline orchestrator MCP server — reads pipeline.toml, resolves DAG, validates artifacts, manages run state",
  "type": "module",
  "bin": "./server.ts",
  "scripts": {
    "start": "node server.ts",
    "test": "node --test tests/*.test.ts"
  },
  "dependencies": {
    "@modelcontextprotocol/sdk": "^1.0.0",
    "smol-toml": "^1.3.0",
    "ajv": "^8.17.0",
    "ajv-formats": "^3.0.0"
  },
  "devDependencies": {
    "@types/node": "^22.0.0",
    "typescript": "^6.0.2"
  }
}
```

- [ ] **Step 2: Install dependencies**

Run: `cd pipeline-mcp && npm install`
Expected: `node_modules/` created, no errors

- [ ] **Step 3: Create types.ts**

```typescript
// ── Pipeline Configuration (parsed from pipeline.toml) ─────

export interface PipelineConfig {
  pipeline: {
    id: string
    version: string
    description: string
  }
  phases: Record<string, PhaseDefinition>
  edges: EdgeDefinition[]
  debate: DebateConfig
  knowledge_bases: KBDefaults
  schemas: Record<string, string>
  storage: StorageConfig
}

export interface PhaseDefinition {
  id: number | string
  description: string
  inputs: string[]
  outputs: string[]
  tools: string[]
  input_mode?: 'any' | 'one_of' | 'one_of_primary' | 'required_optional'
  output_mode?: 'conditional'
  entry_point: boolean
  optional?: boolean
  reusable?: boolean
}

export interface EdgeDefinition {
  from: string
  to: string
  note?: string
  optional?: boolean
  input_as?: string
  when_input?: string
}

export interface DebateConfig {
  agents: Record<string, { role: string; runs_in: string; depends_on?: string[] }>
  rounds: Record<string, string>
  output: { includes_transcript: boolean; includes_refined_artifact: boolean; artifact_version_bump: string }
}

export interface KBDefaults {
  available_in: string[]
  max_queries_per_phase: number
  query_mode: 'proactive' | 'on_demand'
}

export interface StorageConfig {
  base_dir: string
  paths: Record<string, string>
}

// ── Run State ───────────────────────────────────────────────

export type PhaseStatus = 'pending' | 'in_progress' | 'completed' | 'skipped' | 'failed'

export interface PhaseState {
  phase_name: string
  status: PhaseStatus
  started_at?: string
  completed_at?: string
  input_artifacts: string[]
  output_artifacts: string[]
  error?: string
}

export interface RunState {
  run_id: string
  pipeline_version: string
  created_at: string
  updated_at: string
  status: 'initialized' | 'running' | 'completed' | 'failed'
  phases: Record<string, PhaseState>
  available_artifacts: ArtifactRef[]
  config_path: string
}

export interface ArtifactRef {
  type: string
  path: string
  phase: string
  created_at: string
}
```

- [ ] **Step 4: Verify TypeScript compiles**

Run: `cd pipeline-mcp && npx tsc --noEmit types.ts`
Expected: No errors

- [ ] **Step 5: Create tests directory**

Run: `mkdir -p pipeline-mcp/tests`

- [ ] **Step 6: Commit**

```bash
cd pipeline-mcp && git add package.json package-lock.json types.ts tests/
git commit -m "feat(pipeline-mcp): scaffold project with types and dependencies"
```

---

### Task 2: TOML Loader

**Files:**
- Create: `pipeline-mcp/tests/toml-loader.test.ts`
- Create: `pipeline-mcp/toml-loader.ts`

- [ ] **Step 1: Write failing test**

```typescript
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { loadPipelineConfig } from '../toml-loader.ts'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const PIPELINE_TOML = resolve(__dirname, '../../strategies/pipeline/pipeline.toml')

describe('loadPipelineConfig', () => {
  it('loads and parses pipeline.toml', () => {
    const config = loadPipelineConfig(PIPELINE_TOML)

    assert.equal(config.pipeline.id, 'research-to-implementation')
    assert.equal(config.pipeline.version, '1.0.0')
  })

  it('parses all 9 phases', () => {
    const config = loadPipelineConfig(PIPELINE_TOML)
    const phaseNames = Object.keys(config.phases)

    assert.equal(phaseNames.length, 9)
    assert.ok(phaseNames.includes('research_discovery'))
    assert.ok(phaseNames.includes('debate'))
    assert.ok(phaseNames.includes('implementation_scaffold'))
  })

  it('parses phase properties correctly', () => {
    const config = loadPipelineConfig(PIPELINE_TOML)
    const discovery = config.phases.research_discovery

    assert.equal(discovery.id, 0)
    assert.equal(discovery.entry_point, true)
    assert.ok(discovery.inputs.includes('research-manifest'))
    assert.ok(discovery.outputs.includes('raw-collection'))
    assert.ok(discovery.tools.length > 0)
  })

  it('parses edges', () => {
    const config = loadPipelineConfig(PIPELINE_TOML)

    assert.ok(config.edges.length > 0)
    const firstEdge = config.edges[0]
    assert.ok(typeof firstEdge.from === 'string')
    assert.ok(typeof firstEdge.to === 'string')
  })

  it('parses debate config', () => {
    const config = loadPipelineConfig(PIPELINE_TOML)

    assert.ok(config.debate.agents.advocate)
    assert.ok(config.debate.agents.synthesizer)
    assert.equal(config.debate.rounds.round_1, 'divergent')
    assert.equal(config.debate.rounds.round_2, 'convergent')
  })

  it('parses schema references', () => {
    const config = loadPipelineConfig(PIPELINE_TOML)

    assert.equal(Object.keys(config.schemas).length, 9)
    assert.ok(config.schemas.pipeline_run.endsWith('.json'))
  })

  it('parses storage config', () => {
    const config = loadPipelineConfig(PIPELINE_TOML)

    assert.equal(config.storage.base_dir, 'strategies')
    assert.ok(config.storage.paths.specs)
    assert.ok(config.storage.paths.overviews)
  })

  it('throws on missing file', () => {
    assert.throws(
      () => loadPipelineConfig('/nonexistent/pipeline.toml'),
      /ENOENT|no such file/,
    )
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd pipeline-mcp && node --test tests/toml-loader.test.ts`
Expected: FAIL — `loadPipelineConfig` is not exported

- [ ] **Step 3: Implement toml-loader.ts**

```typescript
import { readFileSync } from 'node:fs'
import { parse } from 'smol-toml'
import type { PipelineConfig, PhaseDefinition, EdgeDefinition, DebateConfig, KBDefaults, StorageConfig } from './types.ts'

export function loadPipelineConfig(tomlPath: string): PipelineConfig {
  const raw = readFileSync(tomlPath, 'utf-8')
  const parsed = parse(raw) as Record<string, unknown>

  const pipeline = parsed.pipeline as { id: string; version: string; description: string }
  const rawPhases = parsed.phases as Record<string, Record<string, unknown>>
  const rawEdges = parsed.edges as Record<string, unknown>[]
  const rawDebate = parsed.debate as Record<string, unknown>
  const rawKB = parsed.knowledge_bases as Record<string, unknown> | undefined
  const rawSchemas = parsed.schemas as Record<string, string>
  const rawStorage = parsed.storage as Record<string, unknown>

  const phases: Record<string, PhaseDefinition> = {}
  for (const [name, raw] of Object.entries(rawPhases)) {
    phases[name] = {
      id: raw.id as number | string,
      description: raw.description as string,
      inputs: (raw.inputs as string[]) ?? [],
      outputs: (raw.outputs as string[]) ?? [],
      tools: (raw.tools as string[]) ?? [],
      input_mode: raw.input_mode as PhaseDefinition['input_mode'],
      output_mode: raw.output_mode as PhaseDefinition['output_mode'],
      entry_point: (raw.entry_point as boolean) ?? false,
      optional: raw.optional as boolean | undefined,
      reusable: raw.reusable as boolean | undefined,
    }
  }

  const edges: EdgeDefinition[] = rawEdges.map(e => ({
    from: e.from as string,
    to: e.to as string,
    note: e.note as string | undefined,
    optional: e.optional as boolean | undefined,
    input_as: e.input_as as string | undefined,
    when_input: e.when_input as string | undefined,
  }))

  const debate: DebateConfig = {
    agents: {} as DebateConfig['agents'],
    rounds: (rawDebate.rounds as Record<string, string>) ?? {},
    output: (rawDebate.output as DebateConfig['output']) ?? {
      includes_transcript: true,
      includes_refined_artifact: true,
      artifact_version_bump: 'minor',
    },
  }
  const rawAgents = rawDebate.agents as Record<string, Record<string, unknown>> | undefined
  if (rawAgents) {
    for (const [name, agent] of Object.entries(rawAgents)) {
      debate.agents[name] = {
        role: agent.role as string,
        runs_in: agent.runs_in as string,
        depends_on: agent.depends_on as string[] | undefined,
      }
    }
  }

  const knowledge_bases: KBDefaults = {
    available_in: (rawKB?.available_in as string[]) ?? [],
    max_queries_per_phase: (rawKB?.max_queries_per_phase as number) ?? 20,
    query_mode: (rawKB?.query_mode as 'proactive' | 'on_demand') ?? 'proactive',
  }

  const storage: StorageConfig = {
    base_dir: (rawStorage.base_dir as string) ?? 'strategies',
    paths: (rawStorage.paths as Record<string, string>) ?? {},
  }

  return { pipeline, phases, edges, debate, knowledge_bases, schemas: rawSchemas, storage }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd pipeline-mcp && node --test tests/toml-loader.test.ts`
Expected: All 8 tests PASS

- [ ] **Step 5: Commit**

```bash
cd pipeline-mcp && git add toml-loader.ts tests/toml-loader.test.ts
git commit -m "feat(pipeline-mcp): TOML loader parses pipeline.toml into typed config"
```

---

### Task 3: DAG Resolver

**Files:**
- Create: `pipeline-mcp/tests/dag.test.ts`
- Create: `pipeline-mcp/dag.ts`

- [ ] **Step 1: Write failing test**

```typescript
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { resolveNextPhases, getPhaseInputSatisfaction } from '../dag.ts'
import type { PipelineConfig, PhaseDefinition, EdgeDefinition } from '../types.ts'

// Minimal config for testing
function makeConfig(
  phases: Record<string, Partial<PhaseDefinition>>,
  edges: EdgeDefinition[],
): PipelineConfig {
  const fullPhases: Record<string, PhaseDefinition> = {}
  for (const [name, partial] of Object.entries(phases)) {
    fullPhases[name] = {
      id: partial.id ?? name,
      description: partial.description ?? '',
      inputs: partial.inputs ?? [],
      outputs: partial.outputs ?? [],
      tools: partial.tools ?? [],
      entry_point: partial.entry_point ?? false,
      ...partial,
    } as PhaseDefinition
  }
  return {
    pipeline: { id: 'test', version: '1.0.0', description: '' },
    phases: fullPhases,
    edges,
    debate: { agents: {}, rounds: {}, output: { includes_transcript: true, includes_refined_artifact: true, artifact_version_bump: 'minor' } },
    knowledge_bases: { available_in: [], max_queries_per_phase: 20, query_mode: 'proactive' },
    schemas: {},
    storage: { base_dir: 'test', paths: {} },
  }
}

describe('resolveNextPhases', () => {
  it('returns entry points when no phases completed', () => {
    const config = makeConfig(
      {
        discovery: { inputs: ['research-manifest'], outputs: ['raw-collection'], entry_point: true },
        curation: { inputs: ['raw-collection'], outputs: ['curated-collection'], entry_point: true },
      },
      [{ from: 'discovery', to: 'curation' }],
    )

    const next = resolveNextPhases(config, [], ['research-manifest'])
    assert.ok(next.includes('discovery'))
  })

  it('returns downstream phase after predecessor completes', () => {
    const config = makeConfig(
      {
        discovery: { inputs: ['research-manifest'], outputs: ['raw-collection'], entry_point: true },
        curation: { inputs: ['raw-collection'], outputs: ['curated-collection'], entry_point: true },
      },
      [{ from: 'discovery', to: 'curation' }],
    )

    const next = resolveNextPhases(config, ['discovery'], ['research-manifest', 'raw-collection'])
    assert.ok(next.includes('curation'))
    assert.ok(!next.includes('discovery'))
  })

  it('skips phases without required inputs', () => {
    const config = makeConfig(
      {
        discovery: { inputs: ['research-manifest'], outputs: ['raw-collection'], entry_point: true },
        synthesis: { inputs: ['curated-collection', 'knowledge-overview'], input_mode: 'any', outputs: ['design-spec'], entry_point: true },
      },
      [],
    )

    const next = resolveNextPhases(config, [], ['research-manifest'])
    assert.ok(next.includes('discovery'))
    assert.ok(!next.includes('synthesis'))
  })

  it('handles "any" input_mode — satisfied if at least one input available', () => {
    const config = makeConfig(
      {
        synthesis: { inputs: ['curated-collection', 'knowledge-overview'], input_mode: 'any', outputs: ['design-spec'], entry_point: true },
      },
      [],
    )

    const next = resolveNextPhases(config, [], ['curated-collection'])
    assert.ok(next.includes('synthesis'))
  })

  it('handles optional edges — does not block downstream', () => {
    const config = makeConfig(
      {
        codebase: { inputs: ['codebase-path'], outputs: ['codebase-requirements'], entry_point: true, optional: true },
        synthesis: { inputs: ['curated-collection', 'codebase-requirements'], input_mode: 'any', outputs: ['design-spec'], entry_point: true },
      },
      [{ from: 'codebase', to: 'synthesis', optional: true }],
    )

    const next = resolveNextPhases(config, [], ['curated-collection'])
    assert.ok(next.includes('synthesis'))
  })

  it('does not return already-completed phases', () => {
    const config = makeConfig(
      {
        discovery: { inputs: ['research-manifest'], outputs: ['raw-collection'], entry_point: true },
      },
      [],
    )

    const next = resolveNextPhases(config, ['discovery'], ['research-manifest', 'raw-collection'])
    assert.ok(!next.includes('discovery'))
  })

  it('respects skip list', () => {
    const config = makeConfig(
      {
        discovery: { inputs: ['research-manifest'], outputs: ['raw-collection'], entry_point: true },
        curation: { inputs: ['raw-collection'], outputs: ['curated-collection'], entry_point: true },
      },
      [{ from: 'discovery', to: 'curation' }],
    )

    const next = resolveNextPhases(config, ['discovery'], ['raw-collection'], ['curation'])
    assert.ok(!next.includes('curation'))
  })
})

describe('getPhaseInputSatisfaction', () => {
  it('returns satisfied inputs for a phase', () => {
    const config = makeConfig(
      {
        synthesis: { inputs: ['curated-collection', 'knowledge-overview'], input_mode: 'any', outputs: ['design-spec'], entry_point: true },
      },
      [],
    )

    const result = getPhaseInputSatisfaction(config.phases.synthesis, ['curated-collection'])
    assert.deepEqual(result.satisfied, ['curated-collection'])
    assert.deepEqual(result.missing, ['knowledge-overview'])
    assert.equal(result.can_run, true)
  })

  it('returns can_run false when no inputs available and mode is not any', () => {
    const config = makeConfig(
      {
        curation: { inputs: ['raw-collection'], outputs: ['curated-collection'], entry_point: true },
      },
      [],
    )

    const result = getPhaseInputSatisfaction(config.phases.curation, [])
    assert.equal(result.can_run, false)
    assert.deepEqual(result.missing, ['raw-collection'])
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd pipeline-mcp && node --test tests/dag.test.ts`
Expected: FAIL — `resolveNextPhases` not exported

- [ ] **Step 3: Implement dag.ts**

```typescript
import type { PipelineConfig, PhaseDefinition } from './types.ts'

export interface InputSatisfaction {
  satisfied: string[]
  missing: string[]
  can_run: boolean
}

export function getPhaseInputSatisfaction(
  phase: PhaseDefinition,
  availableArtifacts: string[],
): InputSatisfaction {
  const satisfied = phase.inputs.filter(i => availableArtifacts.includes(i))
  const missing = phase.inputs.filter(i => !availableArtifacts.includes(i))

  let can_run: boolean
  switch (phase.input_mode) {
    case 'any':
      // At least one input must be available
      can_run = satisfied.length > 0
      break
    case 'one_of':
    case 'one_of_primary':
      // Exactly one primary input available (others optional)
      can_run = satisfied.length > 0
      break
    case 'required_optional':
      // First input is required, rest are optional
      can_run = phase.inputs.length > 0 && availableArtifacts.includes(phase.inputs[0])
      break
    default:
      // Default: all inputs required
      can_run = phase.inputs.length === 0 || missing.length === 0
      break
  }

  return { satisfied, missing, can_run }
}

export function resolveNextPhases(
  config: PipelineConfig,
  completedPhases: string[],
  availableArtifacts: string[],
  skipPhases: string[] = [],
): string[] {
  const completed = new Set(completedPhases)
  const skipped = new Set(skipPhases)
  const candidates: string[] = []

  for (const [name, phase] of Object.entries(config.phases)) {
    // Skip already completed or explicitly skipped phases
    if (completed.has(name) || skipped.has(name)) continue

    // Check if inputs are satisfied
    const { can_run } = getPhaseInputSatisfaction(phase, availableArtifacts)
    if (!can_run) continue

    // Check DAG edges: if this phase has incoming edges, at least one
    // non-optional predecessor must have completed (or this is an entry point
    // being accessed directly with conforming input)
    const incomingEdges = config.edges.filter(e => e.to === name)
    if (incomingEdges.length > 0) {
      const requiredEdges = incomingEdges.filter(e => !e.optional)
      const hasCompletedPredecessor = requiredEdges.length === 0 ||
        requiredEdges.some(e => completed.has(e.from))

      // Entry points can be accessed directly if inputs are satisfied
      if (!hasCompletedPredecessor && !phase.entry_point) continue
    }

    candidates.push(name)
  }

  return candidates
}

export function getDownstreamPhases(
  config: PipelineConfig,
  phaseName: string,
): string[] {
  return config.edges
    .filter(e => e.from === phaseName)
    .map(e => e.to)
}

export function getUpstreamPhases(
  config: PipelineConfig,
  phaseName: string,
): string[] {
  return config.edges
    .filter(e => e.to === phaseName)
    .map(e => e.from)
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd pipeline-mcp && node --test tests/dag.test.ts`
Expected: All 9 tests PASS

- [ ] **Step 5: Commit**

```bash
cd pipeline-mcp && git add dag.ts tests/dag.test.ts
git commit -m "feat(pipeline-mcp): DAG resolver with input satisfaction and phase resolution"
```

---

### Task 4: JSON Schema Validator

**Files:**
- Create: `pipeline-mcp/tests/validator.test.ts`
- Create: `pipeline-mcp/validator.ts`

- [ ] **Step 1: Write failing test**

```typescript
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { loadSchemas, validateArtifact } from '../validator.ts'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const SCHEMAS_DIR = resolve(__dirname, '../../strategies/pipeline/schemas')

describe('loadSchemas', () => {
  it('loads all 9 schema files', () => {
    const schemas = loadSchemas(SCHEMAS_DIR)
    assert.equal(Object.keys(schemas).length, 9)
    assert.ok(schemas['pipeline-run.json'])
    assert.ok(schemas['raw-collection.json'])
    assert.ok(schemas['design-spec.json'])
  })
})

describe('validateArtifact', () => {
  it('validates a valid raw collection', () => {
    const schemas = loadSchemas(SCHEMAS_DIR)
    const artifact = {
      collection_id: 'test--12345678',
      created_date: '2026-04-03T14:30:00Z',
      status: 'raw',
      source_runs: [],
      items: [],
    }

    const result = validateArtifact(schemas, 'raw-collection.json', artifact)
    assert.equal(result.valid, true)
    assert.equal(result.errors.length, 0)
  })

  it('rejects a raw collection missing required fields', () => {
    const schemas = loadSchemas(SCHEMAS_DIR)
    const artifact = {
      collection_id: 'test--12345678',
      // missing: created_date, status, items
    }

    const result = validateArtifact(schemas, 'raw-collection.json', artifact)
    assert.equal(result.valid, false)
    assert.ok(result.errors.length > 0)
  })

  it('validates a valid debate transcript', () => {
    const schemas = loadSchemas(SCHEMAS_DIR)
    const artifact = {
      transcript_id: 'dt-12345678',
      input_type: 'design-spec',
      input_id: 'spec-123',
      created_date: '2026-04-03T16:00:00Z',
      rounds: [],
      synthesis: {
        changes_accepted: [],
        changes_rejected: [],
      },
    }

    const result = validateArtifact(schemas, 'debate-transcript.json', artifact)
    assert.equal(result.valid, true)
  })

  it('throws on unknown schema', () => {
    const schemas = loadSchemas(SCHEMAS_DIR)

    assert.throws(
      () => validateArtifact(schemas, 'nonexistent.json', {}),
      /not found/i,
    )
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd pipeline-mcp && node --test tests/validator.test.ts`
Expected: FAIL — `loadSchemas` not exported

- [ ] **Step 3: Implement validator.ts**

```typescript
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import Ajv from 'ajv'
import addFormats from 'ajv-formats'

export interface ValidationResult {
  valid: boolean
  errors: string[]
}

export type SchemaMap = Record<string, object>

export function loadSchemas(schemasDir: string): SchemaMap {
  const schemas: SchemaMap = {}
  const files = readdirSync(schemasDir).filter(f => f.endsWith('.json'))

  for (const file of files) {
    const content = readFileSync(join(schemasDir, file), 'utf-8')
    schemas[file] = JSON.parse(content) as object
  }

  return schemas
}

export function validateArtifact(
  schemas: SchemaMap,
  schemaFile: string,
  artifact: unknown,
): ValidationResult {
  const schema = schemas[schemaFile]
  if (!schema) {
    throw new Error(`Schema "${schemaFile}" not found. Available: ${Object.keys(schemas).join(', ')}`)
  }

  const ajv = new Ajv({ allErrors: true, strict: false })
  addFormats(ajv)

  const validate = ajv.compile(schema)
  const valid = validate(artifact) as boolean

  const errors = valid
    ? []
    : (validate.errors ?? []).map(e => {
        const path = e.instancePath || '/'
        return `${path}: ${e.message ?? 'unknown error'}${e.params ? ` (${JSON.stringify(e.params)})` : ''}`
      })

  return { valid, errors }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd pipeline-mcp && node --test tests/validator.test.ts`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
cd pipeline-mcp && git add validator.ts tests/validator.test.ts
git commit -m "feat(pipeline-mcp): JSON Schema validator loads schemas and validates artifacts"
```

---

### Task 5: Artifact Storage

**Files:**
- Create: `pipeline-mcp/tests/storage.test.ts`
- Create: `pipeline-mcp/storage.ts`

- [ ] **Step 1: Write failing test**

```typescript
import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { storeArtifact, loadArtifact, listArtifacts } from '../storage.ts'
import type { StorageConfig } from '../types.ts'

let tempDir: string
let storageConfig: StorageConfig

beforeEach(() => {
  tempDir = mkdtempSync(join(tmpdir(), 'pipeline-test-'))
  storageConfig = {
    base_dir: tempDir,
    paths: {
      raw_collections: 'collections/raw',
      curated_collections: 'collections/curated',
      specs: 'specs',
      overviews: 'overviews',
      debates: 'debates',
    },
  }
})

afterEach(() => {
  rmSync(tempDir, { recursive: true, force: true })
})

describe('storeArtifact', () => {
  it('stores a JSON artifact in the correct directory', () => {
    const artifact = { collection_id: 'test-123', status: 'raw', items: [] }
    const path = storeArtifact(storageConfig, 'raw_collections', 'test-123.json', artifact)

    assert.ok(existsSync(path))
    const stored = JSON.parse(readFileSync(path, 'utf-8'))
    assert.equal(stored.collection_id, 'test-123')
  })

  it('creates intermediate directories', () => {
    const artifact = { spec_id: 'abc', title: 'Test Spec' }
    const path = storeArtifact(storageConfig, 'specs', 'test-spec.json', artifact)

    assert.ok(existsSync(path))
    assert.ok(path.includes('specs'))
  })

  it('throws on unknown storage key', () => {
    assert.throws(
      () => storeArtifact(storageConfig, 'nonexistent', 'file.json', {}),
      /unknown storage key/i,
    )
  })
})

describe('loadArtifact', () => {
  it('loads a previously stored artifact', () => {
    const artifact = { id: 'test', data: 'hello' }
    storeArtifact(storageConfig, 'specs', 'test.json', artifact)

    const loaded = loadArtifact(storageConfig, 'specs', 'test.json')
    assert.deepEqual(loaded, artifact)
  })

  it('returns null for missing artifact', () => {
    const loaded = loadArtifact(storageConfig, 'specs', 'nonexistent.json')
    assert.equal(loaded, null)
  })
})

describe('listArtifacts', () => {
  it('lists all artifacts in a storage path', () => {
    storeArtifact(storageConfig, 'specs', 'a.json', { id: 'a' })
    storeArtifact(storageConfig, 'specs', 'b.json', { id: 'b' })

    const files = listArtifacts(storageConfig, 'specs')
    assert.equal(files.length, 2)
    assert.ok(files.includes('a.json'))
    assert.ok(files.includes('b.json'))
  })

  it('returns empty array for nonexistent directory', () => {
    const files = listArtifacts(storageConfig, 'overviews')
    assert.deepEqual(files, [])
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd pipeline-mcp && node --test tests/storage.test.ts`
Expected: FAIL — `storeArtifact` not exported

- [ ] **Step 3: Implement storage.ts**

```typescript
import { readFileSync, writeFileSync, mkdirSync, readdirSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import type { StorageConfig } from './types.ts'

function resolveDir(config: StorageConfig, storageKey: string): string {
  const subPath = config.paths[storageKey]
  if (!subPath) {
    throw new Error(`Unknown storage key "${storageKey}". Available: ${Object.keys(config.paths).join(', ')}`)
  }
  return join(config.base_dir, subPath)
}

export function storeArtifact(
  config: StorageConfig,
  storageKey: string,
  fileName: string,
  artifact: unknown,
): string {
  const dir = resolveDir(config, storageKey)
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true })

  const filePath = join(dir, fileName)
  writeFileSync(filePath, JSON.stringify(artifact, null, 2), 'utf-8')
  return filePath
}

export function loadArtifact(
  config: StorageConfig,
  storageKey: string,
  fileName: string,
): unknown | null {
  const dir = resolveDir(config, storageKey)
  const filePath = join(dir, fileName)

  if (!existsSync(filePath)) return null

  return JSON.parse(readFileSync(filePath, 'utf-8')) as unknown
}

export function listArtifacts(
  config: StorageConfig,
  storageKey: string,
): string[] {
  const dir = resolveDir(config, storageKey)
  if (!existsSync(dir)) return []

  return readdirSync(dir).filter(f => f.endsWith('.json'))
}

export function getArtifactPath(
  config: StorageConfig,
  storageKey: string,
  fileName: string,
): string {
  return join(resolveDir(config, storageKey), fileName)
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd pipeline-mcp && node --test tests/storage.test.ts`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
cd pipeline-mcp && git add storage.ts tests/storage.test.ts
git commit -m "feat(pipeline-mcp): artifact storage with store, load, and list operations"
```

---

### Task 6: Run State Manager

**Files:**
- Create: `pipeline-mcp/tests/run-state.test.ts`
- Create: `pipeline-mcp/run-state.ts`

- [ ] **Step 1: Write failing test**

```typescript
import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { initRun, startPhase, completePhase, failPhase, loadRunState, addArtifact } from '../run-state.ts'

let tempDir: string

beforeEach(() => {
  tempDir = mkdtempSync(join(tmpdir(), 'pipeline-run-test-'))
})

afterEach(() => {
  rmSync(tempDir, { recursive: true, force: true })
})

describe('initRun', () => {
  it('creates a new run state with all phases pending', () => {
    const phaseNames = ['discovery', 'curation', 'synthesis']
    const state = initRun('test-run-1', '1.0.0', phaseNames, tempDir)

    assert.equal(state.run_id, 'test-run-1')
    assert.equal(state.status, 'initialized')
    assert.equal(Object.keys(state.phases).length, 3)
    assert.equal(state.phases.discovery.status, 'pending')
    assert.equal(state.phases.curation.status, 'pending')
  })

  it('persists state to disk', () => {
    initRun('test-run-2', '1.0.0', ['discovery'], tempDir)
    assert.ok(existsSync(join(tempDir, 'run-state.json')))
  })
})

describe('startPhase', () => {
  it('marks phase as in_progress', () => {
    const state = initRun('test', '1.0.0', ['discovery'], tempDir)
    const updated = startPhase(state, 'discovery', tempDir)

    assert.equal(updated.phases.discovery.status, 'in_progress')
    assert.ok(updated.phases.discovery.started_at)
    assert.equal(updated.status, 'running')
  })

  it('throws if phase does not exist', () => {
    const state = initRun('test', '1.0.0', ['discovery'], tempDir)
    assert.throws(
      () => startPhase(state, 'nonexistent', tempDir),
      /not found/i,
    )
  })
})

describe('completePhase', () => {
  it('marks phase as completed', () => {
    let state = initRun('test', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    const updated = completePhase(state, 'discovery', tempDir)

    assert.equal(updated.phases.discovery.status, 'completed')
    assert.ok(updated.phases.discovery.completed_at)
  })
})

describe('failPhase', () => {
  it('marks phase as failed with error', () => {
    let state = initRun('test', '1.0.0', ['discovery'], tempDir)
    state = startPhase(state, 'discovery', tempDir)
    const updated = failPhase(state, 'discovery', 'API timeout', tempDir)

    assert.equal(updated.phases.discovery.status, 'failed')
    assert.equal(updated.phases.discovery.error, 'API timeout')
    assert.equal(updated.status, 'failed')
  })
})

describe('addArtifact', () => {
  it('adds artifact ref to run state', () => {
    let state = initRun('test', '1.0.0', ['discovery'], tempDir)
    state = addArtifact(state, {
      type: 'raw-collection',
      path: 'collections/raw/test.json',
      phase: 'discovery',
      created_at: new Date().toISOString(),
    }, tempDir)

    assert.equal(state.available_artifacts.length, 1)
    assert.equal(state.available_artifacts[0].type, 'raw-collection')
  })
})

describe('loadRunState', () => {
  it('loads persisted state from disk', () => {
    initRun('persisted-run', '1.0.0', ['discovery', 'curation'], tempDir)
    const loaded = loadRunState(tempDir)

    assert.ok(loaded)
    assert.equal(loaded.run_id, 'persisted-run')
    assert.equal(Object.keys(loaded.phases).length, 2)
  })

  it('returns null if no state file', () => {
    const loaded = loadRunState(join(tempDir, 'nonexistent'))
    assert.equal(loaded, null)
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd pipeline-mcp && node --test tests/run-state.test.ts`
Expected: FAIL — `initRun` not exported

- [ ] **Step 3: Implement run-state.ts**

```typescript
import { writeFileSync, readFileSync, mkdirSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import type { RunState, PhaseState, ArtifactRef } from './types.ts'

const STATE_FILE = 'run-state.json'

function persist(state: RunState, stateDir: string): void {
  if (!existsSync(stateDir)) mkdirSync(stateDir, { recursive: true })
  state.updated_at = new Date().toISOString()
  writeFileSync(join(stateDir, STATE_FILE), JSON.stringify(state, null, 2), 'utf-8')
}

export function initRun(
  runId: string,
  pipelineVersion: string,
  phaseNames: string[],
  stateDir: string,
): RunState {
  const phases: Record<string, PhaseState> = {}
  for (const name of phaseNames) {
    phases[name] = {
      phase_name: name,
      status: 'pending',
      input_artifacts: [],
      output_artifacts: [],
    }
  }

  const state: RunState = {
    run_id: runId,
    pipeline_version: pipelineVersion,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    status: 'initialized',
    phases,
    available_artifacts: [],
    config_path: stateDir,
  }

  persist(state, stateDir)
  return state
}

export function startPhase(state: RunState, phaseName: string, stateDir: string): RunState {
  const phase = state.phases[phaseName]
  if (!phase) throw new Error(`Phase "${phaseName}" not found in run state`)

  phase.status = 'in_progress'
  phase.started_at = new Date().toISOString()
  state.status = 'running'

  persist(state, stateDir)
  return state
}

export function completePhase(state: RunState, phaseName: string, stateDir: string): RunState {
  const phase = state.phases[phaseName]
  if (!phase) throw new Error(`Phase "${phaseName}" not found in run state`)

  phase.status = 'completed'
  phase.completed_at = new Date().toISOString()

  // Check if all phases are completed
  const allDone = Object.values(state.phases).every(
    p => p.status === 'completed' || p.status === 'skipped',
  )
  if (allDone) state.status = 'completed'

  persist(state, stateDir)
  return state
}

export function failPhase(state: RunState, phaseName: string, error: string, stateDir: string): RunState {
  const phase = state.phases[phaseName]
  if (!phase) throw new Error(`Phase "${phaseName}" not found in run state`)

  phase.status = 'failed'
  phase.error = error
  state.status = 'failed'

  persist(state, stateDir)
  return state
}

export function skipPhase(state: RunState, phaseName: string, stateDir: string): RunState {
  const phase = state.phases[phaseName]
  if (!phase) throw new Error(`Phase "${phaseName}" not found in run state`)

  phase.status = 'skipped'
  persist(state, stateDir)
  return state
}

export function addArtifact(state: RunState, ref: ArtifactRef, stateDir: string): RunState {
  state.available_artifacts.push(ref)

  // Also track in the phase's output_artifacts
  const phase = state.phases[ref.phase]
  if (phase) {
    phase.output_artifacts.push(ref.path)
  }

  persist(state, stateDir)
  return state
}

export function loadRunState(stateDir: string): RunState | null {
  const filePath = join(stateDir, STATE_FILE)
  if (!existsSync(filePath)) return null

  return JSON.parse(readFileSync(filePath, 'utf-8')) as RunState
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd pipeline-mcp && node --test tests/run-state.test.ts`
Expected: All 8 tests PASS

- [ ] **Step 5: Commit**

```bash
cd pipeline-mcp && git add run-state.ts tests/run-state.test.ts
git commit -m "feat(pipeline-mcp): run state manager with init, phase transitions, and persistence"
```

---

### Task 7: MCP Server with Tools

**Files:**
- Create: `pipeline-mcp/server.ts`

- [ ] **Step 1: Run all unit tests to confirm foundation is solid**

Run: `cd pipeline-mcp && node --test tests/toml-loader.test.ts tests/dag.test.ts tests/validator.test.ts tests/storage.test.ts tests/run-state.test.ts`
Expected: All tests PASS

- [ ] **Step 2: Implement server.ts**

```typescript
import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import {
  ListToolsRequestSchema,
  CallToolRequestSchema,
} from '@modelcontextprotocol/sdk/types.js'
import { resolve, dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { loadPipelineConfig } from './toml-loader.ts'
import { resolveNextPhases, getPhaseInputSatisfaction } from './dag.ts'
import { loadSchemas, validateArtifact } from './validator.ts'
import { storeArtifact, loadArtifact, listArtifacts } from './storage.ts'
import {
  initRun, startPhase, completePhase, failPhase, skipPhase,
  addArtifact, loadRunState,
} from './run-state.ts'
import type { RunState, ArtifactRef, StorageConfig } from './types.ts'

// ── Load pipeline config and schemas at startup ─────────────

const __dirname = dirname(fileURLToPath(import.meta.url))
const PIPELINE_DIR = resolve(__dirname, '../strategies/pipeline')
const config = loadPipelineConfig(join(PIPELINE_DIR, 'pipeline.toml'))
const schemas = loadSchemas(join(PIPELINE_DIR, 'schemas'))

// ── In-memory run state (one active run at a time) ──────────

let activeRun: RunState | null = null
let activeRunDir = ''

function getStorageConfig(baseOverride?: string): StorageConfig {
  const base = baseOverride ?? resolve(__dirname, '..', config.storage.base_dir)
  return { base_dir: base, paths: config.storage.paths }
}

// ── Server setup ────────────────────────────────────────────

const server = new Server(
  { name: 'pipeline', version: '0.1.0' },
  {
    capabilities: { tools: {} },
    instructions: [
      'Pipeline orchestrator MCP server.',
      'Reads pipeline.toml to understand phases and DAG edges.',
      'Initialize a run, resolve available phases, validate artifacts,',
      'and manage artifact storage.',
      'Workflow: pipeline_init_run → pipeline_next_phases → pipeline_start_phase',
      '→ (do work) → pipeline_validate_artifact → pipeline_store_artifact',
      '→ pipeline_complete_phase → repeat.',
    ].join(' '),
  },
)

// ── Tool definitions ────────────────────────────────────────

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: 'pipeline_get_config',
      description: 'Get the pipeline configuration: phases, edges, debate config, storage paths, and schema references.',
      inputSchema: { type: 'object' as const, properties: {} },
    },
    {
      name: 'pipeline_init_run',
      description: 'Initialize a new pipeline run. Creates run state with all phases set to pending. Returns the run state and available starting phases.',
      inputSchema: {
        type: 'object' as const,
        properties: {
          run_id: { type: 'string', description: 'Unique run identifier' },
          base_dir: { type: 'string', description: 'Override base directory for artifact storage' },
          skip_phases: {
            type: 'array', items: { type: 'string' },
            description: 'Phases to skip in this run',
          },
          initial_artifacts: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                type: { type: 'string' },
                path: { type: 'string' },
              },
              required: ['type', 'path'],
            },
            description: 'Pre-existing artifacts to register (e.g., existing collections)',
          },
        },
        required: ['run_id'],
      },
    },
    {
      name: 'pipeline_next_phases',
      description: 'Resolve which phases can run next based on current run state: completed phases, available artifacts, and DAG edges.',
      inputSchema: {
        type: 'object' as const,
        properties: {
          skip_phases: {
            type: 'array', items: { type: 'string' },
            description: 'Additional phases to skip beyond those set at init',
          },
        },
      },
    },
    {
      name: 'pipeline_start_phase',
      description: 'Mark a phase as in-progress. Returns the phase definition (inputs, outputs, tools) so the executor knows what to do.',
      inputSchema: {
        type: 'object' as const,
        properties: {
          phase: { type: 'string', description: 'Phase name to start' },
        },
        required: ['phase'],
      },
    },
    {
      name: 'pipeline_complete_phase',
      description: 'Mark a phase as completed.',
      inputSchema: {
        type: 'object' as const,
        properties: {
          phase: { type: 'string', description: 'Phase name to complete' },
        },
        required: ['phase'],
      },
    },
    {
      name: 'pipeline_fail_phase',
      description: 'Mark a phase as failed with an error message.',
      inputSchema: {
        type: 'object' as const,
        properties: {
          phase: { type: 'string', description: 'Phase name that failed' },
          error: { type: 'string', description: 'Error description' },
        },
        required: ['phase', 'error'],
      },
    },
    {
      name: 'pipeline_validate_artifact',
      description: 'Validate a JSON artifact against one of the pipeline schemas. Returns validation result with any errors.',
      inputSchema: {
        type: 'object' as const,
        properties: {
          schema: {
            type: 'string',
            description: 'Schema filename (e.g., "raw-collection.json", "design-spec.json")',
          },
          artifact: {
            type: 'object',
            description: 'The artifact JSON to validate',
          },
        },
        required: ['schema', 'artifact'],
      },
    },
    {
      name: 'pipeline_store_artifact',
      description: 'Store a validated artifact to the correct directory and register it in run state.',
      inputSchema: {
        type: 'object' as const,
        properties: {
          storage_key: {
            type: 'string',
            description: 'Storage path key (e.g., "raw_collections", "specs", "overviews", "debates")',
          },
          file_name: { type: 'string', description: 'Filename for the artifact' },
          artifact: { type: 'object', description: 'The artifact JSON to store' },
          artifact_type: { type: 'string', description: 'Schema type name (e.g., "raw-collection", "design-spec")' },
          phase: { type: 'string', description: 'Phase that produced this artifact' },
        },
        required: ['storage_key', 'file_name', 'artifact', 'artifact_type', 'phase'],
      },
    },
    {
      name: 'pipeline_load_artifact',
      description: 'Load a previously stored artifact by storage key and filename.',
      inputSchema: {
        type: 'object' as const,
        properties: {
          storage_key: { type: 'string' },
          file_name: { type: 'string' },
        },
        required: ['storage_key', 'file_name'],
      },
    },
    {
      name: 'pipeline_list_artifacts',
      description: 'List all stored artifacts in a given storage path.',
      inputSchema: {
        type: 'object' as const,
        properties: {
          storage_key: { type: 'string' },
        },
        required: ['storage_key'],
      },
    },
    {
      name: 'pipeline_run_status',
      description: 'Get the current run state: phase statuses, available artifacts, and overall progress.',
      inputSchema: { type: 'object' as const, properties: {} },
    },
  ],
}))

// ── Tool handlers ───────────────────────────────────────────

server.setRequestHandler(CallToolRequestSchema, async (req) => {
  const args = (req.params.arguments ?? {}) as Record<string, unknown>
  try {
    switch (req.params.name) {
      case 'pipeline_get_config':
        return text(JSON.stringify({
          pipeline: config.pipeline,
          phases: Object.entries(config.phases).map(([name, p]) => ({
            name,
            id: p.id,
            description: p.description,
            inputs: p.inputs,
            outputs: p.outputs,
            input_mode: p.input_mode,
            entry_point: p.entry_point,
            optional: p.optional,
          })),
          edges: config.edges,
          schemas: config.schemas,
          storage: config.storage,
        }, null, 2))

      case 'pipeline_init_run':
        return handleInitRun(args)
      case 'pipeline_next_phases':
        return handleNextPhases(args)
      case 'pipeline_start_phase':
        return handleStartPhase(args)
      case 'pipeline_complete_phase':
        return handleCompletePhase(args)
      case 'pipeline_fail_phase':
        return handleFailPhase(args)
      case 'pipeline_validate_artifact':
        return handleValidateArtifact(args)
      case 'pipeline_store_artifact':
        return handleStoreArtifact(args)
      case 'pipeline_load_artifact':
        return handleLoadArtifact(args)
      case 'pipeline_list_artifacts':
        return handleListArtifacts(args)
      case 'pipeline_run_status':
        return handleRunStatus()

      default:
        return text(`Unknown tool: ${req.params.name}`, true)
    }
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err)
    return text(`Error: ${msg}`, true)
  }
})

// ── Handler implementations ─────────────────────────────────

function text(content: string, isError = false) {
  return { content: [{ type: 'text' as const, text: content }], isError }
}

function handleInitRun(args: Record<string, unknown>) {
  const runId = args.run_id as string
  if (!runId) throw new Error('run_id is required')

  const baseDir = args.base_dir as string | undefined
  const skipPhasesList = (args.skip_phases as string[]) ?? []
  const initialArtifacts = (args.initial_artifacts as ArtifactRef[]) ?? []

  const sc = getStorageConfig(baseDir)
  activeRunDir = join(sc.base_dir, 'runs', runId)
  const phaseNames = Object.keys(config.phases).filter(p => !skipPhasesList.includes(p))

  activeRun = initRun(runId, config.pipeline.version, phaseNames, activeRunDir)

  // Skip explicitly listed phases
  for (const phase of skipPhasesList) {
    if (activeRun.phases[phase]) {
      activeRun = skipPhase(activeRun, phase, activeRunDir)
    }
  }

  // Register initial artifacts
  for (const art of initialArtifacts) {
    activeRun = addArtifact(activeRun, {
      ...art,
      phase: 'pre-existing',
      created_at: new Date().toISOString(),
    }, activeRunDir)
  }

  const artifactTypes = activeRun.available_artifacts.map(a => a.type)
  const completedPhases = Object.entries(activeRun.phases)
    .filter(([, p]) => p.status === 'completed')
    .map(([name]) => name)

  const nextPhases = resolveNextPhases(config, completedPhases, artifactTypes, skipPhasesList)

  return text(JSON.stringify({
    run_id: activeRun.run_id,
    status: activeRun.status,
    total_phases: Object.keys(activeRun.phases).length,
    skipped: skipPhasesList,
    initial_artifacts: artifactTypes,
    available_next_phases: nextPhases,
  }, null, 2))
}

function requireRun(): RunState {
  if (!activeRun) throw new Error('No active run. Call pipeline_init_run first.')
  return activeRun
}

function handleNextPhases(args: Record<string, unknown>) {
  const state = requireRun()
  const extraSkips = (args.skip_phases as string[]) ?? []

  const completed = Object.entries(state.phases)
    .filter(([, p]) => p.status === 'completed')
    .map(([name]) => name)
  const artifactTypes = state.available_artifacts.map(a => a.type)
  const allSkips = [
    ...Object.entries(state.phases).filter(([, p]) => p.status === 'skipped').map(([name]) => name),
    ...extraSkips,
  ]

  const nextPhases = resolveNextPhases(config, completed, artifactTypes, allSkips)

  const details = nextPhases.map(name => {
    const phase = config.phases[name]
    const { satisfied, missing } = getPhaseInputSatisfaction(phase, artifactTypes)
    return { name, inputs_satisfied: satisfied, inputs_missing: missing, description: phase.description }
  })

  return text(JSON.stringify({ available_phases: details, completed, artifacts: artifactTypes }, null, 2))
}

function handleStartPhase(args: Record<string, unknown>) {
  const state = requireRun()
  const phaseName = args.phase as string
  if (!phaseName) throw new Error('phase is required')

  activeRun = startPhase(state, phaseName, activeRunDir)
  const phase = config.phases[phaseName]

  return text(JSON.stringify({
    phase: phaseName,
    status: 'in_progress',
    description: phase.description,
    inputs: phase.inputs,
    input_mode: phase.input_mode,
    outputs: phase.outputs,
    tools: phase.tools,
  }, null, 2))
}

function handleCompletePhase(args: Record<string, unknown>) {
  const state = requireRun()
  const phaseName = args.phase as string
  activeRun = completePhase(state, phaseName, activeRunDir)

  return text(`Phase "${phaseName}" completed.`)
}

function handleFailPhase(args: Record<string, unknown>) {
  const state = requireRun()
  const phaseName = args.phase as string
  const error = args.error as string
  activeRun = failPhase(state, phaseName, error, activeRunDir)

  return text(`Phase "${phaseName}" failed: ${error}`)
}

function handleValidateArtifact(args: Record<string, unknown>) {
  const schemaFile = args.schema as string
  const artifact = args.artifact as unknown
  if (!schemaFile || !artifact) throw new Error('schema and artifact are required')

  const result = validateArtifact(schemas, schemaFile, artifact)

  if (result.valid) {
    return text(`Validation PASSED against ${schemaFile}`)
  }
  return text(`Validation FAILED against ${schemaFile}:\n${result.errors.join('\n')}`, true)
}

function handleStoreArtifact(args: Record<string, unknown>) {
  const state = requireRun()
  const storageKey = args.storage_key as string
  const fileName = args.file_name as string
  const artifact = args.artifact as unknown
  const artifactType = args.artifact_type as string
  const phase = args.phase as string

  const sc = getStorageConfig(state.config_path.replace(/\/runs\/.*$/, ''))
  const path = storeArtifact(sc, storageKey, fileName, artifact)

  activeRun = addArtifact(state, {
    type: artifactType,
    path,
    phase,
    created_at: new Date().toISOString(),
  }, activeRunDir)

  return text(`Artifact stored: ${path}`)
}

function handleLoadArtifact(args: Record<string, unknown>) {
  const storageKey = args.storage_key as string
  const fileName = args.file_name as string

  const baseDir = activeRun ? activeRun.config_path.replace(/\/runs\/.*$/, '') : resolve(__dirname, '..', config.storage.base_dir)
  const sc = getStorageConfig(baseDir)
  const artifact = loadArtifact(sc, storageKey, fileName)

  if (artifact === null) {
    return text(`Artifact not found: ${storageKey}/${fileName}`, true)
  }
  return text(JSON.stringify(artifact, null, 2))
}

function handleListArtifacts(args: Record<string, unknown>) {
  const storageKey = args.storage_key as string
  const baseDir = activeRun ? activeRun.config_path.replace(/\/runs\/.*$/, '') : resolve(__dirname, '..', config.storage.base_dir)
  const sc = getStorageConfig(baseDir)
  const files = listArtifacts(sc, storageKey)

  return text(JSON.stringify({ storage_key: storageKey, files }, null, 2))
}

function handleRunStatus() {
  const state = requireRun()

  const summary = Object.entries(state.phases).map(([name, p]) => ({
    phase: name,
    status: p.status,
    started: p.started_at ?? null,
    completed: p.completed_at ?? null,
    error: p.error ?? null,
    outputs: p.output_artifacts,
  }))

  return text(JSON.stringify({
    run_id: state.run_id,
    status: state.status,
    created: state.created_at,
    updated: state.updated_at,
    phases: summary,
    artifacts: state.available_artifacts,
  }, null, 2))
}

// ── Transport & lifecycle ───────────────────────────────────

const transport = new StdioServerTransport()
await server.connect(transport)

process.stderr.write('pipeline: MCP server started\n')

function shutdown() {
  server.close().catch(() => {})
  process.exit(0)
}

process.stdin.on('end', shutdown)
process.stdin.on('close', shutdown)
process.on('SIGTERM', shutdown)
process.on('SIGINT', shutdown)
process.on('unhandledRejection', (err: unknown) => {
  process.stderr.write(`pipeline: unhandled rejection: ${err}\n`)
})
process.on('uncaughtException', (err: Error) => {
  process.stderr.write(`pipeline: uncaught exception: ${err.message}\n`)
  shutdown()
})
```

- [ ] **Step 3: Verify TypeScript compiles**

Run: `cd pipeline-mcp && npx tsc --noEmit server.ts`
Expected: No errors

- [ ] **Step 4: Commit**

```bash
cd pipeline-mcp && git add server.ts
git commit -m "feat(pipeline-mcp): MCP server with 11 orchestration tools"
```

---

### Task 8: Register in .mcp.json

**Files:**
- Modify: `.mcp.json`

- [ ] **Step 1: Add pipeline server to .mcp.json**

Add to the `mcpServers` object in `.mcp.json`:

```json
"pipeline": {
  "command": "node",
  "args": ["pipeline-mcp/server.ts"],
  "env": {}
}
```

- [ ] **Step 2: Verify server starts**

Run: `cd /c/Users/olive/Documents/ai_trading && timeout 5 node pipeline-mcp/server.ts 2>&1 || true`
Expected: stderr shows `pipeline: MCP server started`

- [ ] **Step 3: Commit**

```bash
git add .mcp.json
git commit -m "feat(pipeline-mcp): register orchestrator in .mcp.json"
```

---

### Task 9: Integration Test

**Files:**
- Create: `pipeline-mcp/tests/integration.test.ts`

- [ ] **Step 1: Write integration test**

```typescript
import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

import { loadPipelineConfig } from '../toml-loader.ts'
import { resolveNextPhases } from '../dag.ts'
import { loadSchemas, validateArtifact } from '../validator.ts'
import { storeArtifact, loadArtifact } from '../storage.ts'
import { initRun, startPhase, completePhase, addArtifact } from '../run-state.ts'
import type { StorageConfig } from '../types.ts'

const __dirname = dirname(fileURLToPath(import.meta.url))
const PIPELINE_DIR = resolve(__dirname, '../../strategies/pipeline')

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
    assert.equal(Object.keys(schemas).length, 9)

    // 2. Init run
    const runDir = join(tempDir, 'runs', 'test-run')
    const phaseNames = Object.keys(config.phases)
    let state = initRun('test-run', config.pipeline.version, phaseNames, runDir)

    assert.equal(state.status, 'initialized')
    assert.equal(Object.keys(state.phases).length, 9)

    // 3. Resolve next phases (should include entry points with no inputs needed)
    const artifactTypes = state.available_artifacts.map(a => a.type)
    let next = resolveNextPhases(config, [], artifactTypes)
    // Only phases whose inputs are satisfied should appear
    // discovery needs 'research-manifest', curation needs 'raw-collection', etc.
    // None should be available yet since we have no artifacts
    assert.equal(next.length, 0)

    // 4. Provide a research manifest artifact
    state = addArtifact(state, {
      type: 'research-manifest',
      path: 'manifests/test.json',
      phase: 'pre-existing',
      created_at: new Date().toISOString(),
    }, runDir)

    next = resolveNextPhases(config, [], ['research-manifest'])
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
    assert.ok(storedPath.includes('collections/raw'))

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
```

- [ ] **Step 2: Run integration test**

Run: `cd pipeline-mcp && node --test tests/integration.test.ts`
Expected: PASS

- [ ] **Step 3: Run all tests together**

Run: `cd pipeline-mcp && node --test tests/*.test.ts`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
cd pipeline-mcp && git add tests/integration.test.ts
git commit -m "test(pipeline-mcp): integration test covering full init→resolve→validate→store→complete flow"
```

---

### Task 10: Final Verification

- [ ] **Step 1: Run full test suite**

Run: `cd pipeline-mcp && npm test`
Expected: All tests PASS

- [ ] **Step 2: Type-check all files**

Run: `cd pipeline-mcp && npx tsc --noEmit server.ts`
Expected: No errors

- [ ] **Step 3: Verify server starts cleanly**

Run: `cd /c/Users/olive/Documents/ai_trading && timeout 5 node pipeline-mcp/server.ts 2>&1 || true`
Expected: `pipeline: MCP server started` on stderr, no other errors

- [ ] **Step 4: Final commit with all files verified**

Run: `cd pipeline-mcp && git status`
Expected: Clean working tree, all changes committed
