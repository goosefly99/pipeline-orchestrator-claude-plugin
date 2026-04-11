# AGENTS.md — pipeline-mcp

## Spec Reference

- Design spec (v0.4.0): `pipeline_mcp_data/specs/pipeline-orchestrator-v0-4-0-update-spec-agentic-design-patt--66731168.json`
- Debate transcript: `pipeline_mcp_data/debates/pipeline-orchestrator-v0.4.0-spec-debate.json`
- Codebase requirements: `pipeline_mcp_data/codebase/pipeline-mcp-requirements.json`
- Development roadmap: `v0.4.0-dev-roadmap.md`

## Architecture Constraints

- **No changes** to `pipeline/pipeline.toml` DAG structure or phase definitions
- **No changes** to `pipeline/schemas/*.json` artifact definitions
- **No new runtime dependencies** — use existing Node.js APIs and packages in `package.json`
- **Flat module structure** — all source files at project root, no subdirectories
- **MCP tool names and required parameters must not change** — additive parameters only
- **Single-client assumption** — server operates over stdio with one client
- **All existing tests must pass** after every change

## Conventions

- ESM imports with `.ts` extensions (`import { X } from './types.ts'`)
- kebab-case filenames, PascalCase interfaces, camelCase functions
- TypeScript interfaces in `types.ts` (pipeline types, `HandlerResponse`, `PipelineError`) and `cc-types.ts` (collection types)
- Tests in `tests/` with `.test.ts` suffix using `node:test`
- Run tests with: `node --test tests/*.test.ts`
- Run type-check with: `npm run typecheck` (tsc --noEmit, strict mode)

## Core Patterns

These patterns are invariants — preserve them when modifying code.

### State transition guards

All state-mutation functions in `run-state.ts` validate preconditions and throw descriptive errors:

- `startPhase` / `skipPhase`: require status `pending`
- `completePhase` / `failPhase`: require status `in_progress`
- Error format: `Cannot <verb> phase "X": status is "Y" (expected "Z")`

When adding new transitions, follow the same guard-then-mutate pattern.

### Artifact storage safety

`storeArtifact(config, key, name, artifact, force?)` in `storage.ts`:

- Throws if the target file exists and `force !== true`
- The `pipeline_store_artifact` tool exposes `force` as an additive parameter

Never bypass this check. If overwrite is intentional, pass `force: true` explicitly.

### Pure helpers for testability

Handler logic that depends on module-level server state is kept minimal. Pure derivations live in their respective modules and are directly testable:

- `buildArtifactSummary(artifact, storageKey, fileName)` in `storage.ts`
- `computeRecommendedAction(state, nextPhases)` in `run-state.ts`
- `computeRunWarnings(state, optionalPhases, nowMs?)` in `run-state.ts`
- `getCompletedPhases(state)`, `getSkippedPhases(state)`, `getInProgressPhases(state)`, `getArtifactTypes(state)` in `run-state.ts` — phase-status extraction helpers used across handler modules

When adding new derivations to server handlers, extract the pure logic into the same pattern — take all inputs as parameters, no server-state references.

### Handler extraction via dependency injection

`server.ts` delegates tool handlers to focused modules using a context interface pattern. Each handler module:

1. Defines a `*Context` interface declaring only the dependencies it needs (no direct imports of server-level state)
2. Exports handler functions that accept `(args: Record<string, unknown>, ctx: Context)` and return `HandlerResponse` (`{json: string, isError?: boolean}`)
3. The server creates a context object wiring module-level state to the interface, and delegates in the tool switch block

Extracted handler modules:
- `debate-handlers.ts` — `DebateContext`, `SaveDebateContext` — 4 debate tool handlers
- `cc-handlers.ts` — `CCContext`, `createCCContext()` factory — 7 Concepts Collector tool handlers
- `artifact-handlers.ts` — `ArtifactContext` — 5 artifact tool handlers + response size enforcement
- `lifecycle-handlers.ts` — `LifecycleContext` — 10 lifecycle tool handlers (including phase handoff and phase brief) + stale-state helpers + events.jsonl logging
- `misc-handlers.ts` — `GetConfigContext`, `IngestContext`, `ValidateRunContext` — ingestion, KB, codebase analysis, validate-run, feature-request, and get-config handlers
- `synth-handlers.ts` — `SynthContext`, `createSynthContext()` factory — 6 synthesis tool handlers

Supporting modules (v0.4.0):
- `quality-gates.ts` — pure quality gate check functions (`checkFieldPresent`, `checkMinItems`, `checkCrossRefValid`) and `runGateChecks` orchestrator
- `hooks.ts` — phase lifecycle hook execution via `execFileSync` with path validation and timeout; `runHooks(hooks, trigger, context, projectRoot)` — pre_start hooks are blocking, post_complete/on_fail are non-blocking
- `handoff.ts` — `generateHandoff(runState, config, qualityGates, nextPhases)` — compact session handoff payload (<5K tokens)
- `phase-brief.ts` — `generatePhaseBrief(phaseName, runState, config, qualityGates)` — self-contained subagent brief for phase execution

When extracting additional handlers, follow this same pattern. Keep handler modules free of server-level state references.

### Ajv validator caching

`validator.ts` holds a single module-level `Ajv2020` instance with `addFormats` applied once, plus a `Map<string, ValidateFunction>` cache. `validateArtifact` returns the cached validator or compiles and caches on first use. Do not instantiate Ajv per call.

## Quality Gates

### Quality Gates — artifact_type Drift Gotcha

**Problem class:** The `pipeline/quality-gates.toml` file references artifact types by string (e.g., `artifact_type = "knowledge-overview"`). The gate executor in `quality-gates.ts` performs an exact `Map.get(artifactType)` lookup against the live artifact registry. If the string in `quality-gates.toml` drifts from the canonical registered type (as happened with `"concept-overview"` vs `"knowledge-overview"`), the gate check silently returns `undefined` instead of the artifact, and the gate emits a spurious failure or warn.

**Root cause pattern:** Renaming an artifact type in code or schemas without updating all references in `quality-gates.toml`. The TOML file has no type system, so the compiler won't catch stale strings.

**Detection:** A dedicated config-integrity test at `tests/quality-gates-config.test.ts` asserts that every `artifact_type` param in `quality-gates.toml` is a member of `KNOWN_ARTIFACT_TYPES`. This test runs as part of `npm test` (`node --test tests/*.test.ts`).

**Rules for agents and contributors:**
1. When adding or renaming an artifact type, update `KNOWN_ARTIFACT_TYPES` in `tests/quality-gates-config.test.ts` first, then update `quality-gates.toml`.
2. After any change to `pipeline/quality-gates.toml`, run `npm test` and confirm the `quality-gates.toml config integrity` test suite passes.
3. Do not interpret a `warn`-level gate failure as benign noise — verify the `artifact_type` string in the failing gate check matches a schema file in `pipeline/schemas/` before assuming the artifact itself is the problem.

**See also:** `tests/quality-gates-config.test.ts`, `pipeline/schemas/`, `quality-gates.ts` line 211.

## Per-Run Artifact Directories

**Layout:** New runs persist all artifacts under a per-run tree rooted at
`pipeline_mcp_data/runs/{sanitized_run_name}-{sanitized_timestamp}/`. Subtypes
(`collections/curated`, `collections/raw`, `debates`, `overviews`, `specs`,
`scaffold`) live as subdirectories of this root. The run's `run-state.json` and
`events.jsonl` sit at the root of the per-run directory (no extra subfolder).

**How a run resolves its directory:** `initRun` in `run-state.ts` reads
`run_parameters.run_name` and `run_parameters.run_directory_timestamp`
(populated by the pre-pipeline-init hook — see Feature A) and sets
`state.run_data_dir` via `getRunDataDir(baseDir, runName, timestamp)`.
Sanitization rules (in `storage.ts`):

- `sanitizeRunName`: `/[^a-zA-Z0-9-]/g → '-'`, lowercase, collapse hyphen
  runs, trim leading/trailing hyphens, truncate to 64 chars. Empty input
  becomes `unnamed-run`. Windows-unsafe characters (`< > : " / \ | ? *`) and
  Unicode codepoints are all normalized to `-`.
- `sanitizeRunTimestamp`: ISO timestamp with `:` and `.` replaced by `-`
  (Windows-safe).

**Backwards compatibility (Option B):** Legacy runs (no `run_parameters`, no
`state.run_data_dir`) keep writing to the legacy top-level folders —
`pipeline_mcp_data/debates/`, `pipeline_mcp_data/specs/`, etc. — and
`pipeline_list_artifacts` falls back to those top-level paths when
`state.run_data_dir` is absent. New runs always use the per-run layout; the
two layouts coexist on disk without migration.

**Rules for agents and contributors:**

1. Never hard-code `pipeline_mcp_data/<subtype>/` path literals in new code or
   tests. Use `getRunDataDir` + `getArtifactDir` to derive paths from the run's
   `run_data_dir`, or the write helper `persistArtifact(runDataDir, subtype,
   fileName, artifact, force?)`.
2. When writing a handler that persists an artifact, branch on the presence of
   `state.run_data_dir`: if set, call `ctx.persistArtifact(...)`; otherwise
   fall through to the legacy `ctx.storeArtifact` path. `handleStoreArtifact`
   in `artifact-handlers.ts` is the canonical example of the dual-path
   routing.
3. The `ArtifactSubtype` union in `storage.ts` is the source of truth for
   run-scoped subtypes. Use `storageKeyToSubtype(storageKey)` to translate
   tool-facing storage keys (`'debates'`, `'raw-collections'`, etc.) into the
   internal subtype union before calling the run-scoped helpers.
4. `run-state.json` and `events.jsonl` follow the same dual routing —
   `resolveRunWriteDir(state, fallbackDir)` returns `state.run_data_dir` when
   set, otherwise the legacy `stateDir`. `persist()` uses this helper so the
   state file lands beside its artifacts for new runs.
5. Tests that need a `run_data_dir` should construct it via `getRunDataDir`
   (as in `tests/per-run-paths.test.ts`), not by joining a literal
   `pipeline_mcp_data/runs/...` string.

**See also:** `storage.ts` (path helpers, `persistArtifact`,
`storageKeyToSubtype`), `run-state.ts` (`initRun`, `resolveRunWriteDir`),
`artifact-handlers.ts` (run-scoped routing for store/load/list),
`tests/per-run-paths.test.ts` (integration coverage across sanitization,
two-run collision, and legacy fallback).

## MCP Tool Classification

Tools are classified into three categories with different response conventions:

### Workflow tools (return `ResponseEnvelope` JSON)

Return `{status: 'ok' | 'error', data: Record<string, unknown>, next_step?, warnings?}`:

- `pipeline_complete_phase` — data includes `phase`, `completed_at`; next_step lists available phases
- `pipeline_fail_phase` — data includes `phase`, `error`; next_step describes recovery
- `pipeline_store_artifact` — data includes `path`, `artifact_type`, `phase`
- `pipeline_validate_artifact` (on pass) — data includes `schema`; next_step suggests storage
- `pipeline_ingest_documents` — data includes `collection_id`, `item_count`, `sources`

New workflow/mutation tools should follow this envelope pattern.

### Query tools (keep natural response shape)

`pipeline_get_config`, `pipeline_run_status`, `pipeline_next_phases`, `pipeline_load_artifact`, `pipeline_list_artifacts`, `pipeline_validate_run`, `pipeline_reload_state`, all `cc_*` tools, all `pipeline_kb_*` tools. These return domain-shaped JSON directly.

### Hybrid enhancements

- `pipeline_run_status` returns its natural shape plus `recommended_action: {action, phase?, reason}` and optional `warnings: string[]`
- `pipeline_load_artifact` accepts `summary: boolean` — when true, returns `{artifact_type, id, key_count, total_size_bytes, truncated, preview_keys}` instead of the full artifact

## Extension Points

### DomainProfile (`debate.ts`)

Debate prompts are domain-agnostic. The `DomainProfile` type drives prompt interpolation:

```typescript
interface DomainProfile {
  name: string
  risk_vocabulary: string[]      // interpolated into risk_specialist prompt
  domain_constraints: string[]   // surfaced in synthesis prompt
  specialist_focus: string       // interpolated into domain_specialist prompt
}
```

Built-in profiles: `FINANCE_PROFILE`, `SOFTWARE_PROFILE`, `RESEARCH_PROFILE`, `DEFAULT_PROFILE`. `pipeline_init_debate` accepts a `domain_profile` parameter (string name or object). Use `resolveProfile(profileOrName)` to resolve — falls back to `DEFAULT_PROFILE` for undefined or unknown names.

To add a new built-in profile: define the constant in `debate.ts`, register it in the `BUILT_IN_PROFILES` map, and add tests exercising prompt interpolation.

### Manifest parsers (`codebase-analyzer.ts`)

Each parser (`parsePackageJson`, `parsePyprojectToml`, `parseCargoToml`, `parseGoMod`) returns a `ManifestParsed` with an optional `parse_error` field. On read or parse failure, use the `emptyManifest(msg)` helper with a descriptive message — never swallow the error silently. To add support for a new manifest type (e.g., `Gemfile`): add a parser following this pattern and wire it into `parseManifest` and `detectManifest`.

## Type Checking

`tsconfig.json` at project root configures `tsc --noEmit` (Node runs `.ts` files directly via native type stripping — never compile to JavaScript). Settings: ES2022, NodeNext, strict, `allowImportingTsExtensions: true`. Run `npm run typecheck` before commits.

The ajv/ajv-formats imports in `validator.ts` use typed casts because those packages ship CJS with dual-shape typings that don't match NodeNext default-export resolution.

## Testing

Test files live in `tests/` matching the source module name (`tests/run-state.test.ts` tests `run-state.ts`). Use `node:test` with `describe`/`it`. Pure helper tests take inputs as arguments and assert on returned values — no mocking of server state.

Verification before every commit:
1. `node --test tests/*.test.ts` — all tests must pass
2. `npm run typecheck` — 0 type errors

## Historical Implementation

The codebase was built in 6 phases (now complete). See `v0.4.0-dev-roadmap.md` for the full tracking table. The v0.4.0 update added: tool metadata and error classification (Phase 1), run state persistence and recovery (Phase 2), quality gates with atomic rollback (Phase 3), lifecycle hooks, events log, and model tiering (Phase 4), token efficiency via response compaction, phase handoff, and delegated execution (Phase 5), and parameterizable Claude Code hooks (Phase 6).
